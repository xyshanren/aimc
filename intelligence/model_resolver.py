"""intel 模型名 → 规范名 (canonical) 解析 + 供给判定 (O1, 2026-11-04)

背景
----
intel 的 `item.model_id` 是**上游展示名 / 线名**（`DeepSeek-V4.1-Flash`、
`Doubao Seedream 5.0 flash`），而 `model_registry` / `price_snapshots` 的键是
**规范名 slug**（`deepseek-flash`）。两者此前没有任何映射层，后果：

- 待确认队列里的行无法关联现网模型（log 58 的 `DeepSeek-V4.1-Flash` 在 registry
  不存在，真身是 `deepseek-flash`）；
- auto-join 分支按 `item.model_id` 原样 upsert，同一模型可能建出第二套 registry 行；
- 文档页抓来的模型名（本站无渠道）和无信息量 metadata 一起进 pending，噪声。

本模块只做「**建议**」，不改写 `model_id`、不自动合并：

- 高置信来源（exact / normalized / raw_model_id / alias_log）→ 直接当 canonical 处理；
- 启发式来源（suffix_strip / display_name）→ 作为 proposal 挂进 pending 等人工点头；
- 全失败 → unresolved（真·新模型）。

别名记忆（**零 schema 变更**）：人工批准过的 metadata 行会把 `resolved_model_id`
写进 `new_value`，下次解析时从"已批准的历史行"读回来（alias_log）。若观察期后
仍反复重判，再考虑独立 `model_alias` 表。

为什么不做 fuzzy（编辑距离 / 向量）：`deepseek-v4-flash` 与 `deepseek-flash` 是不同
代际，误并的代价（错档 / 错价 / 错组）远高于多留一条 pending。

供给判定
--------
`is_suppliable_by_any_channel` 回答"这个模型本站有没有机会供应"。它决定 pending
要不要落库：无渠道无价行的模型（如 volcengine 文档页上的 Seedream / Seedance）
只在日志里留观察记录，不再污染待确认队列。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Channel,
    ModelCapabilitiesLog,
    ModelListSnapshot,
    ModelRegistry,
    PriceSnapshot,
)
from db.normalizers import normalize_model_id

logger = logging.getLogger(__name__)

# 高置信: 数据里的确定性事实, 不需要人工点头
HIGH_CONFIDENCE_METHODS = frozenset({"exact", "normalized", "raw_model_id", "alias_log"})
# 启发式: 规则猜的, 作为 proposal 等人工点头
PROPOSAL_METHODS = frozenset({"suffix_strip", "display_name"})

# 确定性后缀 (只剥这些, 不做模糊匹配)
_DATE_SUFFIX_RE = re.compile(r"-\d{4}$")
MODEL_SUFFIX_ALIASES: tuple[str, ...] = (
    "-preview", "-instruct", "-chat", "-latest", "-beta", "-exp", "-rc",
    "-alpha", "-snapshot",
)


@dataclass
class Resolution:
    """一次解析结果"""

    canonical: Optional[str]      # 规范名 (None = 未解析)
    method: Optional[str]         # exact / normalized / raw_model_id / alias_log / suffix_strip / display_name
    confidence: Optional[str]     # "high" / "proposal" / None

    @property
    def resolved(self) -> bool:
        return self.canonical is not None

    @property
    def is_proposal(self) -> bool:
        return self.method in PROPOSAL_METHODS

    def as_payload(self) -> dict:
        """塞进 pending new_value 的附加字段 (只在解析成功时用)"""
        return {
            "resolved_model_id": self.canonical,
            "resolved_by": self.method,
            "resolution": self.confidence,
        }


def strip_vendor_prefix(model_id: str) -> str:
    """`deepseek-ai/DeepSeek-V4.1-Flash` → `DeepSeek-V4.1-Flash`"""
    if not model_id:
        return ""
    return model_id.rsplit("/", 1)[-1]


def strip_name_suffixes(model_id: str) -> str:
    """剥确定性后缀: 日期版 (`-1017`) 与前缀别名 (`-preview` / `-instruct` ...)"""
    out = model_id or ""
    changed = True
    while changed and out:
        changed = False
        stripped = _DATE_SUFFIX_RE.sub("", out)
        if stripped != out and stripped:
            out = stripped
            changed = True
        for suffix in MODEL_SUFFIX_ALIASES:
            if out.lower().endswith(suffix) and len(out) > len(suffix):
                out = out[: -len(suffix)]
                changed = True
    return out


def _matches_raw_tail(candidate_low: str, raw_low: str) -> bool:
    """candidate 是否命中线上名 (允许 vendor 前缀 + 4 位日期版后缀)

    `deepseek-v4.1-flash` 命中 `deepseek-ai/deepseek-v4.1-flash-1017`
    """
    if not candidate_low or not raw_low:
        return False
    tail = raw_low.rsplit("/", 1)[-1]
    if tail == candidate_low:
        return True
    return _DATE_SUFFIX_RE.sub("", tail) == candidate_low


# ============================================================
# 解析
# ============================================================
async def _registry_has(session: AsyncSession, model_id: str) -> bool:
    return (await session.execute(
        select(ModelRegistry.model_id).where(ModelRegistry.model_id == model_id).limit(1)
    )).scalar_one_or_none() is not None


async def _registry_hit_normalized(
    session: AsyncSession, candidates: set[str],
) -> Optional[str]:
    """按规范化候选取 registry 行; 命中唯一一个才算数 (多义 → 放弃)"""
    if not candidates:
        return None
    rows = (await session.execute(
        select(ModelRegistry.model_id)
        .where(func.lower(ModelRegistry.model_id).in_(sorted(candidates)))
        .limit(5)
    )).scalars().all()
    uniq = sorted(set(rows))
    return uniq[0] if len(uniq) == 1 else None


async def _resolve_by_raw_model_id(
    session: AsyncSession, candidates: set[str],
) -> Optional[str]:
    """price_snapshots.raw_model_id 反查规范名 (严格 raw id 渠道的既有映射)"""
    for cand in sorted(candidates):
        rows = (await session.execute(
            select(PriceSnapshot.model, PriceSnapshot.raw_model_id)
            .where(PriceSnapshot.raw_model_id.isnot(None))
            .where(func.lower(PriceSnapshot.raw_model_id).like(f"%{cand}%"))
            .limit(50)
        )).all()
        for model, raw in rows:
            if _matches_raw_tail(cand, (raw or "").lower()):
                if await _registry_has(session, model):
                    return model
    return None


async def _resolve_from_alias_log(
    session: AsyncSession, model_id: str,
) -> Optional[str]:
    """从人工批准过的历史 metadata 行里读 resolved_model_id (别名记忆)"""
    rows = (await session.execute(
        select(ModelCapabilitiesLog.new_value)
        .where(ModelCapabilitiesLog.model_id == model_id)
        .where(ModelCapabilitiesLog.approved == 1)
        .where(ModelCapabilitiesLog.field_changed == "metadata")
        .order_by(ModelCapabilitiesLog.created_at.desc())
        .limit(10)
    )).scalars().all()
    for raw in rows:
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        canonical = payload.get("resolved_model_id")
        if canonical and await _registry_has(session, canonical):
            return canonical
    return None


async def _resolve_by_display_name(
    session: AsyncSession, display_name: str,
) -> Optional[str]:
    if not display_name:
        return None
    low = normalize_model_id(display_name)
    if not low:
        return None
    return (await session.execute(
        select(ModelRegistry.model_id)
        .where(func.lower(ModelRegistry.display_name) == low)
        .limit(1)
    )).scalar_one_or_none()


async def resolve_canonical_model(
    session: AsyncSession,
    model_id: str,
    display_name: Optional[str] = None,
) -> Resolution:
    """把 intel 的模型名解析成规范的 canonical model_id。

    顺序（确定性优先, 启发式最后）:
      1. exact             registry.model_id 精确
      2. normalized        小写/去空白后精确
      3. raw_model_id      price_snapshots.raw_model_id 反查 (含 vendor 前缀/日期版)
      4. alias_log         人工批准过的历史解析结果
      5. suffix_strip      剥 vendor 前缀 + 日期版/预览后缀后精确
      6. display_name      registry.display_name 精确
    """
    raw = (model_id or "").strip()
    if not raw:
        return Resolution(None, None, None)

    low = normalize_model_id(raw)

    if await _registry_has(session, raw):
        return Resolution(raw, "exact", "high")

    hit = await _registry_hit_normalized(session, {low})
    if hit:
        return Resolution(hit, "normalized", "high")

    candidates = {low, normalize_model_id(strip_vendor_prefix(raw)),
                  normalize_model_id(strip_name_suffixes(raw)),
                  normalize_model_id(strip_name_suffixes(strip_vendor_prefix(raw)))}
    candidates.discard("")

    hit = await _resolve_by_raw_model_id(session, candidates)
    if hit:
        return Resolution(hit, "raw_model_id", "high")

    hit = await _resolve_from_alias_log(session, raw)
    if hit:
        return Resolution(hit, "alias_log", "high")

    stripped = {c for c in candidates if c != low}
    hit = await _registry_hit_normalized(session, stripped)
    if hit:
        return Resolution(hit, "suffix_strip", "proposal")

    hit = await _resolve_by_display_name(session, display_name or "")
    if hit:
        return Resolution(hit, "display_name", "proposal")

    return Resolution(None, None, None)


# ============================================================
# 供给判定
# ============================================================
async def _raw_listed_by_provider(
    session: AsyncSession, providers: list[str], candidates: set[str],
) -> bool:
    """上游 /v1/models 快照里有没有这个模型 (渠道真实列出 = 强供给证据)"""
    if not providers or not candidates:
        return False
    for cand in sorted(candidates):
        rows = (await session.execute(
            select(ModelListSnapshot.model)
            .where(ModelListSnapshot.provider.in_(providers))
            .where(func.lower(ModelListSnapshot.model).like(f"%{cand}%"))
            .limit(50)
        )).scalars().all()
        for listed in rows:
            if _matches_raw_tail(cand, (listed or "").lower()):
                return True
    return False


async def is_suppliable_by_any_channel(
    session: AsyncSession,
    raw_model_id: str,
    canonical: Optional[str] = None,
    source_provider: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """这个模型本站有没有机会供应 (决定 pending 要不要落库)

    判定顺序:
      1. price_row        规范名价行 (canonical 已在路由候选里)
      2. price_raw_id     任意渠道价行的 raw_model_id 命中 (含 vendor/日期版)
      3. upstream_list    渠道 /v1/models 快照真实列出
      4. channel_exists   该厂商渠道存在 (弱证据: 名单可能过时/聚合透传可兜底)

    Returns:
        (suppliable, reason)
    """
    if canonical:
        hit = (await session.execute(
            select(PriceSnapshot.id)
            .join(Channel, PriceSnapshot.channel_id == Channel.id)
            .where(PriceSnapshot.model == canonical)
            .where(Channel.status == "active")
            .limit(1)
        )).scalar_one_or_none()
        if hit is not None:
            return True, "price_row"

    raw = (raw_model_id or "").strip()
    low = normalize_model_id(raw)
    candidates = {low, normalize_model_id(strip_vendor_prefix(raw)),
                  normalize_model_id(strip_name_suffixes(raw)),
                  normalize_model_id(strip_name_suffixes(strip_vendor_prefix(raw)))}
    candidates.discard("")

    for cand in sorted(candidates):
        rows = (await session.execute(
            select(PriceSnapshot.raw_model_id)
            .where(PriceSnapshot.raw_model_id.isnot(None))
            .where(func.lower(PriceSnapshot.raw_model_id).like(f"%{cand}%"))
            .limit(50)
        )).scalars().all()
        if any(_matches_raw_tail(cand, (r or "").lower()) for r in rows):
            return True, "price_raw_id"

    provider = (source_provider or "").strip()
    providers = [provider] if provider else []
    if await _raw_listed_by_provider(session, providers, candidates):
        return True, "upstream_list"

    if provider:
        hit = (await session.execute(
            select(Channel.id)
            .where(Channel.provider == provider)
            .where(Channel.status == "active")
            .limit(1)
        )).scalar_one_or_none()
        if hit is not None:
            return True, "channel_exists"

    return False, None
