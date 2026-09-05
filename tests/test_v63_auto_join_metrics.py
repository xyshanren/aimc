"""V6.3 F3: 自动入组埋点端点测试 + F1 首启检测函数测试

覆盖:
1. GET /dashboard/api/ops/auto-join-metrics — 5 组指标的种子数据验证
   (pending 积压/周桶、tier 分布、审批处理量、组路由命中、infer_tier 盲区)
2. gateway.app._check_empty_channels — 三分支 (空库/有渠道无账号/正常) 日志断言

跑法:
    python -m pytest tests/test_v63_auto_join_metrics.py -v
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ============================================================
# Mock config + 临时 SQLite (同 test_v62_request_logs 模式)
# ============================================================

def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v63_aj_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = "sqlite+aiosqlite:///" + db_path

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


def _get_client():
    import httpx
    from fastapi import FastAPI
    from dashboard.routes import router as dashboard_router

    app = FastAPI()
    app.include_router(dashboard_router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _reset_db():
    import config
    from db.init_db import init_db

    await init_db()
    conn = sqlite3.connect(config.settings.db_path)
    # 表名固定枚举 (非外部输入), 逐条字面量写死以满足静态审查
    conn.execute("DELETE FROM usage_logs")
    conn.execute("DELETE FROM model_capabilities_log")
    conn.execute("DELETE FROM channels")
    conn.execute("DELETE FROM accounts")
    conn.commit()
    conn.close()


async def _seed_cap_log(factory, model_id, field_changed="metadata", old_value=None,
                        new_value=None, source="ai_infer", confidence=0.7,
                        approved=0, created_at=None):
    from db.models import ModelCapabilitiesLog

    async with factory() as session:
        row = ModelCapabilitiesLog(
            model_id=model_id,
            field_changed=field_changed,
            old_value=old_value,
            new_value=new_value,
            source=source,
            confidence=confidence,
            approved=approved,
            provider="mock",
            created_at=created_at or datetime.now(),
        )
        session.add(row)
        await session.commit()
        return row.id


async def _seed_usage_log(factory, group_id=None, created_at=None):
    from db.models import UsageLog

    async with factory() as session:
        row = UsageLog(
            model="mock-model",
            group_id=group_id,
            input_tokens=100,
            output_tokens=50,
            status_code=200,
            created_at=created_at or datetime.now(),
        )
        session.add(row)
        await session.commit()
        return row.id


# ============================================================
# Part 1: /api/ops/auto-join-metrics
# ============================================================

def test_auto_join_metrics_full_seed():
    """种子覆盖 5 组指标: 积压/周桶、tier 分布、处理量、组路由、盲区"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        now = datetime.now()
        # pending 积压: 2 条 (1 条本周 metadata + 1 条 10 天前 tier)
        meta_json = json.dumps({"model_id": "m1", "inferred_tier": None}, ensure_ascii=False)
        await _seed_cap_log(async_session_factory, "m1", field_changed="metadata",
                            new_value=meta_json, created_at=now - timedelta(days=2))
        await _seed_cap_log(async_session_factory, "m2", field_changed="tier",
                            old_value=None, new_value="T2",
                            created_at=now - timedelta(days=10))
        # 已处理 1 条 (approved=1, 3 天前) → 审批负担
        await _seed_cap_log(async_session_factory, "m3", field_changed="tier",
                            old_value="T2", new_value="T1", approved=1,
                            created_at=now - timedelta(days=3))
        # 组路由: 3 条 tier:strong + 1 条 NULL (精确路由)
        await _seed_usage_log(async_session_factory, "tier:strong", created_at=now)
        await _seed_usage_log(async_session_factory, "tier:strong", created_at=now)
        await _seed_usage_log(async_session_factory, "tier:strong", created_at=now)
        await _seed_usage_log(async_session_factory, None, created_at=now)
        async with _get_client() as client:
            resp = await client.get("/dashboard/api/ops/auto-join-metrics?days=14")
        assert resp.status_code == 200
        return resp.json()

    data = asyncio.run(_run())

    assert data["days"] == 14
    assert data["auto_join_enabled"] is False  # AUTO_JOIN_NON_T0 默认 False
    # 1) 积压: 2 条, 本周桶 1 + 7-14d 桶 1
    pr = data["pending_review"]
    assert pr["total"] == 2
    buckets = {w["bucket"]: w["count"] for w in pr["weekly"]}
    assert buckets["0-7d"] == 1
    assert buckets["7-14d"] == 1
    # 2) tier 分布: T2=1 (pending 行), T1=1 (approved 行)
    assert data["tier_distribution"] == {"T2": 1, "T1": 1}
    # 3) 审批负担: 1 次处理 (近 14 天 approved=1)
    assert data["review_burden"]["reviewed_count"] == 1
    # 4) 组路由: 3/4 = 75.0%
    gr = data["group_routing"]
    assert gr["total_requests"] == 4
    assert gr["grouped_requests"] == 3
    assert gr["grouped_pct"] == 75.0
    by_label = {g["group_id"]: g for g in gr["items"]}
    assert by_label["tier:strong"]["count"] == 3
    assert by_label["(精确路由)"]["count"] == 1
    # 5) 盲区: 1 条 metadata 里 inferred_tier=null → 100%
    bs = data["tier_blind_spot"]
    assert bs["metadata_total"] == 1
    assert bs["tier_null"] == 1
    assert bs["pct"] == 100.0


