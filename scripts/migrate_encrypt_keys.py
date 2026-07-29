"""一次性迁移脚本: 把老明文 api_key 转 ciphertext (API-Sec-1)

用法:
    # 1. 先生成 master key
    python scripts/gen_master_key.py
    # 2. 把输出贴到 .env 的 AIMC_MASTER_KEY= 后面
    # 3. 跑这个脚本
    python scripts/migrate_encrypt_keys.py
    # 4. 验证: 重启 uvicorn + chat 测试, 然后
    #    python -c "import sqlite3; c=sqlite3.connect('data/aimc.db'); print(c.execute('SELECT account_name, api_key, length(api_key_ciphertext) FROM accounts').fetchall())"

幂等: 重跑不会重复加密 (已迁移的跳过, 通过 api_key IS NOT NULL 判断)
"""
import asyncio
import sys
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, update

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import Account
from config import settings
from security.crypto import encrypt


async def main():
    print("API-Sec-1: 老明文 api_key 迁移到 ciphertext")
    print("=" * 60)

    # 校验 master key
    if not settings.aimc_master_key:
        print("ERROR: AIMC_MASTER_KEY 未设置")
        print("  1. python scripts/gen_master_key.py")
        print("  2. 把输出贴到 .env")
        print("  3. 重跑这个脚本")
        sys.exit(1)

    if len(settings.aimc_master_key) != 44:
        print(f"ERROR: AIMC_MASTER_KEY 长度异常: {len(settings.aimc_master_key)} (期望 44)")
        sys.exit(1)

    # 初始化 DB (确保新列已加)
    await init_db()

    async with async_session_factory() as session:
        # 查所有还有明文的 account
        stmt = select(Account).where(
            Account.api_key.isnot(None),
            Account.api_key != "",
        )
        accounts = (await session.execute(stmt)).scalars().all()

        if not accounts:
            print("无老明文数据需要迁移 (可能已经迁过)")
            return

        print(f"找到 {len(accounts)} 个 account 持有老明文 key")
        print()

        migrated = 0
        skipped = 0
        for acc in accounts:
            plaintext = acc.api_key
            # 已迁过 (有 ciphertext 但还有明文) → skip
            if acc.api_key_ciphertext is not None:
                print(f"  SKIP  account {acc.id} ({acc.account_name}): 已有 ciphertext")
                skipped += 1
                continue

            try:
                ciphertext = encrypt(plaintext, settings.aimc_master_key)
                acc.api_key_ciphertext = ciphertext
                # 不删除 api_key 明文, 留作迁移期兜底 (新数据写入才会清空)
                migrated += 1
                masked_key = plaintext[:6] + "..." + plaintext[-4:] if len(plaintext) > 12 else "***"
                print(f"  OK    account {acc.id} ({acc.account_name}): {masked_key} -> {len(ciphertext)} bytes ciphertext")
            except Exception as e:
                print(f"  FAIL  account {acc.id} ({acc.account_name}): {e}")
                continue

        await session.commit()
        print()
        print(f"迁移完成: {migrated} 个 account 加密, {skipped} 个已迁过跳过")
        print()
        print("下一步:")
        print("  1. 重启 uvicorn 验证 chat 正常")
        print("  2. 验证 DB dump 看不到明文:")
        print("     python -c \"import sqlite3; c=sqlite3.connect('data/aimc.db'); rows=c.execute('SELECT account_name, api_key, length(api_key_ciphertext) FROM accounts').fetchall(); [print(r) for r in rows]\"")
        print("  3. 验证 OK 后, 后续写新 channel 不会再写明文到 api_key 字段 (会清空)")


if __name__ == "__main__":
    asyncio.run(main())
