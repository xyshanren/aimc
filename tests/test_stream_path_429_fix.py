"""V4.9.2 (2026-08-25): stream 路径 429 修复测试

问题:
- V4.9 引入了错误分类器, 期望 cooldown_manager 收到 quota_exhausted + needs_human=True
- 但 stream=True 模式下 httpx.Response.text 抛 ResponseNotRead 异常
- _update_cooldown 静默吞了异常 → response_body=None → classify 走 status_code fallback → rate_limited
- 结果: 真正的 429 余额不足被错分类为 rate_limited, needs_human 永远不触发

修复: stream 路径调用 _update_cooldown 前先 await response.aread()

跑法:
    python tests/test_stream_path_429_fix.py
"""
import asyncio
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


_passed = 0
_failed = 0


def check(desc, got, expected):
    global _passed, _failed
    if got == expected:
        _passed += 1
        print(f"  PASS  {desc}")
    else:
        _failed += 1
        print(f"  FAIL  {desc}  got={got!r}  expected={expected!r}")


# 复用: 余额不足 body (直接用中文, json.dumps 默认 ensure_ascii=False)
import json
QUOTA_BODY_BYTES = json.dumps(
    {"code": "1113", "message": "余额不足"}, ensure_ascii=False
).encode("utf-8")


def make_quota_handler():
    """返回 429 + 余额不足 body 的 handler"""
    def handler(request):
        return httpx.Response(
            429,
            content=QUOTA_BODY_BYTES,
            headers={"content-length": str(len(QUOTA_BODY_BYTES))},
        )
    return handler


# ===== 1. 复现 bug: 没 aread 时, quota_exhausted 误判为 rate_limited =====
def test_stream_without_aread_misclassifies():
    """不 aread 时, classify 走 status_code fallback 返回 rate_limited"""
    from gateway.error_classifier import classify

    # 模拟 _update_cooldown 内部: response_body=None (因为 .text 抛异常)
    info = classify(429, None, None)
    check("无 body 时 429 走 status_code fallback",
          info.category.value, "rate_limited")
    check("needs_human=False", info.needs_human, False)


# ===== 2. 验证 aread 修复 =====
def test_stream_with_aread_classifies_quota_exhausted():
    """aread 后 body 可读, classify 正确识别 quota_exhausted"""
    from gateway.proxy import _update_cooldown
    from gateway.cooldown import cooldown_manager

    async def _run():
        channel = {
            "name": "glm_bigmodel",
            "account_id": 999,
            "account_name": "glm_test_999",
        }

        transport = httpx.MockTransport(make_quota_handler())
        async with httpx.AsyncClient(transport=transport) as client:
            req = client.build_request("POST", "http://x.test/v1/chat/completions")
            response = await client.send(req, stream=True)

            # === 关键: aread 后 .text 才有值 ===
            if response.status_code >= 400:
                try:
                    await response.aread()
                except Exception:
                    pass

            _update_cooldown(channel, response)

        s = cooldown_manager.snapshot()[999]
        check("account_id=999 进 cooldown", 999 in cooldown_manager.snapshot(), True)
        check("error_type=quota_exhausted",
              s.get("last_error_type"), "quota_exhausted")
        check("needs_human=False (count=1, 阈值 2)",
              s.get("needs_human"), False)
        check("error_code=1113",
              s.get("last_error_code"), "1113")
        check("error_msg 非空",
              bool(s.get("last_error_msg")), True)

        cooldown_manager.reset(999)

    asyncio.run(_run())


