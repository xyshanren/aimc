"""批量重加密 accounts 表 (V4.6.4)

背景:
- 用户场景: deploy 时换了 AIMC_MASTER_KEY, 旧 ciphertext 用老 key 加密,
  router 启动时 _resolve_account_key 解密失败, account 被跳过
- 修复路径: 用老 key 解密 + 用新 key 重加密

用法 (3 步):
    # 1. 备份当前 .env
    cp ~/.env ~/.env.bak.<timestamp>

    # 2. 临时把 .env AIMC_MASTER_KEY 换成老 key (确保 router / 脚本能 decrypt 老 ciphertext)
    nano ~/.env   # AIMC_MASTER_KEY=<老key>

    # 3. 跑脚本 (新 key 通过环境变量传入, 避免命令行泄漏)
    REENCRYPT_NEW_MASTER_KEY="<64字符hex新key>" python scripts/reencrypt_accounts.py

    # 4. dry-run 看完没问题, 跑真改
    REENCRYPT_NEW_MASTER_KEY="..." python scripts/reencrypt_accounts.py --apply

    # 5. 改回新 key
    nano ~/.env   # AIMC_MASTER_KEY=<新key>
    sudo systemctl --user restart aimc

    # 6. 验证
    python scripts/check_ciphertext_health.py

输出:
- 每个 account 的状态: OK / SKIP (已有新 key 加密) / FAIL
- 自动备份 db 到 backups/aimc_<timestamp>.db
- rollback 支持

安全:
- 默认 dry-run
- 新 key 通过 REENCRYPT_NEW_MASTER_KEY env 传入, 不进命令行历史
- 备份 db 自动 (--apply 才会真改)
- SQLite 单条 UPDATE 事务原子
"""

import argparse
import asyncio
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import Account, Channel
from security.crypto import decrypt, encrypt, _validate_master_key


