"""V4.6.4+: 测试 /v1/models 模式 2 (默认只 group + ?include_models=true)

修复前 (V4.6.3 之前): /v1/models 只返回精确 model, agent SDK 严格模式
调 tier:xxx 报 "model not in model list" 错.
修复后: 默认只返回 group, agent 直接用 tier:flagship 等.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from gateway.router import RouteEngine


class FakeGroup:
    def __init__(self, gid, **kwargs):
        self.group_id = gid
        self.fallback_group = kwargs.get("fallback_group")
        self.capabilities = kwargs.get("capabilities")
        self.display_name = kwargs.get("display_name", gid)
        self.tier = kwargs.get("tier", "T1")
        self.strategy = kwargs.get("strategy", "cheapest")
        self.enabled = kwargs.get("enabled", True)
        self.min_context = kwargs.get("min_context", 0)
        self.require_vision = kwargs.get("require_vision", False)
        self.sort_order = kwargs.get("sort_order", 0)
        # 模拟 dict() 返回
        self.created_at = None


class FakeRegistry:
    def __init__(self, mid, **kwargs):
        self.model_id = mid
        self.family = kwargs.get("family", "test")
        self.tier = kwargs.get("tier", "T1")
        self.capabilities = kwargs.get("capabilities", "[]")
        self.context_length = kwargs.get("context_length", 4096)
        self.modality = kwargs.get("modality", "text")
        self.is_open_source = kwargs.get("is_open_source", True)
        self.release_date = kwargs.get("release_date", "2025-01-01")
        self.status = kwargs.get("status", "active")


def _make_engine(groups, members, registry):
    """构造 mock RouteEngine with only cache attrs we need."""
    eng = RouteEngine.__new__(RouteEngine)
    eng._groups_cache = {g.group_id: g for g in groups}
    eng._group_members_cache = members
    eng._registry_cache = {r.model_id: r for r in registry}
    eng._channels_cache = []  # get_models_with_metadata 需要
    return eng


def test_default_only_groups():
    """默认 /v1/models 只返回 group, 不暴露精确 model"""
    eng = _make_engine(
        groups=[FakeGroup("tier:flagship", tier="T1"),
                FakeGroup("scene:code", tier="T2")],
        members={"tier:flagship": ["deepseek-v4-pro"], "scene:code": ["qwen-coder"]},
        registry=[FakeRegistry("deepseek-v4-pro"), FakeRegistry("qwen-coder")],
    )
    data = eng.get_models_for_openai_sdk(include_models=False)
    # 只有 2 个 group
    assert len(data) == 2, f"expected 2 groups, got {len(data)}"
    # 全部 type=group
    for m in data:
        assert m["aimc_metadata"]["type"] == "group"
        assert m["object"] == "model"  # OpenAI SDK 期望
        assert m["owned_by"] == "aimc"
    # id 包含 tier: 和 scene:
    ids = [m["id"] for m in data]
    assert "tier:flagship" in ids
    assert "scene:code" in ids


def test_include_models_returns_both():
    """?include_models=true 同时返回 group 和精确 model"""
    eng = _make_engine(
        groups=[FakeGroup("tier:flagship", tier="T1")],
        members={"tier:flagship": ["deepseek-v4-pro"]},
        registry=[FakeRegistry("deepseek-v4-pro", family="deepseek")],
    )
    data = eng.get_models_for_openai_sdk(include_models=True)
    # 1 group + 1 model = 2
    assert len(data) == 2
    types = sorted([m["aimc_metadata"]["type"] for m in data])
    assert types == ["group", "model"]
    # group 排在前面 (用户主用 group)
    assert data[0]["aimc_metadata"]["type"] == "group"
    assert data[1]["aimc_metadata"]["type"] == "model"


def test_group_metadata_includes_members_and_fallback():
    """group metadata 含 members + fallback_chain, 方便 agent debug"""
    eng = _make_engine(
        groups=[FakeGroup("tier:flagship", fallback_group="tier:strong", tier="T1"),
                FakeGroup("tier:strong", tier="T2")],
        members={"tier:flagship": ["deepseek-v4-pro", "glm-5.1"],
                 "tier:strong": ["qwen-v4-flash"]},
        registry=[FakeRegistry("deepseek-v4-pro"), FakeRegistry("glm-5.1"),
                  FakeRegistry("qwen-v4-flash")],
    )
    data = eng.get_models_for_openai_sdk(include_models=False)
    flags = data[0]
    meta = flags["aimc_metadata"]
    assert meta["type"] == "group"
    assert meta["tier"] == "T1"
    assert meta["fallback_group"] == "tier:strong"
    assert meta["fallback_chain"] == ["tier:flagship", "tier:strong"]
    assert set(meta["members"]) == {"deepseek-v4-pro", "glm-5.1"}
    assert meta["member_count"] == 2


def test_empty_registry():
    """没有任何 group 时返空数组 (不崩)"""
    eng = _make_engine(groups=[], members={}, registry=[])
    data = eng.get_models_for_openai_sdk(include_models=False)
    assert data == []
    data2 = eng.get_models_for_openai_sdk(include_models=True)
    assert data2 == []


def test_group_member_tiers():
    """aimc_metadata.member_tiers 反映每个 member 的 tier"""
    eng = _make_engine(
        groups=[FakeGroup("tier:flagship")],
        members={"tier:flagship": ["model-a", "model-b"]},
        registry=[FakeRegistry("model-a", tier="T1"), FakeRegistry("model-b", tier="T2")],
    )
    data = eng.get_models_for_openai_sdk(include_models=False)
    meta = data["tier:flagship" if False else 0]["aimc_metadata"]
    # get_groups_with_metadata 会读 registry 给每个 member 找 tier
    assert sorted(meta["member_tiers"]) == ["T1", "T2"]


def test_openai_compatibility_shape():
    """响应含 OpenAI 标准字段 (id/object/owned_by/created)"""
    eng = _make_engine(
        groups=[FakeGroup("tier:flagship", tier="T1")],
        members={"tier:flagship": ["m1"]},
        registry=[FakeRegistry("m1")],
    )
    data = eng.get_models_for_openai_sdk(include_models=False)
    flags = data[0]
    # OpenAI 标准字段
    assert flags["id"] == "tier:flagship"
    assert flags["object"] == "model"
    assert flags["owned_by"] == "aimc"
    assert isinstance(flags["created"], int)  # UNIX timestamp
    assert flags["created"] > 0
    # AIMC 扩展
    assert "aimc_metadata" in flags
    assert flags["aimc_metadata"]["type"] == "group"