"""3.E 响应头增强 — 单元测试 (无 fastapi 依赖, 直接测 _build_aimc_headers)"""
import json
import sys
import os

# 把项目根加到 path, 但 mock 掉 fastapi/httpx 以便测试
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Mock fastapi 和 httpx (这两个仅在 proxy.py import 时用到)
class _MockFastAPI:
    class responses:
        class StreamingResponse: pass
        class JSONResponse: pass
        class Response: pass
class _MockStarlette:
    class datastructures:
        class Headers: pass
sys.modules['fastapi'] = _MockFastAPI()
sys.modules['fastapi.responses'] = _MockFastAPI.responses
sys.modules['starlette.datastructures'] = _MockStarlette.datastructures

# 同样 mock httpx
class _MockHttpx:
    class TimeoutException(Exception): pass
    class ConnectError(Exception): pass
    class Response: pass
sys.modules['httpx'] = _MockHttpx()

# 现在 import proxy
import importlib.util
spec = importlib.util.spec_from_file_location("proxy", os.path.join(PROJECT_ROOT, "gateway", "proxy.py"))
proxy = importlib.util.module_from_spec(spec)

# Mock config 模块
class _MockSettings:
    pass
sys.modules['config'] = type(sys)('config')
sys.modules['config'].settings = _MockSettings()

spec.loader.exec_module(proxy)

# ============================================================
# 单元测试
# ============================================================
def test_group_route_with_registry():
    """组路由 + 有 registry: 5 个 header 全部存在"""
    channel = {
        "name": "siliconflow_aggregator",
        "_actual_model": "qwen3-32b",
        "_group_id": "tier:balanced",
        "_registry_tier": "T2",
        "_registry_capabilities": '["reasoning","code"]'
    }
    h = proxy._build_aimc_headers(channel, "tier:balanced")
    print(f"Test 1 (组路由 + registry): {h}")
    assert h["X-AIMC-Actual-Model"] == "qwen3-32b"
    assert h["X-AIMC-Actual-Channel"] == "siliconflow_aggregator"
    assert h["X-AIMC-Group-Id"] == "tier:balanced"
    assert h["X-AIMC-Actual-Tier"] == "T2"
    assert h["X-AIMC-Actual-Capabilities"] == '["reasoning", "code"]'
    print("  PASS")


def test_exact_route_with_registry():
    """精确路由 + 有 registry: 4 个 header (无 X-AIMC-Group-Id)"""
    channel = {
        "name": "deepseek_official",
        "_actual_model": "deepseek-v4-flash",
        "_registry_tier": "T2",
        "_registry_capabilities": '["code","math"]'
    }
    h = proxy._build_aimc_headers(channel, "deepseek-v4-flash")
    print(f"Test 2 (精确路由 + registry): {h}")
    assert h["X-AIMC-Actual-Model"] == "deepseek-v4-flash"
    assert h["X-AIMC-Actual-Tier"] == "T2"
    assert h["X-AIMC-Actual-Capabilities"] == '["code", "math"]'
    assert "X-AIMC-Group-Id" not in h
    print("  PASS")


def test_exact_route_without_registry():
    """精确路由 + 无 registry: 只有 2 个 header"""
    channel = {
        "name": "siliconflow_aggregator",
        "_actual_model": "some-new-model",
    }
    h = proxy._build_aimc_headers(channel, "some-new-model")
    print(f"Test 3 (精确路由 无 registry): {h}")
    assert h["X-AIMC-Actual-Model"] == "some-new-model"
    assert h["X-AIMC-Actual-Channel"] == "siliconflow_aggregator"
    assert "X-AIMC-Actual-Tier" not in h
    assert "X-AIMC-Actual-Capabilities" not in h
    print("  PASS")


def test_caps_invalid_json():
    """capabilities JSON 解析失败 → fallback []"""
    channel = {
        "name": "test",
        "_registry_capabilities": "not-valid-json{",
    }
    h = proxy._build_aimc_headers(channel, "test-model")
    print(f"Test 4 (caps 解析失败): {h}")
    assert h["X-AIMC-Actual-Capabilities"] == "[]"
    print("  PASS")


def test_caps_dict_not_list():
    """capabilities 是 dict 不是 list → fallback []"""
    channel = {
        "name": "test",
        "_registry_capabilities": '{"a": 1}',
    }
    h = proxy._build_aimc_headers(channel, "test-model")
    print(f"Test 5 (caps 是 dict): {h}")
    assert h["X-AIMC-Actual-Capabilities"] == "[]"
    print("  PASS")


def test_caps_chinese():
    """capabilities 含中文 (ensure_ascii=False)"""
    channel = {
        "name": "test",
        "_registry_capabilities": '["推理","代码"]'
    }
    h = proxy._build_aimc_headers(channel, "test-model")
    print(f"Test 6 (caps 中文): {h}")
    assert h["X-AIMC-Actual-Capabilities"] == '["推理", "代码"]'
    print("  PASS")


def test_pollution_protection():
    """组路由后精确路由命中同一个 channel dict, 不会被残留污染"""
    # 模拟: 第一次组路由命中 channel A, 第二次精确路由也命中 channel A
    channel = {
        "name": "siliconflow_aggregator",
        # 残留的组路由字段 (来自前一次请求)
        "_actual_model": "qwen3-32b",  # 上次组路由
        "_group_id": "tier:balanced",  # 上次组路由
        "_registry_tier": "T2",
        "_registry_capabilities": '["reasoning"]'
    }
    # 第二次精确路由请求 deepseek-v4-flash, requested_model = deepseek-v4-flash
    # (select_best_channel 注入 _actual_model = "deepseek-v4-flash")
    channel["_actual_model"] = "deepseek-v4-flash"  # 新覆盖
    h = proxy._build_aimc_headers(channel, "deepseek-v4-flash")
    print(f"Test 7 (污染保护): {h}")
    assert h["X-AIMC-Actual-Model"] == "deepseek-v4-flash"
    assert "X-AIMC-Group-Id" not in h  # 不应触发
    assert h["X-AIMC-Actual-Tier"] == "T2"
    print("  PASS")


if __name__ == "__main__":
    test_group_route_with_registry()
    test_exact_route_with_registry()
    test_exact_route_without_registry()
    test_caps_invalid_json()
    test_caps_dict_not_list()
    test_caps_chinese()
    test_pollution_protection()
    print("\n=== ALL 7 TESTS PASSED ===")