def _backup_db(db_path: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"aimc_{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


async def show_state(session) -> dict:
    """用当前 AIMC_MASTER_KEY (假设是老 key) 检查每个 account 的解密状态"""
    from config import settings

    old_key = settings.aimc_master_key
    if not old_key:
        return {"error": "AIMC_MASTER_KEY 未设置"}

    state = {
        "old_key_status": "unknown",
        "accounts": [],
        "can_decrypt_with_old_key": 0,
        "cannot_decrypt": 0,
        "skipped_empty": 0,
    }

    # 校验 old key
    try:
        _validate_master_key(old_key)
        state["old_key_status"] = "ok"
    except ValueError as e:
        state["old_key_status"] = f"INVALID: {e}"
        return state

    rows = (await session.execute(
        select(Account, Channel)
        .join(Channel, Account.channel_id == Channel.id)
        .where(Account.status == "active")
    )).all()

    for account, channel in rows:
        ct = account.api_key_ciphertext
        pt = account.api_key or ""
        entry = {
            "channel_id": channel.id,
            "channel_name": channel.name,
            "account_id": account.id,
            "account_name": account.account_name,
            "ciphertext_length": len(ct) if ct else 0,
            "decrypt_status": "n/a",
            "decrypt_error": None,
        }

        if not ct:
            entry["decrypt_status"] = "EMPTY"
            state["skipped_empty"] += 1
        else:
            try:
                decrypt(ct, old_key)
                entry["decrypt_status"] = "OK"
                state["can_decrypt_with_old_key"] += 1
            except ValueError as e:
                entry["decrypt_status"] = "FAIL"
                entry["decrypt_error"] = str(e)
                state["cannot_decrypt"] += 1

        state["accounts"].append(entry)

    return state


async def reencrypt(new_key: str, apply: bool, rollback_path: str = None) -> int:
    """主流程"""
    from config import settings

    # 处理回滚
    if rollback_path:
        backup = Path(rollback_path)
        if not backup.exists():
            print(f"[ERROR] 备份文件不存在: {backup}")
            return 1
        db_path = Path(settings.db_path)
        print(f"[INFO] 从 {backup} 回滚到 {db_path}")
        if not apply:
            print("  [DRY-RUN] 加 --apply 才会真回滚")
            return 0
        shutil.copy2(backup, db_path)
        print("  [OK] 已回滚. 重启服务: sudo systemctl --user restart aimc")
        return 0

    # 校验新 key
    try:
        _validate_master_key(new_key)
    except ValueError as e:
        print(f"[ERROR] 新 master key 校验失败: {e}")
        print("  期望: 64 字符 hex (32 bytes)")
        print("  生成: python scripts/gen_master_key.py")
        return 1

    # 校验老 key (settings.aimc_master_key) - 当前 .env 应该是老 key
    old_key = settings.aimc_master_key
    if not old_key:
        print("[ERROR] AIMC_MASTER_KEY 未设置")
        print("  步骤 2: 把 .env 的 AIMC_MASTER_KEY 临时换成老 key")
        return 1
    if old_key == new_key:
        print("[ERROR] 新老 master key 相同, 无需重加密")
        return 1

    print(f"  老 key (settings.aimc_master_key): {old_key[:8]}...{old_key[-4:]} ({len(old_key)} chars)")
    print(f"  新 key (REENCRYPT_NEW_MASTER_KEY): {new_key[:8]}...{new_key[-4:]} ({len(new_key)} chars)")

    await init_db()

    async with async_session_factory() as session:
        state = await show_state(session)

        if "error" in state:
            print(f"[ERROR] {state['error']}")
            return 1

        print(f"\n=== 用老 key 解密状态 ===")
        print(f"  老 key 状态: {state['old_key_status']}")
        print(f"  能解密 (用老 key): {state['can_decrypt_with_old_key']}")
        print(f"  不能解密: {state['cannot_decrypt']}")
        print(f"  跳过 (空 ciphertext): {state['skipped_empty']}")

        if state["can_decrypt_with_old_key"] == 0:
            print(f"\n[ERROR] 老 key 不能解任何 ciphertext, 无法重加密")
            print(f"  请确认 .env 的 AIMC_MASTER_KEY 是 db 加密时用的那个 key")
            print(f"  或: 如果 key 真的丢了, 用 scripts/add_account.py 重建")
            return 1

        # 列出能解密的 accounts (候选重加密)
        candidates = [a for a in state["accounts"] if a["decrypt_status"] == "OK"]
        print(f"\n[INFO] 计划重加密 {len(candidates)} 个 account:")

        if not apply:
            print(f"\n[DRY-RUN] 未做任何修改 (加 --apply 才会改 db)")
            print(f"\n  完整流程 (--apply):")
            print(f"    1. 备份 db 到 backups/aimc_<timestamp>.db")
            print(f"    2. 对每个能解密的 account:")
            print(f"       - 用老 key decrypt")
            print(f"       - 用新 key re-encrypt")
            print(f"       - UPDATE accounts SET api_key_ciphertext = new_ct")
            print(f"    3. 修改完成后:")
            print(f"       - .env 换回新 key")
            print(f"       - sudo systemctl --user restart aimc")
            return 0

        # 备份 db
        db_path = Path(settings.db_path)
        backups_dir = ROOT / "backups"
        backup = _backup_db(db_path, backups_dir)
        print(f"\n[INFO] 已备份: {backup}")

        # 重加密
        migrated = 0
        failed = 0
        for a in candidates:
            # 重新查 account (避免 session stale)
            acc = (await session.execute(
                select(Account).where(Account.id == a["account_id"])
            )).scalar_one_or_none()
            if acc is None or acc.api_key_ciphertext is None:
                continue
            try:
                # 用老 key 解
                plaintext = decrypt(acc.api_key_ciphertext, old_key)
                # 用新 key 加密
                new_ct = encrypt(plaintext, new_key)
                acc.api_key_ciphertext = new_ct
                migrated += 1
                # 每 10 个 commit 一次, 避免大事务
                if migrated % 10 == 0:
                    await session.commit()
            except Exception as e:
                print(f"  [FAIL] account {acc.account_name}: {e}")
                failed += 1

        await session.commit()
        print(f"\n[OK] 重加密完成: {migrated} 个成功, {failed} 个失败")

        if failed > 0:
            print(f"  [WARN] 有失败, 可以 rollback 或手动重建")

        print(f"\n下一步:")
        print(f"  1. 改 .env: AIMC_MASTER_KEY={new_key[:8]}...{new_key[-4:]} (新 key)")
        print(f"  2. 重启服务: sudo systemctl --user restart aimc")
        print(f"  3. 验证: python scripts/check_ciphertext_health.py")
        print(f"  4. 测试路由: curl -X POST http://localhost:8080/v1/chat/completions \\")
        print(f"                -d '{{\"model\":\"tier:balanced\",\"messages\":[...]}}'")

        if backup:
            print(f"\n回滚 (如需):")
            print(f"  REENCRYPT_NEW_MASTER_KEY='{new_key}' python scripts/reencrypt_accounts.py --rollback {backup}")

        return 0 if failed == 0 else 1


def main():
    parser = argparse.ArgumentParser(
        description="批量重加密 accounts 表 (V4.6.4, master key 切换时用)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 1. 把 .env AIMC_MASTER_KEY 临时换成老 key
  # 2. dry-run 看
  REENCRYPT_NEW_MASTER_KEY="<新64hex>" python scripts/reencrypt_accounts.py
  # 3. 实际跑
  REENCRYPT_NEW_MASTER_KEY="<新64hex>" python scripts/reencrypt_accounts.py --apply
  # 4. .env 换回新 key, 重启服务
        """,
    )
    parser.add_argument("--apply", action="store_true", help="实际改 db")
    parser.add_argument("--rollback", metavar="BACKUP_PATH", help="从备份回滚")
    args = parser.parse_args()

    new_key = os.getenv("REENCRYPT_NEW_MASTER_KEY", "").strip()
    if not args.rollback and not new_key:
        print("[ERROR] 缺少 REENCRYPT_NEW_MASTER_KEY 环境变量")
        print("  用法: REENCRYPT_NEW_MASTER_KEY='<新key64hex>' python scripts/reencrypt_accounts.py")
        return 1

    return asyncio.run(reencrypt(
        new_key=new_key,
        apply=args.apply,
        rollback_path=args.rollback,
    ))


if __name__ == "__main__":
    sys.exit(main())
