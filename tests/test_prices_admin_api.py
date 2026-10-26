# -*- coding: utf-8 -*-
"""价格表行内编辑 API 测试 (2026-10-25)

覆盖 dashboard/onboarding.py 价格管理三端点 + GET /api/prices 新字段:
1. PUT /dashboard/api/prices/{id}: 补价 roundtrip + 字段校验 (类型/负数/时段格式)
2. 全天行与时段行并存护栏 (409): 取价按 id 序首个时段命中, 定价的 normal+全天行
   会让峰谷行永不生效 (2026-10-24 补分时段价踩过的坑); NULL 全天行不受限
3. POST /dashboard/api/prices: 新增 + 四元组唯一冲突 409
4. DELETE: 删除 + 404
5. GET /dashboard/api/prices: cached_input_price / channel_id 字段 (modal 依赖)
6. 严格 raw id 渠道 (siliconflow) 规范名行 raw_model_id 护栏: 兄弟行自动回填 /
   无兄弟 422 / 显式直存 / raw 行与非 strict 渠道放行 (2026-10-26 glm-5.3-flash
   规范名行压住 raw 行致 20 天全 400 事件的防复发护栏)

跑法: python -m pytest tests/test_prices_admin_api.py -v
"""

import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_prices_admin_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"
    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=False,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
    )


_install_mock_env()


def _get_client():
    import httpx
    from fastapi import FastAPI
    from dashboard.onboarding import router as onboarding_router

    app = FastAPI()
    app.include_router(onboarding_router)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _seed():
    """渠道 + 2 价行 (NULL 全天 normal + 已定价 offpeak 时段), 返回行 id"""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from db.models import Base, Channel, PriceSnapshot

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    # 用与 endpoints 相同的 session factory: 绑定 db.engine 的默认 engine 不可用
    # (mock env 各测试独立 tmp db), 这里直接建独立库并 monkeypatch factory
    import db.engine as db_engine_mod
    db_engine_mod.async_session_factory = maker

    async with maker() as s:
        s.add(Channel(name="t_ch", provider="t", base_url="https://t.local/v1"))
        await s.flush()
        s.add(PriceSnapshot(channel_id=1, model="m-a", price_type="normal",
                            time_range="全天", input_price=None, output_price=None,
                            source="api:siliconflow"))
        s.add(PriceSnapshot(channel_id=1, model="m-a", price_type="offpeak",
                            time_range="02:00-08:00", input_price=1.0, output_price=4.0))
        await s.commit()
        rows = (await s.execute(
            __import__("sqlalchemy").select(PriceSnapshot.id)
            .order_by(PriceSnapshot.id)
        )).scalars().all()
    return sorted(rows)  # [NULL全天行 id, offpeak 行 id]


def test_edit_roundtrip_and_validation():
    import asyncio

    async def run():
        await _seed()
        async with _get_client() as client:
            # 自建无兄弟行的 NULL 价行 (无时段兄弟 → 补价保留全天不触发护栏)
            r = await client.post("/dashboard/api/prices", json={
                "channel_id": 1, "model": "m-rt", "price_type": "normal", "time_range": "全天"})
            assert r.status_code == 200, r.text
            row_id = r.json()["id"]

            # 补价 roundtrip
            r = await client.put(f"/dashboard/api/prices/{row_id}", json={
                "input_price": "3.0", "output_price": 12, "cached_input_price": None})
            assert r.status_code == 200, r.text
            assert r.json()["ok"] is True

            from db.models import PriceSnapshot
            import db.engine as m
            async with m.async_session_factory() as s:
                row = await s.get(PriceSnapshot, row_id)
                assert row.input_price == 3.0 and row.output_price == 12.0
                assert row.cached_input_price is None
                assert row.source == "manual:dashboard"
                await s.close()

            # 校验: 非法 price_type / 负数 / 非法时段 / 空 body / 不存在行
            r = await client.put(f"/dashboard/api/prices/{row_id}",
                                 json={"price_type": "vip"})
            assert r.status_code == 400
            r = await client.put(f"/dashboard/api/prices/{row_id}",
                                 json={"input_price": -1})
            assert r.status_code == 400
            r = await client.put(f"/dashboard/api/prices/{row_id}",
                                 json={"time_range": "99:00-25:00"})
            assert r.status_code == 400
            r = await client.put(f"/dashboard/api/prices/{row_id}", json={})
            assert r.status_code == 400
            r = await client.put("/dashboard/api/prices/99999", json={"input_price": 1})
            assert r.status_code == 404

    asyncio.run(run())


def test_all_day_conflict_guard():
    """定价的 normal+全天 行 + 时段行并存 → 409; NULL 全天行放行; 改成时段行放行"""
    import asyncio

    async def run():
        ids = await _seed()
        null_row_id = ids[0]
        async with _get_client() as client:
            # NULL 全天行 + offpeak 兄弟行: 直接补价保留全天 → 409 (峰谷行会被饿死)
            r = await client.put(f"/dashboard/api/prices/{null_row_id}",
                                 json={"input_price": 5.0})
            assert r.status_code == 409, r.text
            assert "时段" in r.json()["detail"]

            # 同一行改成时段行 + 补价 → 200 (2026-10-24 补分时段价的正确姿势)
            r = await client.put(f"/dashboard/api/prices/{null_row_id}", json={
                "time_range": "00:00-02:00", "input_price": 2.0, "output_price": 8.0})
            assert r.status_code == 200, r.text

    asyncio.run(run())


