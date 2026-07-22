"""内存滑动窗口频限器

支持多种限制类型：
- QPM：每分钟请求数（滑动窗口 60s deque）
- TPM：每分钟 Token 数（滑动窗口按 token 累加）
- 并发数：asyncio.Semaphore
- 日限额：每日零点重置计数器
"""

import asyncio
import logging
from collections import deque
from datetime import date
from time import time

logger = logging.getLogger(__name__)


class RateLimiter:
    """内存频限器（单进程，无需 Redis）"""

    def __init__(self):
        # QPM 滑动窗口: {account_id: deque([timestamp, ...])}
        self._qpm_windows: dict[int, deque] = {}
        # TPM 滑动窗口: {account_id: deque([(timestamp, token_count), ...])}
        self._tpm_windows: dict[int, deque] = {}
        # 并发控制: {account_id: asyncio.Semaphore}
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        # 日限额: {account_id: {"date": "2024-01-01", "count": 0}}
        self._daily_counters: dict[int, dict] = {}

    def is_limited(self, account_id: int, quotas: list[dict]) -> bool:
        """检查账号是否触发频限

        Args:
            account_id: 账号 ID
            quotas: 该账号的频限配置列表

        Returns:
            True 表示已触发限制，应跳过该账号
        """
        for quota in quotas:
            quota_type = quota["quota_type"]
            limit_value = quota["limit_value"]

            if quota_type == "qpm":
                if self._check_qpm(account_id, limit_value):
                    return True
            elif quota_type == "tpm":
                if self._check_tpm(account_id, limit_value):
                    return True
            elif quota_type == "daily":
                if self._check_daily(account_id, limit_value):
                    return True
            elif quota_type == "concurrent":
                if self._check_concurrent(account_id, limit_value):
                    return True

        return False

    def record_request(self, account_id: int):
        """记录一次请求（QPM 窗口）"""
        now = time()
        if account_id not in self._qpm_windows:
            self._qpm_windows[account_id] = deque()
        self._qpm_windows[account_id].append(now)

    def record_tokens(self, account_id: int, token_count: int):
        """记录 token 使用量（TPM 窗口）"""
        now = time()
        if account_id not in self._tpm_windows:
            self._tpm_windows[account_id] = deque()
        self._tpm_windows[account_id].append((now, token_count))

    def record_daily(self, account_id: int):
        """记录日限额计数"""
        today = date.today().isoformat()
        if account_id not in self._daily_counters:
            self._daily_counters[account_id] = {"date": today, "count": 0}
        counter = self._daily_counters[account_id]
        if counter["date"] != today:
            counter["date"] = today
            counter["count"] = 0
        counter["count"] += 1

    def acquire_concurrent(self, account_id: int, limit: int) -> bool:
        """尝试获取并发信号量（非阻塞）"""
        if account_id not in self._semaphores:
            self._semaphores[account_id] = asyncio.Semaphore(limit)
        sem = self._semaphores[account_id]
        if sem.locked():
            return False
        # 注意：这里用 _value 判断是简化实现
        return True

    def _check_qpm(self, account_id: int, limit: int) -> bool:
        """检查 QPM 是否超限"""
        now = time()
        if account_id not in self._qpm_windows:
            return False
        dq = self._qpm_windows[account_id]
        # 清理 60 秒前的记录
        while dq and dq[0] < now - 60:
            dq.popleft()
        return len(dq) >= limit

    def _check_tpm(self, account_id: int, limit: int) -> bool:
        """检查 TPM 是否超限"""
        now = time()
        if account_id not in self._tpm_windows:
            return False
        dq = self._tpm_windows[account_id]
        # 清理 60 秒前的记录
        while dq and dq[0][0] < now - 60:
            dq.popleft()
        total_tokens = sum(t[1] for t in dq)
        return total_tokens >= limit

    def _check_daily(self, account_id: int, limit: int) -> bool:
        """检查日限额是否超限"""
        today = date.today().isoformat()
        if account_id not in self._daily_counters:
            return False
        counter = self._daily_counters[account_id]
        if counter["date"] != today:
            return False  # 新的一天，重置
        return counter["count"] >= limit

    def _check_concurrent(self, account_id: int, limit: int) -> bool:
        """检查并发数是否超限"""
        if account_id not in self._semaphores:
            return False
        sem = self._semaphores[account_id]
        # Semaphore 内部 _value 表示剩余可用数
        return sem._value <= 0

    def get_status(self, account_id: int) -> dict:
        """获取账号频限状态（用于 Dashboard 展示）"""
        now = time()
        today = date.today().isoformat()

        qpm_count = 0
        if account_id in self._qpm_windows:
            dq = self._qpm_windows[account_id]
            while dq and dq[0] < now - 60:
                dq.popleft()
            qpm_count = len(dq)

        tpm_count = 0
        if account_id in self._tpm_windows:
            dq = self._tpm_windows[account_id]
            while dq and dq[0][0] < now - 60:
                dq.popleft()
            tpm_count = sum(t[1] for t in dq)

        daily_count = 0
        if account_id in self._daily_counters:
            counter = self._daily_counters[account_id]
            if counter["date"] == today:
                daily_count = counter["count"]

        return {
            "qpm_current": qpm_count,
            "tpm_current": tpm_count,
            "daily_current": daily_count,
        }


# 全局单例
rate_limiter = RateLimiter()
