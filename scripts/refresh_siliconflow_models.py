"""从硅基流动 API 拉全 model 列表写入 price_snapshots 表

Phase 2a+ fix (2026-08-13): 重构, 核心逻辑抽到 scripts/_siliconflow_sync.py
- fetch_sf_models: 拉硅基流动 /v1/models
- sync_siliconflow_models_to_price_snapshots: 增量 upsert 到硅基流动 channel
- 本脚本只保留 CLI 入口 (load_sf_key + print + 调上面两个函数)

Phase 3 (#1) 原设计:
- /v1/models 端点依赖 price_snapshots 表的 model 字段,
  手动录入只覆盖 2 个 model (deepseek-ai/DeepSeek-V3 + Qwen/Qwen2.5-72B-Instruct).
- 硅基流动是聚合平台 fallback, 1 个 key 通吃 91+ model, 入库后
  用户打开 /v1/models 能直接看到 AIMC 网关能代理的全部 model.

设计:
- 只入 price_snapshots, input_price/output_price 留空 (聚合平台无自有价格)
- 已存在的 model 跳过 (idempotent, 可重跑)
- price_type=normal / time_range=全天 / source=api:siliconflow
- effective_from/to 留空 (= 长期有效, 跟 _is_price_effective 行为一致)

用法:
    python scripts/refresh_siliconflow_models.py

依赖:
    .env 里有 SILICONFLOW_API_KEY (manual_import.py 跑过就有)
"""
import asyncio
import sys
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values

from db.engine import async_session_factory
from db.init_db import init_db
from scripts._siliconflow_sync import (
    SF_CHANNEL_NAME,
    fetch_sf_models,
    sync_siliconflow_models_to_price_snapshots,
)


def _clean(value: str) -> str:
    """跟 check_env.py / manual_import.py 一致: 清洗行内注释"""
    v = (value or "").strip()
    if v.lstrip().startswith("#"):
        return ""
    if " #" in v:
        v = v.split(" #", 1)[0].rstrip()
    return v


def load_sf_key() -> str:
    env = dotenv_values(ROOT / ".env")
    key = _clean(env.get("SILICONFLOW_API_KEY", ""))
    if not key or "PLACEHOLDER" in key.upper():
        raise RuntimeError("SILICONFLOW_API_KEY 未配置或含 PLACEHOLDER, 先跑 manual_import.py")
    return key


async def main():
    await init_db()
    api_key = load_sf_key()

    print("拉取硅基流动 model 列表...")
    models = await fetch_sf_models(api_key)
    print(f"  拿到 {len(models)} 个 model")

    # 查已存在数量 (print 用)
    from sqlalchemy import select
    from db.models import Channel, PriceSnapshot

    async with async_session_factory() as session:
        ch = (await session.execute(
            select(Channel).where(Channel.name == SF_CHANNEL_NAME)
        )).scalars().first()
        existing = set((await session.execute(
            select(PriceSnapshot.model).where(PriceSnapshot.channel_id == ch.id)
        )).scalars().all()) if ch else set()
        print(f"  已有 {len(existing)} 个 model 在 prices 表里")

        added = await sync_siliconflow_models_to_price_snapshots(session, models)
        print(f"  [OK] 新增 {added} 条 price_snapshot")
        if added:
            print(f"  提示: 重启 uvicorn 或 POST /dashboard/api/cache/refresh 让路由 cache 拿到新 model")


if __name__ == "__main__":
    asyncio.run(main())
