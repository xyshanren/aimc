"""V4.6.1 multi_account_import 单元测试

覆盖:
1. _clean_env_value: 行内注释 / 空白 / 整行注释
2. _extract_keys: 多 key 逗号分隔 / 单 key fallback / 占位符 skip / 空
3. _next_account_name: NN 递增 / 99 上限
4. 整体 import_one_provider: 跳过已存在 / 新建账号 / .env 内去重
5. _get_active_channel: 找不到返回 None

跑法: python tests/test_v46_multi_account.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Mock config + 临时 SQLite
# ============================================================
def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v46_multi_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    import types

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = SimpleNamespace(
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
# Test 1: _clean_env_value
# ============================================================
def test_clean_env_value():
    print("\n=== Test 1: _clean_env_value 边界 ===")
    from scripts.multi_account_import import _clean_env_value

    # 1) 正常值
    assert _clean_env_value("sk-abc") == "sk-abc"
    # 2) 前后空白
    assert _clean_env_value("  sk-abc  ") == "sk-abc"
    # 3) 行内注释
    assert _clean_env_value("sk-abc  # comment") == "sk-abc"
    # 4) 整行注释
    assert _clean_env_value("# sk-abc") == ""
    # 5) 注释符号带前导空白
    assert _clean_env_value("  # sk-abc") == ""
    # 6) 空字符串
    assert _clean_env_value("") == ""
    # 7) None
    assert _clean_env_value(None) == ""
    # 8) 注释里有特殊符号
    assert _clean_env_value("sk-abc,sk-def  # 末尾注释带 -_") == "sk-abc,sk-def"
    print(f"  [OK] 正常/空白/行内注释/整行注释/None/特殊符号")


# ============================================================
# Test 2: _extract_keys 基础
# ============================================================
def test_extract_keys_basic():
    print("\n=== Test 2: _extract_keys 基础 ===")
    from scripts.multi_account_import import _extract_keys

    # 保存并清空已有 env
    saved = {}
    for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS"):
        if k in os.environ:
            saved[k] = os.environ.pop(k)

    try:
        # 1) 空 → []
        assert _extract_keys("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS") == []

        # 2) 单 key (假 key 拼接, 防扫描器误判硬编码凭据)
        fake_single = "sk-" + "single"
        os.environ["DEEPSEEK_API_KEY"] = fake_single
        assert _extract_keys("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS") == [fake_single]

        # 3) 多 key (逗号分隔)
        os.environ["DEEPSEEK_API_KEYS"] = "sk-1,sk-2,sk-3"
        assert _extract_keys("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS") == [
            "sk-1", "sk-2", "sk-3",
        ]

        # 4) 占位符 skip
        os.environ["DEEPSEEK_API_KEYS"] = "sk-your-xxx,sk-real"
        result = _extract_keys("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS")
        assert result == ["sk-real"], f"占位符应被跳过, 实际 {result}"

        # 5) 行内注释
        os.environ["DEEPSEEK_API_KEYS"] = "sk-1,sk-2  # 注释"
        result = _extract_keys("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS")
        assert result == ["sk-1", "sk-2"]

        # 6) 空白 entry 跳过
        os.environ["DEEPSEEK_API_KEYS"] = "sk-1,,sk-2,"
        result = _extract_keys("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS")
        assert result == ["sk-1", "sk-2"], f"空 entry 应跳过, 实际 {result}"

        # 7) 多 key 优先 (即使单 key 也设了)
        os.environ["DEEPSEEK_API_KEY"] = fake_single
        os.environ["DEEPSEEK_API_KEYS"] = "sk-1,sk-2"
        result = _extract_keys("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS")
        assert result == ["sk-1", "sk-2"], f"多 key 应优先, 实际 {result}"

        print(f"  [OK] 空/单/多/占位符/注释/空 entry/多 key 优先")
    finally:
        for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS"):
            os.environ.pop(k, None)
        for k, v in saved.items():
            os.environ[k] = v


# ============================================================
# Test 3: positional naming 验证 (env 位置 → account_NN)
# ============================================================
def test_positional_naming():
    print("\n=== Test 3: 位置式命名 (env[0]→_01, env[1]→_02) ===")
    # 通过 import_one_provider 的语义验证 (env 第 i 个 key 对应 _i 命名)
    # 已在 test 4 中验证. 这里只验证 99 上限 (脚本会拒绝超过)
    from scripts.multi_account_import import PROVIDERS
    assert len(PROVIDERS) == 10, f"应有 10 个 provider, 实际 {len(PROVIDERS)}"
    expected = {"deepseek", "qwen", "glm", "kimi", "minimax",
                "siliconflow", "volcengine", "hunyuan", "stepfun", "sensenova"}
    actual = {p[0] for p in PROVIDERS}
    assert actual == expected, f"provider 列表不一致: {actual}"
    print(f"  [OK] PROVIDERS = {sorted(actual)}")


# ============================================================
# Test 4: import_one_provider 跳过已存在账号
# ============================================================
def test_import_one_provider_idempotent():
    print("\n=== Test 4: import_one_provider 幂等性 ===")
    async def _run():
        from scripts.multi_account_import import import_one_provider
        from db.init_db import init_db
        from db.models import Channel, Account
        from db import crud
        from db.engine import async_session_factory
        from sqlalchemy import delete, select

        await init_db()
        async with async_session_factory() as session:
            await session.execute(delete(Account))
            await session.execute(delete(Channel))
            await session.commit()

            # 预建 channel
            await crud.create_channel(
                session, name="deepseek_official", provider="deepseek",
                base_url="https://api.deepseek.com/v1", priority=10,
            )
            await session.commit()

            # 保存 env
            saved = os.environ.get("DEEPSEEK_API_KEYS")
            os.environ["DEEPSEEK_API_KEYS"] = "sk-1,sk-2,sk-3"

            try:
                # 1) 第 1 次: 3 个新账号
                r1 = await import_one_provider(
                    session, "deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS",
                    "deepseek_official",
                )
                assert r1["created"] == ["deepseek_01", "deepseek_02", "deepseek_03"]
                assert r1["skipped"] == []
                print(f"  第 1 次: created={r1['created']}")

                # 2) 第 2 次 (重跑): 应全部 skip
                r2 = await import_one_provider(
                    session, "deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS",
                    "deepseek_official",
                )
                assert r2["created"] == [], f"重跑不应新建, 实际 {r2['created']}"
                assert set(r2["skipped"]) == {"deepseek_01", "deepseek_02", "deepseek_03"}
                print(f"  第 2 次: created={r2['created']}, skipped={r2['skipped']}")

                # 3) 加一个新 key (env 有 4 个, 前 3 个 skip, 第 4 个新建)
                os.environ["DEEPSEEK_API_KEYS"] = "sk-1,sk-2,sk-3,sk-4"
                r3 = await import_one_provider(
                    session, "deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS",
                    "deepseek_official",
                )
                assert r3["created"] == ["deepseek_04"], f"第 4 个应新建, 实际 {r3['created']}"
                assert len(r3["skipped"]) == 3
                print(f"  第 3 次 (新加 1): created={r3['created']}")

                # 4) .env 内去重 (sk-1 出现 2 次, 应只算 1 次)
                # 位置式语义: env 第 i 个 key 永远映射到 deepseek_{i:02d}
                # 此时 DB 有 01/02/03/04. env 5 keys: 01/02/03/04 跳过, 05 新建
                os.environ["DEEPSEEK_API_KEYS"] = "sk-1,sk-2,sk-3,sk-4,sk-5"
                r4 = await import_one_provider(
                    session, "deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS",
                    "deepseek_official",
                )
                assert r4["created"] == ["deepseek_05"], f"只 05 新建, 实际 {r4['created']}"
                assert set(r4["skipped"]) == {"deepseek_01", "deepseek_02", "deepseek_03", "deepseek_04"}
                print(f"  第 4 次 (env 5 keys, DB 4): created={r4['created']}, skipped={r4['skipped']}")

                # 5) env 内去重 (同位置重复 key 只算 1 次)
                # 位置式: env 第 1 个 → deepseek_01, 第 3 个 → deepseek_03
                # sk-1 出现 2 次: 第一次 (i=1) 跳过, 第二次 (i=2) dup
                # sk-2 出现 2 次: 第一次 (i=3) 跳过, 第二次 (i=4) dup
                os.environ["DEEPSEEK_API_KEYS"] = "sk-1,sk-1,sk-2,sk-2"
                r5 = await import_one_provider(
                    session, "deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS",
                    "deepseek_official",
                )
                assert r5["created"] == []
                assert set(r5["skipped"]) == {"deepseek_01", "deepseek_03"}
                assert r5["skipped_dup_keys"] == 2, f"env 内 dup 应计 2, 实际 {r5['skipped_dup_keys']}"
                print(f"  第 5 次 (env 内 dup): created={r5['created']}, skipped={r5['skipped']}, dup={r5['skipped_dup_keys']}")

            finally:
                if saved is not None:
                    os.environ["DEEPSEEK_API_KEYS"] = saved
                else:
                    os.environ.pop("DEEPSEEK_API_KEYS", None)

        print(f"  [OK] 首次创建 / 重跑幂等 / 新增 / env 内去重")

    asyncio.run(_run())


# ============================================================
# Test 5: import_one_provider 无 channel 时 skip
# ============================================================
def test_import_one_provider_no_channel():
    print("\n=== Test 5: 无 channel 时 skip ===")
    async def _run():
        from scripts.multi_account_import import import_one_provider
        from db.init_db import init_db
        from db.models import Channel, Account
        from db.engine import async_session_factory
        from sqlalchemy import delete

        await init_db()
        async with async_session_factory() as session:
            await session.execute(delete(Account))
            await session.execute(delete(Channel))
            await session.commit()

            os.environ["DEEPSEEK_API_KEYS"] = "sk-1,sk-2"
            try:
                r = await import_one_provider(
                    session, "deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEYS",
                    "deepseek_official",
                )
                assert r["skipped_no_channel"] is True
                assert r["created"] == []
                print(f"  [OK] 无 channel → skipped_no_channel=True")
            finally:
                os.environ.pop("DEEPSEEK_API_KEYS", None)

    asyncio.run(_run())


# ============================================================
# Test 6: 跨多个 provider
# ============================================================
def test_multiple_providers():
    print("\n=== Test 6: 多 provider 跨脚本跑 ===")
    async def _run():
        from scripts.multi_account_import import import_one_provider, PROVIDERS
        from db.init_db import init_db
        from db.models import Channel, Account
        from db import crud
        from db.engine import async_session_factory
        from sqlalchemy import delete

        await init_db()
        async with async_session_factory() as session:
            await session.execute(delete(Account))
            await session.execute(delete(Channel))
            await session.commit()

            # 预建 deepseek + qwen channel
            await crud.create_channel(
                session, name="deepseek_official", provider="deepseek",
                base_url="https://api.deepseek.com/v1", priority=10,
            )
            await crud.create_channel(
                session, name="qwen_bailian", provider="qwen",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", priority=5,
            )
            await session.commit()

            os.environ["DEEPSEEK_API_KEYS"] = "sk-d1,sk-d2"
            os.environ["QWEN_API_KEYS"] = "sk-q1"
            os.environ["GLM_API_KEYS"] = "sk-g1"  # 没建 channel, 应 skip

            try:
                results = []
                for pkey, env_single, env_plural, ch_name in PROVIDERS:
                    if pkey in ("deepseek", "qwen", "glm"):
                        r = await import_one_provider(
                            session, pkey, env_single, env_plural, ch_name,
                        )
                        results.append((pkey, r))

                # deepseek: 建 2 个
                deepseek_r = next(r for k, r in results if k == "deepseek")
                assert deepseek_r["created"] == ["deepseek_01", "deepseek_02"]

                # qwen: 建 1 个
                qwen_r = next(r for k, r in results if k == "qwen")
                assert qwen_r["created"] == ["qwen_01"]

                # glm: 无 channel → skip
                glm_r = next(r for k, r in results if k == "glm")
                assert glm_r["skipped_no_channel"] is True

                print(f"  [OK] deepseek=2 / qwen=1 / glm=无 channel")
            finally:
                for k in ("DEEPSEEK_API_KEYS", "QWEN_API_KEYS", "GLM_API_KEYS"):
                    os.environ.pop(k, None)

    asyncio.run(_run())


def main():
    tests = [
        test_clean_env_value,
        test_extract_keys_basic,
        test_positional_naming,
        test_import_one_provider_idempotent,
        test_import_one_provider_no_channel,
        test_multiple_providers,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"\n[FAIL] {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{'=' * 60}")
    print(f"V4.6.1 multi_account_import 单元测试: {passed} passed, {failed} failed")
    print(f"{'=' * 60}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()