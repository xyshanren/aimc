"""重置 AIMC db 数据 (V4.6.4)

背景:
- 用户场景: master key 改了但 db 里老 ciphertext 解不开, 想从头开始
- 或者: db 损坏 / 测试环境清空 / 切换部署环境
- 干净的方案: 备份 db + 清空业务表 + 重新 seed + 从 .env 重 import

行为:
- 备份 db -> backups/aimc_<timestamp>.db
- 保留 settings_runtime 表 (dashboard 改的告警 channel 配置)
- 清空 14 个业务表 (channels/accounts/price_snapshots/...)
- 重新跑 seed_*.py (model_groups/model_cards/model_registry/parse_rules)
- 跑 manual_import.py (从 .env 创建 channel + account + 价格快照)
- 跑 multi_account_import.py (从 .env 的 _KEYS 创建多账号)

用法:
    # 1. dry-run (默认, 显示会做什么)
    python scripts/reset_db.py

    # 2. 应用 (需要二次确认 --yes)
    python scripts/reset_db.py --apply --yes

    # 3. 回滚 (从备份恢复)
    python scripts/reset_db.py --rollback backups/aimc_<timestamp>.db

警告:
- 删 usage_logs / model_feedback_summary / alerts / cross_source_discrepancies 等
- 历史统计数据会全部丢失 (例如 dashboard 上 24h/7d/30d 流量统计归零)
- 如果只是想换 master key, 优先用 reencrypt_accounts.py (保留历史数据)
"""

import argparse
import asyncio
import shutil
import sys
import warnings
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, text

from db.engine import async_session_factory
from db.init_db import init_db

# === 配置 ===

# 要清空的表 (跟 db/models.py 的 ORM class 对应)
TABLES_TO_CLEAR = [
    # 业务核心数据
    "channels",
    "accounts",
    "price_snapshots",
    "channel_quotas",
    "promotions",
    # 用量统计
    "usage_logs",
    "model_feedback_summary",
    "cross_source_discrepancies",
    "model_capabilities_log",
    # 情报采集配置
    "intel_sources",
    # 模型种子 (有 seed_*.py 重建)
    "model_cards",
    "model_registry",
    "model_groups",
    "model_group_members",
    "parse_rules",
]

# 要保留的表
TABLES_TO_KEEP = [
    "settings_runtime",       # dashboard 配置的告警 channel 等
    "alembic_version",         # alembic 版本 (如有)
    "apscheduler_jobs",        # apscheduler 自己的 (会被 service restart 清掉)
]


