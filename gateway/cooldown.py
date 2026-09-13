"""V4.6.1 + V4.9 CooldownManager — 失败账号冷却管理 (LiteLLM 启发)

背景: 同一 channel 多个账号 (e.g. deepseek_01/deepseek_02) 时, 单个 key 临时失效
(401/403/5xx 连续) 不应让后续请求继续撞. 借鉴 LiteLLM 的 cooldown 机制:
- 失败 N 次 → cooldown N 秒 (默认 60s)
- cooldown 期内路由跳过该 account
- cooldown 过期 → 自动恢复 (无需手动 reset)

V4.6.1 design (按 dev-plan §6.1):
- 仅失败计数达到阈值 (默认 1) 触发 cooldown — 个人使用场景, 1 次失败就跳
- 成功调用 mark_success() 重置计数
- 内存管理 (不持久化) — 重启后 cooldown 清零 (跟频限器一致)

V4.9 design (2026-08-25, 智能探活状态机):
- 状态: healthy / failing / degraded / needs_human / manual_disabled
- _state 扩展字段: first/last_failure_at, last_error_type/code/msg, needs_human,
  next_probe_at, probe_attempts, last_success_at, manual_disabled_at
- mark_failure 接收 ErrorInfo (按错误类型差异化冷却时长)
- 新方法: mark_manual_disabled / clear_manual_disabled / probe_result /
  accounts_needing_human / accounts_due_for_probe
- snapshot 扩展返回所有新字段

为什么不用 DB:
- 频限器也是内存, 已有先例
- cooldown 是临时状态, DB 持久化反而复杂 (重启后上次失败不应继续禁用)
- 探活结果已写 account_health_log 表,失败事件写 usage_logs (V4.9)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_FAILURE_THRESHOLD = 1  # V4.6.1: 1 次失败就触发 (个人场景容错低)

# V4.9: needs_human 触发阈值 (按 ErrorCategory)
# 当某 error_type 累计失败次数 >= 此阈值时, 标记 needs_human=True
DEFAULT_NEEDS_HUMAN_THRESHOLDS = {
    "quota_exhausted": 2,      # quota 失败 2 次 → 需人工 (避免单次偶发误判)
    "auth_failed": 1,           # auth 失败 1 次即需人工 (key 失效不该自愈)
    "model_unsupported": 2,     # model 失败 2 次 → 需人工 (避免误判)
}

# V4.9: 指数退避上限 (探活间隔不会超过这个值)
DEFAULT_MAX_PROBE_BACKOFF_SECONDS = 3600


class CooldownManager:
    """单进程 cooldown 管理器.

    公开方法:
    - is_in_cooldown(account_id) -> bool
    - mark_failure(account_id, reason="", error_info=None) -> None  # 新签名接收 ErrorInfo
    - mark_success(account_id) -> None
    - reset(account_id) -> None                                       # 强制清空
    - cleanup() -> int                                                # 清理过期 entry
    - snapshot() -> dict                                              # 调试用快照
    - mark_manual_disabled(account_id) -> None                        # V4.9: 人工禁用
    - clear_manual_disabled(account_id) -> None                       # V4.9: 解除人工禁用
    - probe_result(account_id, success, error_info) -> None           # V4.9: 探活结果
    - accounts_needing_human() -> list[int]                           # V4.9: 列出需人工
    - accounts_due_for_probe(now=None) -> list[int]                   # V4.9: 该探活的
    """

    def __init__(
        self,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        needs_human_thresholds: Optional[dict] = None,
        max_probe_backoff_seconds: int = DEFAULT_MAX_PROBE_BACKOFF_SECONDS,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.failure_threshold = failure_threshold
        self.needs_human_thresholds = needs_human_thresholds or DEFAULT_NEEDS_HUMAN_THRESHOLDS
        self.max_probe_backoff_seconds = max_probe_backoff_seconds
        # account_id → state dict (V4.9 扩展)
        self._state: dict[int, dict] = {}

    # ============ 状态查询 ============

    def is_in_cooldown(self, account_id: int) -> bool:
        """是否处于冷却期 (cooldown_until > now)."""
        s = self._state.get(account_id)
        if not s or s.get("cooldown_until") is None:
            return False
        if time.time() >= s["cooldown_until"]:
            # 过期了, 自动清掉
            self._state.pop(account_id, None)
            return False
        return True

    def cooldown_remaining(self, account_id: int) -> float:
        """剩余冷却秒数 (0 表示不在冷却)."""
        s = self._state.get(account_id)
        if not s or s.get("cooldown_until") is None:
            return 0.0
        remaining = s["cooldown_until"] - time.time()
        if remaining <= 0:
            self._state.pop(account_id, None)
            return 0.0
        return remaining

    def is_manual_disabled(self, account_id: int) -> bool:
        """是否被人工禁用"""
        s = self._state.get(account_id)
        return bool(s and s.get("manual_disabled_at") is not None)

    def needs_human(self, account_id: int) -> bool:
        """是否需要人工干预"""
        s = self._state.get(account_id)
        return bool(s and s.get("needs_human"))

    def is_due_for_probe(self, account_id: int, now: Optional[float] = None) -> bool:
        """是否到了探活时间 (next_probe_at <= now 且 不在 cooldown 且 非 needs_human)"""
        s = self._state.get(account_id)
        if not s:
            return False
        # needs_human / manual_disabled 不主动探
        if s.get("needs_human") or s.get("manual_disabled_at") is not None:
            return False
        npa = s.get("next_probe_at")
        if npa is None:
            return False
        if now is None:
            now = time.time()
        return now >= npa

    # ============ 状态变更 ============

    def mark_failure(
        self,
        account_id: int,
        reason: str = "",
        error_info=None,  # gateway.error_classifier.ErrorInfo
        sibling_account_ids: Optional[list[int]] = None,
    ) -> None:
        """标记一次失败. 失败次数达阈值 → 进入 cooldown.

        V4.9 新行为 (传 error_info 时):
        - 用 error_info.suggested_cooldown_seconds 决定冷却时长
        - 用 error_info.suggested_probe_after_seconds 决定下次探活
        - 累计次数达 needs_human_thresholds → needs_human=True
        - 记录 last_error_type / last_error_code / last_error_msg

        V6.4.x (2026-09-14):
        - rate_limited 指数退避: 连续失败冷却 30s→60s→…封顶
          max_probe_backoff_seconds (共享池限流下固定 30s 会解冻即重撞)
        - sibling_account_ids: 同渠道兄弟账号. 兄弟已在 rate_limited 冷却中
          说明共享池整体饱和 (sensenova 双账号同池实证), 自身冷却对齐兄弟
          剩余时长, 消除账号轮换乒乓

        向后兼容 (旧调用方只传 reason):
        - reason 字段保留作为日志
        - 行为退化到 V4.6.1 风格 (固定 60s, 不分类, 不探活)
        """
        now = time.time()
        s = self._state.setdefault(account_id, {
            "count": 0,
            "cooldown_until": None,
            # V4.9 扩展字段默认值
            "first_failure_at": now,
            "last_failure_at": now,
            "last_error_type": "",
            "last_error_code": None,
            "last_error_msg": "",
            "needs_human": False,
            "next_probe_at": None,
            "probe_attempts": 0,
            "last_success_at": None,
            "manual_disabled_at": None,
        })

        # 兼容: 如果是旧 state (没 V4.9 字段), 补全
        if "first_failure_at" not in s:
            s["first_failure_at"] = s.get("last_failure_at") or now
            s["last_error_type"] = ""
            s["last_error_code"] = None
            s["last_error_msg"] = ""
            s["needs_human"] = False
            s["next_probe_at"] = None
            s["probe_attempts"] = 0
            s["last_success_at"] = None
            s["manual_disabled_at"] = None

        s["count"] += 1
        s["last_failure_at"] = now

        if error_info is not None:
            # V4.9 新路径: 用 ErrorInfo 决定冷却 + 探活
            from gateway.error_classifier import ErrorCategory

            s["last_error_type"] = error_info.category.value
            s["last_error_code"] = error_info.error_code
            s["last_error_msg"] = error_info.error_msg

            # client_error 不进 cooldown (直接 return)
            # V6.x: context_window 同为客户端侧错误, 行为中性不冷却
            if error_info.category in (ErrorCategory.CLIENT_ERROR, ErrorCategory.CONTEXT_WINDOW):
                return

            # 冷却时长: 用 ErrorInfo 建议值
            cooldown_secs = error_info.suggested_cooldown_seconds
            probe_after = error_info.suggested_probe_after_seconds

            # V6.4.x: rate_limited 指数退避 (count 含本次, 首败即基准值不翻倍)
            if error_info.category == ErrorCategory.RATE_LIMITED and cooldown_secs > 0:
                cooldown_secs = min(
                    cooldown_secs * (2 ** (s["count"] - 1)),
                    self.max_probe_backoff_seconds,
                )

            # 触发条件: 失败次数 >= failure_threshold
            if s["count"] >= self.failure_threshold:
                s["cooldown_until"] = now + cooldown_secs

                # V6.4.x: 渠道级限流同步 — 只对齐"兄弟也在 rate_limited 冷却中"
                # 的场景 (共享池饱和信号); 兄弟因 auth/quota 冷却与本账号无关
                if (
                    error_info.category == ErrorCategory.RATE_LIMITED
                    and sibling_account_ids
                ):
                    sibling_until = max(
                        (
                            o.get("cooldown_until") or 0
                            for aid, o in self._state.items()
                            if aid in sibling_account_ids
                            and o.get("last_error_type") == "rate_limited"
                        ),
                        default=0,
                    )
                    if sibling_until > (s["cooldown_until"] or 0):
                        s["cooldown_until"] = sibling_until
                        logger.info(
                            f"CooldownManager: account_id={account_id} "
                            f"渠道级限流同步, 冷却对齐兄弟账号 "
                            f"(remaining={sibling_until - now:.1f}s)"
                        )

            # needs_human 阈值判断 (quota/auth/model)
            threshold = self.needs_human_thresholds.get(error_info.category.value, 999)
            if threshold < 999 and s["count"] >= threshold:
                s["needs_human"] = True
                # needs_human 后, 清掉 next_probe_at (必须人工, 不主动探活)
                s["next_probe_at"] = None
                probe_after = None

            # 下次探活时间: 指数退避 + ErrorInfo 建议
            # 公式: next_probe_after = min(probe_after * 2^(attempts-1), max_probe_backoff)
            # attempts=0: probe_after * 1 = probe_after
            # attempts=1: probe_after * 1 = probe_after (不退避, 给首次机会)
            # attempts>=2: 翻倍直到 cap
            if probe_after is not None and not s.get("needs_human"):
                attempts = s.get("probe_attempts", 0)
                if attempts <= 1:
                    backoff = probe_after
                else:
                    backoff = min(
                        self.max_probe_backoff_seconds,
                        probe_after * (2 ** (attempts - 1)),
                    )
                s["next_probe_at"] = now + backoff
        else:
            # 旧路径: 固定 60s 冷却, 不分类
            if s["count"] >= self.failure_threshold:
                s["cooldown_until"] = now + self.cooldown_seconds

        logger.warning(
            f"CooldownManager: account_id={account_id} 失败 {s['count']} 次 "
            f"(error_type={s.get('last_error_type') or 'legacy'}, "
            f"reason={reason!r}, needs_human={s.get('needs_human', False)})"
        )

    def mark_success(self, account_id: int) -> None:
        """标记一次成功 → 重置失败计数 + 清 cooldown + 清 needs_human.

        V6.4.x: 冷却期内的"迟到成功"不复位 — 长流式请求在冷却设置前路由、
        完成于冷却期内 (共享池饱和期 200/429 交替, 生产实证 429 冷却被 2s 后
        完成的在途 200 擦掉, 下一请求立刻重撞). 在途成功不证明账号当前健康:
        只记 last_success_at, 保留 count (指数退避延续) 与 cooldown_until
        (到期自愈). 冷却外的成功仍是全量复位.
        """
        s = self._state.get(account_id)
        if not s:
            return
        now = time.time()
        s["last_success_at"] = now
        cu = s.get("cooldown_until")
        if cu is not None and now < cu:
            return
        s["count"] = 0
        s["cooldown_until"] = None
        s["needs_human"] = False
        s["next_probe_at"] = None
        s["probe_attempts"] = 0
        # 保留 manual_disabled_at (人工禁用是独立的)
        # 保留 last_error_* (用于审计)

    def reset(self, account_id: int) -> None:
        """强制清空 (admin 操作). 与 mark_success 类似, 但也清 manual_disabled."""
        self._state.pop(account_id, None)

    def mark_manual_disabled(self, account_id: int) -> None:
        """V4.9: 人工禁用 (路由永久排除, 不进 cooldown 也不探活)."""
        now = time.time()
        s = self._state.setdefault(account_id, {
            "count": 0, "cooldown_until": None,
            "first_failure_at": now, "last_failure_at": now,
            "last_error_type": "", "last_error_code": None, "last_error_msg": "",
            "needs_human": False, "next_probe_at": None, "probe_attempts": 0,
            "last_success_at": None, "manual_disabled_at": None,
        })
        s["manual_disabled_at"] = now
        s["cooldown_until"] = now + 86400 * 365  # 远期, 实际由 DB status 排除
        logger.info(f"CooldownManager: account_id={account_id} 人工禁用")

    def clear_manual_disabled(self, account_id: int) -> None:
        """V4.9: 解除人工禁用."""
        s = self._state.get(account_id)
        if s:
            s["manual_disabled_at"] = None
        logger.info(f"CooldownManager: account_id={account_id} 解除人工禁用")

    def probe_result(
        self,
        account_id: int,
        success: bool,
        error_info=None,
    ) -> None:
        """V4.9: 智能探活结果回写.

        成功 → mark_success (回 healthy)
        失败 → mark_failure + probe_attempts += 1 (可能升级 needs_human)
        """
        if success:
            self.mark_success(account_id)
            return

        # 失败: 增加 probe_attempts 后调 mark_failure
        s = self._state.get(account_id)
        if s:
            s["probe_attempts"] = s.get("probe_attempts", 0) + 1
        else:
            # 没有历史 state (不常见), 先创建
            s = self._state.setdefault(account_id, {
                "count": 0, "cooldown_until": None,
                "first_failure_at": time.time(), "last_failure_at": time.time(),
                "last_error_type": "", "last_error_code": None, "last_error_msg": "",
                "needs_human": False, "next_probe_at": None, "probe_attempts": 0,
                "last_success_at": None, "manual_disabled_at": None,
            })
            s["probe_attempts"] = 1

        self.mark_failure(account_id, reason="probe_failed", error_info=error_info)

    # ============ 批量查询 ============

    def accounts_needing_human(self) -> list[int]:
        """V4.9: 列出所有 needs_human=True 的 account_id."""
        return [
            aid for aid, s in self._state.items()
            if s.get("needs_human") and not s.get("manual_disabled_at")
        ]

    def accounts_due_for_probe(self, now: Optional[float] = None) -> list[int]:
        """V4.9: 列出该探活的 account_id (next_probe_at <= now 且非 needs_human/manual)."""
        if now is None:
            now = time.time()
        return [
            aid for aid in self._state
            if self.is_due_for_probe(aid, now)
        ]

    # ============ 维护 ============

    def cleanup(self) -> int:
        """清理已过期的 entry. 返清理数.

        定期调用 (e.g. 5 分钟) 防止 _state 膨胀. 不强制 (is_in_cooldown 内会自动清).
        保留条件 (entry 不会被清):
        - needs_human=True (等人介入)
        - manual_disabled (人工禁用)
        - next_probe_at 未来 (等智能探活)
        """
        now = time.time()
        expired = []
        for aid, s in self._state.items():
            cu = s.get("cooldown_until")
            # cooldown 必须已过期
            if cu is None or now < cu:
                continue
            # needs_human 永远保留
            if s.get("needs_human"):
                continue
            # 人工禁用永远保留
            if s.get("manual_disabled_at") is not None:
                continue
            # next_probe_at 在未来也保留 (等探活)
            npa = s.get("next_probe_at")
            if npa is not None and npa > now:
                continue
            # 其他清掉
            expired.append(aid)
        for aid in expired:
            self._state.pop(aid, None)
        return len(expired)

    def snapshot(self) -> dict[int, dict]:
        """调试用: 当前所有冷却状态快照 (V4.8.7 + V4.9 字段)."""
        now = time.time()
        result = {}
        for aid, s in self._state.items():
            remaining = 0.0
            cu = s.get("cooldown_until")
            if cu is not None and cu > now:
                remaining = cu - now
            npa = s.get("next_probe_at")
            npa_remaining = max(0.0, npa - now) if npa else None
            result[aid] = {
                "count": s.get("count", 0),
                "cooldown_until": cu,
                "remaining_seconds": round(remaining, 1),
                "in_cooldown": remaining > 0,
                # V4.9 扩展字段
                "first_failure_at": s.get("first_failure_at"),
                "last_failure_at": s.get("last_failure_at"),
                "last_success_at": s.get("last_success_at"),
                "last_error_type": s.get("last_error_type") or "",
                "last_error_code": s.get("last_error_code"),
                "last_error_msg": s.get("last_error_msg") or "",
                "needs_human": bool(s.get("needs_human")),
                "next_probe_at": npa,
                "next_probe_in_seconds": round(npa_remaining, 1) if npa_remaining is not None else None,
                "probe_attempts": s.get("probe_attempts", 0),
                "manual_disabled": bool(s.get("manual_disabled_at")),
            }
        return result


# 全局单例 (跟 limiter 一致)
# V4.9: 从 settings 读 needs_human_thresholds 和 max_probe_backoff_seconds
def _build_cooldown_manager():
    """从 settings 构造 CooldownManager 单例 (延迟初始化, 避免 config 顺序问题)"""
    try:
        from config import settings
        return CooldownManager(
            cooldown_seconds=60,
            failure_threshold=1,
            needs_human_thresholds={
                "quota_exhausted": getattr(settings, 'needs_human_quota_threshold', 2),
                "auth_failed": getattr(settings, 'needs_human_auth_threshold', 1),
                "model_unsupported": getattr(settings, 'needs_human_model_threshold', 2),
            },
            max_probe_backoff_seconds=getattr(settings, 'health_probe_max_backoff', 3600),
        )
    except Exception:
        return CooldownManager()


cooldown_manager = _build_cooldown_manager()