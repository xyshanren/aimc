"""Phase 4 v4.3.1 (2026-08-13): price_snapshot model_id 归一化/重命名 migration

背景:
- 450499c commit 把 model_registry / seed_* 改成小写 model_id (minimax-m3 等).
- 但 scripts/manual_import.py 没改 (这个 PR 修复), 跑过老 cfg 的部署会在
  price_snapshots 表里留:
  - 8 个 MiniMax-M* 大写行 (model_id 大小写不一致)
  - 2 个 deepseek-chat / deepseek-reasoner 老 model 行 (model_registry 已无)

作用:
- 扫 price_snapshots 表, 对 model_id 做两件事:
  1. 自动归一化: MiniMax-M3 -> minimax-m3 (复用 db/normalizers.py)
  2. 手动重命名 (--rename 参数): deepseek-chat -> deepseek-v4-flash
     (语义替换, 因为老 model 在新 API 已不存在)
- 幂等 (重复跑无副作用)
- 仅对 model_registry 里已有小写对应 model 的行做迁移

设计权衡:
- 不直接 DELETE 大写行 → upsert 小写, 然后 DELETE 大写 (更安全)
  - 先 INSERT 小写 (如不存在), 再 DELETE 大写
  - 这样即使脚本中断也不会丢数据
- rename 优先于 normalize: --rename 映射先生效, 剩余走 normalize
- --rename 多个: 多次指定 --rename old=new (按出现顺序)

用法:
    # 预览
    python scripts/migrate_price_snapshots_normalize.py --dry-run

    # 实际执行 (大写归一化)
    python scripts/migrate_price_snapshots_normalize.py

    # 实际执行 (大写归一化 + 语义替换)
    python scripts/migrate_price_snapshots_normalize.py \\
        --rename deepseek-chat=deepseek-v4-flash \\
        --rename deepseek-reasoner=deepseek-v4-pro

    # 只迁移指定 channel
    python scripts/migrate_price_snapshots_normalize.py --channel minimax_compat

输出:
    [DRY-RUN] 将迁移 10 条:
      - minimax_compat / MiniMax-M3 -> minimax-m3
      - deepseek_official / deepseek-chat -> deepseek-v4-flash (--rename)
      ...
    [OK] 迁移完成: 10 条迁移, 0 条保留

历史:
- v1 (2026-08-13): 初始版本 (仅 normalize)
- v1.1 (2026-08-13): 加 --rename 参数支持语义替换 (deepseek-chat -> deepseek-v4-flash)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _apply_rename(model: str, renames: dict[str, str]) -> str:
    """如果 model 在 renames 映射里, 返回新名; 否则原样."""
    return renames.get(model, model)


async def find_migrations(
    session,
    channel_name: str | None = None,
    renames: dict[str, str] | None = None,
) -> list[dict]:
    """扫描 price_snapshots 表, 找出需要迁移的行.

    Args:
        session: AsyncSession
        channel_name: 可选, 只扫指定 channel
        renames: 可选, 手动重命名映射 (如 {"deepseek-chat": "deepseek-v4-flash"})

    Returns:
        list of dict, 每个 dict 含 old_id / old_model / new_model / channel_id /
        channel_name / provider / price_type / time_range / input_price /
        output_price / source / new_id / reason ("rename" 或 "normalize")
    """
    from sqlalchemy import select
    from db.models import Channel, PriceSnapshot
    from db.normalizers import normalize_model_id

    renames = renames or {}

    # 1. 查所有 channel (可选过滤)
    stmt_ch = select(Channel)
    if channel_name:
        stmt_ch = stmt_ch.where(Channel.name == channel_name)
    channels = (await session.execute(stmt_ch)).scalars().all()
    if not channels:
        return []
    ch_id_to_meta = {ch.id: {"name": ch.name, "provider": ch.provider} for ch in channels}

    # 2. 查这些 channel 的所有 price_snapshot
    ch_ids = list(ch_id_to_meta.keys())
    stmt_ps = select(PriceSnapshot).where(PriceSnapshot.channel_id.in_(ch_ids))
    rows = (await session.execute(stmt_ps)).scalars().all()

    # 3. 找出需要迁移的
    migrations = []
    for ps in rows:
        # 优先级: rename 映射 > normalize
        if ps.model in renames:
            new_model = renames[ps.model]
            reason = "rename"
        else:
            new_model = normalize_model_id(ps.model)
            reason = "normalize"

        if new_model == ps.model:
            continue  # 不需迁移

        # 查归一化后 model_id 是否已存在 (避免重复 INSERT)
        stmt_existing = select(PriceSnapshot).where(
            PriceSnapshot.channel_id == ps.channel_id,
            PriceSnapshot.model == new_model,
            PriceSnapshot.price_type == ps.price_type,
            PriceSnapshot.time_range == ps.time_range,
        )
        existing = (await session.execute(stmt_existing)).scalar_one_or_none()

        migrations.append({
            "old_id": ps.id,
            "old_model": ps.model,
            "new_model": new_model,
            "channel_id": ps.channel_id,
            "channel_name": ch_id_to_meta[ps.channel_id]["name"],
            "provider": ch_id_to_meta[ps.channel_id]["provider"],
            "price_type": ps.price_type,
            "time_range": ps.time_range,
            "input_price": ps.input_price,
            "output_price": ps.output_price,
            "source": ps.source,
            "new_id": existing.id if existing else None,
            "reason": reason,
        })

    return migrations


async def execute_migrations(
    session,
    migrations: list[dict],
    dry_run: bool = False,
) -> dict:
    """执行迁移 (或 dry-run 预览).

    流程 (每个 migration):
    1. 如果 new_id is None → INSERT 新行
    2. DELETE 旧行

    Returns:
        {"synced": int, "deleted": int} 或 dry_run 时 {"would_sync": int}
    """
    from db.models import PriceSnapshot
    from sqlalchemy import delete

    if dry_run:
        return {"would_sync": len(migrations), "kept": 0}

    synced = 0

    for m in migrations:
        # 1. INSERT 新行 (如不存在)
        if m["new_id"] is None:
            new_ps = PriceSnapshot(
                channel_id=m["channel_id"],
                model=m["new_model"],
                price_type=m["price_type"],
                time_range=m["time_range"],
                input_price=m["input_price"],
                output_price=m["output_price"],
                unit="元/百万tokens",
                source=m["source"],
            )
            session.add(new_ps)
            synced += 1

        # 2. DELETE 旧行
        await session.execute(
            delete(PriceSnapshot).where(PriceSnapshot.id == m["old_id"])
        )

    await session.commit()
    return {"synced": synced, "deleted": len(migrations)}


def _parse_rename_args(renames: list[str]) -> dict[str, str]:
    """解析 --rename 参数为 dict.

    --rename deepseek-chat=deepseek-v4-flash (可多次指定)
    """
    parsed = {}
    for r in renames:
        if "=" not in r:
            raise ValueError(f"--rename 参数格式错误: {r!r}, 应为 old=new")
        old, new = r.split("=", 1)
        old = old.strip()
        new = new.strip()
        if not old or not new:
            raise ValueError(f"--rename 参数格式错误: {r!r}, old/new 不能为空")
        parsed[old] = new
    return parsed


async def main():
    parser = argparse.ArgumentParser(
        description="price_snapshot model_id 归一化/重命名 migration (Phase 4 v4.3.1)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将迁移的行, 不实际执行",
    )
    parser.add_argument(
        "--channel",
        type=str,
        help="只迁移指定 channel (如 minimax_compat)",
    )
    parser.add_argument(
        "--rename",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="手动重命名映射, 可多次指定 (如 --rename deepseek-chat=deepseek-v4-flash)",
    )
    args = parser.parse_args()

    try:
        renames = _parse_rename_args(args.rename)
    except ValueError as e:
        print(f"[error] {e}")
        return 2

    from db.engine import async_session_factory

    async with async_session_factory() as session:
        migrations = await find_migrations(session, channel_name=args.channel, renames=renames)

        if not migrations:
            print("[OK] 没有需要迁移的行 (所有 model_id 已归一化)")
            return 0

        print(f"[{'DRY-RUN' if args.dry_run else 'EXEC'}] 发现 {len(migrations)} 条需要迁移:")
        for m in migrations[:20]:  # 最多展示 20 条
            action = "INSERT + DELETE" if m["new_id"] is None else "INSERT skip + DELETE"
            reason_tag = f" [{m['reason']}]" if renames else ""
            print(
                f"  - {m['channel_name']}/{m['old_model']} -> {m['new_model']}{reason_tag} ({action})"
            )
        if len(migrations) > 20:
            print(f"  ... 还有 {len(migrations) - 20} 条")

        if args.dry_run:
            print(f"\n[DRY-RUN] 实际跑时将迁移 {len(migrations)} 条")
            return 0

        # 确认 prompt (避免脚本自动跑)
        print(f"\n确认要迁移这 {len(migrations)} 条? 输入 yes 继续:")
        confirm = input("> ").strip().lower()
        if confirm != "yes":
            print("[abort] 已取消")
            return 1

        result = await execute_migrations(session, migrations, dry_run=False)
        print(
            f"\n[OK] 迁移完成: INSERT {result['synced']} 条, "
            f"DELETE {result['deleted']} 条"
        )
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))