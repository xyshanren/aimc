"""Phase 2E 聚合透传闸门测试 (2026-10-29)

背景: V6.18 exclude_pairs 把组内唯一价行渠道排除后, select_best_channel 候选
清空 → Phase 2E 兜底走「无 fuzzy 价行就透传模型名」分支, 把只有特定渠道才有的
模型透传给聚合平台, 必 400 (生产实录: tier:balanced 的 sensenova-6.8-flash-lite
→ siliconflow 20012, siliconflow 被误冷却 1800s + needs_human 假告警, ~30min 重撞)。

修复: 模型在任一渠道已有价行时跳过透传; 零渠道定价的纯新模型发现场景保留。

测试方法 (同 test_null_price_routing.py):
- 不依赖真实 DB / uvicorn, 直接构造 RouteEngine._channels_cache 内存 mock
- 调 select_best_channel 验证选择结果

跑法:
    python -m pytest tests/test_phase2e_transparent_pass_gate.py -v
"""
import sys
from pathlib import Path

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


def _channel(cid, name, priority, account_id, prices):
    return {
        "id": cid,
        "name": name,
        "provider": name.split("_")[0],
        "priority": priority,
        "provider_type": "aggregator" if priority <= 1 else "official",
        "billing_mode": "pay_per_token",
        "account_id": account_id,
        "account_name": f"{name}-acc",
        "api_key": "sk-mock",
        "quota_total": None,
        "quota_used": 0,
        "quota_remaining": None,
        "prices": prices,
        "quotas": [],
    }


def _price(model, raw=None, inp=0.0, out=0.0):
    return {"model": model, "raw_model_id": raw or model,
            "price_type": "normal", "time_range": "全天",
            "input_price": inp, "output_price": out}


def _engine(channels):
    from gateway.router import RouteEngine
    engine = RouteEngine()
    engine._channels_cache = channels
    engine._registry_cache = {}
    return engine


def test_priced_model_skips_transparent_pass():
    """唯一价行渠道被 exclude_pairs 排除 → 不透传聚合平台, 返回 None (修复点)"""
    channels = [
        _channel(1, "siliconflow_aggregator", 1, 11, [_price("some-unrelated-model")]),
        _channel(2, "sensenova_official", 5, 12, [_price("sensenova-6.8-flash-lite")]),
    ]
    engine = _engine(channels)
    out = engine.select_best_channel(
        "sensenova-6.8-flash-lite", exclude_pairs={(12, "sensenova-6.8-flash-lite")})
    assert out is None, "有价行模型被排除后应返回 None, 而不是透传给聚合平台"


def test_priced_model_routes_normally_without_exclusion():
    """不排除时照常路由到价行渠道 (证明 None 只由排除触发, 未误伤正常路由)"""
    channels = [
        _channel(1, "siliconflow_aggregator", 1, 11, [_price("some-unrelated-model")]),
        _channel(2, "sensenova_official", 5, 12, [_price("sensenova-6.8-flash-lite")]),
    ]
    engine = _engine(channels)
    out = engine.select_best_channel("sensenova-6.8-flash-lite")
    assert out is not None and out["name"] == "sensenova_official"


def test_unpriced_model_still_transparent_pass():
    """零渠道定价的纯新模型 → 透传保留 (发现场景行为不变)"""
    channels = [_channel(1, "siliconflow_aggregator", 1, 11, [_price("some-unrelated-model")])]
    engine = _engine(channels)
    out = engine.select_best_channel("totally-new-thing")
    assert out is not None, "纯新模型仍应走聚合透传"
    assert out["name"] == "siliconflow_aggregator"
    assert out["_channel_model"] == "totally-new-thing"


def test_fuzzy_branch_preserved():
    """聚合平台有 fuzzy 价行 → 走 raw id 改写分支, 不受闸门影响 (V4.6.4 场景)"""
    channels = [_channel(1, "siliconflow_aggregator", 1, 11,
                         [_price("zai-org/GLM-4.5V", raw="zai-org/GLM-4.5V", inp=2.0, out=8.0)])]
    engine = _engine(channels)
    out = engine.select_best_channel("glm-4.5v")
    assert out is not None, "fuzzy 价行命中应正常兜底"
    assert out["_channel_model"] == "zai-org/GLM-4.5V"
