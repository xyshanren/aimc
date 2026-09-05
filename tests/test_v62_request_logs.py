"""Phase 3b: 请求日志端点测试

覆盖:
1. GET /dashboard/api/requests — 时间窗/状态过滤 + 分页 + channel name 反查
2. GET /dashboard/api/requests/{id}/transcript — model+时间窗最近匹配 (best-effort),
   未匹配 matched=false, 超长截断 truncated=true, 未知 id 404

跑法:
    python -m pytest tests/test_v62_request_logs.py -v
"""

import asyncio
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
# Mock config + 临时 SQLite (同 test_v48_dashboard 模式)
# ============================================================

def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v62_reqlog_")
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
    conn.execute("DELETE FROM usage_logs")
    conn.execute("DELETE FROM request_transcripts")
    conn.execute("DELETE FROM channels")
    conn.commit()
    conn.close()


async def _seed_channel(factory, name="glm_bigmodel"):
    """原生 SQL 种子 (参数化绑定): channels 表结构跨版本有列差, 只填四个必有列"""
    from sqlalchemy import text

    async with factory() as session:
        result = await session.execute(
            text("INSERT INTO channels (name, provider, base_url, status) "
                 "VALUES (:n, :p, :b, 'active')"),
            {"n": name, "p": "zhipu", "b": "https://mock.local"},
        )
        await session.commit()
        return result.lastrowid


async def _seed_api_key(factory, label="rag", key_prefix="aimc-lV2"):
    from sqlalchemy import text
    from db.models import ApiKey, User

    async with factory() as session:
        # api_keys.user_id 有 FK, 先确保测试用户存在 (幂等)
        row = (await session.execute(
            text("SELECT id FROM users WHERE username = 'tester'")
        )).scalar()
        if row is None:
            user = User(username="tester", display_name="T", role="user", status="active")
            session.add(user)
            await session.commit()
            user_id = user.id
        else:
            user_id = row
        k = ApiKey(user_id=user_id, key_hash="hash-" + label, key_prefix=key_prefix,
                   is_admin=0, status="active", label=label)
        session.add(k)
        await session.commit()
        return k.id


async def _seed_log(factory, model="glm-5.3-flash", channel_id=None, key_id=None,
                    status_code=200, created_at=None, cached=0):
    from db.models import UsageLog

    async with factory() as session:
        log = UsageLog(
            model=model,
            channel_id=channel_id,
            key_id=key_id,
            input_tokens=1000,
            output_tokens=500,
            cached_input_tokens=cached,
            status_code=status_code,
            latency_ms=1500,
            cost_estimate=0.01,
            list_cost_estimate=0.05,
            created_at=created_at or datetime.now(),
        )
        session.add(log)
        await session.commit()
        return log.id


async def _seed_transcript(factory, model="glm-5.3-flash", channel_id=None, key_id=None,
                           request_body='{"model":"x"}', response_body='{"ok":true}',
                           created_at=None):
    from db.models import RequestTranscript

    async with factory() as session:
        tr = RequestTranscript(
            model=model,
            channel_id=channel_id,
            key_id=key_id,
            request_body=request_body,
            response_body=response_body,
            status_code=200,
            created_at=created_at or datetime.now(),
        )
        session.add(tr)
        await session.commit()
        return tr.id


# ============================================================
# Part 1: /api/requests 列表
# ============================================================

def test_requests_list_filters_and_pagination():
    """时间窗过滤 + ok/error 状态过滤 + channel name 反查 + 分页"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        ch_id = await _seed_channel(async_session_factory)
        key_id = await _seed_api_key(async_session_factory, label="rag")
        now = datetime.now()
        # 2 条 7 天内 (1 ok 1 error) + 1 条 10 天前 (应被 days=7 过滤)
        id_ok = await _seed_log(async_session_factory, "glm-5.3-flash",
                                channel_id=ch_id, key_id=key_id,
                                status_code=200, created_at=now - timedelta(hours=1))
        await _seed_log(async_session_factory, "deepseek-v4-flash",
                        channel_id=ch_id, status_code=500,
                        created_at=now - timedelta(hours=2))
        await _seed_log(async_session_factory, "glm-5.3",
                        status_code=200, created_at=now - timedelta(days=10))

        async with _get_client() as client:
            r_all = await client.get("/dashboard/api/requests?days=7")
            r_err = await client.get("/dashboard/api/requests?days=7&status=error")
            r_page = await client.get("/dashboard/api/requests?days=7&page=1&page_size=1")
        return id_ok, r_all.json(), r_err.json(), r_page.json()

    id_ok, all_data, err_data, page_data = asyncio.run(_run())

    assert all_data["total"] == 2
    assert len(all_data["items"]) == 2
    # 时间倒序: 最新的在前
    assert all_data["items"][0]["id"] == id_ok
    assert all_data["items"][0]["channel_name"] == "glm_bigmodel"
    assert all_data["items"][0]["key_label"] == "rag"  # key label 反查
    # 未带 key_id 的行 → key_label None
    assert all_data["items"][1]["key_label"] is None
    assert all_data["items"][0]["cached_tokens"] == 0
    assert all_data["items"][0]["list_cost_yuan"] == 0.05

    assert err_data["total"] == 1
    assert err_data["items"][0]["status_code"] == 500
    assert err_data["items"][0]["model"] == "deepseek-v4-flash"

    assert page_data["total"] == 2
    assert len(page_data["items"]) == 1


def test_requests_list_days_window_excludes_old():
    """days=1 只含 24h 内"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        now = datetime.now()
        await _seed_log(async_session_factory, "a", created_at=now - timedelta(hours=2))
        await _seed_log(async_session_factory, "b", created_at=now - timedelta(days=3))
        async with _get_client() as client:
            resp = await client.get("/dashboard/api/requests?days=1")
        return resp.json()

    data = asyncio.run(_run())
    assert data["total"] == 1
    assert data["items"][0]["model"] == "a"


