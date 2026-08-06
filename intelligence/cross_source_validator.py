"""Phase 4 E3 (2026-07-29) — 跨源交叉验证

扫描 `promotions.raw_data._intelligence_kind == "model_metadata"` 行的 models[] 字段，
按 model_id 分组聚合，对每个 model 比对各源在 tier / capabilities / context_length 等
关键字段上的取值，发现分歧时落库到 `cross_source_discrepancies`。

设计要点：
- 同一 (model_id, field_changed) 在 pending 状态只保留 1 条 (upsert)
- 分歧程度 = 0 (全源一致) 不入库
- 数据稀疏时 (出现该 model 的源 < MIN_SOURCES) 不做对比 (避免误判)
- 与 model_sync 解耦：本模块只做"发现"，不直接改 model_registry
"""

import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from db import crud
from db.engine import async_session_factory

logger = logging.getLogger(__name__)

# 至少多少个独立源提到同一个 model 才做对比
MIN_SOURCES = 2

# 需要做交叉验证的字段
COMPARED_FIELDS = ("tier", "capabilities", "context_length", "modality")

# Tier 顺序表（T0 最高=旗舰，T4 最低=微型/嵌入，与 architecture.md §4.2 一致）
_TIER_ORDER = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}


def _normalize_field_value(field: str, value):
    """字段值归一化便于比对。"""
    if value is None:
        return None
    if field == "capabilities":
        # capabilities 是数组，统一成 sorted tuple 字符串
        if isinstance(value, list):
            return "|".join(sorted(str(v) for v in value))
        return str(value)
    if field == "tier":
        return str(value) if value in _TIER_ORDER else None
    if field == "context_length":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return str(value)


def _calc_discrepancy_score(field: str, distinct_values: dict[str, list]) -> float:
    """计算分歧程度分数 (越大越值得关注)。

    简单启发式：
    - tier 不一致：1.0 (直接影响路由)
    - capabilities 不一致：0.8 (影响能力匹配)
    - context_length 差距 > 2 倍：0.7 (影响长文本任务)
    - modality 不一致：0.5 (text vs vision)
    - 一致：0
    """
    if len(distinct_values) <= 1:
        return 0.0
    if field == "tier":
        return 1.0
    if field == "capabilities":
        return 0.8
    if field == "context_length":
        nums = [v for v in distinct_values.keys() if isinstance(v, int)]
        if len(nums) >= 2:
            ratio = max(nums) / min(nums) if min(nums) > 0 else 999
            return 0.7 if ratio >= 2 else 0.3
        return 0.5
    if field == "modality":
        return 0.5
    return 0.4


async def _scan_promotions_with_models(session) -> list:
    """扫描所有 _intelligence_kind=model_metadata 的 promotions，提取 models[]。

    Returns: list of {promotion_id, provider, source_type, model: ModelIntelItem}
    """
    from db.models import Promotion
    from sqlalchemy import select

    stmt = select(Promotion).where(Promotion.status == "active")
    rows = (await session.execute(stmt)).scalars().all()

    results = []
    for p in rows:
        raw = p.raw_data or ""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if data.get("_intelligence_kind") != "model_metadata":
            continue
        for model_dict in data.get("models") or []:
            results.append({
                "promotion_id": p.id,
                "provider": p.provider,
                "source_type": data.get("_source_type", "unknown"),
                "model": model_dict,
            })
    return results


async def _group_models_by_id(records: list) -> dict:
    """按 model_id 分组聚合。

    Returns: {model_id: {field: {value: [{source, provider, confidence, ts}]}}}
    """
    grouped: dict[str, dict] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

    for rec in records:
        m = rec["model"]
        model_id = m.get("model_id") or m.get("id") or ""
        if not model_id:
            continue
        source_label = f"{rec['source_type']}:{rec['provider']}"
        ts = datetime.now().isoformat()
        confidence = m.get("confidence")

        for field in COMPARED_FIELDS:
            if field not in m:
                continue
            value = _normalize_field_value(field, m.get(field))
            if value is None:
                continue
            grouped[model_id][field][value].append({
                "source": source_label,
                "provider": rec["provider"],
                "confidence": confidence,
                "ts": ts,
            })
    return grouped


async def detect_and_record() -> dict:
    """检测跨源分歧并落库。

    Returns: { total_models: N, with_discrepancy: M, recorded: K }
    """
    async with async_session_factory() as session:
        records = await _scan_promotions_with_models(session)
        grouped = await _group_models_by_id(records)

        total = len(grouped)
        with_discrepancy = 0
        recorded = 0

        for model_id, field_dict in grouped.items():
            # 累计 source 总数（去重）
            sources_seen: set[str] = set()
            for fd in field_dict.values():
                for val_list in fd.values():
                    for entry in val_list:
                        sources_seen.add(entry["source"])
            if len(sources_seen) < MIN_SOURCES:
                continue  # 数据稀疏，跳过

            for field, value_dict in field_dict.items():
                if len(value_dict) <= 1:
                    continue  # 该字段全源一致
                score = _calc_discrepancy_score(field, value_dict)
                if score <= 0:
                    continue

                with_discrepancy += 1

                sources_flat = []
                for value, entries in value_dict.items():
                    for e in entries:
                        sources_flat.append({
                            "source": e["source"],
                            "provider": e["provider"],
                            "value": value,
                            "confidence": e["confidence"],
                            "ts": e["ts"],
                        })

                try:
                    row = await crud.upsert_cross_source_discrepancy(
                        session,
                        model_id=model_id,
                        field_changed=field,
                        sources_json=json.dumps(sources_flat, ensure_ascii=False),
                        discrepancy_score=score,
                    )
                    if row is None:
                        # 极小概率: 并发 INSERT 撞 UniqueConstraint,
                        # 但 winner 紧接着 resolve 掉同一个 pending 行,
                        # 导致 fallback 重新 SELECT 也找不到东西.
                        # 不计入 recorded, 但不阻塞后续字段.
                        logger.warning(
                            "跨源分歧 upsert 跑空: model=%s field=%s (并发 winner 已 resolve)",
                            model_id, field,
                        )
                        continue
                    recorded += 1
                except Exception as e:
                    # P0-7 fix: IntegrityError 已在 crud.upsert_cross_source_discrepancy
                    # 内部捕获 + 回滚 + fallback UPDATE, 这里只是防御性兜底
                    # (例如 DB 连接异常 / 网络超时). 不影响其他字段循环.
                    logger.warning(
                        "跨源分歧落库失败: model=%s field=%s err=%s",
                        model_id, field, e,
                    )

        await session.commit()

    summary = {
        "total_models": total,
        "with_discrepancy": with_discrepancy,
        "recorded": recorded,
    }
    logger.info(
        "跨源交叉验证完成: models=%d, 有分歧=%d, 入库=%d",
        total, with_discrepancy, recorded,
    )
    return summary