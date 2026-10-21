"""组成员 priority 语义 (V6.17d, 2026-10-21)

看板「加成员」原先默认 priority = 组内现有最大值 +5 (追加队尾), 且 UI 不可改 ——
用户把 minimax-m3.1-flash 加回 tier:vision 时拿到 125 (而它当初在 vision 是 90,
排最前), 想调都调不动。

本模块把 priority 语义集中:
- **继承优先**: 加成员时若该模型在同组有"同族旧行"(改名/预览后缀不同, 如
  minimax-m3.1-flash ← minimax-m3.1-flash-preview), 继承其历史 priority ——
  保住当初的策展排位, 而不是无声地掉到队尾;
- **否则追加**: 组内现有最大值 +5 (不抢占现有成员的位次);
- **显式优先**: 请求里带了 priority 就用它 (UI 可填)。
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import select

from db.models import ModelGroupMember

# 预览/后缀别名 —— 同族判定时剥掉这些后缀
MODEL_SUFFIX_ALIASES: tuple[str, ...] = (
    "-preview", "-beta", "-latest", "-exp", "-rc", "-alpha", "-snapshot",
)


def normalize_model_key(model_id: str) -> str:
    """模型名归一 (去预览/后缀别名) —— 用于"改名模型"与旧行匹配"""
    low = (model_id or "").strip().lower()
    changed = True
    while changed:
        changed = False
        for suffix in MODEL_SUFFIX_ALIASES:
            if low.endswith(suffix) and len(low) > len(suffix):
                low = low[: -len(suffix)]
                changed = True
    return low


def build_sibling_priority_map(member_rows: Iterable[ModelGroupMember]) -> dict[str, int]:
    """同族旧行 → priority 映射 (取最近一次出组的那条)

    只收**软删行** (is_active=0): 软删行 = 当初的策展排位留档 (改名/预览后缀的
    旧 model_id 正是这类)。活跃成员行不算"历史排位" —— 否则同一模型的现役行
    会把自己的位次当成继承来源, 继承就失效了。
    """
    out: dict[str, int] = {}
    best_ts: dict[str, tuple] = {}
    for row in member_rows:
        if row.is_active == 1:
            continue
        key = normalize_model_key(row.model_id)
        ts = (row.removed_at.isoformat() if row.removed_at else "",
              row.created_at.isoformat() if row.created_at else "",
              row.id or 0)
        if key not in best_ts or ts > best_ts[key]:
            best_ts[key] = ts
            out[key] = int(row.priority or 0)
    return out


def default_priority(active_priorities: Iterable[int]) -> int:
    """追加到队尾的默认值 (组内现有最大值 +5; 空组 100)"""
    pris = [int(p) for p in (active_priorities or []) if p is not None]
    return (max(pris) + 5) if pris else 100


def suggest_priority(
    model_id: str,
    sibling_map: dict[str, int],
    active_priorities: Iterable[int],
) -> int:
    """加某模型时的 priority 建议值 (同族继承 > 队尾追加)"""
    inherited = sibling_map.get(normalize_model_key(model_id))
    if inherited is not None:
        return inherited
    return default_priority(active_priorities)


async def resolve_add_priority(
    session,
    group_id: str,
    model_id: str,
    explicit: Optional[int] = None,
) -> int:
    """加成员时确定 priority (显式 > 同族继承 > 队尾追加)

    注: 同一 model_id 的软删行走"复活"分支, 由调用方保留其原 priority。
    """
    if explicit is not None:
        return int(explicit)
    rows = list((await session.execute(
        select(ModelGroupMember).where(ModelGroupMember.group_id == group_id)
    )).scalars().all())
    active_prios = [r.priority for r in rows if r.is_active == 1]
    sibling_map = build_sibling_priority_map(rows)
    return suggest_priority(model_id, sibling_map, active_prios)