def test_requests_list_model_filter_v63():
    """V6.3 F2: model 模糊匹配 (LIKE %model%) + 与 status 组合"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        now = datetime.now()
        await _seed_log(async_session_factory, "glm-5.3-flash", status_code=200,
                        created_at=now - timedelta(hours=1))
        await _seed_log(async_session_factory, "glm-5.3", status_code=500,
                        created_at=now - timedelta(hours=2))
        await _seed_log(async_session_factory, "deepseek-v4-flash", status_code=200,
                        created_at=now - timedelta(hours=3))
        async with _get_client() as client:
            r_fuzzy = await client.get("/dashboard/api/requests?days=7&model=glm")
            r_exact = await client.get("/dashboard/api/requests?days=7&model=glm-5.3-flash")
            r_combo = await client.get("/dashboard/api/requests?days=7&model=glm&status=error")
            r_none = await client.get("/dashboard/api/requests?days=7&model=qwen")
        return r_fuzzy.json(), r_exact.json(), r_combo.json(), r_none.json()

    fuzzy, exact, combo, none_ = asyncio.run(_run())
    # 模糊匹配: glm 命中 2 条
    assert fuzzy["total"] == 2
    assert all("glm" in i["model"] for i in fuzzy["items"])
    # 精确子串只命中 1 条
    assert exact["total"] == 1
    assert exact["items"][0]["model"] == "glm-5.3-flash"
    # 组合过滤: glm + error 只剩 glm-5.3
    assert combo["total"] == 1
    assert combo["items"][0]["model"] == "glm-5.3"
    assert combo["model"] == "glm"  # 回显
    # 无命中
    assert none_["total"] == 0


# ============================================================
# Part 2: transcript 详情
# ============================================================

def test_transcript_matched_by_model_and_window():
    """同 model + 时间窗内的转录 → matched=true, body 返回; 窗外干扰项不命中"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        now = datetime.now()
        log_id = await _seed_log(async_session_factory, "glm-5.3-flash", key_id=3,
                                 created_at=now)
        # 干扰项: 同 model 但 30 分钟前 (窗外)
        await _seed_transcript(async_session_factory, "glm-5.3-flash", key_id=3,
                               request_body="OLD", response_body="OLD",
                               created_at=now - timedelta(minutes=30))
        # 命中项: 窗口内
        await _seed_transcript(async_session_factory, "glm-5.3-flash", key_id=3,
                               request_body='{"prompt":"hi"}',
                               response_body='{"answer":"yo"}',
                               created_at=now + timedelta(seconds=2))
        async with _get_client() as client:
            resp = await client.get(f"/dashboard/api/requests/{log_id}/transcript")
        assert resp.status_code == 200
        return resp.json()

    data = asyncio.run(_run())
    assert data["matched"] is True
    assert data["request_body"] == '{"prompt":"hi"}'
    assert data["response_body"] == '{"answer":"yo"}'
    assert data["truncated"] is False


def test_transcript_no_match_returns_matched_false():
    """窗外无转录 → matched=false, body 为 null, 不报错"""
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        now = datetime.now()
        log_id = await _seed_log(async_session_factory, "glm-5.3-flash", created_at=now)
        await _seed_transcript(async_session_factory, "glm-5.3-flash",
                               created_at=now - timedelta(hours=1))
        async with _get_client() as client:
            resp = await client.get(f"/dashboard/api/requests/{log_id}/transcript")
        assert resp.status_code == 200
        return resp.json()

    data = asyncio.run(_run())
    assert data["matched"] is False
    assert data["request_body"] is None


def test_transcript_truncates_oversize_body():
    """超 256KB 截断 + truncated=true (读取侧第二道闸)"""
    from db.engine import async_session_factory
    from dashboard.routes import _TRANSCRIPT_CLIP

    async def _run():
        await _reset_db()
        now = datetime.now()
        log_id = await _seed_log(async_session_factory, "glm-5.3-flash", created_at=now)
        big = "x" * (_TRANSCRIPT_CLIP + 1000)
        await _seed_transcript(async_session_factory, "glm-5.3-flash",
                               request_body=big, response_body='{"ok":1}',
                               created_at=now + timedelta(seconds=1))
        async with _get_client() as client:
            resp = await client.get(f"/dashboard/api/requests/{log_id}/transcript")
        assert resp.status_code == 200
        return resp.json()

    data = asyncio.run(_run())
    assert data["matched"] is True
    assert data["truncated"] is True
    assert len(data["request_body"]) == _TRANSCRIPT_CLIP
    assert data["response_body"] == '{"ok":1}'


def test_transcript_unknown_log_404():
    from db.engine import async_session_factory

    async def _run():
        await _reset_db()
        async with _get_client() as client:
            resp = await client.get("/dashboard/api/requests/99999/transcript")
        return resp.status_code

    assert asyncio.run(_run()) == 404
