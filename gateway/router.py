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
import os
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings

logger = logging.getLogger(__name__)


# P0-4 (2026-08-14): router cache TTL + db file mtime 监听
# - 避免 "db 改了必须 systemctl restart" 的运维陷阱
# - TTL 默认 60s (跟 db write 频次匹配: pricing_sync 86400s, manual_import 即时)
# - mtime 检查比 TTL 更灵敏: db 文件被外部进程改 (sqlite3 CLI / 修复脚本 /
#   dashboard 操作) 立即生效, 不需要等 60s TTL
ROUTER_CACHE_TTL_SECONDS = int(os.getenv("AIMC_ROUTER_CACHE_TTL", "60"))


def _has_multimodal_content(messages):
    """P0-3 (2026-08-14): 判断 messages 是否含 multimodal content (image/audio/file)

    检测 OpenAI / Anthropic 两种 content 格式:
    - content: "string" (纯文本)
    - content: [{"type": "image_url"|"image"|"input_audio"|"audio"|"file", ...}, ...]

    Returns:
        True = 含 multimodal, False = 纯文本或 None
    """
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in (
                    "image_url", "image", "input_audio", "audio", "file",
                ):
                    return True
    return False


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
        # P0-4: db file mtime 监听 (cache 自动失效)
        self._last_db_mtime: float | None = None  # 上次加载时 db 文件 mtime
        # Phase 2.4: 模型组路由元信息
        self._registry_cache: dict = {}        # model_id -> ModelRegistry ORM
        self._groups_cache: dict = {}          # group_id -> ModelGroup ORM
        self._group_members_cache: dict = {}   # group_id -> [model_id]
        # V4.6.4 (C3 of fix-branch): member priority tie-break
        # group_id -> {model_id: priority}  (越小越优先, 同价时生效)
        self._member_priority_cache: dict = {}
        # V4.5.1: 能力评分 (model_cards) — sort_key tie-break
        self._cards_cache: dict = {}           # model_id -> [ModelCard ORM]
        # V4.6.1: least-used tie-break (同 channel 多账号轮询)
        # account_id → 最近路由选中的时间戳 (time.time())
        self._account_last_used: dict[int, float] = {}
        # P0-7 (2026-08-14): score weights 缓存, 从 settings_runtime 表读
        # 后续 dashboard 可调, 不需改代码
        self._score_weights: dict = {
            "W_PRICE": 0.40, "W_QUOTA": 0.30, "W_SUCCESS": 0.20,
            "W_RECENCY": 0.05, "W_PRIORITY": 0.05,
        }

    async def refresh_cache(self):
        """从数据库刷新渠道缓存到内存 (全量)

        Phase 4 v2 (I6): 业务场景普遍不需要全量刷新 (启动/admin 手动):
        - 价格变动 -> invalidate_price(channel_id)
        - channel 启用/禁用 -> invalidate_channel(channel_id)
        - model group 成员调整 -> invalidate_group(group_id)
        全量刷新会重新加载所有 channels + accounts + prices + quotas + groups,
        在 100+ channel 场景下 ~5s 阻塞窗口, 没必要.

        P0-4 (2026-08-14): 加载完记录 db file mtime, 给 _auto_refresh_if_stale 用
        """
        from db.engine import async_session_factory

        async with async_session_factory() as session:
            await self._load_channels_cache(session)
            await self._load_groups_cache(session)
            await self._load_cards_cache(session)
            # P0-7: 从 settings_runtime 表读 score weights (dashboard 可改)
            await self._load_score_weights(session)

        self._last_refresh = datetime.now()
        # P0-4: 记录 db 文件 mtime (用于后续 stale 检测)
        try:
            db_path = Path(settings.db_path)
            if db_path.exists():
                self._last_db_mtime = db_path.stat().st_mtime
        except Exception as e:
            logger.debug(f"[P0-4] 记录 db mtime 失败 (非致命): {e}")
        logger.info(
            f"路由缓存已全量刷新: {len(self._channels_cache)} 个可用渠道账号"
        )

    async def _auto_refresh_if_stale(self) -> bool:
        """P0-4 (2026-08-14): cache stale 时自动刷新, 避免 "db 改了必须重启 service"

        触发条件 (满足任一):
        1. TTL 超时: 距离上次 refresh > AIMC_ROUTER_CACHE_TTL (默认 60s)
        2. db mtime 变了: 外部进程改了 db 文件 (sqlite3 CLI / 修复脚本 / dashboard 操作)

        调用位置: select_channel / get_candidates / get_models_with_metadata 等
        入口方法前, 让 cache 总是新鲜。

        Returns:
            True = 本次触发了 refresh, False = cache 仍然新鲜
        """
        # 还没初始化过 (首次调用), 强制 refresh
        if self._last_refresh is None:
            await self.refresh_cache()
            return True

        # 检查 TTL
        ttl_age = (datetime.now() - self._last_refresh).total_seconds()
        if ttl_age > ROUTER_CACHE_TTL_SECONDS:
            logger.debug(
                f"[P0-4] cache TTL 超时 ({ttl_age:.1f}s > {ROUTER_CACHE_TTL_SECONDS}s), "
                f"自动 refresh"
            )
            await self.refresh_cache()
            return True

        # 检查 db file mtime
        try:
            db_path = Path(settings.db_path)
            if not db_path.exists():
                return False
            current_mtime = db_path.stat().st_mtime
            if self._last_db_mtime is None or current_mtime > self._last_db_mtime:
                logger.debug(
                    f"[P0-4] db mtime 变化 ({self._last_db_mtime} -> {current_mtime}), "
                    f"自动 refresh"
                )
                await self.refresh_cache()
                return True
        except Exception as e:
            logger.debug(f"[P0-4] mtime 检查失败 (非致命): {e}")

        return False

    async def invalidate_channel(self, channel_id: int) -> None:
        """Phase 4 v2 (I6): 精准刷新单个 channel

        场景:
        - channel 启用/禁用 (dashboard 切换 status)
        - channel 账号增删 (admin 后台调)
        - channel 配额调整 (频限重设)

        不动 groups cache.
        """
        from db.engine import async_session_factory
        from db.models import Channel

        async with async_session_factory() as session:
            ch = await session.get(Channel, channel_id)
            if not ch:
                # channel 不存在 (e.g. 被管理员删了), 从 cache 里也移除
                self._channels_cache = [
                    c for c in self._channels_cache if c["id"] != channel_id
                ]
                logger.info(
                    f"invalidate_channel: channel {channel_id} 不存在, 从 cache 移除"
                )
                return

            # 重新加载该 channel 的 cache 条目
            new_entries = await self._build_channel_entries(session, ch)

            # 替换: 先移除该 channel 的旧条目, 再插入新条目
            self._channels_cache = [
                c for c in self._channels_cache if c["id"] != channel_id
            ] + new_entries

            if not new_entries:
                logger.info(
                    f"invalidate_channel: channel {channel_id} "
                    f"({ch.name}) 重新加载后 0 个可用账号"
                )
            else:
                logger.info(
                    f"invalidate_channel: channel {channel_id} "
                    f"({ch.name}) 重新加载 {len(new_entries)} 个账号"
                )

    async def invalidate_price(self, channel_id: int) -> None:
        """Phase 4 v2 (I6): 精准刷新单个 channel 的 prices

        场景:
        - pricing_sync.sync_price_change 调 -> 只变 price, 不动 accounts/quotas
        - heal/refresh 脚本手动调

        只重查 PriceSnapshot, 替换 cache 中该 channel 的 prices 字段.
        """
        from db.engine import async_session_factory
        from db.models import Channel

        async with async_session_factory() as session:
            ch = await session.get(Channel, channel_id)
            if not ch:
                logger.warning(
                    f"invalidate_price: channel {channel_id} 不存在, 跳过"
                )
                return

            new_prices = [
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

            # 替换该 channel 的 prices 字段 (所有账号公用一份 prices)
            replaced = 0
            for entry in self._channels_cache:
                if entry["id"] == channel_id:
                    entry["prices"] = new_prices
                    replaced += 1

            logger.info(
                f"invalidate_price: channel {channel_id} ({ch.name}) "
                f"重载 {len(new_prices)} 条 prices, 覆盖 {replaced} 个 cache 条目"
            )

    async def invalidate_group(self, group_id: str) -> None:
        """Phase 4 v2 (I6): 精准刷新单个 model group

        场景:
        - dashboard 调整组成员 (member add/remove)
        - admin 调整 group fallback_group/tier/capabilities

        重查 ModelGroup + ModelGroupMember, 替换 _groups_cache 和 _group_members_cache.
        """
        from db.engine import async_session_factory
        from sqlalchemy import select
        from db.models import (
            ModelGroup, ModelGroupMember,
        )

        async with async_session_factory() as session:
            grp = await session.execute(
                select(ModelGroup).where(ModelGroup.group_id == group_id)
            )
            grp = grp.scalar_one_or_none()
            if not grp:
                self._groups_cache.pop(group_id, None)
                self._group_members_cache.pop(group_id, None)
                logger.info(
                    f"invalidate_group: group {group_id} 不存在, 从 cache 移除"
                )
                return

            self._groups_cache[group_id] = grp

            # P1-2: 仅拉 is_active=1 的成员. inactive 行保留在表里作审计,
            # 路由不能命中 (避免 cache 里出现 deprecated model).
            mem_result = await session.execute(
                select(ModelGroupMember).where(
                    ModelGroupMember.group_id == group_id,
                    ModelGroupMember.is_active == 1,
                )
            )
            self._group_members_cache[group_id] = [
                m.model_id for m in mem_result.scalars().all()
            ]

            logger.info(
                f"invalidate_group: group {group_id} 重新加载, "
                f"{len(self._group_members_cache[group_id])} 个 member"
            )

    async def _load_channels_cache(self, session) -> None:
        """Phase 4 v2 (I6): 从给定 session 加载 channels cache (内部辅助)

        复用逻辑从原 refresh_cache() 抽出, 给 refresh_cache() 和
        invalidate_channel() 共享.
        """
        from db.models import Channel

        # 查询所有活跃渠道及其关联数据
        stmt = select(Channel).where(Channel.status == "active")
        result = await session.execute(stmt)
        channels = result.scalars().all()

        cache = []
        for ch in channels:
            cache.extend(await self._build_channel_entries(session, ch))

        self._channels_cache = cache

    async def _build_channel_entries(self, session, ch) -> list[dict]:
        """Phase 4 v2 (I6): 构造单个 channel 的 cache 条目列表

        一个 channel 可能有多 active account, 每个 account 展开成一条 cache 条目.
        如果 channel 没有 active account, 返回空列表.
        """
        active_accounts = [
            acc for acc in ch.accounts
            if acc.status == "active"
        ]
        if not active_accounts:
            return []

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

        quotas = [
            {
                "quota_type": q.quota_type,
                "limit_value": q.limit_value,
                "account_id": q.account_id,
            }
            for q in ch.quotas
        ]

        entries = []
        for acc in active_accounts:
            plaintext_key = self._resolve_account_key(acc)
            if plaintext_key is None:
                logger.warning(
                    f"账号 {ch.name}/{acc.account_name} 既无 ciphertext 也无明文 key, 跳过"
                )
                continue

            entries.append({
                "id": ch.id,
                "name": ch.name,
                "provider": ch.provider,
                "base_url": ch.base_url,
                "api_base": ch.api_base,  # P1-12 (2026-08-14): dashboard 可改
                "api_style": ch.api_style or "openai",  # P1-12: 协议风格
                "supports_prompt_cache": ch.supports_prompt_cache,  # P1-12: cache 支持
                "priority": ch.priority,
                "provider_type": ch.provider_type,
                "billing_mode": ch.billing_mode,
                "account_id": acc.id,
                "account_name": acc.account_name,
                "api_key": plaintext_key,
                "quota_total": acc.quota_total,
                "quota_used": acc.quota_used,
                "quota_remaining": acc.quota_remaining,
                "prices": prices,
                "quotas": [
                    q for q in quotas
                    if q["account_id"] is None or q["account_id"] == acc.id
                ],
            })
        return entries

    async def _load_groups_cache(self, session) -> None:
        """Phase 4 v2 (I6): 从给定 session 加载 groups cache (内部辅助)"""
        from db.models import (
            ModelRegistry, ModelGroup, ModelGroupMember,
        )

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
        # P1-2: 仅拉 is_active=1 的成员 (软删除的行不进路由 cache).
        mem_result = await session.execute(
            select(ModelGroupMember).where(ModelGroupMember.is_active == 1)
        )
        self._group_members_cache = {}
        for m in mem_result.scalars().all():
            self._group_members_cache.setdefault(m.group_id, []).append(m.model_id)
        logger.info(
            f"路由缓存: {sum(len(v) for v in self._group_members_cache.values())} "
            f"个 group members 跨 {len(self._group_members_cache)} 个组"
        )

    # ===========================================================
    # V4.5.1: 能力评分路由 (model_cards 缓存 + score_bonus + sort_key 接入)
    # ===========================================================

    async def _load_cards_cache(self, session) -> None:
        """V4.5.1: 加载 model_cards 评分 (model_id → [ModelCard]).

        反向索引: 一个 model 可对应多个 benchmark source (OpenCompass + LMSYS + ...).
        score_bonus() 取 max(score × recency) — 历史最强分 (考虑时效).
        """
        from db.models import ModelCard
        result = await session.execute(select(ModelCard))
        cards = result.scalars().all()
        self._cards_cache = {}
        for card in cards:
            self._cards_cache.setdefault(card.model, []).append(card)
        logger.info(
            f"路由缓存: {sum(len(v) for v in self._cards_cache.values())} 个 model_card "
            f"跨 {len(self._cards_cache)} 个 model"
        )

    async def _load_score_weights(self, session) -> None:
        """P0-7 (2026-08-14): 从 settings_runtime 表读 score weights

        设计:
        - key = "router_score_weights"
        - value = JSON string: {"W_PRICE": 0.4, "W_QUOTA": 0.3, "W_SUCCESS": 0.2, "W_RECENCY": 0.05, "W_PRIORITY": 0.05}
        - 缺失或解析失败 → 用 __init__ 的默认值 (兜底)
        - dashboard PUT /api/settings/router-weights 写入, 下次 refresh_cache (60s TTL) 生效
        - 不支持部分更新: 改一两个 weight 必须传全部 5 个
        """
        import json as _json
        from db.models import SettingsRuntime
        try:
            row = (await session.execute(
                select(SettingsRuntime).where(SettingsRuntime.key == "router_score_weights")
            )).scalars().first()
            if row is None or row.value is None:
                logger.debug("router_score_weights 未设置, 用 __init__ 默认值")
                return
            parsed = _json.loads(row.value)
            # 校验: 必须含 5 个 W_* 字段 + 数值 + sum ~ 1.0
            required = {"W_PRICE", "W_QUOTA", "W_SUCCESS", "W_RECENCY", "W_PRIORITY"}
            if not required.issubset(parsed.keys()):
                logger.warning(f"router_score_weights 缺字段: {set(required) - set(parsed.keys())}")
                return
            for k, v in parsed.items():
                if not isinstance(v, (int, float)) or v < 0 or v > 1:
                    logger.warning(f"router_score_weights.{k}={v} 不在 [0,1] 范围")
                    return
            self._score_weights = parsed
            logger.info(f"P0-7: 加载 score weights: {parsed}")
        except Exception as e:
            logger.warning(f"P0-7: 读 score weights 失败 (用默认值): {e}")

    def score_bonus(self, model: str) -> float:
        """V4.5.1: model 的能力评分加成 [0.0, 1.0].

        计算: max(card.score × recency_weight(card.updated_at)) over cards.
        - 无 card / 无 updated_at → 0.0
        - score 100% + recency 1.0 → 1.0 (满分)
        - 半年内的 OpenCompass 引用 → ~1.0
        - 一年前的 → ~0.5
        - 二年前 / 无时间戳 → 0.0

        用法: sort_key 加 -score_bonus 项, 价格平手时高分排前. 权重 ≤ 0.2 (Phase 2 design).
        """
        from intelligence.recency import recency_weight

        cards = self._cards_cache.get(model, [])
        if not cards:
            return 0.0
        best = 0.0
        for card in cards:
            if card.score is None:
                continue
            recency = recency_weight(card.updated_at)
            # score 是 0~100, 归一化到 0~1
            weighted = (card.score / 100.0) * recency
            if weighted > best:
                best = weighted
        return best

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

    def select_best_channel(self, model: str, messages: Optional[list] = None) -> Optional[dict]:
        """核心决策：为指定模型选择最优渠道

        Args:
            model: 请求的模型名称
            messages: P0-3 (2026-08-14) — 请求的 messages 列表, 用于判断是否含 multimodal content
                    纯文本请求时, 跳过 multimodal channel (它们拒纯文本, 上游 400)

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
        from gateway.cooldown import cooldown_manager
        # V4.6.1: 同时过滤 cooldown (单次失败就冷却, 避免反复撞死 key)
        candidates = [
            (ch, price) for ch, price in candidates
            if not cooldown_manager.is_in_cooldown(ch["account_id"])
            and not rate_limiter.is_limited(ch["account_id"], ch["quotas"])
        ]

        if not candidates:
            # 无候选时回 fallback; 再用 _is_channel_usable 检查一次 (含 cooldown)
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

        # 阶段3 (2026-08-14): score-based 多维排序
        # 替代原 round-robin + 多 key 排序, 改为单 score (DESC 排序)
        # 公式: score = w_price * price_score
        #             + w_quota * quota_ratio
        #             + w_success * success_rate
        #             + w_recency * recency_score
        #             + w_priority * (priority / max_priority)
        #             - cooldown_penalty (cooldown 中账号 score = 0)
        #
        # weights 起步: 0.4 / 0.3 / 0.2 / 0.05 / 0.05 (价格为主, 配额为辅)
        # 后续可通过 settings_runtime 表或 config 调
        import time as _t
        # P0-7 (2026-08-14): 从 settings_runtime 表读 (dashboard 可改)
        # 缺失时 fallback __init__ 默认值
        W_PRICE = self._score_weights["W_PRICE"]
        W_QUOTA = self._score_weights["W_QUOTA"]
        W_SUCCESS = self._score_weights["W_SUCCESS"]
        W_RECENCY = self._score_weights["W_RECENCY"]
        W_PRIORITY = self._score_weights["W_PRIORITY"]

        prices = [p.get("input_price") or 0 for _, p in candidates]
        max_price = max(prices) if prices else 1
        min_price = min(prices) if prices else 0
        price_range = (max_price - min_price) if (max_price - min_price) > 0 else 1

        def score_account(ch_price_pair):
            ch, price = ch_price_pair
            # 1. 价格分数 (越便宜越高, 归一化 0-1)
            p = price.get("input_price") or 0
            price_score = 1.0 - (p - min_price) / price_range if price_range > 0 else 0.5

            # 2. 配额分数 (剩余越多越高)
            q_remaining = ch["quota_remaining"] if ch["quota_remaining"] is not None else None
            q_total = ch["quota_total"] if ch["quota_total"] else None
            if q_remaining is not None and q_total and q_total > 0:
                quota_ratio = min(q_remaining / q_total, 1.0)
            else:
                quota_ratio = 0.5  # 无数据时给中间分

            # 3. 成功率 (cooldown 中 = 0, 否则从 rate_limiter 历史推)
            # 简化: 用 _account_last_used 频率代替 (越久没被路由 = 越稳)
            last_used = self._account_last_used.get(ch["account_id"], 0.0)
            if last_used > 0:
                # 最近 1h 内用过: success_rate = 0.5 (近期成功未知)
                # 1h 外: success_rate = 1.0 (历史表现稳)
                import time
                age = time.time() - last_used
                success_rate = 0.5 if age < 3600 else 1.0
            else:
                success_rate = 1.0  # 从未用过 = 假设 OK

            # 4. Recency (round-robin tie-break, 越小越久没被路由越优先)
            recency_score = last_used  # 0 = 从未用过, 越大 = 越近用过

            # 5. Priority (越大越优先)
            priority_norm = (ch["priority"] or 0) / 100.0  # 假设 priority 0-100

            # 6. 阶段3.2: cooldown 惩罚 — 已经在 _is_channel_usable 过滤
            #    但加这里做双保险, 避免任何 bypass 路径

            score = (
                W_PRICE * price_score
                + W_QUOTA * quota_ratio
                + W_SUCCESS * success_rate
                + W_RECENCY * (1.0 / (1.0 + recency_score / 3600))  # 反向归一化
                + W_PRIORITY * priority_norm
            )
            return score

        candidates.sort(key=score_account, reverse=True)  # 分数高优先

        best_channel, best_price = candidates[0]
        # V4.6.1: 记录 last_used (路由层只 record, 不 mark_success — 成功标记由 proxy 层处理)
        self._account_last_used[best_channel["account_id"]] = time.time()
        logger.debug(
            f"路由决策: model={model} -> {best_channel['name']} "
            f"(price={best_price.get('input_price')}, priority={best_channel['priority']})"
        )

        # Phase 3.E: 精确路由响应头增强 — 注入 _registry_* 字段
        # 跟 _select_by_group 保持一致, 让 _build_aimc_headers 不用关心路由类型
        best_channel["_actual_model"] = model
        reg = self._registry_cache.get(model)
        if reg:
            best_channel["_registry_tier"] = reg.tier
            best_channel["_registry_family"] = reg.family
            best_channel["_registry_capabilities"] = reg.capabilities

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
        """Phase 2E: 检查 channel 是否可用 (限频 + 配额), 不检查 model 支持

        V4.6.1 加 cooldown 检查 — 失败的账号 (mark_failure 触发) 进入冷却期,
        路由应跳过, 等冷却过期再恢复. 跟 limiter 一致: 内存管理, 不持久化.
        """
        from gateway.cooldown import cooldown_manager
        from gateway.limiter import rate_limiter
        if cooldown_manager.is_in_cooldown(ch["account_id"]):
            return False
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

    def get_available_groups(self) -> list[dict]:
        """Phase 2.8: 获取所有启用的 model group 列表 (含 members)

        返回格式:
        [
            {
                "id": "tier:balanced",
                "type": "model_group",
                "display_name": "均衡性价比组",
                "tier": "T2",
                "strategy": "cheapest",
                "fallback_group": "tier:light",
                "members": ["deepseek-v2.5", "qwen-plus", ...],
                "member_count": 7,
            },
            ...
        ]
        按 sort_order 升序 (dashboard 展示顺序)
        """
        groups = []
        # 按 sort_order 升序
        sorted_groups = sorted(
            self._groups_cache.values(),
            key=lambda g: (g.sort_order or 0, g.group_id),
        )
        for g in sorted_groups:
            members = self._group_members_cache.get(g.group_id, [])
            groups.append({
                "id": g.group_id,
                "type": "model_group",
                "display_name": g.display_name or g.group_id,
                "tier": g.tier,
                "strategy": g.strategy or "cheapest",
                "fallback_group": g.fallback_group,
                "members": members,
                "member_count": len(members),
            })
        return groups

    def get_models_with_metadata(self) -> list[dict]:
        """Phase 3.B: 获取带 aimc_metadata 的 model 列表 (Agent 协同场景)

        返回每个 model 的:
        - OpenAI 标准字段 (id/object/owned_by) — 向后兼容
        - aimc_metadata 字段 (AIMC 扩展)：
          - family / tier / capabilities / context_length / modality
          - is_open_source / release_date / status
          - providers: 在哪些 channel 中有价格 (siliconflow / deepseek_official / ...)
          - groups: 属于哪些 model group (tier:balanced / scene:code / ...)

        用途: hermes-agent-cn 启动时拉一次, 可以:
        1. 验证 profile 合法 (group 名存在)
        2. 看到 model 元信息, 不需额外查 model registry
        3. 看到 "这个 model 在哪些 channel 可用" — 跨模型比价可见性
        """
        import json as _json

        # 1) 反向索引: model -> [provider, ...]
        model_providers: dict[str, set[str]] = {}
        for ch in self._channels_cache:
            provider = ch.get("provider", "")
            for p in ch.get("prices", []):
                model = p.get("model", "")
                if model and provider:
                    model_providers.setdefault(model, set()).add(provider)

        # 2) 反向索引: model -> [group_id, ...]
        model_groups: dict[str, set[str]] = {}
        for gid, members in self._group_members_cache.items():
            for m in members:
                model_groups.setdefault(m, set()).add(gid)

        # 3) 拼装结果
        results = []
        # 优先遍历 _registry_cache (有元信息的 model)
        seen = set()
        for model_id, reg in sorted(self._registry_cache.items()):
            seen.add(model_id)
            caps = []
            if reg.capabilities:
                try:
                    caps = _json.loads(reg.capabilities)
                except (ValueError, TypeError):
                    caps = []

            results.append({
                "id": model_id,
                "object": "model",
                "owned_by": reg.family or "aimc",
                "aimc_metadata": {
                    "family": reg.family,
                    "tier": reg.tier,
                    "capabilities": caps,
                    "context_length": reg.context_length,
                    "modality": reg.modality or "text",
                    "is_open_source": bool(reg.is_open_source),
                    "release_date": reg.release_date,
                    "status": reg.status or "active",
                    "providers": sorted(model_providers.get(model_id, set())),
                    "groups": sorted(model_groups.get(model_id, set())),
                },
            })

        # 4) 补充: 仅在 channels 有价格但 registry 没有的 model
        for model_id in sorted(model_providers.keys() - seen):
            results.append({
                "id": model_id,
                "object": "model",
                "owned_by": "aimc",
                "aimc_metadata": {
                    # registry 缺失, 只有 price 线索
                    "family": None,
                    "tier": None,
                    "capabilities": [],
                    "context_length": None,
                    "modality": "text",
                    "is_open_source": None,
                    "release_date": None,
                    "status": "active",
                    "providers": sorted(model_providers.get(model_id, set())),
                    "groups": sorted(model_groups.get(model_id, set())),
                },
            })
        return results

    def get_groups_with_metadata(self) -> list[dict]:
        """Phase 3.B: 获取带 aimc_metadata 的 group 列表 (Agent 协同场景)

        在 get_available_groups() 基础上加 aimc_metadata:
        - fallback_chain: 降级链 (递归走 fallback_group, 最多 N 跳防环)
        - member_tiers: 每个 member 的 tier 列表
        - member_count / enabled / min_context / require_vision
        """
        sorted_groups = sorted(
            self._groups_cache.values(),
            key=lambda g: (g.sort_order or 0, g.group_id),
        )
        results = []
        for g in sorted_groups:
            members = self._group_members_cache.get(g.group_id, [])
            member_tiers = []
            for m in members:
                reg = self._registry_cache.get(m)
                if reg and reg.tier:
                    member_tiers.append(reg.tier)
            caps = []
            if g.capabilities:
                try:
                    caps = json.loads(g.capabilities)
                except (ValueError, TypeError):
                    caps = []
            # fallback chain 展开 (最多 5 跳防环)
            chain = [g.group_id]
            cur = g.fallback_group
            visited = {g.group_id}
            for _ in range(5):
                if not cur or cur in visited:
                    break
                chain.append(cur)
                visited.add(cur)
                next_grp = self._groups_cache.get(cur)
                if not next_grp:
                    break
                cur = next_grp.fallback_group
            results.append({
                "id": g.group_id,
                "type": "model_group",
                "display_name": g.display_name or g.group_id,
                "tier": g.tier,
                "capabilities": caps,
                "strategy": g.strategy or "cheapest",
                "fallback_group": g.fallback_group,
                "enabled": bool(g.enabled),
                "members": members,
                "member_count": len(members),
                "aimc_metadata": {
                    "fallback_chain": chain,
                    "member_tiers": member_tiers,
                    "min_context": g.min_context or 0,
                    "require_vision": bool(g.require_vision),
                    "enabled": bool(g.enabled),
                },
            })
        return results

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

    def select_channel(self, model: str, prefs: Optional[dict] = None,
                      messages: Optional[list] = None) -> Optional[dict]:
        """Phase 2.4 入口: 按 model 字符串分发

        - "tier:xxx" / "scene:xxx" → _select_by_group (组路由 + fallback chain)
        - 其他 → select_best_channel (精确模型路由, 现有行为不变)

        Args:
            model: 请求的 model 字符串
            prefs: Phase 2.6 Agent 偏好, 3 个字段:
                - prefer_family: 同价时优先的 model family (e.g. "deepseek")
                - exclude_providers: 排除的 channel provider 列表 (e.g. ["local"])
                - max_input_price: 价格上限 (元/百万 tokens, 超过的排除)
              只对组路由生效, 精确模型路由 Phase 2 不实现偏好

        返回的 channel 字典会带额外字段 (组路由时注入):
        - _actual_model: 实际命中的精确 model_id
        - _group_id: 命中的 model group (e.g. "tier:balanced")
        - _registry_tier: 命中 model 的能力档

        P0-4 (2026-08-14): stale check 由 caller (gateway/routes.py) 显式 await
        _auto_refresh_if_stale, 避免在 sync 入口处 fire-and-forget 引入 race condition。
        """
        if model.startswith("tier:") or model.startswith("scene:"):
            return self._select_by_group(model, prefs=prefs, messages=messages)
        # 精确模型路由 Phase 2.6 不实现偏好 (跟 design §4.5 一致)
        return self.select_best_channel(model, messages=messages)

    def _select_by_group(
        self, group_id: str, prefs: Optional[dict] = None,
        messages: Optional[list] = None,
    ) -> Optional[dict]:
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

        偏好 (Phase 2.6):
        - exclude_providers: 排除 channel provider (candidates 阶段过滤)
        - max_input_price: 价格上限 (candidates 阶段过滤)
        - prefer_family: 同价时 tie-break (sort_key 阶段)
        """
        prefs = prefs or {}
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

                # P0-3 (2026-08-14): 纯文本请求跳过 multimodal candidate
                # 背景: multimodal model (modality=multimodal) 拒纯文本 → 上游 400
                # 修复: messages 不含 image/audio/file 时, 跳过 multimodal member
                # 例外: tier:vision 组用户显式选 multimodal (但仍可走 text model fallback)
                has_mm = _has_multimodal_content(messages)
                if not has_mm and registry.modality == "multimodal":
                    logger.debug(
                        f"  [{current}] {model_id} modality=multimodal 但请求纯文本, 跳过"
                    )
                    skipped += 1
                    continue

                # 跑精确模型路由 (复用 select_best_channel)
                best = self.select_best_channel(model_id, messages=messages)
                if not best:
                    logger.debug(f"  [{current}] {model_id} 无可用渠道, 跳过")
                    skipped += 1
                    continue

                # 取 input_price (用于跨 member 排序)
                input_price = self._get_input_price(best, model_id)

                # Phase 2.6: Agent 偏好过滤
                # 1) exclude_providers: 排除指定 provider 的 channel
                exclude_providers = prefs.get("exclude_providers") or []
                if exclude_providers and best["provider"] in exclude_providers:
                    logger.debug(
                        f"  [{current}] {model_id} via {best['name']}: "
                        f"provider {best['provider']} 在 exclude_providers, 跳过"
                    )
                    skipped += 1
                    continue
                # 2) max_input_price: 价格上限
                max_input_price = prefs.get("max_input_price")
                if max_input_price is not None and input_price > max_input_price:
                    logger.debug(
                        f"  [{current}] {model_id} via {best['name']}: "
                        f"input_price {input_price} > max_input_price {max_input_price}, 跳过"
                    )
                    skipped += 1
                    continue
                # 3) prefer_family: tie-break (0 = 优先, 1 = 兜底)
                # 预计算, sort 时不查 cache
                family_bonus = 1  # 默认 1 (非 prefer)
                if prefs.get("prefer_family"):
                    if registry.family == prefs["prefer_family"]:
                        family_bonus = 0

                candidates.append((best, model_id, input_price, family_bonus))

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
                # priority 策略: channel.priority DESC (family_bonus 忽略)
                candidates.sort(key=lambda x: x[0].get("priority", 0), reverse=True)
            else:
                # cheapest (default): subscription/free 视为 0 元, 永远最便宜
                # 排序 key: (price, family_bonus, -priority) — Phase 2.6 加 family_bonus tie-break
                def sort_key(item):
                    ch, _model_id, price, family_bonus = item
                    billing = ch.get("billing_mode", "pay_per_token")
                    if billing in ("subscription", "free"):
                        return (0.0, family_bonus, -ch.get("priority", 0))
                    return (
                        price if price is not None else 999.0,
                        family_bonus,
                        -ch.get("priority", 0),
                    )
                candidates.sort(key=sort_key)

            best_ch, best_model, _, _ = candidates[0]

            # 4) 注入调试字段 (Phase 2.5 响应头用)
            best_ch["_actual_model"] = best_model
            best_ch["_group_id"] = current
            registry = self._registry_cache.get(best_model)
            if registry:
                best_ch["_registry_tier"] = registry.tier
                best_ch["_registry_family"] = registry.family
                # Phase 3.E: 加 _registry_capabilities (JSON 字符串, _build_aimc_headers 会解析)
                best_ch["_registry_capabilities"] = registry.capabilities

            logger.info(
                f"组路由: {group_id} -> {best_model} via {best_ch['name']} "
                f"(strategy={strategy}, "
                f"members={len(members)}, candidates={len(candidates)}, "
                f"skipped={skipped}, "
                f"prefs={prefs if prefs else '{}'})"
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
