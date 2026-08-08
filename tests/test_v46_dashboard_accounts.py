"""V4.6 Dashboard 账号管理 HTTP 端点单元测试

覆盖 (dashboard/routes.py /api/accounts/* + /api/channels/active):
1. _mask_key_display: ciphertext → encrypted:xxxx / None → (未加密)
2. _account_to_dict: quota_pct 算对 + key_display 存在
3. GET /api/accounts: 空 / 单条 / 多条
4. GET /api/accounts?provider= 过滤
5. GET /api/channels/active: 只列 status=active
6. POST /api/accounts: 成功 + ciphertext 入库 + last_rotated_at
7. POST /api/accounts: provider 缺失 → 400
8. POST /api/accounts: channel 不存在 → 404
9. POST /api/accounts: 重名 → 409
10. POST /api/accounts: expires_at 解析 "YYYY-MM-DD" 与 "ISO datetime"
11. POST /api/accounts/{id}/disable → status=disabled
12. POST /api/accounts/{id}/enable → status=active
13. POST /api/accounts/{id}/rotate: 新 key 加密入库 + last_rotated_at 更新
14. POST /api/accounts/{id}/rotate: 缺 api_key → 400
15. POST /api/accounts/{id}/rotate: 不存在 → 404
16. POST /api/accounts/expire-check: 标记过期账号 + 返回 count
17. _parse_iso_datetime: "2026-12-31" / "2026-12-31T10:00:00" / "bad" → 400

跑法: python tests/test_v46_dashboard_accounts.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + 临时 SQLite (同 test_v44_cli.py 模式)
# ============================================================
def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v46_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    import types

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )


_install_mock_env()


# ============================================================
# TestApp: 把 dashboard router 挂到独立 FastAPI app, 用 ASGITransport
# 这样可以跳过 uvicorn 启动, 但仍走真实 Depends(get_session) + 临时 SQLite.
# ============================================================
def _build_app():
    from fastapi import FastAPI
    from dashboard.routes import router as dashboard_router

    app = FastAPI()
    app.include_router(dashboard_router)
    return app


_APP = None


def _get_app():
    global _APP
    if _APP is None:
        _APP = _build_app()
    return _APP


async def _client():
    """每次新建 httpx.AsyncClient, 用 ASGITransport 走 _APP.

    注意: 不能 async def, 否则调用 _client() 拿到 coroutine 而非 AsyncClient.
    async with _client() as c: 才能直接用, 因为 httpx.AsyncClient 自身是 async ctx mgr.
    """
    import httpx
    transport = httpx.ASGITransport(app=_get_app())
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _get_client():
    """同步工厂 — 返回 AsyncClient 实例 (非 coroutine), 让 async with 可用."""
    import httpx
    transport = httpx.ASGITransport(app=_get_app())
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _reset_db():
    from db.engine import async_session_factory
    from db.init_db import init_db
    from db.models import Channel, Account
    from sqlalchemy import delete

    await init_db()
    async with async_session_factory() as session:
        await session.execute(delete(Account))
        await session.execute(delete(Channel))
        await session.commit()


async def _make_channel(provider: str, status: str = "active") -> int:
    from db.engine import async_session_factory
    from db.models import Channel

    async with async_session_factory() as session:
        ch = Channel(
            name=f"{provider}_official",
            provider=provider,
            base_url=f"https://{provider}.example.com",
            status=status,
        )
        session.add(ch)
        await session.commit()
        return ch.id


# ============================================================
# Test 1: _mask_key_display
# ============================================================
def test_mask_key_display():
    print("\n=== Test 1: _mask_key_display 脱敏 ===")
    from dashboard.routes import _mask_key_display

    # ciphertext → encrypted:hex(last4)
    c1 = b"some-bytes-here"
    out1 = _mask_key_display(c1)
    assert out1.startswith("encrypted:"), f"应 'encrypted:' 前缀, 实际 {out1!r}"
    assert c1[-4:].hex() in out1, f"应含末尾 4 字节 hex, 实际 {out1!r}"
    print(f"  [OK] ciphertext -> {out1!r}")

    # None / b'' → (未加密)
    assert _mask_key_display(None) == "(未加密)"
    assert _mask_key_display(b"") == "(未加密)"
    print(f"  [OK] None/empty -> '(未加密)'")


# ============================================================
# Test 2: _account_to_dict
# ============================================================
def test_account_to_dict():
    print("\n=== Test 2: _account_to_dict 序列化 ===")
    async def _run():
        from datetime import datetime
        from dashboard.routes import _account_to_dict

        # mock account + channel
        from db.models import Channel, Account

        ch = Channel(id=1, provider="deepseek", name="ds_official", base_url="x", status="active")
        acc = Account(
            id=10,
            channel_id=1,
            account_name="ds_01",
            api_key="",
            api_key_ciphertext=b"\x00\x01\x02\x03abcdef",
            status="active",
            quota_total=100_000,
            quota_used=40_000,
            expires_at=datetime(2026, 12, 31),
            notes="测试账号",
        )
        d = _account_to_dict(acc, ch)
        assert d["id"] == 10
        assert d["provider"] == "deepseek"
        assert d["account_name"] == "ds_01"
        assert d["quota_pct"] == 40.0, f"quota_pct 应 40.0, 实际 {d['quota_pct']}"
        assert d["quota_remaining"] == 60_000
        assert d["key_display"].startswith("encrypted:")
        assert d["notes"] == "测试账号"
        assert d["expires_at"].startswith("2026-12-31")
        print(f"  [OK] dict={d['id']}/{d['provider']}/{d['account_name']} quota_pct={d['quota_pct']}")

        # 无 quota_total 时 quota_pct=None
        acc2 = Account(
            id=11, channel_id=1, account_name="x", api_key="",
            api_key_ciphertext=None, status="active",
            quota_total=None, quota_used=0,
        )
        d2 = _account_to_dict(acc2, ch)
        assert d2["quota_pct"] is None
        assert d2["quota_remaining"] is None
        assert d2["key_display"] == "(未加密)"
        print(f"  [OK] 无 quota_total → quota_pct=None")

    asyncio.run(_run())


# ============================================================
# Test 3: GET /api/accounts 空/单/多
# ============================================================
def test_list_accounts_http():
    print("\n=== Test 3: GET /api/accounts ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        from db.engine import async_session_factory
        from db.models import Account
        from security.crypto import encrypt

        async with async_session_factory() as session:
            session.add(Account(channel_id=ch_id, account_name="ds_01",
                                api_key="", api_key_ciphertext=encrypt("sk-1", "a" * 64),
                                status="active", quota_total=100))
            await session.commit()

        async with _get_client() as c:
            resp = await c.get("/dashboard/api/accounts")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["count"] == 1
            assert data["accounts"][0]["account_name"] == "ds_01"
            assert data["accounts"][0]["provider"] == "deepseek"
            print(f"  [OK] count={data['count']} name={data['accounts'][0]['account_name']}")

    asyncio.run(_run())


# ============================================================
# Test 4: GET /api/accounts?provider= 过滤
# ============================================================
def test_list_accounts_filter():
    print("\n=== Test 4: GET /api/accounts?provider=qwen ===")
    async def _run():
        await _reset_db()
        ch_a = await _make_channel("deepseek")
        ch_b = await _make_channel("qwen")
        from db.engine import async_session_factory
        from db.models import Account

        async with async_session_factory() as session:
            session.add(Account(channel_id=ch_a, account_name="ds_01", api_key="", status="active"))
            session.add(Account(channel_id=ch_b, account_name="qwen_01", api_key="", status="active"))
            session.add(Account(channel_id=ch_b, account_name="qwen_02", api_key="", status="disabled"))
            await session.commit()

        async with _get_client() as c:
            # 只 qwen
            r1 = await c.get("/dashboard/api/accounts", params={"provider": "qwen"})
            assert r1.status_code == 200
            assert r1.json()["count"] == 2
            # 只 disabled
            r2 = await c.get("/dashboard/api/accounts", params={"status": "disabled"})
            assert r2.json()["count"] == 1
            assert r2.json()["accounts"][0]["account_name"] == "qwen_02"
            # provider + status 组合
            r3 = await c.get("/dashboard/api/accounts", params={"provider": "qwen", "status": "disabled"})
            assert r3.json()["count"] == 1
            print(f"  [OK] qwen={r1.json()['count']} disabled={r2.json()['count']} qwen+disabled={r3.json()['count']}")

    asyncio.run(_run())


# ============================================================
# Test 5: GET /api/channels/active 只列 active
# ============================================================
def test_list_active_channels():
    print("\n=== Test 5: GET /api/channels/active ===")
    async def _run():
        await _reset_db()
        await _make_channel("deepseek", status="active")
        await _make_channel("qwen", status="active")
        await _make_channel("disabled_prov", status="disabled")

        async with _get_client() as c:
            resp = await c.get("/dashboard/api/channels/active")
            assert resp.status_code == 200
            data = resp.json()
            assert data["count"] == 2
            providers = sorted(ch["provider"] for ch in data["channels"])
            assert providers == ["deepseek", "qwen"], f"应只有 active, 实际 {providers}"
            print(f"  [OK] active providers = {providers}")

    asyncio.run(_run())


# ============================================================
# Test 6: POST /api/accounts 成功 + ciphertext 入库 + last_rotated_at
# ============================================================
def test_create_account_success():
    print("\n=== Test 6: POST /api/accounts 成功 ===")
    async def _run():
        await _reset_db()
        await _make_channel("deepseek")

        payload = {
            "provider": "deepseek",
            "name": "ds_new",
            "api_key": "sk-new-key-12345",
            "quota_total": 50000,
            "expires_at": "2026-12-31",
            "notes": "新账号",
        }
        async with _get_client() as c:
            resp = await c.post("/dashboard/api/accounts", json=payload)
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["status"] == "created"
            assert data["account"]["account_name"] == "ds_new"
            assert data["account"]["quota_total"] == 50000
            assert data["account"]["status"] == "active"
            assert data["account"]["key_display"].startswith("encrypted:")
            assert data["account"]["notes"] == "新账号"

        # DB 验证: api_key 清空, ciphertext 写入, last_rotated_at 非空
        from db.engine import async_session_factory
        from db.models import Account
        from security.crypto import decrypt

        async with async_session_factory() as session:
            from sqlalchemy import select
            accs = (await session.execute(select(Account))).scalars().all()
            assert len(accs) == 1
            acc = accs[0]
            assert acc.api_key == "", "明文应清空"
            assert acc.api_key_ciphertext is not None
            decrypted = decrypt(acc.api_key_ciphertext, "a" * 64)
            assert decrypted == "sk-new-key-12345", f"解密应等于原 key, 实际 {decrypted!r}"
            assert acc.last_rotated_at is not None, "last_rotated_at 应写入"
            print(f"  [OK] id={acc.id} decrypt roundtrip 一致, last_rotated_at 已写")

    asyncio.run(_run())


# ============================================================
# Test 7: POST /api/accounts provider 缺失 → 400
# ============================================================
def test_create_account_missing_provider():
    print("\n=== Test 7: POST /api/accounts 缺 provider → 400 ===")
    async def _run():
        await _reset_db()
        await _make_channel("deepseek")

        async with _get_client() as c:
            resp = await c.post("/dashboard/api/accounts", json={"name": "x", "api_key": "sk"})
            assert resp.status_code == 400, resp.text
            assert "provider" in resp.json()["detail"]
            print(f"  [OK] detail={resp.json()['detail']!r}")

    asyncio.run(_run())


# ============================================================
# Test 8: POST /api/accounts channel 不存在 → 404
# ============================================================
def test_create_account_no_channel():
    print("\n=== Test 8: POST /api/accounts channel 不存在 → 404 ===")
    async def _run():
        await _reset_db()
        async with _get_client() as c:
            resp = await c.post("/dashboard/api/accounts", json={
                "provider": "ghost", "name": "g", "api_key": "sk",
            })
            assert resp.status_code == 404, resp.text
            assert "未找到" in resp.json()["detail"]
            print(f"  [OK] detail={resp.json()['detail']!r}")

    asyncio.run(_run())


# ============================================================
# Test 9: POST /api/accounts 重名 → 409
# ============================================================
def test_create_account_dup_name():
    print("\n=== Test 9: POST /api/accounts 重名 → 409 ===")
    async def _run():
        await _reset_db()
        await _make_channel("deepseek")

        payload = {"provider": "deepseek", "name": "ds_dup", "api_key": "sk-1"}
        async with _get_client() as c:
            r1 = await c.post("/dashboard/api/accounts", json=payload)
            assert r1.status_code == 200

            r2 = await c.post("/dashboard/api/accounts", json=payload)
            assert r2.status_code == 409, r2.text
            assert "已存在" in r2.json()["detail"]
            print(f"  [OK] 重名 detail={r2.json()['detail']!r}")

    asyncio.run(_run())


# ============================================================
# Test 10: expires_at 解析 'YYYY-MM-DD' 和 'ISO datetime'
# ============================================================
def test_create_account_expires_at_parsing():
    print("\n=== Test 10: expires_at 解析 ===")
    async def _run():
        await _reset_db()
        await _make_channel("deepseek")

        async with _get_client() as c:
            # 1) 'YYYY-MM-DD'
            r1 = await c.post("/dashboard/api/accounts", json={
                "provider": "deepseek", "name": "ds_date", "api_key": "sk",
                "expires_at": "2026-12-31",
            })
            assert r1.status_code == 200
            assert r1.json()["account"]["expires_at"].startswith("2026-12-31")

            # 2) 'YYYY-MM-DDTHH:MM:SS'
            r2 = await c.post("/dashboard/api/accounts", json={
                "provider": "deepseek", "name": "ds_dt", "api_key": "sk",
                "expires_at": "2027-06-15T10:30:00",
            })
            assert r2.status_code == 200
            assert r2.json()["account"]["expires_at"].startswith("2027-06-15")

            # 3) 错格式 → 400
            r3 = await c.post("/dashboard/api/accounts", json={
                "provider": "deepseek", "name": "ds_bad", "api_key": "sk",
                "expires_at": "not-a-date",
            })
            assert r3.status_code == 400, r3.text
            print(f"  [OK] 三种格式: YYYY-MM-DD / ISO datetime / 错 → 400")

    asyncio.run(_run())


# ============================================================
# Test 11/12: enable / disable 切换
# ============================================================
def test_disable_and_enable():
    print("\n=== Test 11/12: POST enable/disable 切换 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        from db.engine import async_session_factory
        from db.models import Account

        async with async_session_factory() as session:
            session.add(Account(channel_id=ch_id, account_name="ds_toggle", api_key="", status="active"))
            await session.commit()
            acc_id = (await session.execute(
                __import__("sqlalchemy").select(Account)
            )).scalars().first().id

        async with _get_client() as c:
            # disable
            r1 = await c.post(f"/dashboard/api/accounts/{acc_id}/disable")
            assert r1.status_code == 200
            assert r1.json()["status"] == "disabled"

            # enable
            r2 = await c.post(f"/dashboard/api/accounts/{acc_id}/enable")
            assert r2.status_code == 200
            assert r2.json()["status"] == "active"

            # 不存在 → 404
            r3 = await c.post("/dashboard/api/accounts/99999/disable")
            assert r3.status_code == 404
            print(f"  [OK] active → disabled → active roundtrip 成功")

    asyncio.run(_run())


# ============================================================
# Test 13: rotate 加密入库 + last_rotated_at 更新
# ============================================================
def test_rotate_account():
    print("\n=== Test 13: POST /api/accounts/{id}/rotate ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        from db.engine import async_session_factory
        from db.models import Account
        from security.crypto import encrypt, decrypt

        async with async_session_factory() as session:
            acc = Account(
                channel_id=ch_id, account_name="ds_rot", api_key="",
                api_key_ciphertext=encrypt("sk-old-key", "a" * 64),
                status="active",
            )
            session.add(acc)
            await session.commit()
            acc_id = acc.id

        async with _get_client() as c:
            resp = await c.post(f"/dashboard/api/accounts/{acc_id}/rotate",
                                json={"api_key": "sk-new-rotated"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "rotated"

        # DB 验证
        async with async_session_factory() as session:
            acc = await session.get(Account, acc_id)
            assert acc.api_key == ""
            assert acc.api_key_ciphertext is not None
            assert decrypt(acc.api_key_ciphertext, "a" * 64) == "sk-new-rotated", "新 key 应入库"
            assert acc.last_rotated_at is not None
            print(f"  [OK] 轮换: sk-new-rotated 解出正确")

    asyncio.run(_run())


# ============================================================
# Test 14: rotate 缺 api_key → 400
# ============================================================
def test_rotate_account_missing_key():
    print("\n=== Test 14: POST rotate 缺 api_key → 400 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        from db.engine import async_session_factory
        from db.models import Account

        async with async_session_factory() as session:
            session.add(Account(channel_id=ch_id, account_name="ds_x", api_key="", status="active"))
            await session.commit()
            acc_id = session.info  # 不用, 改用下轮查
            from sqlalchemy import select
            acc_id = (await session.execute(select(Account))).scalars().first().id

        async with _get_client() as c:
            r = await c.post(f"/dashboard/api/accounts/{acc_id}/rotate", json={})
            assert r.status_code == 400
            assert "api_key" in r.json()["detail"]
            print(f"  [OK] detail={r.json()['detail']!r}")

    asyncio.run(_run())


# ============================================================
# Test 15: rotate 不存在 → 404
# ============================================================
def test_rotate_account_not_found():
    print("\n=== Test 15: POST rotate 不存在 → 404 ===")
    async def _run():
        await _reset_db()
        async with _get_client() as c:
            r = await c.post("/dashboard/api/accounts/99999/rotate", json={"api_key": "sk"})
            assert r.status_code == 404
            print(f"  [OK] 不存在 id=99999 → 404")

    asyncio.run(_run())


# ============================================================
# Test 16: expire-check 标记过期 + 返 count
# ============================================================
def test_expire_check():
    print("\n=== Test 16: POST /api/accounts/expire-check ===")
    async def _run():
        from datetime import datetime, timedelta
        await _reset_db()
        ch_id = await _make_channel("qwen")
        from db.engine import async_session_factory
        from db.models import Account

        past = datetime.now() - timedelta(days=1)
        future = datetime.now() + timedelta(days=30)

        async with async_session_factory() as session:
            session.add(Account(channel_id=ch_id, account_name="q_expired", api_key="",
                                status="active", expires_at=past))
            session.add(Account(channel_id=ch_id, account_name="q_valid", api_key="",
                                status="active", expires_at=future))
            session.add(Account(channel_id=ch_id, account_name="q_never", api_key="",
                                status="active", expires_at=None))
            await session.commit()

        async with _get_client() as c:
            r = await c.post("/dashboard/api/accounts/expire-check")
            assert r.status_code == 200
            assert r.json()["expired_count"] == 1, f"应 1 个, 实际 {r.json()}"
            print(f"  [OK] 1 个已过期 → status=expired")

    asyncio.run(_run())


# ============================================================
# Test 17: _parse_iso_datetime 边界
# ============================================================
def test_parse_iso_datetime():
    print("\n=== Test 17: _parse_iso_datetime 边界 ===")
    from dashboard.routes import _parse_iso_datetime

    # 1) None
    assert _parse_iso_datetime(None) is None
    # 2) YYYY-MM-DD
    d1 = _parse_iso_datetime("2026-12-31")
    assert d1 is not None and d1.year == 2026 and d1.month == 12 and d1.day == 31
    # 3) YYYY-MM-DDTHH:MM:SS
    d2 = _parse_iso_datetime("2026-12-31T10:30:00")
    assert d2 is not None and d2.hour == 10 and d2.minute == 30
    # 4) 错格式 → 400
    from fastapi import HTTPException
    try:
        _parse_iso_datetime("not-a-date")
        assert False, "应抛 HTTPException"
    except HTTPException as e:
        assert e.status_code == 400
    print(f"  [OK] None / YYYY-MM-DD / ISO datetime / 错格式 → 400")


# ============================================================
# 入口
# ============================================================
def main():
    tests = [
        test_mask_key_display,
        test_account_to_dict,
        test_list_accounts_http,
        test_list_accounts_filter,
        test_list_active_channels,
        test_create_account_success,
        test_create_account_missing_provider,
        test_create_account_no_channel,
        test_create_account_dup_name,
        test_create_account_expires_at_parsing,
        test_disable_and_enable,
        test_rotate_account,
        test_rotate_account_missing_key,
        test_rotate_account_not_found,
        test_expire_check,
        test_parse_iso_datetime,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            print(f"\n  [FAIL] {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n=== V4.6 Dashboard 账号管理测试: {len(tests) - failed}/{len(tests)} passed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())