def test_auto_join_metrics_empty_db():
    """空库: 全零指标, 不报错, pct 为 0.0"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        async with _get_client() as client:
            resp = await client.get("/dashboard/api/ops/auto-join-metrics?days=14")
        assert resp.status_code == 200
        return resp.json()

    data = asyncio.run(_run())
    assert data["pending_review"]["total"] == 0
    assert data["tier_distribution"] == {}
    assert data["review_burden"]["reviewed_count"] == 0
    assert data["group_routing"]["total_requests"] == 0
    assert data["group_routing"]["grouped_pct"] == 0.0
    assert data["tier_blind_spot"]["metadata_total"] == 0


def test_auto_join_metrics_invalid_new_value_json_skipped():
    """metadata 行 new_value 非法 JSON → 计数跳过 (不 crash 也不计入)"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        await _seed_cap_log(async_session_factory, "mx", field_changed="metadata",
                            new_value="not-json{{{")
        async with _get_client() as client:
            resp = await client.get("/dashboard/api/ops/auto-join-metrics?days=14")
        assert resp.status_code == 200
        return resp.json()

    data = asyncio.run(_run())
    assert data["tier_blind_spot"]["metadata_total"] == 0


# ============================================================
# Part 2: F1 首启检测 _check_empty_channels
# ============================================================

async def _reset_db_full():
    import config
    from db.init_db import init_db

    await init_db()
    conn = sqlite3.connect(config.settings.db_path)
    conn.execute("DELETE FROM accounts")
    conn.execute("DELETE FROM channels")
    conn.commit()
    conn.close()


async def _seed_channel_via_sql(name="ch1"):
    from sqlalchemy import text

    from db.engine import async_session_factory

    async with async_session_factory() as session:
        await session.execute(
            text("INSERT INTO channels (name, provider, base_url, status) "
                 "VALUES (:n, :p, :b, 'active')"),
            {"n": name, "p": "zhipu", "b": "https://mock.local"},
        )
        await session.commit()


def test_check_empty_channels_warns_on_empty_db(caplog):
    """空库 (无渠道) → WARN 首启引导, 提示看板/manual_import 两条路径"""
    from gateway.app import _check_empty_channels

    async def _run():
        # 不重跑 _install_mock_env: db.engine 首次导入时已绑定模块级临时库,
        # 重装 settings 会让 init_db 与 sqlite3.connect 指向不同文件
        await _reset_db_full()
        with caplog.at_level(logging.WARNING, logger="gateway.app"):
            await _check_empty_channels()
        return [r for r in caplog.records if "首启引导" in r.message]

    records = asyncio.run(_run())
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "没有任何渠道" in msg
    assert "账号管理" in msg
    assert "manual_import" in msg


def test_check_empty_channels_warns_on_no_active_accounts(caplog):
    """有渠道无 active 账号 → WARN 引导加账号"""
    from gateway.app import _check_empty_channels

    async def _run():
        await _reset_db_full()
        await _seed_channel_via_sql("only-channel")
        with caplog.at_level(logging.WARNING, logger="gateway.app"):
            await _check_empty_channels()
        return [r for r in caplog.records if "首启引导" in r.message]

    records = asyncio.run(_run())
    assert len(records) == 1
    msg = records[0].getMessage()
    assert "无 active 账号" in msg


def test_check_empty_channels_silent_when_ready(caplog):
    """有渠道 + 有 active 账号 → 无任何首启引导日志"""
    from gateway.app import _check_empty_channels
    from db.engine import async_session_factory
    from db.models import Account

    async def _run():
        await _reset_db_full()
        await _seed_channel_via_sql("ready-ch")
        async with async_session_factory() as session:
            session.add(Account(channel_id=1, account_name="acc1", api_key="sk-x",
                                status="active"))
            await session.commit()
        with caplog.at_level(logging.WARNING, logger="gateway.app"):
            await _check_empty_channels()
        return [r for r in caplog.records if "首启引导" in r.message]

    records = asyncio.run(_run())
    assert records == []
