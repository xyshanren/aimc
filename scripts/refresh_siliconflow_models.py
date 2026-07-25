"""从硅基流动 API 拉全 model 列表写入 price_snapshots 表

Phase 3 (#1): /v1/models 端点依赖 price_snapshots 表的 model 字段,
手动录入只覆盖 2 个 model (deepseek-ai/DeepSeek-V3 + Qwen/Qwen2.5-72B-Instruct).
硅基流动是聚合平台 fallback, 1 个 key 通吃 91+ model, 入库后
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
import httpx
from sqlalchemy import select

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import Channel, PriceSnapshot


SF_BASE = "https://api.siliconflow.cn/v1"
SF_CHANNEL_NAME = "siliconflow_aggregator"


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


async def fetch_sf_models(api_key: str) -> list[str]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{SF_BASE}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        r.raise_for_status()
        data = r.json()
    return [m["id"] for m in data.get("data", [])]


async def main():
    await init_db()
    api_key = load_sf_key()

    print("拉取硅基流动 model 列表...")
    models = await fetch_sf_models(api_key)
    print(f"  拿到 {len(models)} 个 model")

    async with async_session_factory() as session:
        # 找 channel
        ch = (
            await session.execute(
                select(Channel).where(Channel.name == SF_CHANNEL_NAME)
            )
        ).scalars().first()
        if ch is None:
            raise RuntimeError(
                f"{SF_CHANNEL_NAME} channel 不存在, "
                "先跑 scripts/manual_import.py 创建"
            )
        ch_id = ch.id

        # 已存在的
        existing = set(
            (
                await session.execute(
                    select(PriceSnapshot.model).where(PriceSnapshot.channel_id == ch_id)
                )
            ).scalars().all()
        )
        print(f"  已有 {len(existing)} 个 model 在 prices 表里")

        to_add = [m for m in models if m not in existing]
        print(f"  新增 {len(to_add)} 个")

        if not to_add:
            print("  无新增, 数据库已是最新")
            return

        for model in to_add:
            session.add(
                PriceSnapshot(
                    channel_id=ch_id,
                    model=model,
                    price_type="normal",
                    time_range="全天",
                    input_price=None,
                    output_price=None,
                    unit="元/百万tokens",
                    source="api:siliconflow",
                )
            )
        await session.commit()
        print(f"  [OK] 提交 {len(to_add)} 条 price_snapshot")

    # 提示 router cache
    print()
    print("提示: router cache 是启动时加载的, 重启 uvicorn 后 /v1/models 才看得到新 model")


if __name__ == "__main__":
    asyncio.run(main())
