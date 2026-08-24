"""自愈循环模块

Phase 2B-2 改造后的流程：
1. AI 解析（异常向上抛）
2. 成功 + 置信度 ≥ 阈值 → 入库
3. 失败 OR 置信度 < 阈值 → CSS 降级（接 ParseRule 表）
4. CSS 降级成功 → 入库（标 parse_rule.confidence）
5. 全部失败 → 告警人工

借鉴 crawl4ai 多 extraction strategy fallback。

改进历史：
- 2026-07-24: Phase 2D-1 加 sanity_check + flagged 标黄
  - 解决 LLM 编造错误优惠/折扣的风险
  - 不阻止入库，标黄让 dashboard 醒目标记
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from config import settings
from schemas.intel import ParseResult

logger = logging.getLogger(__name__)

# V4.8.5 P0-4: 折扣率正则 — 匹配 "5折" "半价" "8.5折" "打5折" "5折优惠" 等
_DISCOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*折|半价")


async def _promotion_to_price_rows(result: ParseResult, provider: str) -> int:
    """V4.8.5 P0-4: 高置信度优惠 → 限时优惠价格行（打通核心卖点）。

    从 discount_desc/content 解析折扣率, 乘以当前 normal 价生成 promotion 价行.
    price_type="promotion", is_promotion=1, effective_from/to 来自 result 日期.
    路由侧 _is_price_effective 自动按日期生效/失效, 零改动消费.

    Returns: 成功生成/刷新的 promotion 价行数
    """
    from intelligence.pricing_sync import sync_price_change, apply_price_change_to_route_cache
    from db.engine import async_session_factory
    from db.models import PriceSnapshot

    # 1. 解析折扣率
    text = f"{result.discount_desc or ''} {result.content or ''}"
    m = _DISCOUNT_RE.search(text)
    if not m:
        logger.debug(f"优惠转价格行跳过(无法解析折扣率): {provider} {result.title}")
        return 0
    if m.group(0) == "半价":
        discount = 0.5
    else:
        raw = float(m.group(1))
        # "5折"→0.5, "0.5折"→0.05; raw>=1 视为 X折 (raw/10), raw<1 视为直接比例
        discount = raw / 10.0 if raw >= 1 else raw
    if discount <= 0 or discount >= 1:
        logger.debug(f"优惠转价格行跳过(折扣率异常 {discount}): {provider}")
        return 0

    # 2. 查 provider 的 channel + 当前 normal 价格行
    async with async_session_factory() as session:
        from db import crud
        channel = await crud.get_channel_by_provider(session, provider)
        if not channel:
            return 0
        stmt = select(PriceSnapshot).where(
            PriceSnapshot.channel_id == channel.id,
            PriceSnapshot.price_type == "normal",
        )
        rows = (await session.execute(stmt)).scalars().all()

    if not rows:
        logger.debug(f"优惠转价格行跳过(无 normal 价基础): {provider}")
        return 0

    # 3. 逐 model 生成 promotion 价行
    effective_from = result.start_date
    effective_to = result.end_date
    synced = 0
    for row in rows:
        if row.input_price is None and row.output_price is None:
            continue
        promo_input = row.input_price * discount if row.input_price is not None else None
        promo_output = row.output_price * discount if row.output_price is not None else None
        change = await sync_price_change(
            provider, row.model, promo_input, promo_output,
            source="crawler", price_type="promotion",
            is_promotion=1,
            effective_from=effective_from, effective_to=effective_to,
        )
        if change and change.changed:
            await apply_price_change_to_route_cache(change)
            synced += 1

    if synced:
        logger.info(
            f"优惠转价格行: {provider} {result.title} 折扣={discount} "
            f"→ {synced} 个 model 生成 promotion 价行 "
            f"(effective {effective_from}~{effective_to})"
        )
    return synced

# Sanity check 阈值 (Phase 2D-1, 2026-07-24)
# 这些是经验值，不追求精确 — 宁可误标也不漏标（人工过滤比 AI 编造好）
_SANITY_MIN_CONFIDENCE = 0.6      # < 0.6 标黄（AI 自己都不确信）
_SANITY_MIN_TITLE_LEN = 5          # 标题太短大概率是解析失败
_SANITY_MAX_TITLE_LEN = 200        # 标题太长可能是 HTML 污染
_SANITY_MIN_DISCOUNT = 1.0         # "X 折" < 1 折 = 超过 99% 折扣（异常）
_SANITY_MAX_DISCOUNT = 9.5         # "X 折" > 9.5 折 = 折扣 < 5%（几乎没意义）

# Phase 3.1 (2026-07-29): 按 source_type 调整 sanity check 阈值
# model_list / benchmark / price_aggregator 不是促销信息
# AI 解析置信度天然较低（榜单变频繁/列表变频繁）
# 阈值放低到 0.4 避免大量误报标黄
_SOURCE_TYPE_SANITY_OVERRIDES = {
    "model_list": 0.4,
    "price_aggregator": 0.4,
    "benchmark": 0.4,
}


def _sanity_check(result: ParseResult, source_type: str = "web_page") -> tuple[bool, str | None]:
    """简单 sanity check 解析结果

    Phase 2D-1: 防止 LLM 编造看似合理的错误优惠入库
    Phase 3.1: 按 source_type 调整阈值 (model_list / benchmark / price_aggregator 放低)

    Args:
        result: AI 解析结果
        source_type: 采集源类型 (Phase 3.1 用于调阈值)

    Returns:
        (flagged, reason)
        - (False, None) 正常
        - (True, "low_confidence(0.45)") 异常（不阻止入库，标黄让人看）

    规则（按优先级）：
    1. confidence < 阈值 → low_confidence (Phase 3.1: model_list/benchmark/price_aggregator 用 0.4)
    2. title 太短/太长 → title_length
    3. "X 折" < 1 或 > 9.5 → abnormal_discount
    4. "免费/白嫖" 但 confidence < 0.8 → free_but_uncertain
    """
    # Phase 3.1: 按 source_type 选阈值
    min_confidence = _SOURCE_TYPE_SANITY_OVERRIDES.get(source_type, _SANITY_MIN_CONFIDENCE)
    if result.confidence < min_confidence:
        return True, f"low_confidence({result.confidence:.2f}, type={source_type})"

    title = result.title or ""
    if len(title) < _SANITY_MIN_TITLE_LEN:
        return True, f"title_too_short(len={len(title)})"
    if len(title) > _SANITY_MAX_TITLE_LEN:
        return True, f"title_too_long(len={len(title)})"

    desc = result.discount_desc or ""
    if desc:
        m = re.search(r"(\d+(?:\.\d+)?)\s*折", desc)
        if m:
            try:
                discount = float(m.group(1))
                if discount < _SANITY_MIN_DISCOUNT or discount > _SANITY_MAX_DISCOUNT:
                    return True, f"abnormal_discount({discount}折)"
            except ValueError:
                pass

        if result.confidence < 0.8 and ("免费" in desc or "白嫖" in desc):
            return True, f"free_but_uncertain({result.confidence:.2f})"

    return False, None


async def handle_parse_result(content: str, source):
    """自愈循环主入口

    Args:
        content: 页面 HTML（已通过 differ 检测有变化）
        source: 采集源对象

    流程图：
    ┌─────────────────┐
    │   AI 解析        │ try/except
    │  parse_content  │ ←──┐
    └────────┬────────┘    │
             │             │
        ┌────┴────┐        │
        │ 成功?   │        │ 异常
        ▼         ▼        │
    ┌──────┐  ┌──────┐     │
    │入库  │  │CSS降级│ ←──┘
    └──┬───┘  └──┬───┘
       │         │
       │    ┌────┴────┐
       │    │ 成功?   │
       │    ▼         ▼
       │ ┌──────┐  ┌──────┐
       │ │入库  │  │告警  │
       │ └──────┘  └──────┘
       │
       └─→ 全部结束
    """
    # === 1. AI 解析 ===
    ai_result: ParseResult | None = None
    try:
        from intelligence.parser import parse_content
        ai_result = await parse_content(
            content, source.provider, source.url, source.source_type
        )
        logger.debug(
            f"AI 解析完成: provider={source.provider}, type={source.source_type}, "
            f"is_promotion={ai_result.is_promotion}, "
            f"confidence={ai_result.confidence:.2f}"
        )
    except Exception as e:
        logger.warning(f"AI 解析异常: {source.provider} - {e}")
        ai_result = None

    # === 1.5 Phase 3.1: model_list / benchmark / price_aggregator 类型特殊处理 ===
    # 这些类型不是促销信息, 不应入 promotions 表 (会污染 dashboard)
    # 但当前是"半天最小可用版": 仍入 promotions, 加 source_type 标记让 3.3 消费者认出来
    # 后续可以加 model_registry_candidates / benchmark_snapshots 表分流 (但需要 schema 改动)

    # === 1.6 Phase 3.4: price_aggregator 类型同步价格到 price_snapshots + 刷路由缓存 ===
    if ai_result and source.source_type == "price_aggregator":
        await _handle_price_aggregator_result(ai_result, source)

    # === 2. AI 成功 + 置信度足够 → 入库 ===
    if ai_result and (
        (ai_result.is_promotion and ai_result.confidence >= settings.parse_confidence_threshold)
        or ai_result.models
    ):
        await _save_promotion(ai_result, source)
        if ai_result.models:
            logger.info(
                f"模型元信息候选(AI): [{ai_result.provider}] "
                f"count={len(ai_result.models)}, "
                f"confidence={ai_result.confidence:.2f}"
            )
        else:
            logger.info(
                f"优惠入库(AI): [{ai_result.provider}] {ai_result.title} "
                f"(置信度: {ai_result.confidence:.2f})"
            )
        return

    # === 3. 触发降级：AI 失败 OR AI 成功但低置信度 OR AI 判断非优惠 ===
    # （注：即使 AI 判断"非优惠"，CSS 降级也可能捞到手动漏掉的活动）
    css_result = await _try_css_fallback(content, source)
    if css_result:
        await _save_promotion(css_result, source)
        logger.info(
            f"优惠入库(CSS 降级): [{css_result.provider}] "
            f"selector={css_result.summary!r}"
        )
        return

    # === 4. 全部失败 → 告警 ===
    if ai_result and ai_result.is_promotion:
        # AI 解析出"是优惠"但 CSS 降级也没救 → 人工确认
        await _alert_low_confidence(ai_result, source)
    else:
        # AI 失败 / AI 判断非优惠 / CSS 降级也失败 → 静默
        logger.debug(
            f"非优惠或全部失败，跳过: {source.provider} "
            f"(ai={'failed' if ai_result is None else 'no_promotion'})"
        )


async def _handle_price_aggregator_result(result: ParseResult, source):
    """Phase 3.4: 把 price_aggregator 解析出的模型价格同步到 price_snapshots

    期望 AI 在 content / summary / discount_desc 中给出 (model, input_price, output_price)
    表。本实现采用保守的"key=value 行解析"，避免依赖结构化字段（Phase 3.2 未引入）。
    未识别行被忽略，避免污染。
    """
    from intelligence.pricing_sync import apply_price_change_to_route_cache, sync_price_change

    text = "\n".join(
        part for part in (result.discount_desc, result.content, result.summary) if part
    )
    rows = _parse_price_lines(text)

    synced = 0
    refreshed = 0
    for model, input_price, output_price in rows:
        change = await sync_price_change(
            source.provider,
            model,
            input_price,
            output_price,
            source="crawler",
        )
        if change is None:
            continue
        synced += 1
        if change.changed and await apply_price_change_to_route_cache(change):
            refreshed += 1

    if synced:
        logger.info(
            "price_aggregator 同步: provider=%s synced=%s, refreshed=%s",
            source.provider, synced, refreshed,
        )


_PRICE_LINE_RE = re.compile(
    r"^(?P<model>[A-Za-z0-9_./\-:]{2,})\s*[=:]\s*"
    r"(?:(?P<input>\d+(?:\.\d+)?)\s*/\s*)?"
    r"(?P<output>\d+(?:\.\d+)?)\s*$"
)


def _parse_price_lines(text: Optional[str]) -> list[tuple[str, Optional[float], Optional[float]]]:
    """Phase 3.4: 从纯文本中解析 (model, input, output) 价格行。

    支持格式：
    - `deepseek-v3: 2.0/3.0`        # input/output
    - `qwen-plus = 0.8`             # 仅 output
    - `Pro/deepseek-ai/DeepSeek-V3: 1.5/2.5`

    未识别行被跳过，避免污染 price_snapshots。
    """
    if not text:
        return []

    rows: list[tuple[str, Optional[float], Optional[float]]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _PRICE_LINE_RE.match(line)
        if not match:
            continue
        input_price = float(match.group("input")) if match.group("input") else None
        output_price = float(match.group("output"))
        # input 缺失时沿用 output (保守处理, 1 元 1M tokens 是常见聚合定价)
        if input_price is None:
            input_price = output_price
        rows.append((match.group("model"), input_price, output_price))
    return rows


async def _try_css_fallback(content: str, source) -> ParseResult | None:
    """尝试 CSS 降级

    Returns:
        ParseResult 成功提取 / None 没有可用 rule 或提取失败
    """
    try:
        from db.engine import async_session_factory
        from db import crud
        from intelligence.extractor import extract_by_css

        async with async_session_factory() as session:
            rule = await crud.get_active_parse_rule(session, source.provider)

        if not rule:
            logger.debug(f"无 {source.provider} 的 CSS 降级规则")
            return None

        logger.info(
            f"尝试 CSS 降级: {source.provider} (rule.confidence={rule.confidence:.2f}, "
            f"selector={rule.css_selector!r})"
        )
        return extract_by_css(
            html=content,
            css_selector=rule.css_selector,
            confidence=rule.confidence or 0.5,
            provider=source.provider,
            url=source.url,
        )
    except Exception as e:
        logger.error(f"CSS 降级异常: {e}")
        return None


async def _save_promotion(result: ParseResult, source):
    """将解析结果存入优惠活动表

    Phase 2D-1: 加 sanity_check 标黄
    - 标黄不阻止入库（你之前说倾向后者 — 不丢数据，只是醒目标记）
    - 标黄原因写到 flagged_reason

    Phase 3.1: 接收 source_type
    - sanity check 阈值按 type 调整 (model_list/benchmark/price_aggregator 放低)
    - source_type 写进 raw_data JSON, 让 3.3 消费者认出来

    Phase 3.3 接通 (P0-1 fix): 入库 commit 后, 若 intelligence_kind=model_metadata 且
    models 非空, 调用 model_sync.process_promotion 立即消费.
    - 用新 session (model_sync 内部自行 async_session_factory),
      避免与本函数已 commit 的事务重叠
    - 调用失败仅 logger.warning, 不影响优惠入库主流程
    """
    promo_id: Optional[int] = None
    raw_data_json: Optional[str] = None
    try:
        from db.engine import async_session_factory
        from db import crud

        # P2 fix (2026-08-20): title 为空的解析结果直接丢弃, 不再入库为"未知活动"
        # 背景: AI 解析 pricing/news 页面时经常提取不到活动标题, 之前 fallback
        # 写入 "未知活动", 累积 917 条垃圾数据淹没 dashboard 优惠情报栏
        if not result.title or not result.title.strip():
            logger.info(
                f"优惠丢弃(无标题): [{result.provider or source.provider}] "
                f"url={result.url} conf={result.confidence}"
            )
            return

        # sanity check（先于入库，但不影响入库）— Phase 3.1: 传 source_type
        flagged, flagged_reason = _sanity_check(result, source.source_type)

        # Phase 3.2: 把 models 序列化进 raw_data, 供 Phase 3.3 自动入组消费
        raw_data_dict = result.model_dump()
        raw_data_dict["_source_type"] = source.source_type
        raw_data_dict["_intelligence_kind"] = result.intelligence_kind
        raw_data_json = json.dumps(raw_data_dict, ensure_ascii=False)

        async with async_session_factory() as session:
            # 去重: 7 天内同 (provider, title) 已有记录则跳过, 避免重扫重复入库
            from db.models import Promotion
            existing = await session.execute(
                select(Promotion.id).where(
                    Promotion.provider == (result.provider or source.provider),
                    Promotion.title == result.title.strip(),
                    Promotion.created_at >= datetime.now() - timedelta(days=7),
                ).limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                logger.debug(
                    f"优惠去重(7d内同名): [{result.provider or source.provider}] "
                    f"{result.title}"
                )
                return

            promo = await crud.create_promotion(
                session,
                provider=result.provider or source.provider,
                title=result.title.strip(),
                content=result.content,
                url=result.url,
                discount_desc=result.discount_desc,
                start_date=result.start_date,
                end_date=result.end_date,
                status="active",
                confidence=result.confidence,
                raw_data=raw_data_json,
                flagged=1 if flagged else 0,
                flagged_reason=flagged_reason,
            )
            await session.commit()
            promo_id = promo.id

        if flagged:
            logger.warning(
                f"⚠️  优惠入库(标黄): [{result.provider or source.provider}] "
                f"{result.title!r} reason={flagged_reason}"
            )
    except Exception as e:
        logger.error(f"优惠入库失败: {e}")
        return  # P0-1 fix: 优惠入库失败时不要跳下游 process_promotion

    # V4.8.5 P0-4: 高置信度优惠 → 限时优惠价格行（打通"情报→省钱"核心链路）
    # 折扣率可解析时, 乘以当前 normal 价生成 promotion 价行, 路由侧自动按日期生效/失效
    if (promo_id is not None
            and result.is_promotion
            and result.confidence >= settings.parse_confidence_threshold):
        try:
            await _promotion_to_price_rows(result, result.provider or source.provider)
        except Exception as e:
            logger.warning(f"优惠转价格行失败(非阻断): {e}")

    # P0-1 fix: model_metadata 入库后, 立即触发 Phase 3.3 自动入组消费
    if (promo_id is not None
            and result.intelligence_kind == "model_metadata"
            and result.models):
        try:
            from intelligence.model_sync import process_promotion
            sync_results = await process_promotion(promo_id, raw_data_json or "")
            joined = sum(1 for r in sync_results if r.action == "auto_joined")
            pending = sum(1 for r in sync_results if r.action == "pending_review")
            rejected = sum(1 for r in sync_results if r.action == "rejected")
            left = sum(1 for r in sync_results if r.action == "auto_left")
            logger.info(
                "Phase 3.3 自动消费: promo=%s models=%d (joined=%d pending=%d rejected=%d left=%d)",
                promo_id, len(sync_results), joined, pending, rejected, left,
            )
        except Exception as e:
            # 仅 logger.warning, 不影响 _save_promotion 主流程
            # 下次 scheduler 重扫 / run-model-sync 重试会补救
            logger.warning(
                "Phase 3.3 自动消费失败: promo=%s err=%s (优惠已入库, 下次重试可补救)",
                promo_id, e,
            )


async def _alert_low_confidence(result: ParseResult, source):
    """低置信度优惠 → 写入审查队列 + 发飞书告警

    V4.8.4: 不再只发告警丢数据。低置信度 ParseResult 持久化到
    promotion_review_queue 等待人工审批, 飞书告警提示去 Dashboard 处理。
    Promotion 表只含高置信度 + 人工确认的优惠, 保持干净。
    """
    from intelligence.alerter import send_alert
    from db.engine import async_session_factory
    from db import crud
    from db.models import PromotionReviewQueue

    # 1. 持久化到审查队列 (24h 内同 provider+title 去重)
    raw_data_json = json.dumps(result.model_dump(), ensure_ascii=False)
    try:
        async with async_session_factory() as session:
            existing = await session.execute(
                select(PromotionReviewQueue.id).where(
                    PromotionReviewQueue.provider == (result.provider or source.provider),
                    PromotionReviewQueue.title == (result.title or ""),
                    PromotionReviewQueue.approved == 0,
                    PromotionReviewQueue.created_at >= datetime.now() - timedelta(hours=24),
                ).limit(1)
            )
            if existing.scalar_one_or_none() is not None:
                logger.debug(
                    f"低置信度审查去重(24h): [{source.provider}] {result.title}"
                )
                return

            await crud.create_promotion_review(
                session,
                provider=result.provider or source.provider,
                title=result.title,
                summary=result.summary,
                url=result.url,
                confidence=result.confidence,
                raw_data=raw_data_json,
            )
            await session.commit()
    except Exception as e:
        logger.error(f"低置信度优惠写入审查队列失败: {e}")
        return  # DB 写入失败时不发告警 (避免告警了却没数据可审)

    # 2. 发飞书告警 (带 dashboard 提示)
    message = (
        f"[低置信度] {source.provider} 发现疑似优惠\n"
        f"标题: {result.title}\n"
        f"置信度: {result.confidence:.2f}\n"
        f"摘要: {result.summary}\n"
        f"链接: {result.url}\n"
        f"已加入审查队列, 请到 Dashboard 确认是否入库。"
    )
    await send_alert(
        message,
        key=f"low_confidence:{source.provider}",
        silence_seconds=3600,
    )
    logger.info(f"低置信度优惠已入审查队列: [{source.provider}] {result.title}")


async def handle_fetch_error(source, error: str):
    """处理采集失败

    Phase 2C-1: 升级告警策略
    - 第 1 次失败：立即告警（不要延迟发现故障）
    - 第 2 次失败：log only（避免刷屏）
    - 第 3 次失败：告警 + 自动 disable source
    - 第 4+ 次：log only（已 disabled）

    V4.8.1: 告警加 throttle key + 分级静默
    - 首次失败: key=fetch_error:{provider}:{url}, 静默 30min
    - 连续 3 次: 同 key, 静默 2h (避免间歇性故障的恢复→再失败循环刷屏)
    - 恢复时由 differ._update_source_hash 发独立恢复告警

    Args:
        source: 采集源对象（in-memory error_count 会被累加并同步到 DB）
        error: 错误描述（来自 fetch_page_with_error 的 reason）
    """
    try:
        from db.engine import async_session_factory
        from db import crud
        from intelligence.alerter import send_alert

        async with async_session_factory() as session:
            # V4.8.3: 从 DB 读权威 error_count (修 pickle 反序列化重置 bug)
            # APScheduler SQLAlchemyJobStore 每次 tick 反序列化全新 source 副本,
            # in-memory error_count 永远=0 → 永远走"首次失败"分支 → 永不自动禁用。
            # 与 differ.has_changed 读 DB last_hash 同一 bug 类型, 同样修法。
            db_source = await crud.get_intel_source_by_provider_url(
                session, source.provider, source.url
            )
            if db_source:
                source.error_count = db_source.error_count  # 用 DB 值覆盖内存值

            # 增加错误计数（in-memory + DB）
            source.error_count = (source.error_count or 0) + 1

            if db_source:
                db_source.error_count = source.error_count
                if source.error_count >= 3:
                    db_source.status = "error"
                await session.commit()
            else:
                # DB 没对应行（首次失败）→ upsert 一条
                # 修复 V4.6.4: crud.upsert_intel_source 内部 hardcode status="active",
                # 不能从 caller 再传 status (multiple values for keyword arg)
                # 解决: 先 upsert (默认 status=active), 然后 update status
                new_source = await crud.upsert_intel_source(
                    session,
                    provider=source.provider,
                    url=source.url,
                    source_type=getattr(source, "source_type", "web_page"),
                    poll_interval=getattr(source, "poll_interval", 900),
                    error_count=source.error_count,
                )
                # 显式 update status (避免硬编码冲突)
                if source.error_count >= 3:
                    new_source.status = "error"
                await session.commit()

            # V4.8.1: throttle key 按 (provider, url) 维度, 分级静默
            alert_key = f"fetch_error:{source.provider}:{source.url}"

            if source.error_count == 1:
                # 首次失败：立即告警, 30 分钟内同 source 不重复
                await send_alert(
                    f"[采集异常] {source.provider} 首次失败\n"
                    f"URL: {source.url}\n"
                    f"错误: {error}\n"
                    f"error_count={source.error_count}/3",
                    key=alert_key,
                    silence_seconds=1800,
                )
            elif source.error_count >= 3:
                # 连续 3 次失败：升级告警 + 自动禁用, 2 小时内同 source 不重复
                await send_alert(
                    f"[采集异常] {source.provider} 连续失败 {source.error_count} 次\n"
                    f"URL: {source.url}\n"
                    f"错误: {error}\n"
                    f"已自动禁用，请人工检查。",
                    key=alert_key,
                    silence_seconds=7200,
                )

    except Exception as e:
        logger.error(f"处理采集错误失败: {e}")
