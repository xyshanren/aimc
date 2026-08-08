"""V4.4 账号 CLI 单元测试

覆盖:
1. mask_key 已加密返 "已加密"
2. mask_key 未配置返 "未配置"
3. crud.list_accounts 返回格式 (account, channel) tuple
4. crud.list_accounts 按 provider 过滤
5. crud.update_account_status active → disabled
6. crud.rotate_account_key 加密 + 清 api_key + 更新 last_rotated_at
7. expire_accounts 标记 expires_at 已过期的账号
8. add_account 创建账号 + 重复 name 报错
9. add_account 缺 master_key 报错

跑法: python tests/test_v44_cli.py
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + 临时 SQLite
# ============================================================
def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v44_cli_")
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
        aimc_master_key="a" * 64,  # 必须 64 字符 hex — security/crypto.py 校验
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
    )


_install_mock_env()


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
# Test 1: mask_key 已加密
# ============================================================
def test_mask_key_with_ciphertext():
    print("\n=== Test 1: mask_key 已加密 ===")
    from scripts.list_accounts import mask_key

    assert mask_key(b"some-bytes") == "已加密"
    assert mask_key(None) == "未配置"
    assert mask_key(b"") == "未配置"  # falsy bytes 视为未配置
    print("  [OK] ciphertext=None/b'' → '未配置'; 任意 bytes → '已加密'")


# ============================================================
# Test 2: mask_key 未配置
# ============================================================
def test_mask_key_none():
    print("\n=== Test 2: mask_key 未配置 ===")
    from scripts.list_accounts import mask_key

    assert mask_key(None) == "未配置"
    print("  [OK] ciphertext=None → '未配置'")


# ============================================================
# Test 3: crud.list_accounts 返回 (account, channel) tuple
# ============================================================
def test_crud_list_accounts_basic():
    print("\n=== Test 3: crud.list_accounts 返回格式 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        from db.engine import async_session_factory
        from db.models import Account
        from db import crud

        async with async_session_factory() as session:
            session.add(Account(
                channel_id=ch_id, account_name="ds_01",
                api_key="", status="active",
            ))
            await session.commit()

            rows = await crud.list_accounts(session)
            assert len(rows) == 1, f"应 1 行, 实际 {len(rows)}"
            account, channel = rows[0]
            assert account.account_name == "ds_01"
            assert channel.provider == "deepseek"
            print(f"  [OK] rows[0] = (account={account.account_name}, channel={channel.provider})")

    asyncio.run(_run())


# ============================================================
# Test 4: crud.list_accounts 按 provider 过滤
# ============================================================
def test_crud_list_accounts_filter():
    print("\n=== Test 4: crud.list_accounts 按 provider 过滤 ===")
    async def _run():
        await _reset_db()
        ch_id_a = await _make_channel("deepseek")
        ch_id_b = await _make_channel("qwen")
        from db.engine import async_session_factory
        from db.models import Account
        from db import crud

        async with async_session_factory() as session:
            session.add(Account(channel_id=ch_id_a, account_name="ds_01", api_key="", status="active"))
            session.add(Account(channel_id=ch_id_b, account_name="qwen_01", api_key="", status="active"))
            session.add(Account(channel_id=ch_id_b, account_name="qwen_02", api_key="", status="active"))
            await session.commit()

            ds_rows = await crud.list_accounts(session, provider="deepseek")
            assert len(ds_rows) == 1, f"deepseek 应 1 行, 实际 {len(ds_rows)}"

            qwen_rows = await crud.list_accounts(session, provider="qwen")
            assert len(qwen_rows) == 2, f"qwen 应 2 行, 实际 {len(qwen_rows)}"

            active_only = await crud.list_accounts(session, status="active")
            assert len(active_only) == 3, f"全 active 应 3 行, 实际 {len(active_only)}"
            print(f"  [OK] deepseek={len(ds_rows)} qwen={len(qwen_rows)} active={len(active_only)}")

    asyncio.run(_run())


# ============================================================
# Test 5: crud.update_account_status active → disabled
# ============================================================
def test_update_account_status():
    print("\n=== Test 5: crud.update_account_status active → disabled ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        from db.engine import async_session_factory
        from db.models import Account
        from db import crud

        async with async_session_factory() as session:
            acc = Account(channel_id=ch_id, account_name="ds_01", api_key="", status="active")
            session.add(acc)
            await session.commit()
            acc_id = acc.id

        async with async_session_factory() as session:
            updated = await crud.update_account_status(session, acc_id, "disabled")
            assert updated is not None
            assert updated.status == "disabled"
            await session.commit()

        async with async_session_factory() as session:
            reloaded = await session.get(Account, acc_id)
            assert reloaded.status == "disabled", f"应 disabled, 实际 {reloaded.status}"
            print(f"  [OK] id={acc_id} active → disabled 已 commit")

    asyncio.run(_run())


# ============================================================
# Test 6: crud.rotate_account_key 加密 + 清 api_key + last_rotated_at
# ============================================================
def test_rotate_account_key():
    print("\n=== Test 6: crud.rotate_account_key 加密 + 清 api_key + 更新 last_rotated_at ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("deepseek")
        from db.engine import async_session_factory
        from db.models import Account
        from db import crud
        from security.crypto import encrypt, decrypt

        async with async_session_factory() as session:
            acc = Account(
                channel_id=ch_id, account_name="ds_01",
                api_key="plain-old-key",  # 模拟迁移期老明文
                api_key_ciphertext=None,
                status="active",
            )
            session.add(acc)
            await session.commit()
            acc_id = acc.id

        # 轮换
        async with async_session_factory() as session:
            new_key = "sk-new-rotated-key"
            ciphertext = encrypt(new_key, "a" * 64)
            updated = await crud.rotate_account_key(session, acc_id, ciphertext)
            assert updated is not None
            await session.commit()

        # 验证
        async with async_session_factory() as session:
            reloaded = await session.get(Account, acc_id)
            assert reloaded.api_key == "", f"轮换后 api_key 应清空, 实际 '{reloaded.api_key}'"
            assert reloaded.api_key_ciphertext is not None, "api_key_ciphertext 应已写"
            decrypted = decrypt(reloaded.api_key_ciphertext, "a" * 64)
            assert decrypted == "sk-new-rotated-key", f"解出应等于新 key, 实际 {decrypted}"
            assert reloaded.last_rotated_at is not None, "last_rotated_at 应更新"
            print(f"  [OK] 轮换: 清 api_key + 写 ciphertext + last_rotated_at={reloaded.last_rotated_at}")

    asyncio.run(_run())


# ============================================================
# Test 7: expire_accounts 标记过期
# ============================================================
def test_expire_accounts():
    print("\n=== Test 7: expire_accounts 标记过期 ===")
    async def _run():
        await _reset_db()
        ch_id = await _make_channel("qwen")
        from db.engine import async_session_factory
        from db.models import Account
        from scripts.expire_accounts import expire_accounts

        past = datetime.now() - timedelta(days=1)
        future = datetime.now() + timedelta(days=30)

        async with async_session_factory() as session:
            session.add(Account(channel_id=ch_id, account_name="qwen_expired",
                                api_key="", status="active", expires_at=past))
            session.add(Account(channel_id=ch_id, account_name="qwen_valid",
                                api_key="", status="active", expires_at=future))
            session.add(Account(channel_id=ch_id, account_name="qwen_never",
                                api_key="", status="active", expires_at=None))
            session.add(Account(channel_id=ch_id, account_name="qwen_already",
                                api_key="", status="expired", expires_at=past))
            await session.commit()

        n = await expire_accounts()
        assert n == 1, f"应标记 1 个, 实际 {n}"

        async with async_session_factory() as session:
            from sqlalchemy import select
            accounts = (await session.execute(select(Account))).scalars().all()
            by_name = {a.account_name: a.status for a in accounts}
            assert by_name["qwen_expired"] == "expired", "过期 → expired"
            assert by_name["qwen_valid"] == "active", "未到期保持 active"
            assert by_name["qwen_never"] == "active", "无 expires_at 跳过"
            assert by_name["qwen_already"] == "expired", "已 expired 不再处理"
            print(f"  [OK] 1 个 expired 已标记, 其它不受影响")

    asyncio.run(_run())


# ============================================================
# Test 8: add_account 创建账号 + 重复 name 报错
# ============================================================
def test_add_account_basic_and_dup():
    print("\n=== Test 8: add_account 创建 + 重复 name 报错 ===")
    async def _run():
        await _reset_db()
        await _make_channel("deepseek")
        from scripts.add_account import build_parser, add_account

        args = build_parser().parse_args([
            "--channel", "deepseek",
            "--name", "ds_01",
            "--api-key", "sk-new-key-123",
            # V4.4.D: --notes 暂未入库 (Account 模型无该列), 留待后续
        ])
        acc_id = await add_account(args)
        assert acc_id > 0, f"应创建并返 id, 实际 {acc_id}"
        print(f"  [OK] 创建成功 id={acc_id}")

        # 重复 name 应抛 ValueError
        from db import crud
        try:
            await add_account(args)
            assert False, "应抛 ValueError"
        except ValueError as e:
            assert "已存在" in str(e), f"应包含 '已存在', 实际 {e}"
            print(f"  [OK] 重复 name 报错: {e}")

    asyncio.run(_run())


# ============================================================
# Test 9: add_account 缺 master_key 报错
# ============================================================
def test_add_account_no_master_key():
    print("\n=== Test 9: add_account 缺 AIMC_MASTER_KEY 报错 ===")
    async def _run():
        await _reset_db()
        await _make_channel("deepseek")

        # 临时把 master_key 设为空
        import config
        original = config.settings.aimc_master_key
        config.settings.aimc_master_key = ""

        try:
            from scripts.add_account import build_parser, add_account
            args = build_parser().parse_args([
                "--channel", "deepseek",
                "--name", "ds_no_key",
                "--api-key", "sk-x",
            ])
            try:
                await add_account(args)
                assert False, "应抛 RuntimeError"
            except RuntimeError as e:
                assert "AIMC_MASTER_KEY" in str(e)
                print(f"  [OK] 缺 master_key 报错: {e}")
        finally:
            config.settings.aimc_master_key = original

    asyncio.run(_run())


# ============================================================
# 入口
# ============================================================
def main():
    tests = [
        test_mask_key_with_ciphertext,
        test_mask_key_none,
        test_crud_list_accounts_basic,
        test_crud_list_accounts_filter,
        test_update_account_status,
        test_rotate_account_key,
        test_expire_accounts,
        test_add_account_basic_and_dup,
        test_add_account_no_master_key,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            print(f"  [FAIL] {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n=== V4.4 账号 CLI 测试: {len(tests) - failed}/{len(tests)} passed ===")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())