# ===== 3. 验证 429 quota + 1 次不触发 (count=1 < 2) =====
def test_first_429_quota_no_needs_human_yet():
    """第 1 次 quota 失败, count=1 < threshold=2, 不该 needs_human"""
    from gateway.proxy import _update_cooldown
    from gateway.cooldown import cooldown_manager

    async def _run():
        channel = {"name": "test_ch_1", "account_id": 1001, "account_name": "acc_1001"}
        transport = httpx.MockTransport(make_quota_handler())
        async with httpx.AsyncClient(transport=transport) as client:
            req = client.build_request("POST", "http://x.test/y")
            response = await client.send(req, stream=True)
            if response.status_code >= 400:
                try:
                    await response.aread()
                except Exception:
                    pass
            _update_cooldown(channel, response)

        s = cooldown_manager.snapshot()[1001]
        check("count=1", s.get("count"), 1)
        check("needs_human=False (阈值未到)", s.get("needs_human"), False)
        cooldown_manager.reset(1001)

    asyncio.run(_run())


def test_second_429_quota_triggers_needs_human():
    """第 2 次 quota 失败, count=2 = threshold=2 → needs_human=True"""
    from gateway.proxy import _update_cooldown
    from gateway.cooldown import cooldown_manager

    async def _run():
        channel = {"name": "test_ch_2", "account_id": 1002, "account_name": "acc_1002"}
        transport = httpx.MockTransport(make_quota_handler())
        async with httpx.AsyncClient(transport=transport) as client:
            for i in range(2):
                req = client.build_request("POST", "http://x.test/y")
                response = await client.send(req, stream=True)
                if response.status_code >= 400:
                    try:
                        await response.aread()
                    except Exception:
                        pass
                    _update_cooldown(channel, response)

        s = cooldown_manager.snapshot()[1002]
        check("count=2", s.get("count"), 2)
        check("needs_human=True (阈值达到)", s.get("needs_human"), True)
        cooldown_manager.reset(1002)

    asyncio.run(_run())


# ===== 4. 验证非 stream 路径也工作 (regression) =====
def test_non_stream_429_classifies_correctly():
    """非流式 (client.post) 路径也能正确分类"""
    from gateway.proxy import _update_cooldown
    from gateway.cooldown import cooldown_manager

    async def _run():
        channel = {"name": "test_ch_3", "account_id": 1003, "account_name": "acc_1003"}
        transport = httpx.MockTransport(make_quota_handler())
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post("http://x.test/y", content=b"")

        _update_cooldown(channel, response)

        s = cooldown_manager.snapshot()[1003]
        check("error_type=quota_exhausted", s.get("last_error_type"), "quota_exhausted")
        check("needs_human=False (count=1)", s.get("needs_human"), False)
        cooldown_manager.reset(1003)

    asyncio.run(_run())


# ===== 5. 验证: stream + 500 (5xx) 路径也正确 =====
def test_stream_500_classifies_upstream_5xx():
    """stream 路径 500 也走 aread, 分类为 upstream_5xx"""
    from gateway.proxy import _update_cooldown
    from gateway.cooldown import cooldown_manager

    async def _run():
        channel = {"name": "test_ch_5", "account_id": 1005, "account_name": "acc_1005"}

        def handler(req):
            return httpx.Response(500, content=b"Internal Server Error")
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            req = client.build_request("POST", "http://x.test/y")
            response = await client.send(req, stream=True)
            if response.status_code >= 400:
                try:
                    await response.aread()
                except Exception:
                    pass
            _update_cooldown(channel, response)

        s = cooldown_manager.snapshot()[1005]
        check("error_type=upstream_5xx", s.get("last_error_type"), "upstream_5xx")
        cooldown_manager.reset(1005)

    asyncio.run(_run())


def main():
    tests = [
        test_stream_without_aread_misclassifies,
        test_stream_with_aread_classifies_quota_exhausted,
        test_first_429_quota_no_needs_human_yet,
        test_second_429_quota_triggers_needs_human,
        test_non_stream_429_classifies_correctly,
        test_stream_500_classifies_upstream_5xx,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        try:
            t()
        except Exception as e:
            global _failed
            _failed += 1
            print(f"  EXCEPTION: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n=== 结果: {_passed} passed, {_failed} failed ===")
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())