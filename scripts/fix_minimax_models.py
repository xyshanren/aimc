"""一次性修复: 同步 minimax 渠道的 model 注册表 (V4.6.4)

背景:
- 用户实测: 之前 manual_import.py 把 minimax 渠道的 price_snapshot 登记成 gpt-4o-mini
  (错的 model 名), 且 model_registry 只挂了 1 个不存在的 MiniMax-Text-01
- 导致 minimax 渠道实际不可用 — 上游不认 gpt-4o-mini (MiniMax 官方是 MiniMax-M*)
- V4.6.4 fix: 用 MiniMax 官方当前 8 个 model 替换 (platform.minimaxi.com/docs 查, 2026-08-08)

修复:
- scripts/seed_model_registry.py: 加 8 个 MiniMax model (M3 / M2.7 / M2.7-highspeed / M2.5 / M2.5-highspeed / M2.1 / M2.1-highspeed / M2)
- scripts/seed_model_cards.py: 补 5 张 MiniMax model_card
- scripts/manual_import.py: prices 列表改用 MiniMax 官方 8 个 model (替换 gpt-4o-mini)
- 本脚本: 已部署 user 一次性 sync db (sync price_snapshots)

用法:
    # 1. dry-run (默认)
    python scripts/fix_minimax_models.py

    # 2. 应用变更 (自动备份 db)
    python scripts/fix_minimax_models.py --apply

    # 3. 回滚到备份
    python scripts/fix_minimax_models.py --rollback backups/aimc_20260812_xxxxxx.db

    # 4. 同时重新 seed model_registry
    python scripts/fix_minimax_models.py --apply --seed-registry

改动:
- price_snapshots 表: 删 gpt-4o-mini (如果存在) + 删 MiniMax-Text-01 (老错 model) + 加 8 个新 model
- model_registry 表 (--seed-registry): 清后插 (跟 seed_model_registry.py 一样)

安全:
- 默认 dry-run
- --apply 自动备份 db 到 backups/aimc_<timestamp>.db
- SQLite 单条 INSERT/DELETE 事务原子
- 不需要停服务: gateway 缓存 5min 自动刷新; 或 systemctl --user restart aimc 立即生效
"""

import argparse
import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import Channel, PriceSnapshot

# === 配置 ===
TARGET_PROVIDER = "minimax"
# 移除: 错的 model 名 (MiniMax 官方不认)
#   - gpt-4o-mini: 抄 OpenAI 的, 跟 MiniMax 没关系
#   - MiniMax-Text-01: 老 model 已被 M2/M2.1/M2.5/M2.7/M3 取代
MODELS_TO_REMOVE = ["gpt-4o-mini", "MiniMax-Text-01"]
# 添加: MiniMax 官方当前 8 个 Model ID (2026-08-08 查 platform.minimaxi.com/docs)
MODELS_TO_ADD = [
    # (model, price_type, time_range, input_price, output_price)
    ("MiniMax-M3",              "normal", "全天", 0.0, 0.0),
    ("MiniMax-M2.7",            "normal", "全天", 0.0, 0.0),
    ("MiniMax-M2.7-highspeed",  "normal", "全天", 0.0, 0.0),
    ("MiniMax-M2.5",            "normal", "全天", 0.0, 0.0),
    ("MiniMax-M2.5-highspeed",  "normal", "全天", 0.0, 0.0),
    ("MiniMax-M2.1",            "normal", "全天", 0.0, 0.0),
    ("MiniMax-M2.1-highspeed",  "normal", "全天", 0.0, 0.0),
    ("MiniMax-M2",              "normal", "全天", 0.0, 0.0),
]


def _backup_db(db_path: Path, backups_dir: Path) -> Path:
    """备份 SQLite db"""
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"aimc_{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


async def show_state(session) -> dict:
    """显示 minimax 渠道当前 price_snapshots 状态"""
    # 找 minimax channel
    stmt = select(Channel).where(Channel.provider == TARGET_PROVIDER)
    channels = (await session.execute(stmt)).scalars().all()
    if not channels:
        return {"channels": [], "error": f"db 里没 provider='{TARGET_PROVIDER}' 渠道"}

    state = {"channels": [], "need_remove": [], "need_add": []}
    for ch in channels:
        # 当前 prices
        prices = list((await session.execute(
            select(PriceSnapshot).where(PriceSnapshot.channel_id == ch.id)
        )).scalars().all())
        current_models = {p.model for p in prices}
        print(f"  channel_id={ch.id}  name={ch.name}")
        print(f"    当前 {len(prices)} 个 price_snapshot:")
        for p in prices:
            marker = "[DEL]" if p.model in MODELS_TO_REMOVE else \
                     "[DUP]" if p.model in {m[0] for m in MODELS_TO_ADD} else "      "
            print(f"      {marker} {p.model:30s}  {p.price_type:6s}  {p.time_range}")

        # 需要删除的
        for m in MODELS_TO_REMOVE:
            if m in current_models:
                state["need_remove"].append((ch.id, m))
        # 需要添加的
        for m in MODELS_TO_ADD:
            if m[0] not in current_models:
                state["need_add"].append((ch.id, m))
        state["channels"].append(ch)

    return state


