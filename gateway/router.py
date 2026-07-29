"""路由决策引擎

核心决策流程：
1. 筛选支持该 model 的渠道
2. 识别当前时段 -> 获取各渠道当前时段价格
3. 频限过滤（排除 QPM/TPM/并发 已达阈值的账号）
4. 配额过滤（排除 quota 耗尽的账号）
5. 多维排序：价格 ASC > 剩余配额 DESC > 优先级 DESC
6. 选择最优渠道+账号执行
"""

import json
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

    缓存 (Phase 2.4):
    - _channels_cache: list[dict]  渠道 + 账号 + 价格 (精确模型路由用)
    - _registry_cache: dict[str, ORM]  model_id → ModelRegistry (组路由元信息)
    - _groups_cache: dict[str, ORM]  group_id → ModelGroup (组定义)
    - _group_members_cache: dict[str, list[str]]  group_id → [model_id] (组成员)
    """

    def __init__(self):
        # 现有: 渠道列表（含账号和价格信息）
        self._channels_cache: list[dict] = []
        self._last_refresh: datetime | None = None
        # Phase 2.4: 模型组路由元信息
        self._registry_cache: dict = {}        # model_id -> ModelRegistry ORM
        self._groups_cache: dict = {}          # group_id -> ModelGroup ORM
        self._group_members_cache: dict = {}   # group_id -> [model_id]

    async def refresh_cache(self):
        """从数据库刷新渠道缓存到内存"""
        from db.engine import async_session_factory
        from db.models import (
            Channel, Account, PriceSnapshot, ChannelQuota,
            ModelRegistry, ModelGroup, ModelGroupMember,
        )

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

            # ========== Phase 2.4: 模型组路由元信息 (3 个新 cache) ==========
            # 1) model_registry: model_id → ORM
            reg_result = await session.execute(
                select(ModelRegistry).where(ModelRegistry.status == "active")
            )
            self._registry_cache = {
                r.model_id: r for r in reg_result.scalars().all()
            }
            logger.info(
                f"路由缓存: {len(self._registry_cache)} 个 model registry "
                f"(family: {set(r.family for r in self._registry_cache.values())})"
            )

            # 2) model_groups: group_id → ORM
            grp_result = await session.execute(
                select(ModelGroup).where(ModelGroup.enabled == 1)
            )
            self._groups_cache = {
                g.group_id: g for g in grp_result.scalars().all()
            }
            logger.info(
                f"路由缓存: {len(self._groups_cache)} 个 model group "
                f"({list(self._groups_cache.keys())})"
            )

            # 3) model_group_members: group_id → [model_id]
            mem_result = await session.execute(select(ModelGroupMember))
            self._group_members_cache = {}
            for m in mem_result.scalars().all():
                self._group_members_cache.setdefault(m.group_id, []).append(m.model_id)
            logger.info(
                f"路由缓存: {sum(len(v) for v in self._group_members_cache.values())} "
                f"个 group members 跨 {len(self._group_members_cache)} 个组"
            )

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

    # ===========================================================
    # Phase 2.4: 模型组路由 (tier:/scene: 前缀 + fallback chain)
    # ===========================================================

    def select_channel(self, model: str) -> Optional[dict]:
        """Phase 2.4 入口: 按 model 字符串分发

        - "tier:xxx" / "scene:xxx" → _select_by_group (组路由 + fallback chain)
        - 其他 → select_best_channel (精确模型路由, 现有行为不变)

        返回的 channel 字典会带额外字段 (组路由时注入):
        - _actual_model: 实际命中的精确 model_id
        - _group_id: 命中的 model group (e.g. "tier:balanced")
        - _registry_tier: 命中 model 的能力档
        """
        if model.startswith("tier:") or model.startswith("scene:"):
            return self._select_by_group(model)
        return self.select_best_channel(model)

    def _select_by_group(self, group_id: str) -> Optional[dict]:
        """模型组路由: 在 group 内选最优 (model, channel) 三元组

        流程:
        1. visited set 防 fallback chain 死循环
        2. 查 group 定义 (enabled/tier/capabilities/min_context/require_vision 过滤)
        3. 查 group members (model_id 列表)
        4. 每个 member 跑 select_best_channel (复用现有精确模型路由)
        5. 跨 member 按 input_price ASC 选最便宜
        6. 失败 → fallback_group 递归
        7. 全部不可用 → 返回 None (调用方 503)

        路由策略 (Phase 2 只实现 cheapest + priority):
        - cheapest: input_price ASC (subscription/free 视为 0 元, 永远优先)
        - priority: channel.priority DESC
        """
        visited: set[str] = set()
        current: Optional[str] = group_id

        while current and current not in visited:
            visited.add(current)

            group = self._groups_cache.get(current)
            if not group:
                logger.warning(f"组路由: 组 {current} 不存在, fallback chain 终止")
                return None
            if not group.enabled:
                logger.warning(
                    f"组路由: 组 {current} 已禁用, 尝试 fallback → {group.fallback_group}"
                )
                current = group.fallback_group
                continue

            members = self._group_members_cache.get(current, [])
            if not members:
                logger.warning(
                    f"组路由: 组 {current} 没有成员, 尝试 fallback → {group.fallback_group}"
                )
                current = group.fallback_group
                continue

            # 1) 组过滤 + 2) 对每个 member 跑精确路由
            candidates: list[tuple[dict, str, float]] = []  # (channel, model_id, input_price)
            skipped = 0
            for model_id in members:
                registry = self._registry_cache.get(model_id)
                if not registry:
                    logger.debug(
                        f"  [{current}] member {model_id} 不在 model_registry, 跳过"
                    )
                    skipped += 1
                    continue

                # 组过滤: tier (NULL=不限)
                if group.tier and registry.tier != group.tier:
                    logger.debug(
                        f"  [{current}] {model_id} tier={registry.tier} "
                        f"!= {group.tier}, 跳过"
                    )
                    skipped += 1
                    continue

                # 组过滤: capabilities (要求所有 group 能力 model 都有)
                if group.capabilities:
                    try:
                        group_caps = set(json.loads(group.capabilities))
                    except (TypeError, json.JSONDecodeError):
                        group_caps = set()
                    try:
                        model_caps = set(
                            json.loads(registry.capabilities) if registry.capabilities else []
                        )
                    except (TypeError, json.JSONDecodeError):
                        model_caps = set()
                    if not group_caps.issubset(model_caps):
                        logger.debug(
                            f"  [{current}] {model_id} caps={model_caps} "
                            f"不包含 {group_caps - model_caps}, 跳过"
                        )
                        skipped += 1
                        continue

                # 组过滤: min_context
                if group.min_context and (
                    not registry.context_length
                    or registry.context_length < group.min_context
                ):
                    logger.debug(
                        f"  [{current}] {model_id} ctx={registry.context_length} "
                        f"< {group.min_context}, 跳过"
                    )
                    skipped += 1
                    continue

                # 组过滤: require_vision
                if group.require_vision and registry.modality != "vision":
                    logger.debug(
                        f"  [{current}] {model_id} modality={registry.modality} "
                        f"!= vision, 跳过"
                    )
                    skipped += 1
                    continue

                # 跑精确模型路由 (复用 select_best_channel)
                best = self.select_best_channel(model_id)
                if not best:
                    logger.debug(f"  [{current}] {model_id} 无可用渠道, 跳过")
                    skipped += 1
                    continue

                # 取 input_price (用于跨 member 排序)
                input_price = self._get_input_price(best, model_id)
                candidates.append((best, model_id, input_price))

            if not candidates:
                logger.warning(
                    f"组路由: 组 {current} 全部不可用 "
                    f"(members={len(members)}, skipped={skipped}), "
                    f"fallback → {group.fallback_group}"
                )
                current = group.fallback_group
                continue

            # 3) 跨 member 排序 (按组策略)
            strategy = group.strategy or "cheapest"
            if strategy == "priority":
                candidates.sort(key=lambda x: x[0].get("priority", 0), reverse=True)
            else:
                # cheapest (default): subscription/free 视为 0 元, 永远最便宜
                # billing_mode 在 ch dict 里 (Phase 2.3 加的, refresh_cache 会注入)
                # 但当前 cache.append 还没注入这 2 字段, 走 fallback: subscription=0, free=0, 其他按 price
                def sort_key(item):
                    ch, _model_id, price = item
                    billing = ch.get("billing_mode", "pay_per_token")
                    if billing in ("subscription", "free"):
                        return (0.0, -ch.get("priority", 0))
                    return (price if price is not None else 999.0, -ch.get("priority", 0))
                candidates.sort(key=sort_key)

            best_ch, best_model, _ = candidates[0]

            # 4) 注入调试字段 (Phase 2.5 响应头用)
            best_ch["_actual_model"] = best_model
            best_ch["_group_id"] = current
            registry = self._registry_cache.get(best_model)
            if registry:
                best_ch["_registry_tier"] = registry.tier
                best_ch["_registry_family"] = registry.family

            logger.info(
                f"组路由: {group_id} -> {best_model} via {best_ch['name']} "
                f"(strategy={strategy}, "
                f"members={len(members)}, candidates={len(candidates)}, "
                f"skipped={skipped})"
            )
            return best_ch

        logger.error(
            f"组路由: fallback chain 死循环或全部不可用: {group_id} "
            f"(visited={visited})"
        )
        return None

    def _get_input_price(self, channel: dict, model: str) -> float:
        """取 channel 当前时段对 model 的 input_price

        复用 _get_current_price 逻辑, 返回 float (None → 999 兜底)
        """
        price_info = self._get_current_price(channel, model, datetime.now().hour)
        if price_info and price_info.get("input_price") is not None:
            return float(price_info["input_price"])
        return 999.0  # 未知价格排到最后


# 全局单例
route_engine = RouteEngine()
