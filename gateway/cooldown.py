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
    - reconcile_manual_disabled(db_status) -> dict                    # V6.14: DB 对账内存禁用标记
    - probe_result(account_id, success, error_info) -> None           # V4.9: 探活结果
    - accounts_needing_human() -> list[int]                           # V4.9: 列出需人工
    - accounts_due_for_probe(now=None) -> list[int]                   # V4.9: 该探活的
    """

    # V6.5: 冷却过期后 count 的保留窗 (秒) — 窗口内再次失败延续指数退避序列,
    # 超过窗口视作新的一系列失败, count 从 0 重来 (防陈年 count 吃满退避上限)
    _COUNT_RETENTION_SECONDS: float = 600.0

    def __init__(
        self,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        needs_human_thresholds: Optional[dict] = None,
        max_probe_backoff_seconds: int = DEFAULT_MAX_PROBE_BACKOFF_SECONDS,
        quota_probe_initial_seconds: int = 300,
        quota_probe_max_seconds: int = 1800,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.failure_threshold = failure_threshold
        self.needs_human_thresholds = needs_human_thresholds or DEFAULT_NEEDS_HUMAN_THRESHOLDS
        self.max_probe_backoff_seconds = max_probe_backoff_seconds
        # V6.15: 配额桶恢复探测节拍 (指数退避 initial → max)
        self.quota_probe_initial_seconds = quota_probe_initial_seconds
        self.quota_probe_max_seconds = quota_probe_max_seconds
        # account_id → state dict (V4.9 扩展)
        self._state: dict[int, dict] = {}
        # V6.10 (2026-09-20): 模型桶级隔离 — (account_id, model) → 冷却截止时间戳.
        # 上游按模型拆分配额桶 (e.g. taotoken deepseek_for_coding/glm_for_coding) 时,
        # 桶 A quota 耗尽只封 A, 桶 B 继续可用 — 账号级冷却会把 B 一起拖死 (9/20
        # 实证: deepseek 桶 429 后 glm 本可承接, 却 fallback 到 glm_bigmodel).
        # 仅 QUOTA_EXHAUSTED 且携带 model 的失败进入这里; 其余错误仍账号级.
        # 与 _state 同生命周期 (内存, 重启清零).
        self._quarantine: dict[tuple[int, str], float] = {}
        # V6.15 (2026-09-28): 配额桶恢复探测节拍 — (account_id, model) →
        # {"next_probe_at": ts, "attempts": n}. 探活 GET /models 零 token 感知
        # 不到配额恢复 (上游窗口重置 / 用户后台手动重置都探不到), 需 1-token
        # 真聊天探测; 到点由 intelligence/channel_health.quota_recovery_probe 执行.
        self._quarantine_probe: dict[tuple[int, str], dict] = {}

    # ============ 状态查询 ============

    def is_in_cooldown(self, account_id: int, model: Optional[str] = None) -> bool:
        """是否处于冷却期 (cooldown_until > now).

        V6.10: model 给定时先查该 (account, model) 配额桶隔离, 未命中再落回
        账号级检查; model 缺省时纯账号级 (旧语义不变).
        """
        if model:
            until = self._quarantine.get((account_id, model))
            if until is not None:
                if time.time() >= until:
                    # 桶隔离过期即移除 (配额桶无指数退避序列, 不需要保留 count)
                    self._quarantine.pop((account_id, model), None)
                    self._quarantine_probe.pop((account_id, model), None)
                    return False
                return True
        s = self._state.get(account_id)
        if not s or s.get("cooldown_until") is None:
            return False
        if time.time() >= s["cooldown_until"]:
            # V6.5: 过期不再 pop 整个 state — count 承载指数退避序列, 而
            # 「到期 → router 候选过滤查询 → 重撞」是主场景, pop 会把 count
            # 清零使退避永远停在首档 (v64x-429-fix-review.md §2.1 实证)。
            # 只记录过期时刻, state 由 cleanup() 按保留窗物理清理。
            s["cooldown_expired_at"] = s["cooldown_until"]
            s["cooldown_until"] = None
            return False
        return True

    def cooldown_remaining(self, account_id: int, model: Optional[str] = None) -> float:
        """剩余冷却秒数 (0 表示不在冷却).

        V6.15: model 给定时先查桶隔离 (语义对齐 is_in_cooldown) — 路由候选
        过滤告警需要报桶级剩余秒数, 账号级 cooldown_remaining 会报 0 误导.
        """
        if model:
            until = self._quarantine.get((account_id, model))
            if until is not None:
                remaining = until - time.time()
                if remaining <= 0:
                    self._quarantine.pop((account_id, model), None)
                    self._quarantine_probe.pop((account_id, model), None)
                    return 0.0
                return remaining
        s = self._state.get(account_id)
        if not s or s.get("cooldown_until") is None:
            return 0.0
        remaining = s["cooldown_until"] - time.time()
        if remaining <= 0:
            # V6.5: 同 is_in_cooldown — 过期不 pop, 保留 count 供退避延续
            s["cooldown_expired_at"] = s["cooldown_until"]
            s["cooldown_until"] = None
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
        model: Optional[str] = None,
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
            "cooldown_expired_at": None,
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
        if "cooldown_expired_at" not in s:
            s["cooldown_expired_at"] = None

        # V6.5: 上一次冷却过期已超过保留窗 → count 归零视作新的一系列失败
        # (否则陈年 count 会让久未触发的账号一次偶发失败就吃到退避上限)
        exp_at = s.get("cooldown_expired_at")
        if exp_at is not None and (now - exp_at) > self._COUNT_RETENTION_SECONDS:
            s["count"] = 0
        s["cooldown_expired_at"] = None

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
                # V6.10: 模型桶级配额隔离 — quota_exhausted 且携带 model 时只封该
                # 上游配额桶, 不设账号级 cooldown_until (同账号另一模型桶继续路由).
                if (
                    model
                    and error_info.category == ErrorCategory.QUOTA_EXHAUSTED
                    and cooldown_secs > 0
                ):
                    self._quarantine[(account_id, model)] = now + cooldown_secs
                    s["last_error_model"] = model
                    # V6.15: 安排恢复探测节拍 — GET /models 探不到配额恢复,
                    # 由 quota_recovery_probe 发 1-token 真聊天感知 (上游窗口
                    # 重置 / 用户后台手动重置都靠它提前放行)
                    self._quarantine_probe[(account_id, model)] = {
                        "next_probe_at": now + self.quota_probe_initial_seconds,
                        "attempts": 0,
                    }
                    logger.warning(
                        f"CooldownManager: account_id={account_id} model={model} "
                        f"配额桶隔离冷却 {cooldown_secs:.0f}s "
                        f"(不封账号, 兄弟模型桶可继续路由; "
                        f"恢复探测首探 {self.quota_probe_initial_seconds}s 后)"
                    )
                    # 桶级失败不安排账号级探活 (探活 GET /models 无桶语义;
                    # needs_human 升级后本就清除 next_probe_at)
                    probe_after = None
                else:
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

    def mark_success(
        self,
        account_id: int,
        model: Optional[str] = None,
        *,
        force: bool = False,
    ) -> None:
        """标记一次成功 → 重置失败计数 + 清 cooldown + 清 needs_human.

        V6.4.x: 冷却期内的"迟到成功"不复位 — 长流式请求在冷却设置前路由、
        完成于冷却期内 (共享池饱和期 200/429 交替, 生产实证 429 冷却被 2s 后
        完成的在途 200 擦掉, 下一请求立刻重撞). 在途成功不证明账号当前健康:
        只记 last_success_at, 保留 count (指数退避延续) 与 cooldown_until
        (到期自愈). 冷却外的成功仍是全量复位.

        V6.10: model 给定时先清该 (account, model) 配额桶隔离 — glm 桶的 200
        不应擦掉 deepseek 桶的 1h 冷却 (配额桶独立, 见 _quarantine); 账号级
        复位逻辑照旧.

        V6.17 (2026-10-21): force=True 越过迟到成功护栏, 供人工确认恢复
        (ack-recover) 使用. 冷却内点"确认恢复"是用户已核实上游恢复的强信号,
        应当立即清状态 + 立即探活, 不能被在途护栏拦住 (此前 ack-recover 调
        mark_success 对冷却中账号 early-return 什么都不清, 按钮形同虚设).
        """
        if model:
            self._quarantine.pop((account_id, model), None)
        if force:
            # 人工确认 = 整账号放行: 同时清该账号所有模型桶隔离 + 恢复探测节拍
            # (桶不清的话, 确认恢复后该模型仍被路由排除, 按钮等于没按)。
            # 放在 state 检查之前 — cleanup() 会清掉过期的 _state 条目, 而 5h
            # 桶可能还活着, 此时不能因为没有 state 就漏清。
            for key in [k for k in self._quarantine if k[0] == account_id]:
                self._quarantine.pop(key, None)
            for key in [k for k in self._quarantine_probe if k[0] == account_id]:
                self._quarantine_probe.pop(key, None)
        s = self._state.get(account_id)
        if not s:
            return
        now = time.time()
        s["last_success_at"] = now
        if not force:
            cu = s.get("cooldown_until")
            if cu is not None and now < cu:
                return
        s["count"] = 0
        s["cooldown_until"] = None
        s["cooldown_expired_at"] = None
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
        """V4.9: 解除人工禁用.

        V6.14: 同时清掉 mark_manual_disabled 埋的远期 cooldown_until — 否则
        解除后 is_in_cooldown 仍为 True 一整年, 路由继续排除 (glm_01 启用后
        僵死 4 天实证), 待处理栏也会从「人工禁用」变成「自动冷却中 31212725s」。
        只清人工禁用自身留下的痕迹: 未禁用过 = no-op, 不擦正常冷却退避序列
        (禁用期间路由排除, 不会有新失败进来, 无被误清的风险)。
        """
        s = self._state.get(account_id)
        if not s or s.get("manual_disabled_at") is None:
            return
        s["manual_disabled_at"] = None
        s["cooldown_until"] = None
        s["cooldown_expired_at"] = None
        logger.info(f"CooldownManager: account_id={account_id} 解除人工禁用")

    def reconcile_manual_disabled(self, db_status_by_id: dict[int, str]) -> dict[str, int]:
        """V6.14: 人工禁用对账 — 以 DB accounts.status 为准校正内存标记.

        人工禁用有两份真相 (DB status+manual_disabled_at / 内存 manual_disabled_at),
        而写入路径各修一半: 待处理卡两端都写, 账号表 /enable /disable 与
        rotate_account.py 只写 DB。曾双向漂移: glm_01 DB 已启用但内存仍禁用
        (路由真排除, 僵死 4 天), taotoken_01 DB 禁用但重启后内存丢标记 (从
        待处理消失)。refresh_cache 每轮 (启动 / 60s TTL / db mtime 变化) 调用,
        DB 永远赢。不在 db_status_by_id 里的账号视作已删除, 同样清标记。

        Returns: {"marked": n, "cleared": n} 供日志。
        """
        marked = cleared = 0
        for aid, status in db_status_by_id.items():
            disabled_in_db = status == "manual_disabled"
            s = self._state.get(aid)
            in_memory = bool(s and s.get("manual_disabled_at") is not None)
            if disabled_in_db and not in_memory:
                self.mark_manual_disabled(aid)
                marked += 1
            elif not disabled_in_db and in_memory:
                self.clear_manual_disabled(aid)
                cleared += 1
        for aid in list(self._state):
            if aid not in db_status_by_id and self._state[aid].get("manual_disabled_at") is not None:
                self.clear_manual_disabled(aid)
                cleared += 1
        return {"marked": marked, "cleared": cleared}

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

    def quota_buckets_due_for_probe(self, now: Optional[float] = None) -> list[tuple[int, str]]:
        """V6.15: 列出该做恢复探测的配额桶 (account_id, model).

        范围: 仍在隔离中 (now < until) 且到探测点 (next_probe_at <= now) 的桶;
        桶已到期的不探 (路由过滤自然复检, 省一次上游调用)。
        manual_disabled 账号不探 (已被路由层排除)。

        V6.17 (2026-10-21): needs_human 账号**继续**探 — 配额失败第 2 次会
        升级 needs_human, 而订阅窗口类配额 (minimax Token Plan 5h) 在窗口
        耗尽期内会反复失败, 一旦 needs_human 停掉探测, 桶只能等冷却到期
        (窗口刷新后仍不自清), 自动恢复彻底断链。探测是"窗口是否重置"的
        观察而非路由失败, 不该被 needs_human 掐断 (与 quota_probe_result
        "探测不动 needs_human" 同原则)。
        """
        if now is None:
            now = time.time()
        due: list[tuple[int, str]] = []
        for (aid, model), until in self._quarantine.items():
            if now >= until:
                continue
            st = self._quarantine_probe.get((aid, model))
            if not st or st.get("next_probe_at", 0) > now:
                continue
            s = self._state.get(aid)
            if s and s.get("manual_disabled_at") is not None:
                continue
            due.append((aid, model))
        return due

    def quota_probe_result(
        self,
        account_id: int,
        model: str,
        success: bool,
        *,
        transient: bool = False,
    ) -> None:
        """V6.15: 配额桶恢复探测结果回写.

        成功 → mark_success 清桶隔离 + 复位账号失败计数 (渠道即刻回到路由池);
        失败 → 只推探测节拍, 不动 count/needs_human/cooldown — 探测是观察不是
        路由失败, 自愈型配额账号不该被探测推进 needs_human (needs_human 后本
        方法所在的探测循环也会停, 桶只能干等 until)。

        V6.19 (2026-10-27): transient=True 表示探测**自身异常** (超时/连接失败/
        job 级超时) 而非上游明确拒绝 (429 = 真没恢复)。两类失败节拍不同:
        - 上游明确拒绝: 原指数退避 300s→1800s (窗口离重置还远, 探了也白探)
        - 自身异常: 不确定性最大 — 上游可能已恢复 (2026-10-27 生产实证:
          minimax 窗口重置后探测超时被当"未恢复", 用户手点 30 分钟后才恢复),
          且异常与窗口重置强相关 (重置瞬间上游负载抖动)。短重试
          quota_probe_transient_seconds (默认 90s, 上限 300s 防风暴), 同时
          不递增 attempts (异常轮不应消耗退避曲线)。
        """
        if success:
            self._quarantine_probe.pop((account_id, model), None)
            self.mark_success(account_id, model=model)
            logger.info(
                f"CooldownManager: account_id={account_id} model={model} "
                f"恢复探测 200 → 清桶隔离, 回到路由池"
            )
            return
        st = self._quarantine_probe.get((account_id, model))
        if not st:
            return
        if transient:
            try:
                from config import settings
                short = int(getattr(settings, 'quota_probe_transient_seconds', 90))
            except Exception:
                short = 90
            st["next_probe_at"] = time.time() + short
            logger.info(
                f"CooldownManager: account_id={account_id} model={model} "
                f"恢复探测自身异常 (非上游拒绝), {short}s 后短重试 "
                f"(不消耗退避曲线)"
            )
            return
        st["attempts"] = st.get("attempts", 0) + 1
        backoff = min(
            self.quota_probe_max_seconds,
            self.quota_probe_initial_seconds * (2 ** (st["attempts"] - 1)),
        )
        st["next_probe_at"] = time.time() + backoff
        logger.info(
            f"CooldownManager: account_id={account_id} model={model} "
            f"恢复探测未恢复 (桶仍隔离), 下次探测 {backoff:.0f}s 后"
        )

    # ============ 维护 ============

    def cleanup(self) -> int:
        """清理已过期的 entry. 返清理数.

        定期调用 (e.g. 5 分钟) 防止 _state 膨胀.
        V6.5: 过期不再立即清 — 「冷却过期 → 查询 → 重撞」主场景下 count 需
        存活以延续指数退避 (v64x-429-fix-review.md §2.1), 过期超过
        _COUNT_RETENTION_SECONDS 保留窗才物理清理.
        保留条件 (entry 不会被清):
        - needs_human=True (等人介入)
        - manual_disabled (人工禁用)
        - next_probe_at 未来 (等智能探活)
        - 过期未超保留窗 (count 供退避延续)
        """
        now = time.time()
        expired = []
        for aid, s in self._state.items():
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
            # 过期参考点: 未被查询过的取 cooldown_until, 已被查询置空的取
            # cooldown_expired_at; 都没有 = 不在冷却序列, 保留 (如刚 mark_success)
            cu = s.get("cooldown_until")
            exp_at = s.get("cooldown_expired_at")
            if cu is not None and now >= cu:
                expiry_ref = cu
            elif cu is None and exp_at is not None:
                expiry_ref = exp_at
            else:
                continue
            if (now - expiry_ref) < self._COUNT_RETENTION_SECONDS:
                continue
            expired.append(aid)
        for aid in expired:
            self._state.pop(aid, None)
        # V6.10: 清理过期的配额桶隔离 (无保留窗语义, 到期即清)
        expired_q = [k for k, until in self._quarantine.items() if now >= until]
        for k in expired_q:
            self._quarantine.pop(k, None)
        # V6.15: 探测节拍记录跟随桶隔离 — 桶已不存在 (到期/被成功清掉) 的节拍无意义
        orphan_qp = [k for k in self._quarantine_probe if k not in self._quarantine]
        for k in orphan_qp:
            self._quarantine_probe.pop(k, None)
        return len(expired) + len(expired_q)

    def snapshot(self) -> dict[int, dict]:
        """调试用: 当前所有冷却状态快照 (V4.8.7 + V4.9 字段 + V6.10 配额桶隔离)."""
        now = time.time()
        result = {}
        for aid, s in self._state.items():
            remaining = 0.0
            cu = s.get("cooldown_until")
            if cu is not None and cu > now:
                remaining = cu - now
            npa = s.get("next_probe_at")
            npa_remaining = max(0.0, npa - now) if npa else None
            # V6.10: 该账号下的配额桶隔离 (model → 剩余秒)
            q_remaining = {
                model: round(until - now, 1)
                for (acc, model), until in self._quarantine.items()
                if acc == aid and until > now
            }
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
                "last_error_model": s.get("last_error_model"),
                "needs_human": bool(s.get("needs_human")),
                "next_probe_at": npa,
                "next_probe_in_seconds": round(npa_remaining, 1) if npa_remaining is not None else None,
                "probe_attempts": s.get("probe_attempts", 0),
                "manual_disabled": bool(s.get("manual_disabled_at")),
                "quarantine_models": q_remaining,
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
            quota_probe_initial_seconds=getattr(
                settings, 'quota_recovery_probe_initial_seconds', 300),
            quota_probe_max_seconds=getattr(
                settings, 'quota_recovery_probe_max_seconds', 1800),
        )
    except Exception:
        return CooldownManager()


cooldown_manager = _build_cooldown_manager()