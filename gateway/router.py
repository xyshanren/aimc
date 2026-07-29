"""路由决策引擎

核心决策流程：
1. 筛选支持该 model 的渠道
2. 识别当前时段 -> 获取各渠道当前时段价格
3. 频限过滤（排除 QPM/TPM/并发 已达阈值的账号）
4. 配额过滤（排除 quota 耗尽的账号）
5. 多维排序：价格 ASC > 剩余配额 DESC > 优先级 DESC
6. 选择最优渠道+账号执行
"""

import logging
from datetime import datetime, date
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings

logger = logging.getLogger(__name__)


class RouteEngine:
    """路由决策引擎

    启动时从 SQLite 加载渠道+价格到内存缓存，定时刷新。
    """

    def __init__(self):
        # 内存缓存：渠道列表（含账号和价格信息）
        self._channels_cache: list[dict] = []
        self._last_refresh: datetime | None = None

    async def refresh_cache(self):
        """从数据库刷新渠道缓存到内存"""
        from db.engine import async_session_factory
        from db.models import Channel, Account, PriceSnapshot, ChannelQuota

        async with async_session_factory() as session:
            # 查询所有活跃渠道及其关联数据
            stmt = select(Channel).where(Channel.status == "active")
            result = await session.execute(stmt)
            channels = result.scalars().all()

            cache = []
            for ch in channels:
                # 获取该渠道的活跃账号
                active_accounts = [
                    acc for acc in ch.accounts
                    if acc.status == "active"
                ]
                if not active_accounts:
                    continue

                # 获取该渠道的价格快照
                prices = [
                    {
                        "model": p.model,
                        "price_type": p.price_type,
                        "time_range": p.time_range,
                        "input_price": p.input_price,
                        "output_price": p.output_price,
                    }
                    for p in ch.price_snapshots
                    if self._is_price_effective(p)
                ]

                # 获取频限配置
                quotas = [
                    {
                        "quota_type": q.quota_type,
                        "limit_value": q.limit_value,
                        "account_id": q.account_id,
                    }
                    for q in ch.quotas
                ]

                for acc in active_accounts:
                    # API-Sec-1: 优先用 ciphertext 解密, fallback 到 api_key 明文 (迁移期)
                    plaintext_key = self._resolve_account_key(acc)
                    if plaintext_key is None:
                        # 既无 ciphertext 也无明文, 跳过这个 account
                        logger.warning(
                            f"账号 {ch.name}/{acc.account_name} 既无 ciphertext 也无明文 key, 跳过"
                        )
                        continue

                    cache.append({
                        "id": ch.id,
                        "name": ch.name,
                        "provider": ch.provider,
                        "base_url": ch.base_url,
                        "priority": ch.priority,
                        "account_id": acc.id,
                        "account_name": acc.account_name,
                        "api_key": plaintext_key,
                        "quota_total": acc.quota_total,
                        "quota_used": acc.quota_used,
                        "quota_remaining": acc.quota_remaining,
                        "prices": prices,
                        "quotas": [q for q in quotas if q["account_id"] is None or q["account_id"] == acc.id],
                    })

            self._channels_cache = cache
            self._last_refresh = datetime.now()
            logger.info(f"路由缓存已刷新: {len(cache)} 个可用渠道账号")

    def _resolve_account_key(self, account) -> Optional[str]:
        """API-Sec-1: 透明获取 account 的明文 api_key

        优先用 ciphertext 解密 (Fernet), fallback 到明文 (仅迁移期)
        都拿不到返回 None (调用方 skip 这个 account)
        """
        # 优先 ciphertext
        if account.api_key_ciphertext is not None:
            try:
                from security.crypto import try_decrypt
                from config import settings
                plaintext = try_decrypt(account.api_key_ciphertext, settings.aimc_master_key)
                if plaintext is not None:
                    return plaintext
            except Exception as e:
                logger.error(f"账号 {account.account_name} ciphertext 解密失败: {e}")

        # Fallback: 明文 (deprecated, 仅迁移期)
        if account.api_key:
            return account.api_key

        return None

    def _is_price_effective(self, price) -> bool:
        """检查价格是否在有效期内"""
        today = date.today().isoformat()
        if price.effective_from and price.effective_from > today:
            return False
        if price.effective_to and price.effective_to < today:
            return False
        return True

    def select_best_channel(self, model: str) -> Optional[dict]:
        """核心决策：为指定模型选择最优渠道

        Args:
            model: 请求的模型名称

        Returns:
            最优渠道字典，或 None（无可用渠道）
        """
        if not self._channels_cache:
            return None

        current_hour = datetime.now().hour

        # 1. 筛选支持该模型的渠道
        candidates = []
        for ch in self._channels_cache:
            price_info = self._get_current_price(ch, model, current_hour)
            if price_info is not None:
                candidates.append((ch, price_info))

        if not candidates:
            # 尝试模糊匹配（如 deepseek-chat 匹配 deepseek 前缀）
            for ch in self._channels_cache:
                price_info = self._get_fuzzy_price(ch, model, current_hour)
                if price_info is not None:
                    candidates.append((ch, price_info))

        if not candidates:
            # Phase 2E: 无渠道明确支持该 model — 尝试 fallback 到 priority 最低的 channel
            # (默认是聚合平台, 例如硅基流动: 1 个 key 通吃 100+ model)
            fallback = self._get_fallback_channel()
            if fallback and self._is_channel_usable(fallback):
                logger.info(
                    f"无可用渠道支持模型: {model}, fallback 到 {fallback['name']} "
                    f"(priority={fallback['priority']})"
                )
                return fallback
            logger.warning(f"无可用渠道支持模型: {model}")
            return None

        # 2. 频限过滤
        from gateway.limiter import rate_limiter
        candidates = [
            (ch, price) for ch, price in candidates
            if not rate_limiter.is_limited(ch["account_id"], ch["quotas"])
        ]

        if not candidates:
            logger.warning(f"所有渠道频限已满: {model}")
            return None

        # 3. 配额过滤
        candidates = [
            (ch, price) for ch, price in candidates
            if ch["quota_remaining"] is None or ch["quota_remaining"] > 0
        ]

        if not candidates:
            logger.warning(f"所有渠道配额耗尽: {model}")
            return None

        # 4. 多维排序：价格 ASC > 剩余配额 DESC > 优先级 DESC
        def sort_key(item):
            ch, price = item
            input_price = price.get("input_price") or 0
            quota_remaining = ch["quota_remaining"] if ch["quota_remaining"] is not None else 999999999
            priority = ch["priority"]
            return (input_price, -quota_remaining, -priority)

        candidates.sort(key=sort_key)

        best_channel, best_price = candidates[0]
        logger.debug(
            f"路由决策: model={model} -> {best_channel['name']} "
            f"(price={best_price.get('input_price')}, priority={best_channel['priority']})"
        )

        return best_channel

    def _get_current_price(self, channel: dict, model: str, current_hour: int) -> Optional[dict]:
        """获取渠道在指定模型和当前时段的价格"""
        for p in channel["prices"]:
            if p["model"] != model:
                continue
            if self._match_time_range(p["time_range"], current_hour):
                return p
        return None

    def _get_fuzzy_price(self, channel: dict, model: str, current_hour: int) -> Optional[dict]:
        """模糊匹配模型名（支持前缀匹配）"""
        model_lower = model.lower()
        for p in channel["prices"]:
            price_model = p["model"].lower()
            # 前缀匹配：deepseek-chat 匹配 deepseek
            if model_lower.startswith(price_model) or price_model.startswith(model_lower):
                if self._match_time_range(p["time_range"], current_hour):
                    return p
        return None

    def _get_fallback_channel(self) -> Optional[dict]:
        """Phase 2E: 获取 priority 最低的 channel 作为 fallback (默认 = 聚合平台)

        设计: 硅基流动这种第三方聚合 (priority=1) 1 个 key 通吃 100+ model,
        当 prices 表里没列某个 model 时, 走 fallback 把 model 名透传给硅基流动,
        硅基流动再根据 model 名路由到对应厂商.

        Returns:
            priority 最小的 channel, 或 None (没有可用 channel)
        """
        if not self._channels_cache:
            return None
        return min(self._channels_cache, key=lambda ch: ch.get("priority", 999))

    def _is_channel_usable(self, ch: dict) -> bool:
        """Phase 2E: 检查 channel 是否可用 (限频 + 配额), 不检查 model 支持"""
        from gateway.limiter import rate_limiter
        if rate_limiter.is_limited(ch["account_id"], ch["quotas"]):
            return False
        if ch.get("quota_remaining") is not None and ch["quota_remaining"] <= 0:
            return False
        return True

    def _match_time_range(self, time_range: str, current_hour: int) -> bool:
        """判断当前时间是否在时段范围内

        支持格式：
        - "全天" -> 始终匹配
        - "00:00-08:00" -> 0点到8点
        - "08:00-24:00" -> 8点到24点
        """
        if not time_range or time_range == "全天":
            return True

        try:
            parts = time_range.split("-")
            if len(parts) != 2:
                return True
            start_hour = int(parts[0].split(":")[0])
            end_hour = int(parts[1].split(":")[0])
            if end_hour == 24:
                end_hour = 24
            return start_hour <= current_hour < end_hour
        except (ValueError, IndexError):
            return True  # 解析失败则默认匹配

    def get_available_models(self) -> list[str]:
        """获取所有可用模型列表"""
        models = set()
        for ch in self._channels_cache:
            for p in ch["prices"]:
                models.add(p["model"])
        return sorted(models)

    def get_candidates(self, model: str) -> list[dict]:
        """获取指定模型的所有候选渠道（用于 fallback）"""
        current_hour = datetime.now().hour
        candidates = []

        for ch in self._channels_cache:
            price_info = self._get_current_price(ch, model, current_hour)
            if price_info is None:
                price_info = self._get_fuzzy_price(ch, model, current_hour)
            if price_info is not None:
                candidates.append(ch)

        return candidates


# 全局单例
route_engine = RouteEngine()
