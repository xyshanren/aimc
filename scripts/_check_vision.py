"""Check vision models in registry."""
import asyncio
import sys

sys.path.insert(0, "/home/lxc/aimc")

from db.engine import async_session_factory
from sqlalchemy import text


async def main():
    async with async_session_factory() as s:
        r = await s.execute(
            text(
                "SELECT model_id, family, tier, capabilities, modality "
                "FROM model_registry "
                "WHERE modality IN ('vision', 'multimodal') "
                "   OR capabilities LIKE '%vision%' "
                "ORDER BY tier LIMIT 30"
            )
        )
        print("=== vision / multimodal models in registry ===")
        for row in r:
            d = dict(row._mapping)
            print(f"  {d}")


asyncio.run(main())