def _backup_db(db_path: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"aimc_{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


async def _table_counts(session) -> dict:
    """查询每个表的行数

    V4.6.4: 改成 async + await session.execute, 避免 coroutine never awaited RuntimeWarning
    (之前 sync def + AsyncSession.execute 没 await, GC 触发 warning)

    之前 dcda904 commit 在函数内加 warnings.catch_warnings 但 catch 不到
    (coroutine 在函数 return 后才被 GC, catch 上下文已退出)
    """
    counts = {}
    for table in TABLES_TO_CLEAR + TABLES_TO_KEEP:
        try:
            result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
            counts[table] = result.scalar() or 0
        except Exception:
            counts[table] = "N/A (表不存在)"
    return counts


async def show_state(apply: bool, rollback_path: str = None) -> int:
    """主流程"""
    from config import settings
    db_path = Path(settings.db_path)

    # 处理回滚
    if rollback_path:
        backup = Path(rollback_path)
        if not backup.exists():
            print(f"[ERROR] 备份文件不存在: {backup}")
            return 1
        print(f"[INFO] 从 {backup} 回滚到 {db_path}")
        if not apply:
            print("  [DRY-RUN] 加 --apply 才会真回滚")
            return 0
        shutil.copy2(backup, db_path)
        print("  [OK] 已回滚. 重启服务: sudo systemctl --user restart aimc")
        return 0

    # 校验 db 存在
    if not db_path.exists():
        print(f"[ERROR] db 不存在: {db_path}")
        print("  请先启动一次服务让 init_db() 建表")
        return 1

    # 校验 master key
    if not settings.aimc_master_key:
        print(f"[ERROR] AIMC_MASTER_KEY 未设置")
        print("  新 db 数据会用 .env 当前的 key 加密, 没有 key 就无法加密")
        return 1
    mk = settings.aimc_master_key
    if len(mk) != 64:
        print(f"[ERROR] AIMC_MASTER_KEY 长度异常: {len(mk)} (期望 64)")
        return 1
    print(f"  AIMC_MASTER_KEY: {mk[:8]}...{mk[-4:]} (新 db 数据会用这个 key 加密)")

    await init_db()

    async with async_session_factory() as session:
        counts = await _table_counts(session)
        print(f"\n=== 当前 db 行数 ({db_path}) ===")
        for table in TABLES_TO_CLEAR:
            n = counts.get(table, "N/A")
            marker = "[DEL]" if isinstance(n, int) and n > 0 else "      "
            print(f"  {marker} {table:35s} {n} 行 (将删除)")
        print()
        for table in TABLES_TO_KEEP:
            n = counts.get(table, "N/A")
            print(f"  [KEEP] {table:35s} {n} 行 (保留)")

        if not apply:
            print(f"\n[DRY-RUN] 未做任何修改 (加 --apply --yes 才会真清空)")
            print(f"\n  完整流程 (--apply --yes):")
            print(f"    1. 备份 db -> backups/aimc_<timestamp>.db")
            print(f"    2. DELETE FROM {len(TABLES_TO_CLEAR)} 表")
            print(f"    3. 跑 seed_model_groups.py / seed_model_cards.py")
            print(f"       seed_model_registry.py / seed_parse_rules.py")
            print(f"    4. 跑 manual_import.py (从 .env 创建 channel + account + price_snapshot)")
            print(f"    5. 跑 multi_account_import.py (从 .env _KEYS 创建多账号)")
            return 0

    # 真改流程
    # 1. 备份
    backups_dir = ROOT / "backups"
    backup = _backup_db(db_path, backups_dir)
    print(f"\n[INFO] 已备份: {backup}")

    # 2. 清空表
    async with async_session_factory() as session:
        # 关掉外键约束 (SQLite 默认开)
        await session.execute(text("PRAGMA foreign_keys = OFF"))
        try:
            for table in TABLES_TO_CLEAR:
                await session.execute(text(f"DELETE FROM {table}"))
            await session.commit()
        finally:
            await session.execute(text("PRAGMA foreign_keys = ON"))
        print(f"[OK] 清空 {len(TABLES_TO_CLEAR)} 表")

    # 3. 重 seed
    print(f"\n[INFO] 重新 seed ...")
    try:
        from scripts.seed_model_groups import seed as seed_groups
        await seed_groups()
        print(f"  [OK] seed_model_groups")
    except Exception as e:
        print(f"  [WARN] seed_model_groups 失败: {e}")

    try:
        from scripts.seed_model_cards import seed as seed_cards
        await seed_cards()
        print(f"  [OK] seed_model_cards")
    except Exception as e:
        print(f"  [WARN] seed_model_cards 失败: {e}")

    try:
        from scripts.seed_model_registry import seed as seed_registry
        await seed_registry()
        print(f"  [OK] seed_model_registry")
    except Exception as e:
        print(f"  [WARN] seed_model_registry 失败: {e}")

    try:
        from scripts.seed_parse_rules import seed as seed_rules
        await seed_rules()
        print(f"  [OK] seed_parse_rules")
    except Exception as e:
        print(f"  [WARN] seed_parse_rules 失败: {e}")

    # 4. 重 import channels
    print(f"\n[INFO] 重新从 .env 导入 channels ...")
    try:
        from scripts.manual_import import import_demo_data
        await import_demo_data()
        print(f"  [OK] manual_import")
    except Exception as e:
        print(f"  [WARN] manual_import 失败: {e}")

    # 5. 多账号
    print(f"\n[INFO] 跑 multi_account_import (从 .env 的 _KEYS 读取) ...")
    try:
        from scripts.multi_account_import import run_import
        await run_import()
        print(f"  [OK] multi_account_import")
    except Exception as e:
        print(f"  [WARN] multi_account_import 失败: {e}")

    print(f"\n下一步:")
    print(f"  1. 重启服务: sudo systemctl --user restart aimc")
    print(f"  2. 验证:")
    print(f"     python scripts/check_ciphertext_health.py")
    print(f"     python scripts/list_accounts.py")
    print(f"  3. 测试路由:")
    print(f"     curl -X POST http://localhost:8080/v1/chat/completions \\")
    print(f"       -H 'Content-Type: application/json' \\")
    print(f"       -d '{{\"model\":\"tier:balanced\",\"messages\":[...]}}'")
    print(f"\n回滚 (如需):")
    print(f"  python scripts/reset_db.py --rollback {backup}")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="重置 AIMC db 数据 (V4.6.4) — 备份 + 清空 + 重建 seed + 重 import",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/reset_db.py                       # dry-run
  python scripts/reset_db.py --apply --yes         # 真清空 (二次确认)
  python scripts/reset_db.py --rollback backups/aimc_xxx.db
        """,
    )
    parser.add_argument("--apply", action="store_true", help="实际修改 db")
    parser.add_argument("--yes", action="store_true", help="跳过二次确认")
    parser.add_argument("--rollback", metavar="BACKUP_PATH", help="从备份回滚")
    args = parser.parse_args()

    # 二次确认
    if args.apply and not args.yes:
        print("[ERROR] --apply 必须配合 --yes (防止误操作)")
        print("  用法: python scripts/reset_db.py --apply --yes")
        return 1

    if args.apply:
        print("=" * 70)
        print("!! 警告: 即将清空 14 个业务表, 历史数据 (usage_logs/feedback/alerts) 会丢失")
        print("=" * 70)
        print("按 Ctrl+C 取消, 或输入 'YES' 继续:")

    return asyncio.run(show_state(
        apply=args.apply,
        rollback_path=args.rollback,
    ))


if __name__ == "__main__":
    sys.exit(main())
