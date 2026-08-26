"""一次性修复: 同步 sensenova 渠道的 model 注册表 (V4.6.4)

背景:
- 用户实测: 请求 deepseek-v4-flash 网关 400, 原因不是 glm 不认, 而是
  sensenova 渠道的 price_snapshots 没 deepseek-v4-flash,
  router 查 _get_current_price 返回 None,
  fallback 到 priority 最低 channel (glm, 用户没配 siliconflow),
  glm 不认 deepseek-v4-flash, 返回 1214 modelCode不存在
- 根因: sensenova 渠道的 model 注册表与商汤官方文档脱节
- SenseChat 已失效 (上游 code 5 model not found)
- 官方实际有 3 个 Model ID: sensenova-6.8-flash-lite / sensenova-u1-fast / deepseek-v4-flash
  sensenova-6.7-flash-lite 仅靠 8/31 前重定向可用

修复:
- scripts/seed_model_registry.py: 加 4 个 model (1 deprecated, 3 active)
- scripts/manual_import.py: prices 列表移除 SenseChat + 加 3 个新 model (init 路径)
- 本脚本: 已部署 user 一次性 sync db (sync price_snapshots)

用法:
    # 1. dry-run (默认)
    python scripts/fix_sensenova_models.py

    # 2. 应用变更 (自动备份 db)
    python scripts/fix_sensenova_models.py --apply

    # 3. 回滚到备份
    python scripts/fix_sensenova_models.py --rollback backups/aimc_20260812_xxxxxx.db

    # 4. 同时重新 seed model_registry (清后插)
    python scripts/fix_sensenova_models.py --apply --seed-registry

改动:
- price_snapshots 表: 删 SenseChat (如果存在) + 加 3 个新 model (不存在则加)
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
TARGET_PROVIDER = "sensenova"
# 移除: 商汤上游已失效 (code 5 model not found)
MODELS_TO_REMOVE = ["SenseChat"]
# 添加: 官方当前 3 个 Model ID
MODELS_TO_ADD = [
    # (model, price_type, time_range, input_price, output_price)
    ("sensenova-6.8-flash-lite", "normal", "全天", 0.0, 0.0),
    ("sensenova-u1-fast",        "normal", "全天", 0.0, 0.0),
    ("deepseek-v4-flash",        "normal", "全天", 0.0, 0.0),
    ("glm-5.2",                  "normal", "全天", 0.0, 0.0),  # 公测免费 (跨渠道, 免费第二渠道)
    # 6.7 保留 (8/31 前仍可用)
    # ("sensenova-6.7-flash-lite", "normal", "全天", 0.0, 0.0),
]


def _backup_db(db_path: Path, backups_dir: Path) -> Path:
    """备份 SQLite db"""
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"aimc_{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


async def show_state(session) -> dict:
    """显示 sensenova 渠道当前 price_snapshots 状态"""
    # 找 sensenova channel
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
            print(f"      {marker} {p.model:35s}  {p.price_type:6s}  {p.time_range}")

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
            print(f"  请先跑 python scripts/manual_import.py 创建 sensenova_official 渠道")
            return 1

        need_remove = state["need_remove"]
        need_add = state["need_add"]

        if not need_remove and not need_add:
            print(f"\n[OK] sensenova 渠道 model 注册表已是最新, 无需修改")
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
        print(f"  2. 验证: python scripts/fix_sensenova_models.py  (应该显示 [OK] 无需修改)")
        print(f"  3. 测试:")
        print(f"     curl -X POST http://localhost:8080/v1/chat/completions \\")
        print(f"       -H 'Content-Type: application/json' \\")
        print(f"       -d '{{\"model\": \"deepseek-v4-flash\", \"messages\": [{{\"role\":\"user\",\"content\":\"ping\"}}]}}'")
        print(f"     # 期望: 200 OK + content 字段 (sensenova 渠道直接命中)")
        print(f"\n回滚 (如需):")
        print(f"  python scripts/fix_sensenova_models.py --rollback {backup}")

        return 0


def main():
    parser = argparse.ArgumentParser(
        description="同步 sensenova 渠道 model 注册表 (V4.6.4)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/fix_sensenova_models.py                       # dry-run
  python scripts/fix_sensenova_models.py --apply               # 应用 price_snapshots 变更
  python scripts/fix_sensenova_models.py --apply --seed-registry  # 同时重新 seed model_registry
  python scripts/fix_sensenova_models.py --rollback backups/aimc_xxx.db
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