async def apply_changes(apply: bool, seed_registry: bool, rollback_path: str = None) -> int:
    """主流程"""
    # 处理回滚
    if rollback_path:
        backup = Path(rollback_path)
        if not backup.exists():
            print(f"[ERROR] 备份文件不存在: {backup}")
            return 1
        from config import settings
        db_path = Path(settings.db_path)
        print(f"[INFO] 从 {backup} 回滚到 {db_path}")
        if not apply:
            print(f"  [DRY-RUN] 加 --apply 才会真回滚")
            return 0
        shutil.copy2(backup, db_path)
        print(f"  [OK] 已回滚. 重启服务: sudo systemctl --user restart aimc")
        return 0

    await init_db()

    async with async_session_factory() as session:
        state = await show_state(session)

        if "error" in state:
            print(f"[ERROR] {state['error']}")
            print(f"  可能原因 1: .env 未配 MINIMAX_API_KEY (manual_import 已 skip, 跟 V4.6.4 dcda904 一致)")
            print(f"  可能原因 2: .env 未配 MINIMAX_BASE_URL (manual_import skip PLACEHOLDER)")
            print(f"  修复: 在 .env 加 MINIMAX_API_KEY + MINIMAX_BASE_URL=https://api.minimaxi.com/v1")
            print(f"       然后跑 python scripts/manual_import.py 创建渠道")
            return 1

        need_remove = state["need_remove"]
        need_add = state["need_add"]

        if not need_remove and not need_add:
            print(f"\n[OK] minimax 渠道 model 注册表已是最新, 无需修改")
            if seed_registry:
                print(f"\n[INFO] --seed-registry 选项: 仍要重新 seed model_registry 表")
            else:
                return 0

        print(f"\n[INFO] 计划变更:")
        if need_remove:
            print(f"  删除 {len(need_remove)} 个 price_snapshot:")
            for ch_id, m in need_remove:
                print(f"    - channel_id={ch_id}  model={m}")
        if need_add:
            print(f"  添加 {len(need_add)} 个 price_snapshot:")
            for ch_id, m in need_add:
                print(f"    + channel_id={ch_id}  model={m[0]}  ({m[1]}, {m[2]})")
        if seed_registry:
            print(f"  重新 seed model_registry 表 (清后插)")

        if not apply:
            print(f"\n[DRY-RUN] 未做任何修改 (加 --apply 才会改 db)")
            if seed_registry:
                print(f"  + --seed-registry 会重跑 scripts/seed_model_registry.py")
            return 0

        # 自动备份
        from config import settings
        db_path = Path(settings.db_path)
        backups_dir = ROOT / "backups"
        backup = _backup_db(db_path, backups_dir)
        print(f"\n[INFO] 已备份: {backup}")

        # 删除
        for ch_id, model in need_remove:
            stmt = delete(PriceSnapshot).where(
                PriceSnapshot.channel_id == ch_id,
                PriceSnapshot.model == model,
            )
            await session.execute(stmt)
        if need_remove:
            await session.commit()
            print(f"[OK] 删除 {len(need_remove)} 个 price_snapshot")

        # 添加
        for ch_id, (model, price_type, time_range, in_p, out_p) in need_add:
            session.add(PriceSnapshot(
                channel_id=ch_id,
                model=model,
                price_type=price_type,
                time_range=time_range,
                input_price=in_p,
                output_price=out_p,
                source="manual",
            ))
        if need_add:
            await session.commit()
            print(f"[OK] 添加 {len(need_add)} 个 price_snapshot")

        # 重新 seed model_registry
        if seed_registry:
            from scripts.seed_model_registry import seed as registry_seed
            await registry_seed()
            print(f"[OK] model_registry 表已重新 seed")

        print(f"\n下一步:")
        print(f"  1. 重启服务: sudo systemctl --user restart aimc  (立即生效)")
        print(f"     或等 5min 自动缓存刷新")
        print(f"  2. 验证: python scripts/fix_minimax_models.py  (应该显示 [OK] 无需修改)")
        print(f"  3. 测试:")
        print(f"     直连验证:")
        print(f"       python -c \"import os; from openai import OpenAI;")
        print(f"       c=OpenAI(base_url='https://api.minimaxi.com/v1', api_key=os.environ['MINIMAX_API_KEY']);")
        print(f"       print(c.chat.completions.create(model='MiniMax-M2.7', messages=[{{'role':'user','content':'hi'}}], max_tokens=20).choices[0].message.content)\"")
        print(f"     网关验证:")
        print(f"       curl -isS -X POST http://localhost:8080/v1/chat/completions \\")
        print(f"         -H 'Content-Type: application/json' \\")
        print(f"         -d '{{\"model\": \"MiniMax-M2.7\", \"messages\": [{{\"role\":\"user\",\"content\":\"hi\"}}], \"max_tokens\": 20}}' \\")
        print(f"         | grep -i x-aimc")
        print(f"       # 期望: X-AIMC-Actual-Channel: minimax_compat")
        print(f"\n回滚 (如需):")
        print(f"  python scripts/fix_minimax_models.py --rollback {backup}")

        return 0


def main():
    parser = argparse.ArgumentParser(
        description="同步 minimax 渠道 model 注册表 (V4.6.4)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/fix_minimax_models.py                       # dry-run
  python scripts/fix_minimax_models.py --apply               # 应用 price_snapshots 变更
  python scripts/fix_minimax_models.py --apply --seed-registry  # 同时重新 seed model_registry
  python scripts/fix_minimax_models.py --rollback backups/aimc_xxx.db
        """,
    )
    parser.add_argument("--apply", action="store_true", help="实际修改 db")
    parser.add_argument("--seed-registry", action="store_true", help="同时重新 seed model_registry 表")
    parser.add_argument("--rollback", metavar="BACKUP_PATH", help="从备份回滚 db")
    args = parser.parse_args()
    return asyncio.run(apply_changes(
        apply=args.apply,
        seed_registry=args.seed_registry,
        rollback_path=args.rollback,
    ))


if __name__ == "__main__":
    sys.exit(main())
