"""V6.0: P0-3 收窄 — 纯文本跳过 multimodal 仅在 require_vision=1 组内生效

背景 (docs/next-major-v6-plan.md §1.3):
- glm-5.3-flash modality 改 multimodal 后同时在 strong/code/vision 三组
- 原全局规则把纯文本请求里的 multimodal 成员在所有组跳过 → 它在
  strong/code 的文本角色被打断 (用户明确要求: 多分组模型不跳文本)
- 收窄: 仅 group.require_vision=1 时跳过 (vision 组内保留保护)

测试方法: mock caches 直接调 _select_by_group (同 test_v497 模式, 不依赖 db)

跑法:
    .venv/Scripts/python.exe -m pytest tests/test_v60_p03_narrow.py -v
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_config():
    """mock config.settings 让 router.py 能 import"""
    import types

    class _MockSettings:
        aimc_master_key = "0" * 64

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


_install_mock_config()


def _channel(name="glm_bigmodel", model="glm-5.3-flash", price=0.0):
    """单 channel entry, 跟 router._build_channel_entries 构造格式一致"""
    fake_key = "sk-" + "mock" + "-key"  # 拼接避免字面量 (扫描器防硬编码凭据)
    return {
        "id": 2,
        "name": name,
        "provider": "glm",
        "priority": 8,
        "provider_type": "official",
        "billing_mode": "subscription",
        "account_id": 2,
        "account_name": "glm_01",
        "api_key": fake_key,
        "quota_total": None,
        "quota_used": 0,
        "quota_remaining": None,
        "prices": [
            {"model": model, "price_type": "normal", "time_range": "全天",
             "input_price": price, "output_price": price},
        ],
        "quotas": [],
    }


def _reg(model_id, tier="T2", modality="multimodal", family="glm"):
    """mock model_registry ORM (只含组路由用到的字段)"""
    return SimpleNamespace(
        model_id=model_id, tier=tier, family=family, modality=modality,
        context_length=128000, capabilities=json.dumps(["general", "code"]),
    )


def _engine(groups, members, registries, channels):
    from gateway.router import RouteEngine
    engine = RouteEngine()
    engine._channels_cache = channels
    engine._registry_cache = registries
    engine._groups_cache = groups
    engine._group_members_cache = members
    return engine


def _groups():
    """strong (require_vision=0) + vision (require_vision=1), tier 全 NULL"""
    def _grp(gid, require_vision=0, fallback=None):
        return SimpleNamespace(
            group_id=gid, tier=None, capabilities=None, min_context=0,
            require_vision=require_vision, strategy="cheapest",
            fallback_group=fallback, enabled=1,
        )

    return {
        "tier:strong": _grp("tier:strong", 0, None),
        "tier:vision": _grp("tier:vision", 1, None),
    }


PURE_TEXT = [{"role": "user", "content": "hi"}]
WITH_IMAGE = [{"role": "user", "content": [
    {"type": "text", "text": "what is this"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
]}]


def test_multimodal_not_skipped_in_text_group():
    """核心: multimodal 成员在 require_vision=0 组 (strong) 纯文本不跳过

    收窄前: 全局规则跳过 → glm-5.3-flash 在 strong/code 的文本角色被打断
    """
    ch = _channel()
    engine = _engine(
        _groups(),
        {"tier:strong": ["glm-5.3-flash"]},
        {"glm-5.3-flash": _reg("glm-5.3-flash")},
        [ch],
    )
    best = engine._select_by_group("tier:strong", messages=PURE_TEXT)
    assert best is not None, "multimodal 成员在非 vision 组被纯文本跳过 (P0-3 收窄失效)"
    assert best["_registry_model"] == "glm-5.3-flash"


def test_multimodal_skipped_in_vision_group_for_text():
    """vision 组 (require_vision=1) 纯文本仍跳过 multimodal 成员"""
    ch = _channel()
    engine = _engine(
        _groups(),
        {"tier:vision": ["glm-5.3-flash"]},
        {"glm-5.3-flash": _reg("glm-5.3-flash")},
        [ch],
    )
    best = engine._select_by_group("tier:vision", messages=PURE_TEXT)
    assert best is None, "vision 组纯文本应跳过 multimodal 成员 (保护未保留)"


def test_multimodal_selected_in_vision_group_for_image():
    """vision 组带图请求: multimodal 成员正常选中 (require_vision 过滤放行)"""
    ch = _channel()
    engine = _engine(
        _groups(),
        {"tier:vision": ["glm-5.3-flash"]},
        {"glm-5.3-flash": _reg("glm-5.3-flash")},
        [ch],
    )
    best = engine._select_by_group("tier:vision", messages=WITH_IMAGE)
    assert best is not None, "vision 组带图请求应选中 multimodal 成员"
    assert best["_registry_model"] == "glm-5.3-flash"


def test_vision_member_takes_text_over_multimodal_in_vision_group():
    """vision 组纯文本: multimodal 跳过后 vision 模态成员顶上 (组内保护场景)"""
    ch = _channel()
    engine = _engine(
        _groups(),
        {"tier:vision": ["glm-5.3-flash", "deepseek-v4-flash-vision-exp"]},
        {
            "glm-5.3-flash": _reg("glm-5.3-flash", modality="multimodal"),
            "deepseek-v4-flash-vision-exp": _reg(
                "deepseek-v4-flash-vision-exp", modality="vision"),
        },
        [ch],
    )
    best = engine._select_by_group("tier:vision", messages=PURE_TEXT)
    assert best is not None
    assert best["_registry_model"] == "deepseek-v4-flash-vision-exp", (
        "vision 组纯文本应落 vision 模态成员而非 multimodal"
    )


def test_p03_source_narrowed():
    """源码校验: P0-3 跳过条件以 group.require_vision 为前置"""
    import inspect

    from gateway.router import RouteEngine
    src = inspect.getsource(RouteEngine._select_by_group)
    assert "group.require_vision and not has_mm and registry.modality" in src, \
        "P0-3 跳过条件应包含 group.require_vision 前置 (router.py)"


if __name__ == "__main__":
    test_multimodal_not_skipped_in_text_group()
    test_multimodal_skipped_in_vision_group_for_text()
    test_multimodal_selected_in_vision_group_for_image()
    test_vision_member_takes_text_over_multimodal_in_vision_group()
    test_p03_source_narrowed()
    print("all 5 tests passed")