def test_create_delete_and_dup_409():
    import asyncio

    async def run():
        await _seed()
        async with _get_client() as client:
            r = await client.post("/dashboard/api/prices", json={
                "channel_id": 1, "model": "m-b", "price_type": "normal",
                "time_range": "全天", "input_price": 0.0, "output_price": 0.0})
            assert r.status_code == 200, r.text
            new_id = r.json()["id"]

            # 四元组重复 → 409
            r = await client.post("/dashboard/api/prices", json={
                "channel_id": 1, "model": "m-b", "price_type": "normal",
                "time_range": "全天", "input_price": 1.0})
            assert r.status_code == 409

            # 渠道不存在 / 缺 model
            r = await client.post("/dashboard/api/prices", json={
                "channel_id": 99, "model": "x"})
            assert r.status_code == 404
            r = await client.post("/dashboard/api/prices", json={"channel_id": 1, "model": " "})
            assert r.status_code == 400

            # 删除 → 200, 再删 → 404
            r = await client.delete(f"/dashboard/api/prices/{new_id}")
            assert r.status_code == 200
            r = await client.delete(f"/dashboard/api/prices/{new_id}")
            assert r.status_code == 404

    asyncio.run(run())


def test_get_prices_fields_for_modal():
    import asyncio

    async def run():
        await _seed()
        import httpx
        from fastapi import FastAPI
        from dashboard.routes import router as dashboard_router

        app = FastAPI()
        app.include_router(dashboard_router)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver") as client:
            r = await client.get("/dashboard/api/prices")
            assert r.status_code == 200
            rows = r.json()
            assert rows, "种子两行应可见"
            for row in rows:
                assert "cached_input_price" in row and "channel_id" in row

    asyncio.run(run())


def test_modelscope_size_gate_default():
    """modelscope 大请求门槛默认值 (2026-10-25): 与 sensenova 同窗 30k"""
    from config import Settings
    s = Settings()
    assert s.channel_max_input_tokens.get("modelscope_inference") == 30000
    assert s.channel_max_input_tokens.get("sensenova_official") == 30000
    assert s.channel_min_input_tokens.get("taotoken_coding") == 30000


def test_raw_model_id_guard_siliconflow():
    """严格 raw id 渠道护栏: 规范名行缺 raw_model_id → 兄弟行回填 / 无兄弟 422"""
    import asyncio

    async def run():
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from db.models import Base, Channel, PriceSnapshot

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        import db.engine as db_engine_mod
        db_engine_mod.async_session_factory = maker

        async with maker() as s:
            s.add(Channel(name="sf_ch", provider="siliconflow",
                          base_url="https://sf.local/v1"))
            s.add(Channel(name="t_ch", provider="t", base_url="https://t.local/v1"))
            await s.flush()
            s.add(PriceSnapshot(channel_id=1, model="zai-org/GLM-5.3-Flash",
                                price_type="normal", time_range="全天",
                                input_price=0.8, output_price=2.8,
                                raw_model_id="zai-org/GLM-5.3-Flash",
                                source="api:siliconflow"))
            await s.commit()

        async with _get_client() as client:
            # 1. sf 规范名行 + 同渠道 raw 兄弟 → 200, 自动回填 raw_model_id
            r = await client.post("/dashboard/api/prices", json={
                "channel_id": 1, "model": "glm-5.3-flash", "input_price": 0.8})
            assert r.status_code == 200, r.text
            glm_row_id = r.json()["id"]
            async with maker() as s:
                row = await s.get(PriceSnapshot, glm_row_id)
                assert row.raw_model_id == "zai-org/GLM-5.3-Flash"

            # 2. 显式 raw_model_id → 直存不覆盖
            r = await client.post("/dashboard/api/prices", json={
                "channel_id": 1, "model": "mock-canonical",
                "raw_model_id": "mock-org/Mock-Raw"})
            assert r.status_code == 200, r.text
            mock_row_id = r.json()["id"]
            async with maker() as s:
                row = await s.get(PriceSnapshot, mock_row_id)
                assert row.raw_model_id == "mock-org/Mock-Raw"

            # 3. sf 规范名行 + 无兄弟 + 缺 raw_model_id → 422 (裸发上游必 400)
            r = await client.post("/dashboard/api/prices", json={
                "channel_id": 1, "model": "orphan-model"})
            assert r.status_code == 422, r.text
            assert "raw_model_id" in r.json()["detail"]

            # 4. raw id 行 (model 含 "/") 本身不受限
            r = await client.post("/dashboard/api/prices", json={
                "channel_id": 1, "model": "org/New-Raw-Model"})
            assert r.status_code == 200, r.text

            # 5. 非 strict 渠道规范名行缺 raw_model_id → 放行 (上游认规范名)
            r = await client.post("/dashboard/api/prices", json={
                "channel_id": 2, "model": "plain-model"})
            assert r.status_code == 200, r.text

            # 6. PUT 清空 raw_model_id → 有兄弟时自动兜底回填
            r = await client.put(f"/dashboard/api/prices/{glm_row_id}",
                                 json={"raw_model_id": ""})
            assert r.status_code == 200, r.text
            async with maker() as s:
                row = await s.get(PriceSnapshot, glm_row_id)
                assert row.raw_model_id == "zai-org/GLM-5.3-Flash"

            # 7. GET /api/prices 带 raw_model_id 字段 (modal 可见)
            import httpx
            from fastapi import FastAPI
            from dashboard.routes import router as dashboard_router

            app = FastAPI()
            app.include_router(dashboard_router)
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver") as client2:
                r = await client2.get("/dashboard/api/prices")
                assert r.status_code == 200
                rows = r.json()
                assert rows
                assert all("raw_model_id" in row for row in rows)

    asyncio.run(run())
