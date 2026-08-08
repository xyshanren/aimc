"""V4.6.1 CooldownManager — 失败账号冷却管理 (LiteLLM 启发)

背景: 同一 channel 多个账号 (e.g. deepseek_01/deepseek_02) 时, 单个 key 临时失效
(401/403/5xx 连续) 不应让后续请求继续撞. 借鉴 LiteLLM 的 cooldown 机制:
- 失败 N 次 → cooldown N 秒 (默认 60s)
- cooldown 期内路由跳过该 account
- cooldown 过期 → 自动恢复 (无需手动 reset)

V4.6.1 design (按 dev-plan §6.1):
- 仅失败计数达到阈值 (默认 1) 触发 cooldown — 个人使用场景, 1 次失败就跳
- 成功调用 mark_success() 重置计数
- 内存管理 (不持久化) — 重启后 cooldown 清零 (跟频限器一致)

为什么不用 DB:
- 频限器也是内存, 已有先例
- cooldown 是临时状态, DB 持久化反而复杂 (重启后上次失败不应继续禁用)
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_COOLDOWN_SECONDS = 60
DEFAULT_FAILURE_THRESHOLD = 1  # V4.6.1: 1 次失败就触发 (个人场景容错低)


class CooldownManager:
    """单进程 cooldown 管理器.

    公开方法:
    - is_in_cooldown(account_id) -> bool
    - mark_failure(account_id, reason="") -> None  # 触发冷却
    - mark_success(account_id) -> None              # 重置计数
    - reset(account_id) -> None                     # 强制清空
    - cleanup() -> int                              # 清理过期 entry, 返清理数
    """

    def __init__(
        self,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.failure_threshold = failure_threshold
        # account_id → {count, cooldown_until}
        # cooldown_until = None 表示未冷却, 只累计失败次数
        self._state: dict[int, dict] = {}

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

    def mark_failure(self, account_id: int, reason: str = "") -> None:
        """标记一次失败. 失败次数达阈值 → 进入 cooldown."""
        s = self._state.setdefault(account_id, {"count": 0, "cooldown_until": None})
        s["count"] += 1
        if s["count"] >= self.failure_threshold:
            s["cooldown_until"] = time.time() + self.cooldown_seconds
            logger.warning(
                f"CooldownManager: account_id={account_id} 失败 {s['count']} 次 → "
                f"进入冷却 {self.cooldown_seconds}s (reason={reason!r})"
            )

    def mark_success(self, account_id: int) -> None:
        """标记一次成功 → 重置失败计数 + 清 cooldown."""
        if account_id in self._state:
            self._state.pop(account_id, None)

    def reset(self, account_id: int) -> None:
        """强制清空 (admin 操作)."""
        self._state.pop(account_id, None)

    def cleanup(self) -> int:
        """清理已过期的 entry. 返清理数.

        定期调用 (e.g. 5 分钟) 防止 _state 膨胀. 不强制 (is_in_cooldown 内会自动清).
        """
        now = time.time()
        expired = [
            aid for aid, s in self._state.items()
            if s.get("cooldown_until") is not None and now >= s["cooldown_until"]
        ]
        for aid in expired:
            self._state.pop(aid, None)
        return len(expired)

    def snapshot(self) -> dict[int, dict]:
        """调试用: 当前所有冷却状态快照."""
        return {k: dict(v) for k, v in self._state.items()}


# 全局单例 (跟 limiter 一致)
cooldown_manager = CooldownManager()