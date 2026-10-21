"""组准入规则单一事实源 (V6.17d, 2026-10-21)

背景:
  看板「加成员」下拉此前在前端硬编码 tier 白名单, 与路由层各走各的 ——
  ① 能选到运行时会被跳过的**死成员** (tier:vision 组 require_vision=1,
     纯文本模型入组后运行时直接 skip);
  ② 选不到该选的 (min_context=64000 的 flagship 组曾能选上下文不足的模型);
  ③ 组语义 (视觉组只收多模态 / 代码组只收代码特化) 完全没有约束。

本模块把规则集中成两层, 看板候选列表与路由层共用:

  **运行时层** (group_runtime_accepts): 严格镜像 model_groups 的四个约束
  (tier 精确匹配 / capabilities 子集 / min_context / require_vision),
  路由层 (gateway/router.py) 与看板候选列表都调它 → 不可能漂移。
  运行时层**表达不了**"T0 或 T1"这种集合语义, 故这层只做等值/子集判定。

  **策展层** (GROUP_TIER_POLICY): 路由无法表达的语义策展 (每组该收哪几档),
  只作用于看板候选列表, 不影响路由。

语义要求 (用户 2026-10-21 拍板):
  - tier:vision / tier:vision-light → 必须是多模态 (modality ∈ vision/multimodal),
    由组定义 require_vision=1 在运行时强制, 候选列表同源镜像;
  - tier:code → 必须是**代码特化**模型, 由组定义 capabilities=["code_specialized"]
    在运行时强制 (tag 由策展给特化模型打, 泛用模型即便 capabilities 含 'code'
    也不算特化); 候选列表同源镜像。
  - 以后若"模型大多数都支持视觉"或"不再做代码特化", 只需改组定义一行
    (清 require_vision / 清 capabilities), 路由与看板同时生效, 或直接简化分组。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# 非聊天模型关键词 (向量/重排/语音/生图/视频/OCR 等) —— 聊天组不收
# 与 dashboard/onboarding.py 的待补价盘点同源 (原先两处各写一份)
NON_CHAT_KW: tuple[str, ...] = (
    "embed", "rerank", "asr", "speech", "voice", "tts", "ocr", "image",
    "video", "i2v", "t2v", "caption", "kolors", "lora", "diffusion",
    "gsr", "flux", "/wan",
)

# 组 → 可入组 tier 档位 (策展层; None = 不限)。
# 与路由层的等值 tier 约束互补: 路由只能表达"等于某一档", 这里表达集合。
GROUP_TIER_POLICY: dict[str, Optional[list[str]]] = {
    "tier:flagship": ["T0", "T1"],
    "tier:strong": ["T0", "T1"],
    "tier:balanced": ["T2"],
    "tier:light": ["T3", "T4"],
    "tier:vision": ["T0", "T1"],
    "tier:vision-light": ["T2", "T3", "T4"],
    "tier:code": None,          # 代码组不限档, 但要求 code_specialized 能力
}


def is_chat_model(model_id: str) -> bool:
    """是否聊天类模型 (排除向量/语音/生图/视频/OCR 等)"""
    low = (model_id or "").lower()
    return not any(k in low for k in NON_CHAT_KW)


def parse_capabilities(raw: Any) -> set[str]:
    """capabilities 列 (JSON 字符串/列表) → set; 解析失败按空集处理"""
    if not raw:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(x) for x in raw}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(x) for x in parsed}


def group_runtime_accepts(group: Any, registry: Any) -> tuple[bool, str]:
    """组定义 × registry 行 → (是否可入组/可被路由选中, 拒绝原因)

    严格镜像 gateway/router.py 的组过滤 (tier / capabilities / min_context /
    require_vision) —— 路由层与看板候选列表共用, 杜绝"能选到但路由会跳过"的死成员。

    group:    有 tier / capabilities / min_context / require_vision 属性的对象
    registry: 有 tier / modality / capabilities / context_length 属性的对象
    """
    # 1) tier 精确匹配 (组未限定 tier = 不限)
    g_tier = getattr(group, "tier", None)
    if g_tier and getattr(registry, "tier", None) != g_tier:
        return False, "tier"

    # 2) capabilities: 组要求的能力必须是模型能力的子集
    g_caps = parse_capabilities(getattr(group, "capabilities", None))
    if g_caps:
        m_caps = parse_capabilities(getattr(registry, "capabilities", None))
        if not g_caps.issubset(m_caps):
            return False, "capabilities"

    # 3) min_context: 上下文不足
    min_ctx = getattr(group, "min_context", None) or 0
    if min_ctx:
        ctx = getattr(registry, "context_length", None)
        if not ctx or ctx < min_ctx:
            return False, "min_context"

    # 4) require_vision: modality ∈ (vision, multimodal)
    #    (multimodal 同样是 vision-capable — P2-A 修复的语义, 勿改回 != "vision")
    if getattr(group, "require_vision", None):
        if getattr(registry, "modality", None) not in ("vision", "multimodal"):
            return False, "require_vision"

    return True, ""


def group_tier_allowed(group_id: str, tier: Optional[str]) -> bool:
    """策展层 tier 白名单 (组未登记 → 不限)"""
    if not tier:
        return True
    allowed = GROUP_TIER_POLICY.get(group_id)
    return allowed is None or tier in allowed


def group_candidate_models(
    group: Any,
    registry_rows: Iterable[Any],
    member_model_ids: Iterable[str] = (),
) -> list[dict]:
    """算某个组的候选模型 (可加入该组的 registry 行)

    条件 = 运行时层 (group_runtime_accepts) + 策展层 tier 白名单
            + 聊天类 + status=active + 尚未是成员
    返回 [{model_id, tier, modality, capabilities, context_length}]
    """
    group_id = getattr(group, "group_id", "") or ""
    in_group = set(member_model_ids or ())
    out: list[dict] = []
    for reg in registry_rows:
        model_id = getattr(reg, "model_id", None)
        if not model_id or model_id in in_group:
            continue
        if (getattr(reg, "status", None) or "active") != "active":
            continue
        modality = getattr(reg, "modality", None) or "text"
        # 聊天组候选只收聊天模态 (embedding/rerank/生图等 modality 直接排除),
        # 名称关键词黑名单兜底 (is_chat_model, 与 onboarding 待补价盘点同源)
        if modality not in ("text", "multimodal", "vision") or not is_chat_model(model_id):
            continue
        if not group_tier_allowed(group_id, getattr(reg, "tier", None)):
            continue
        ok, _reason = group_runtime_accepts(group, reg)
        if not ok:
            continue
        caps = sorted(parse_capabilities(getattr(reg, "capabilities", None)))
        out.append({
            "model_id": model_id,
            "tier": getattr(reg, "tier", None),
            "modality": modality,
            "capabilities": caps,
            "context_length": getattr(reg, "context_length", None),
        })
    out.sort(key=lambda m: (m.get("tier") or "", m["model_id"]))
    return out


def suggest_member_priority(
    group_id: str,
    active_priorities: Iterable[int],
    sibling_priority: Optional[int] = None,
) -> int:
    """新成员 priority 建议值 (越小越优先)

    1) 有同族旧行 (改名/复活场景, 如 minimax-m3.1-flash ← -preview) → 继承其 priority
       (保住当初的策展排位, 不被追加到队尾)
    2) 否则按组内现有最大值 +5 追加到队尾 (不抢占现有成员的位次)
    3) 空组 → 100
    """
    if sibling_priority is not None:
        return int(sibling_priority)
    pris = [int(p) for p in (active_priorities or []) if p is not None]
    return (max(pris) + 5) if pris else 100
