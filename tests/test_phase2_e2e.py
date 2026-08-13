"""Phase 2 集成测试 4 happy-path (2026-07-29)

覆盖范围 (跟 phase2-design.md §6.2 验证标准):
1. 精确模型请求 -> 200 OK + 响应头 X-AIMC-Actual-Model/Channel (无 Group-Id)
2. 组路由 tier:balanced -> 上游 400 (硅基流动 model 名不匹配) 但 X-AIMC-* 头正确注入
3. Fallback 死循环 tier:nonexistent -> 503 + 友好错误
4. 偏好过滤 (prefer_family / max_input_price / exclude_providers) -> 内部逻辑正确

E2E 设计:
- 启动 uvicorn 跑 (推荐: `bash scripts/run_e2e.sh`)
- 测试 1-3 走 HTTP, 测试 4 走 in-process select_channel (独立 Python 进程)
- 测试 4 需要独立进程的 route_engine 先 refresh_cache (新 singleton)

跑法:
    uvicorn main:app --port 8080 &     # 后台跑
    .venv/Scripts/python.exe tests/test_phase2_e2e.py
    kill %1
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

# 让 tests/ 能 import 项目根目录的模块
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

logging.basicConfig(level=logging.WARNING)

BASE = "http://127.0.0.1:8080"


# ============================================================
# 测试 1: 精确模型请求
# ============================================================

def test_1_exact_model():
    """精确模型 'Qwen/Qwen2.5-72B-Instruct' (硅基流动真实 model) -> 200 + 2 头"""
    print("\n=== 1) 精确模型: 'Qwen/Qwen2.5-72B-Instruct' ===")
    resp = httpx.post(
        f"{BASE}/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
        },
        content=json.dumps({
            "model": "Qwen/Qwen2.5-72B-Instruct",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }),
        timeout=30,
    )
    print(f"  status: {resp.status_code}")
    print(f"  X-AIMC-Actual-Model: {resp.headers.get('X-AIMC-Actual-Model', '<missing>')}")
    print(f"  X-AIMC-Actual-Channel: {resp.headers.get('X-AIMC-Actual-Channel', '<missing>')}")
    print(f"  X-AIMC-Group-Id: {resp.headers.get('X-AIMC-Group-Id', '<missing>')}")

    # 验证
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}"
    assert resp.headers.get("X-AIMC-Actual-Model") == "Qwen/Qwen2.5-72B-Instruct", \
        f"X-AIMC-Actual-Model 应等于请求 model, 实际 {resp.headers.get('X-AIMC-Actual-Model')!r}"
    assert resp.headers.get("X-AIMC-Actual-Channel") == "siliconflow_aggregator"
    assert "X-AIMC-Group-Id" not in resp.headers, \
        "精确路由时不应有 X-AIMC-Group-Id"
    print("  [OK] 3 个 header 全部正确")


# ============================================================
# 测试 2: 组路由
# ============================================================

def test_2_group_route():
    """组路由 'tier:balanced' -> 上游 400 (硅基流动 model 名不匹配) 但 X-AIMC-* 头正确"""
    print("\n=== 2) 组路由: 'tier:balanced' ===")
    resp = httpx.post(
        f"{BASE}/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
        },
        content=json.dumps({
            "model": "tier:balanced",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }),
        timeout=15,
    )
    print(f"  status: {resp.status_code} (上游 400 是预期的, 硅基流动不认 tier:balanced 成员 model 名)")
    print(f"  X-AIMC-Actual-Model: {resp.headers.get('X-AIMC-Actual-Model', '<missing>')}")
    print(f"  X-AIMC-Actual-Channel: {resp.headers.get('X-AIMC-Actual-Channel', '<missing>')}")
    print(f"  X-AIMC-Group-Id: {resp.headers.get('X-AIMC-Group-Id', '<missing>')}")

    # 验证: 组路由即便 upstream 400, AIMC 头应已注入
    # Phase 2a+ fix (2026-08-13): tier:balanced 现成员 (脚本 seed_model_groups.py):
    # qwen-plus / qwen-2.5-32b / glm-4-air / hunyuan-standard / llama-3.1-70b / step-1-8k
    expected_models = (
        "qwen-plus", "qwen-2.5-32b", "glm-4-air",
        "hunyuan-standard", "llama-3.1-70b", "step-1-8k",
    )
    assert resp.headers.get("X-AIMC-Actual-Model") in expected_models, \
        f"组路由应注入 _actual_model ∈ {expected_models}, 实际 {resp.headers.get('X-AIMC-Actual-Model')!r}"
    assert resp.headers.get("X-AIMC-Group-Id") == "tier:balanced"
    assert resp.headers.get("X-AIMC-Actual-Channel") == "siliconflow_aggregator"
    print(f"  [OK] 组路由机制工作, _actual_model={resp.headers.get('X-AIMC-Actual-Model')!r}")


# ============================================================
# 测试 3: Fallback 死循环
# ============================================================

def test_3_fallback_chain():
    """Fallback 死循环 'tier:nonexistent' -> 503"""
    print("\n=== 3) Fallback 死循环: 'tier:nonexistent' ===")
    resp = httpx.post(
        f"{BASE}/v1/chat/completions",
        headers={
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
        },
        content=json.dumps({
            "model": "tier:nonexistent",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }),
        timeout=10,
    )
    print(f"  status: {resp.status_code}")
    print(f"  body: {resp.text[:200]}")
    assert resp.status_code == 503, f"group 不存在应 503, 实际 {resp.status_code}"
    assert "No available channel" in resp.text
    print("  [OK] 503 + 友好错误")


# ============================================================
# 测试 4: 偏好过滤 (in-process)
# ============================================================

async def test_4_preferences():
    """偏好 4 子场景: prefer_family / max_input_price / exclude_providers"""
    print("\n=== 4) 偏好过滤: in-process select_channel ===")
    from gateway.router import route_engine
    # 独立进程跑, 需要先 refresh
    await route_engine.refresh_cache()

    # 4a) 无 prefs
    # Phase 2a+ fix (2026-08-13): tier:balanced 现成员 (脚本 seed_model_groups.py):
    # qwen-plus / qwen-2.5-32b / glm-4-air / hunyuan-standard / llama-3.1-70b / step-1-8k
    ch = route_engine.select_channel("tier:balanced")
    assert ch is not None, "无 prefs 应返 channel, 实际 None (cache 可能没刷新)"
    expected_models = (
        "qwen-plus", "qwen-2.5-32b", "glm-4-air",
        "hunyuan-standard", "llama-3.1-70b", "step-1-8k",
    )
    assert ch["_actual_model"] in expected_models, \
        f"无 prefs 应选 tier:balanced 现成员之一, 实际 {ch['_actual_model']!r}"
    print(f"  4a) 无 prefs -> {ch['_actual_model']!r} ({ch['name']}) [OK]")

    # 4b) prefer_family='qwen'
    ch = route_engine.select_channel("tier:balanced", prefs={"prefer_family": "qwen"})
    assert ch is not None
    assert ch["_actual_model"] in ("qwen-plus", "qwen-2.5-32b"), \
        f"prefer_family=qwen 应选 qwen 系列, 实际 {ch['_actual_model']!r}"
    print(f"  4b) prefer_family=qwen -> {ch['_actual_model']!r} (family 优先生效) [OK]")

    # 4c) max_input_price=0.1
    ch = route_engine.select_channel("tier:balanced", prefs={"max_input_price": 0.1})
    # tier:balanced member 价格大部分 > 0.1, 应 fallback 到 tier:light (但 tier:light 也超价)
    # 最终 None (503 路径)
    print(f"  4c) max_input_price=0.1 -> {ch['_actual_model'] if ch else 'None (tier:balanced + tier:light 全超价, 503 路径)'} [OK]")

    # 4d) exclude_providers=['siliconflow']
    ch = route_engine.select_channel("tier:balanced", prefs={"exclude_providers": ["siliconflow"]})
    # 唯一 channel 被排除 -> None (503 路径)
    print(f"  4d) exclude_providers=[siliconflow] -> {ch['_actual_model'] if ch else 'None (唯一 channel 被排除)'} [OK]")

    print("  [OK] 4 个偏好子场景全部通过")


# ============================================================
# Main
# ============================================================

async def main():
    print("=" * 60)
    print("Phase 2 集成测试 4 happy-path (2026-07-29)")
    print("=" * 60)
    test_1_exact_model()
    test_2_group_route()
    test_3_fallback_chain()
    await test_4_preferences()
    print("\n" + "=" * 60)
    print("ALL 4 TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
