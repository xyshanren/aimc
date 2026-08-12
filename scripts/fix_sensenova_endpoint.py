"""一次性修复: 把 sensenova 渠道 base_url 从 api.sensenova.cn 改为 token.sensenova.cn (V4.6.4)

背景:
- manual_import.py 写死的默认 base_url 是 https://api.sensenova.cn/v1 (历史错误)
- 实际官方推荐 https://token.sensenova.cn/v1 (公测 token plan 入口)
- 现象: deepseek-v4-flash 报 1214 modelCode不存在 / sensenova-6.7-flash-lite 报 404

修复:
- gateway/router.py: 已加 _resolve_effective_base_url (env 优先 db)
- 本脚本: db UPDATE (可选, 已有 sensenova_official 渠道才需要)

用法:
    # 1. dry-run (默认)
    python scripts/fix_sensenova_endpoint.py

    # 2. 应用变更 (自动备份 db)
    python scripts/fix_sensenova_endpoint.py --apply

    # 3. 回滚到备份
    python scripts/fix_sensenova_endpoint.py --rollback backups/aimc_20260812_xxxxxx.db

输出:
    [INFO] sensenova 渠道当前状态:
      channel_id=7  name=sensenova_official  base_url=https://api.sensenova.cn/v1
    [INFO] 目标 base_url:
      https://token.sensenova.cn/v1
    [DRY-RUN] 未做任何修改 (加 --apply 才会改 db)

安全:
- 默认 dry-run, 不动 db
- --apply 自动备份到 backups/aimc_<timestamp>.db
- SQLite 单条 UPDATE 是事务原子的, 失败自动回滚
- 不需要停服务: gateway 缓存 5min 自动刷新; 或 systemctl --user restart aimc 立即生效
- 但 env SENSENOVA_BASE_URL 设了的话, router 已经用 env 走 token.sensenova.cn, db 修改可选
"""

import argparse
import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 项目根目录加入 sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import Channel


OLD_URL = "https://api.sensenova.cn/v1"
NEW_URL = "https://token.sensenova.cn/v1"
TARGET_PROVIDER = "sensenova"


def _backup_db(db_path: Path, backups_dir: Path) -> Path:
    """备份 SQLite db 文件 (SQLite 备份要保证一致性)"""
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"aimc_{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


async def show_current(session) -> bool:
    """显示 sensenova 渠道当前 base_url, 返回是否需要修改"""
    stmt = select(Channel).where(Channel.provider == TARGET_PROVIDER)
    channels = (await session.execute(stmt)).scalars().all()
    if not channels:
        print(f"[ERROR] db 里没有 provider='{TARGET_PROVIDER}' 的渠道")
        print("  请先跑 python scripts/manual_import.py 创建 sensenova_official 渠道")
        return False

    need_fix = False
    for ch in channels:
        marker = "[FIX]" if ch.base_url and ch.base_url.rstrip("/") == OLD_URL else "[OK]"
        print(f"  {marker} channel_id={ch.id}  name={ch.name}  base_url={ch.base_url}")
        if ch.base_url and ch.base_url.rstrip("/") == OLD_URL:
            need_fix = True

    return need_fix


async def apply_update(apply: bool, rollback_path: str = None) -> int:
    """主流程. 返回 0 成功, 1 失败"""
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

    # 初始化 db (确保新列已加, 幂等)
    await init_db()

    async with async_session_factory() as session:
        need_fix = await show_current(session)
        if not need_fix:
            print(f"\n[OK] sensenova 渠道 base_url 已是 {NEW_URL} 或未配置, 无需修改")
            return 0

        print(f"\n[INFO] 目标 base_url: {NEW_URL}")

        if not apply:
            print(f"\n[DRY-RUN] 未做任何修改 (加 --apply 才会改 db)")
            print(f"  应用后建议: sudo systemctl --user restart aimc  (让 router 缓存立刻刷新)")
            return 0

        # 自动备份
        from config import settings
        db_path = Path(settings.db_path)
        backups_dir = ROOT / "backups"
        backup = _backup_db(db_path, backups_dir)
        print(f"\n[INFO] 已备份: {backup}")

        # UPDATE
        stmt = (
            update(Channel)
            .where(Channel.provider == TARGET_PROVIDER)
            .where(Channel.base_url == OLD_URL)
            .values(base_url=NEW_URL, updated_at=datetime.now())
        )
        result = await session.execute(stmt)
        await session.commit()

        affected = result.rowcount
        if affected == 0:
            print(f"[WARN] UPDATE 影响 0 行 (可能 base_url 已被手动改过)")
            print(f"  检查: SELECT id, name, base_url FROM channels WHERE provider='{TARGET_PROVIDER}';")
            return 0

        print(f"\n[OK] {affected} 个 sensenova 渠道已更新到 {NEW_URL}")
        print(f"\n下一步:")
        print(f"  1. 重启服务: sudo systemctl --user restart aimc  (立即生效)")
        print(f"     或等 5min 自动缓存刷新")
        print(f"  2. 验证: python scripts/fix_sensenova_endpoint.py  (应该显示 [OK])")
        print(f"  3. 测试: curl -X POST http://localhost:8080/v1/chat/completions \\")
        print(f"           -d '{{\"model\": \"sensenova-6.8-flash-lite\", \"messages\": [...]}}'")
        print(f"\n回滚 (如需):")
        print(f"  python scripts/fix_sensenova_endpoint.py --rollback {backup}")

        return 0


def main():
    parser = argparse.ArgumentParser(
        description="修复 sensenova 渠道 base_url (V4.6.4, manual_import.py 历史错误默认)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/fix_sensenova_endpoint.py            # dry-run
  python scripts/fix_sensenova_endpoint.py --apply    # 应用 (自动备份)
  python scripts/fix_sensenova_endpoint.py --rollback backups/aimc_xxx.db
        """,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际修改 db (默认 dry-run)",
    )
    parser.add_argument(
        "--rollback",
        metavar="BACKUP_PATH",
        help="从备份文件回滚 db",
    )
    args = parser.parse_args()

    return asyncio.run(apply_update(apply=args.apply, rollback_path=args.rollback))


if __name__ == "__main__":
    sys.exit(main())
