"""P2-A (2026-08-19): tier:vision 400 专项修复测试

覆盖 5 个修复点:
- Fix 1: require_vision 过滤器接受 multimodal (不再只接受 vision)
- Fix 2: infer_modality 识别 glm-4.5v / qwen-vl-* 为 vision
- Fix 3: seed_model_registry / seed_model_groups vision 模型定义完整
- Fix 4: 组路由 (tier:/scene:) 上游 400 自动 failover
- Fix 5: _resolve_actual_model 精确路由用 _channel_model 兜底 (400 无 model 时)

跑法:
    .venv/Scripts/python.exe tests/test_vision_routing.py
    pytest tests/test_vision_routing.py -v
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Fix 2: infer_modality 识别 glm-4.5v / qwen-vl-* 为 vision
# ============================================================

def test_infer_modality_glm_45v():
    """glm-4.5v 应识别为 vision (修复前误判为 text)"""
    from scripts.sync_siliconflow_to_registry import infer_modality
    assert infer_modality("glm-4.5v") == "vision"
    assert infer_modality("glm-4.5v-flash") == "vision"


def test_infer_modality_qwen_vl():
    """qwen-vl-* / qwen3-vl-* 应识别为 vision"""
    from scripts.sync_siliconflow_to_registry import infer_modality
    assert infer_modality("qwen3-vl-32b-instruct") == "vision"
    assert infer_modality("qwen3-vl-8b-instruct") == "vision"
    assert infer_modality("qwen-qwen3-vl-32b-instruct") == "vision"


def test_infer_modality_text_unchanged():
    """text 模型不受 vision 关键词扩展影响"""
    from scripts.sync_siliconflow_to_registry import infer_modality
    assert infer_modality("deepseek-v3") == "text"
    assert infer_modality("qwen-max") == "text"
    assert infer_modality("glm-4-plus") == "text"
    assert infer_modality("minimax-m2.5") == "text"


def test_infer_modality_vl_retrieval_family():
    """*-VL-Embedding-* / *-VL-Reranker-* 归 embedding 而非 vision

    2026-09-30 修复: embedding/reranker 检查须先于 -vl- 关键词,
    否则多模态检索模型被误归 vision, 有被注册进 tier:vision 组
    当 chat 模型路由的风险 (生产库曾误标 qwen3-vl-embedding-8b)。
    """
    from scripts.sync_siliconflow_to_registry import infer_modality
    assert infer_modality("qwen3-vl-embedding-8b") == "embedding"
    assert infer_modality("qwen3-vl-reranker-8b") == "embedding"
    assert infer_modality("Qwen/Qwen3-VL-Embedding-8B") == "embedding"
    # 纯文本 embedding/reranker 不回归
    assert infer_modality("baai-bge-m3") == "embedding"
    assert infer_modality("qwen3-reranker-4b") == "embedding"


def test_convert_sf_model_id_glm_45v():
    """zai-org/GLM-4.5V → family=glm, model_id=glm-4.5v (去重 family 前缀)"""
    from scripts.sync_siliconflow_to_registry import convert_sf_model_id
    fam, mid = convert_sf_model_id("zai-org/GLM-4.5V")
    assert fam == "glm", f"family 应为 glm, 实际 {fam!r}"
    assert mid == "glm-4.5v", f"model_id 应为 glm-4.5v, 实际 {mid!r}"


def test_convert_sf_model_id_qwen_vl():
    """Qwen/Qwen3-VL-32B-Instruct → family=qwen, model_id 含 vl"""
    from scripts.sync_siliconflow_to_registry import convert_sf_model_id
    fam, mid = convert_sf_model_id("Qwen/Qwen3-VL-32B-Instruct")
    assert fam == "qwen"
    assert "vl" in mid and "32b" in mid


# ============================================================
# Fix 3: seed 脚本包含 vision 模型定义
# ============================================================

def test_seed_registry_has_vision_models():
    """seed_model_registry.py 的 MODEL_REGISTRY 包含 glm-4.5v + 4 个 qwen-vl-*"""
    from scripts.seed_model_registry import MODEL_REGISTRY
    model_ids = {row[0] for row in MODEL_REGISTRY}
    # glm-4.5v
    assert "glm-4.5v" in model_ids, "seed 缺少 glm-4.5v"
    # 4 个 qwen-vl-*
    expected_qwen_vl = {
        "qwen3-vl-32b-instruct",
        "qwen3-vl-32b-thinking",
        "qwen3-vl-30b-a3b-thinking",
        "qwen3-vl-8b-instruct",
    }
    missing = expected_qwen_vl - model_ids
    assert not missing, f"seed 缺少 qwen-vl 模型: {missing}"


def test_seed_registry_vision_modality():
    """vision 模型的 modality 字段应为 'vision'"""
    from scripts.seed_model_registry import MODEL_REGISTRY
    by_id = {row[0]: row for row in MODEL_REGISTRY}
    assert by_id["glm-4.5v"][6] == "vision", "glm-4.5v modality 应为 vision"
    assert by_id["qwen3-vl-32b-instruct"][6] == "vision"
    assert by_id["qwen3-vl-8b-instruct"][6] == "vision"


def test_seed_registry_vision_validate_namespace():
    """所有新增 vision 模型通过 validate_model_id 校验"""
    from db.normalizers import validate_model_id, normalize_model_id, normalize_family
    from scripts.seed_model_registry import MODEL_REGISTRY
    for row in MODEL_REGISTRY:
        model_id, family = row[0], row[1]
        mid_n = normalize_model_id(model_id)
        fam_n = normalize_family(family)
        assert validate_model_id(mid_n, fam_n), \
            f"model_id={model_id!r} family={family!r} 未通过 namespace 校验"


def test_seed_groups_vision_members():
    """seed_model_groups.py 的 tier:vision 组 member

    V6.0: glm-4.5v 移除, qwen-vl 系列下沉 vision-light, 双旗舰互备。
    V6.14 (2026-09-27): + minimax-m3.1-flash-preview (90, 首选, 用户拍板) +
    minimax-m3 (100); deepseek-v4-flash-vision-exp 是 V6.0 时代的名字
    (生产已改名 deepseek-flash, 本 seed 行为意图留档, 生产以 db 为准)。
    2026-10-19: m3.1-flash GA 改名并移出 vision → strong, vision 首选回落 m3。
    """
    from scripts.seed_model_groups import GROUP_MEMBERS
    vision_members = [m for m in GROUP_MEMBERS if m[0] == "tier:vision"]
    assert len(vision_members) == 3, f"tier:vision 应有 3 个 member, 实际 {len(vision_members)}"
    model_ids = {m[1] for m in vision_members}
    expected = {
        "minimax-m3",
        "deepseek-v4-flash-vision-exp",
        "glm-5.3-flash",
    }
    assert model_ids == expected, f"tier:vision 成员不匹配: {model_ids ^ expected}"
    # m3.1 已移出 vision (2026-10-19 GA → strong)
    assert all("m3.1" not in m[1] for m in vision_members), \
        "minimax-m3.1-flash 应已移出 tier:vision (2026-10-19 GA 移入 strong)"


def test_seed_groups_strong_m31_flash_first():
    """2026-10-19 GA: minimax-m3.1-flash 进 tier:strong 且提到最前 (用户拍板)

    strong 三成员 0 元订阅边际成本同价, cheapest 策略下由 member_priority 决位:
    m3.1-flash(100) < glm-5.2(108) < glm-5.3-flash(120)。
    """
    from scripts.seed_model_groups import GROUP_MEMBERS
    strong_members = [m for m in GROUP_MEMBERS if m[0] == "tier:strong"]
    model_ids = {m[1] for m in strong_members}
    assert model_ids == {"minimax-m3.1-flash", "glm-5.2", "glm-5.3-flash"}, \
        f"tier:strong 成员不匹配: {model_ids}"
    pri = {m[1]: m[2] for m in strong_members}
    assert pri["minimax-m3.1-flash"] < pri["glm-5.2"], \
        f"m3.1-flash priority ({pri['minimax-m3.1-flash']}) 应小于 glm-5.2 ({pri['glm-5.2']})"
    assert pri["minimax-m3.1-flash"] < pri["glm-5.3-flash"], \
        f"m3.1-flash priority ({pri['minimax-m3.1-flash']}) 应小于 glm-5.3-flash ({pri['glm-5.3-flash']})"


def test_seed_groups_flagship_v4pro_departed():
    """2026-10-19: deepseek-v4-pro 出组 flagship (V4.1-Flash-1017 后拉开差距)

    registry + ch1/modelscope 价行保留, 精确直连不受影响 — 仅组路由移除。
    """
    from scripts.seed_model_groups import GROUP_MEMBERS
    flagship = [m for m in GROUP_MEMBERS if m[0] == "tier:flagship"]
    model_ids = {m[1] for m in flagship}
    assert "deepseek-v4-pro" not in model_ids, \
        f"deepseek-v4-pro 应已出组 flagship, 实际成员: {model_ids}"
    assert model_ids == {"deepseek-flash", "kimi-k3", "glm-5.3"}, \
        f"tier:flagship 成员不匹配: {model_ids}"
    pri = {m[1]: m[2] for m in flagship}
    assert pri["deepseek-flash"] < pri["kimi-k3"] < pri["glm-5.3"], \
        f"flagship 位次应 flash < kimi < glm-5.3, 实际 {pri}"


def test_seed_groups_vision_null_tier_fallback_vision_light():
    """V6.0: tier:vision tier→NULL (glm-5.3-flash T2 免被 tier 过滤判死),
    fallback strong → vision-light (先降轻量视觉再落文本组)"""
    from scripts.seed_model_groups import GROUPS
    by_id = {g[0]: g for g in GROUPS}
    v = by_id["tier:vision"]
    assert v[2] is None, f"tier:vision tier 应为 NULL, 实际 {v[2]!r}"
    assert v[7] == "tier:vision-light", \
        f"tier:vision fallback 应为 tier:vision-light, 实际 {v[7]!r}"


def test_seed_groups_vision_light_group():
    """V6.0: tier:vision-light 组定义 (require_vision=1, tier=NULL, fallback=light)"""
    from scripts.seed_model_groups import GROUPS
    by_id = {g[0]: g for g in GROUPS}
    assert "tier:vision-light" in by_id, "缺少 tier:vision-light 组"
    vl = by_id["tier:vision-light"]
    assert vl[2] is None, "vision-light tier 应为 NULL (混 T1/T3 成员)"
    assert vl[5] == 1, "vision-light require_vision 应为 1"
    assert vl[7] == "tier:light", "vision-light fallback 应为 tier:light"


def test_seed_groups_vision_light_members():
    """V6.0: vision-light 成员 = glm-4.6v-flash (免费兜底) + qwen-vl×4"""
    from scripts.seed_model_groups import GROUP_MEMBERS
    vl = sorted(m[1] for m in GROUP_MEMBERS if m[0] == "tier:vision-light")
    assert vl == sorted([
        "glm-4.6v-flash",
        "qwen3-vl-32b-instruct",
        "qwen3-vl-32b-thinking",
        "qwen3-vl-30b-a3b-thinking",
        "qwen3-vl-8b-instruct",
    ]), f"vision-light 成员不匹配: {vl}"


def test_seed_groups_glm45v_removed_from_all_groups():
    """V6.0 用户决策: glm-4.5v 直接移除出所有分组 (registry 保留, 硅基直连不受影响)"""
    from scripts.seed_model_groups import GROUP_MEMBERS
    in_group = [m for m in GROUP_MEMBERS if m[1] == "glm-4.5v"]
    assert in_group == [], f"glm-4.5v 应不在任何分组, 实际 {in_group}"


def test_seed_groups_light_has_glm47_flash():
    """V6.0: tier:light 补 glm-4.7-flash (文本免费, 补回 V5.1 丢掉的免费车道)"""
    from scripts.seed_model_groups import GROUP_MEMBERS
    light_ids = {m[1] for m in GROUP_MEMBERS if m[0] == "tier:light"}
    assert "glm-4.7-flash" in light_ids


def test_seed_registry_glm_flash_models():
    """V6.0: 新增 glm-4.7-flash / glm-4.6v-flash; glm-5.3-flash 多模态化"""
    from scripts.seed_model_registry import MODEL_REGISTRY
    by_id = {row[0]: row for row in MODEL_REGISTRY}
    assert "glm-4.7-flash" in by_id, "缺少 glm-4.7-flash (bigmodel 文本免费)"
    assert by_id["glm-4.7-flash"][6] == "text"
    assert by_id["glm-4.7-flash"][3] == "T3", "glm-4.7-flash 应为 T3 (tier:light 过滤要求)"
    assert "glm-4.6v-flash" in by_id, "缺少 glm-4.6v-flash (bigmodel 视觉免费)"
    assert by_id["glm-4.6v-flash"][6] == "vision"
    assert by_id["glm-5.3-flash"][6] == "multimodal", \
        "glm-5.3-flash modality 应为 multimodal (进 tier:vision 要求)"


def test_seed_all_group_members_in_registry():
    """跨脚本一致性: 所有组成员都在 MODEL_REGISTRY (V6.0 扩到全组)"""
    from scripts.seed_model_groups import GROUP_MEMBERS
    from scripts.seed_model_registry import MODEL_REGISTRY
    reg_ids = {row[0] for row in MODEL_REGISTRY}
    for member in GROUP_MEMBERS:
        group_id, model_id = member[0], member[1]
        assert model_id in reg_ids, f"{group_id}/{model_id} 不在 seed_model_registry"


def test_seed_groups_vision_members_in_registry():
    """tier:vision 的每个 member 都在 MODEL_REGISTRY 里 (跨脚本一致性)"""
    from scripts.seed_model_groups import GROUP_MEMBERS
    from scripts.seed_model_registry import MODEL_REGISTRY
    reg_ids = {row[0] for row in MODEL_REGISTRY}
    vision_members = [m for m in GROUP_MEMBERS if m[0] == "tier:vision"]
    for _, model_id, _, _, _, _ in vision_members:
        assert model_id in reg_ids, \
            f"tier:vision member {model_id!r} 不在 seed_model_registry"


# ============================================================
# V6.x: tier:light 命名规范化 (qwen-qwen3-8b → qwen3-8b, validator 已放宽)
# ============================================================

def test_tier_light_members_validate_namespace():
    """tier:light 的每个 member 都通过 validate_model_id namespace 校验

    V6.x 语义: validator 放宽后接受 "{family}<版本数字>" 形态,
    规范名是 qwen3-8b / qwen3.5-4b (不再需要 qwen-qwen3-8b 双前缀 workaround)。
    注意 family 推断: qwen3-8b 的第一个 "-" 之前是 "qwen3", normalize_family
    归一后仍以 qwen 开头 — 校验按 family="qwen" 语义走 (startswith 前缀匹配)。
    """
    from scripts.seed_model_groups import GROUP_MEMBERS
    from db.normalizers import validate_model_id, normalize_model_id
    light_members = [m for m in GROUP_MEMBERS if m[0] == "tier:light"]
    for _, model_id, _, _, _, _ in light_members:
        mid_n = normalize_model_id(model_id)
        # family 从 seed_registry 反查 (qwen3-8b 的 family 是 qwen)
        from scripts.seed_model_registry import MODEL_REGISTRY
        fam = next(row[1] for row in MODEL_REGISTRY if row[0] == model_id)
        assert validate_model_id(mid_n, fam), \
            f"tier:light member {model_id!r} 未通过 namespace 校验 (family={fam!r})"


def test_tier_light_qwen_canonical_names():
    """V6.x 反转 P2-B: tier:light 的 qwen 模型用规范名 (qwen3-8b, 无双前缀)"""
    from scripts.seed_model_groups import GROUP_MEMBERS
    light_ids = {m[1] for m in GROUP_MEMBERS if m[0] == "tier:light"}
    assert "qwen3-8b" in light_ids, "tier:light 应有 qwen3-8b (V6.x 规范名)"
    assert "qwen3.5-4b" in light_ids, "tier:light 应有 qwen3.5-4b"
    assert "qwen-qwen3-8b" not in light_ids, "不应再有 qwen-qwen3-8b 双前缀 (V6.x 已规范化)"
    assert "qwen-qwen3.5-4b" not in light_ids, "不应再有 qwen-qwen3.5-4b 双前缀"


# ============================================================
# Fix 1: require_vision 接受 multimodal (router.py 源码校验)
# ============================================================

def test_require_vision_accepts_multimodal():
    """require_vision 过滤用 not in (vision, multimodal) 而非 != vision

    V6.17d: 判定抽到 gateway/group_rules.group_runtime_accepts (路由层与
    看板候选列表共用), 这里校验单一事实源本身 + 路由层确实走它。
    """
    from gateway.router import RouteEngine
    import inspect
    from gateway import group_rules

    rules_src = inspect.getsource(group_rules)
    # 关键: 不再用 != "vision" 拒绝 multimodal
    assert 'not in ("vision", "multimodal")' in rules_src, \
        "require_vision 过滤器应接受 (vision, multimodal), 检查 gateway/group_rules.py"
    router_src = inspect.getsource(RouteEngine._select_by_group)
    assert "group_runtime_accepts" in router_src, \
        "路由层组过滤应调用 gateway/group_rules.group_runtime_accepts"


# ============================================================
# 2026-10-19: minimax-m3.1-flash GA → tier:strong 首位; vision 组 m3 首选回归
# ============================================================

def _reg_entry(model_id, tier, modality):
    class _Reg:
        def __init__(self):
            self.model_id = model_id
            self.tier = tier
            self.modality = modality
            self.status = "active"
            self.family = model_id.split("-")[0]
            self.capabilities = '["reasoning", "code", "general"]'
            self.context_length = 1000000
    return _Reg()


def _build_group_engine(group_id, fallback_group, member_priorities,
                        channels, registry_model_ids):
    """构造组选型用的 RouteEngine (mock 缓存)

    member_priorities: {model_id: priority}
    channels: 渠道 dict 列表 (含 prices, 0 元价行)
    registry_model_ids: 进 _registry_cache 的 model_id 集合 —
    生产 _load_groups_cache 只拉 status='active' 行, 用集合模拟该语义。
    """
    from gateway.router import RouteEngine

    eng = RouteEngine()
    eng._channels_cache = channels

    all_regs = {
        "minimax-m3": _reg_entry("minimax-m3", "T2", "multimodal"),
        "minimax-m3.1-flash": _reg_entry("minimax-m3.1-flash", "T1", "multimodal"),
        "deepseek-flash": _reg_entry("deepseek-flash", "T0", "multimodal"),
        "glm-5.2": _reg_entry("glm-5.2", "T1", "text"),
    }
    eng._registry_cache = {k: v for k, v in all_regs.items() if k in registry_model_ids}

    class _Group:
        pass

    grp = _Group()
    grp.group_id = group_id
    grp.tier = None
    grp.capabilities = None
    grp.min_context = 0
    grp.require_vision = 1 if group_id.startswith("tier:vision") else 0
    grp.strategy = "cheapest"
    grp.fallback_group = fallback_group
    grp.enabled = 1

    eng._groups_cache = {group_id: grp}
    eng._group_members_cache = {group_id: list(member_priorities.keys())}
    eng._member_priority_cache = {group_id: dict(member_priorities)}
    return eng


def _price_row(model, raw=None, channel_id=6, channel_name="minimax_compat"):
    return {
        "model": model, "price_type": "normal", "time_range": "全天",
        "input_price": 0.0, "output_price": 0.0, "cached_input_price": None,
        "cache_write_price": None, "is_promotion": None,
        "raw_model_id": raw or model,
    }


def _channel(channel_id, name, prices, priority=5):
    return {
        "id": channel_id, "name": name, "provider": name.split("_")[0],
        "priority": priority, "billing_mode": "pay_per_token",
        "account_id": channel_id * 10, "quota_remaining": None, "quota_total": None,
        "quotas": [], "prices": prices,
    }


_VISION_IMAGE_MESSAGES = [{
    "role": "user",
    "content": [
        {"type": "text", "text": "What color? one word"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
    ],
}]


def test_strong_group_m31_flash_first_multi_channel():
    """2026-10-19 GA: tier:strong 纯文本请求 minimax-m3.1-flash(100) 跨渠道首选

    strong 三成员 0 元同价跨三渠道 (minimax_compat/sensenova/glm_bigmodel),
    V5.1 排序键 (price, family_bonus, member_priority, -channel.priority)
    同价由 member_priority 决位 — 用户拍板"优先使用 m3.1, 提到最前"。
    """
    eng = _build_group_engine(
        "tier:strong", "tier:balanced",
        {"minimax-m3.1-flash": 100, "glm-5.2": 108, "glm-5.3-flash": 120},
        [
            _channel(6, "minimax_compat",
                     [_price_row("minimax-m3.1-flash")]),
            _channel(2, "sensenova_official", [_price_row("glm-5.2")]),
            _channel(8, "glm_bigmodel", [_price_row("glm-5.3-flash")], priority=8),
        ],
        {"minimax-m3.1-flash", "glm-5.2", "glm-5.3-flash"},
    )
    best = eng._select_by_group("tier:strong", prefs={}, messages=[{"role": "user", "content": "hi"}])
    assert best is not None, "tier:strong 应有可用候选"
    assert best.get("_registry_model") == "minimax-m3.1-flash", \
        f"strong 组应首选 m3.1-flash (priority=100), 实际 {best.get('_registry_model')}"
    assert best["name"] == "minimax_compat"


def test_vision_group_member_priority_selection():
    """2026-10-19: m3.1 移出 vision 后, 带图请求首选回落 m3(100) 优于 deepseek-flash(120)

    m3/deepseek-flash 价行同 0 元同渠道, V5.1 排序键同价由 priority 决位。
    """
    eng = _build_group_engine(
        "tier:vision", "tier:vision-light",
        {"minimax-m3": 100, "deepseek-flash": 120},
        [_channel(6, "minimax_compat",
                  [_price_row("minimax-m3"), _price_row("deepseek-flash")])],
        {"minimax-m3", "deepseek-flash"},
    )
    best = eng._select_by_group("tier:vision", prefs={}, messages=_VISION_IMAGE_MESSAGES)
    assert best is not None, "tier:vision 应有可用候选"
    assert best.get("_registry_model") == "minimax-m3", \
        f"带图请求应首选 m3 (priority=100 < 120), 实际 {best.get('_registry_model')}"
    assert best["name"] == "minimax_compat"


def test_vision_group_skips_non_active_registry_member():
    """V6.14 实证坑回归锚点: registry 缓存无该成员 (=status 非 active) 时组路由跳过

    生产 _load_groups_cache 只拉 status='active'; 非 active 成员曾被
    当"不在 model_registry"跳过 (e2e members=3/candidates=2 实证)。本测试钉住:
    该语义下次位成员兜底胜出 — 若未来放行非 active 入组, 应连同
    _load_groups_cache 一起改并更新本测试。
    """
    eng = _build_group_engine(
        "tier:vision", "tier:vision-light",
        {"minimax-m3": 100, "deepseek-flash": 120},
        [_channel(6, "minimax_compat",
                  [_price_row("minimax-m3"), _price_row("deepseek-flash")])],
        {"deepseek-flash"},  # m3 不在缓存 (=非 active)
    )
    best = eng._select_by_group("tier:vision", prefs={}, messages=_VISION_IMAGE_MESSAGES)
    assert best is not None
    assert best.get("_registry_model") == "deepseek-flash", \
        f"m3 不在 registry 缓存时应由 deepseek-flash 兜底, 实际 {best.get('_registry_model')}"


# ============================================================
# Fix 5: _resolve_actual_model 精确路由用 _channel_model 兜底
# ============================================================

def test_resolve_model_exact_route_uses_channel_model():
    """精确路由: channel 有 _channel_model 时优先用它 (而非 requested_model)

    场景: 用户请求 glm-4.5v (不在 registry), router fuzzy match 到
    zai-org/GLM-4.5V, 注入 _channel_model. 上游 400 时 response 无 model 字段,
    usage_logs.model 应记 zai-org/GLM-4.5V (而非空).
    """
    from gateway.proxy import _resolve_actual_model
    channel = {"_channel_model": "zai-org/GLM-4.5V"}
    result = _resolve_actual_model(channel, "glm-4.5v", fallback_model="")
    assert result == "zai-org/GLM-4.5V", \
        f"精确路由应优先 _channel_model, 实际 {result!r}"


def test_resolve_model_exact_route_no_channel_model():
    """精确路由: 无 _channel_model 时用 requested_model"""
    from gateway.proxy import _resolve_actual_model
    channel = {}
    assert _resolve_actual_model(channel, "deepseek-v3") == "deepseek-v3"


def test_resolve_model_exact_route_empty_all():
    """精确路由: _channel_model / requested_model / fallback 全空 -> 空字符串"""
    from gateway.proxy import _resolve_actual_model
    channel = {}
    assert _resolve_actual_model(channel, "", fallback_model="") == ""


def test_resolve_model_group_route_still_uses_registry_model():
    """组路由: 仍优先 _registry_model (P2-A 不改变组路由行为)"""
    from gateway.proxy import _resolve_actual_model
    channel = {
        "_registry_model": "glm-4.5v",
        "_channel_model": "zai-org/GLM-4.5V",
    }
    assert _resolve_actual_model(channel, "tier:vision") == "glm-4.5v"


def test_resolve_model_exact_route_channel_model_over_response():
    """精确路由: _channel_model 优先于上游返回的 fallback_model"""
    from gateway.proxy import _resolve_actual_model
    channel = {"_channel_model": "zai-org/GLM-4.5V"}
    assert _resolve_actual_model(channel, "glm-4.5v", fallback_model="some-upstream-model") \
        == "zai-org/GLM-4.5V"


# ============================================================
# Fix 4: 组路由 400 自动 failover (routes.py 源码校验)
# ============================================================

def test_routes_400_failover_for_group():
    """routes.py 源码: 组路由 400 参与 failover

    校验 routes.py 的重试逻辑包含对 tier:/scene: 前缀 400 的 failover.
    """
    import gateway.routes as routes_mod
    import inspect
    src = inspect.getsource(routes_mod)
    # 关键: 400 对组路由也要 failover
    assert "is_group_route" in src, "routes.py 应有 is_group_route 判断"
    assert 'response.status_code == 400' in src, \
        "routes.py 应对 400 做组路由 failover 判断"


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 60)
    print("P2-A: tier:vision 400 专项修复测试")
    print("=" * 60)

    tests = [
        ("infer_modality glm-4.5v", test_infer_modality_glm_45v),
        ("infer_modality qwen-vl", test_infer_modality_qwen_vl),
        ("infer_modality text 不变", test_infer_modality_text_unchanged),
        ("infer_modality VL 检索族归 embedding", test_infer_modality_vl_retrieval_family),
        ("convert_sf_model_id glm-4.5v", test_convert_sf_model_id_glm_45v),
        ("convert_sf_model_id qwen-vl", test_convert_sf_model_id_qwen_vl),
        ("seed registry 有 vision 模型", test_seed_registry_has_vision_models),
        ("seed registry vision modality", test_seed_registry_vision_modality),
        ("seed registry vision namespace", test_seed_registry_vision_validate_namespace),
        ("seed groups vision members", test_seed_groups_vision_members),
        ("seed groups strong m3.1 首位 (2026-10-19)", test_seed_groups_strong_m31_flash_first),
        ("seed groups flagship v4-pro 出组 (2026-10-19)", test_seed_groups_flagship_v4pro_departed),
        ("seed groups vision tier/fallback (V6.0)", test_seed_groups_vision_null_tier_fallback_vision_light),
        ("seed groups vision-light 组定义 (V6.0)", test_seed_groups_vision_light_group),
        ("seed groups vision-light 成员 (V6.0)", test_seed_groups_vision_light_members),
        ("seed groups glm-4.5v 移除 (V6.0)", test_seed_groups_glm45v_removed_from_all_groups),
        ("seed groups light+glm-4.7-flash (V6.0)", test_seed_groups_light_has_glm47_flash),
        ("seed registry glm flash 免费模型 (V6.0)", test_seed_registry_glm_flash_models),
        ("seed 全组成员在 registry (V6.0)", test_seed_all_group_members_in_registry),
        ("seed groups vision 在 registry", test_seed_groups_vision_members_in_registry),
        ("require_vision 接受 multimodal", test_require_vision_accepts_multimodal),
        ("strong 组 m3.1 跨渠道首选 (2026-10-19)", test_strong_group_m31_flash_first_multi_channel),
        ("vision 组 m3 首选回归 (2026-10-19)", test_vision_group_member_priority_selection),
        ("组路由跳过非 active registry 成员 (V6.14 坑)", test_vision_group_skips_non_active_registry_member),
        ("resolve model 精确路由用 _channel_model", test_resolve_model_exact_route_uses_channel_model),
        ("resolve model 精确路由无 _channel_model", test_resolve_model_exact_route_no_channel_model),
        ("resolve model 精确路由全空", test_resolve_model_exact_route_empty_all),
        ("resolve model 组路由不变", test_resolve_model_group_route_still_uses_registry_model),
        ("resolve model _channel_model 优先", test_resolve_model_exact_route_channel_model_over_response),
        ("routes 400 组路由 failover", test_routes_400_failover_for_group),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK] {name}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  [ERROR] {name}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"Result: {passed} passed, {failed} failed, {len(tests)} total")
    print(f"{'=' * 60}")
    if failed:
        print("\n结论: 有失败项, 需修复")
        sys.exit(1)
    else:
        print("\n结论:")
        print("- Fix 1: require_vision 过滤器接受 (vision, multimodal)")
        print("- Fix 2: infer_modality 识别 glm-4.5v / qwen-vl-* 为 vision")
        print("- Fix 3: seed 脚本包含 5 个 vision 模型 + tier:vision 6 个成员")
        print("- Fix 4: 组路由 400 自动 failover")
        print("- Fix 5: 精确路由 _resolve_actual_model 用 _channel_model 兜底")


if __name__ == "__main__":
    main()
