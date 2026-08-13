"""同步 .env 的 _API_KEY 到 db accounts (V4.6.4)

背景:
- 现有架构: .env -> manual_import.py -> db accounts.ciphertext (单向, 手动同步)
- 问题: 用户改了 .env 的 *API_KEY 后, 必须重跑 manual_import, 否则 db 里的 ciphertext 还是老的
- 结果: router 用 db 里的老 ciphertext 解密得到的明文跟 .env 不一致, 用户不知道为什么请求失败

场景:
- reset_db 之后用户改了 .env 的 key, 但没重跑 manual_import
- 用户新增了 SKILL 又改了几个渠道的 key, 但没意识到要 sync

fix_***_models.py / add_account.py / reencrypt_accounts.py 都不覆盖这个场景:
- fix_*_models.py: 动 price_snapshots 表, 不动 accounts
- add_account.py: 添加新账号, 不更新已有账号
- reencrypt_accounts.py: master key 切换场景, 新老 key 都是用, 但 plaintext 还是 db 里解出来的

本脚本职责:
- 单职责: 同步 .env 的 _API_KEY 明文到 db accounts.api_key_ciphertext
- 对每个 channel:
  - 读 .env 的 _API_KEY 明文
  - 用当前 AIMC_MASTER_KEY 解 db 里的 ciphertext
  - 比较两者, 列出 4 种状态 (见下)
  - --apply 时 UPDATE ciphertext

设计的 4 种状态:
| 状态                   | 含义                                              | 处理                    |
|-----------------------|---------------------------------------------------|------------------------|
| OK                    | .env 值 == 解密后的明文                            | 无需操作               |
| OUT_OF_SYNC           | .env 值 != 解密后的明文 (用户改了 .env 但没 sync)   | --apply: 重加密更新    |
| MISSING_DB            | .env 有值, 但 db 没该 channel/account              | 提示跑 manual_import   |
| MASTER_KEY_MISMATCH   | ciphertext 解不开 (老 master key 加密的)           | 提示跑 reencrypt_accounts |
| MISSING_ENV           | .env 没值, db 有 ciphertext (老残留)               | 提示是否清理           |

用法:

    # 1. dry-run (默认) — 列出所有 channel 的 sync 状态
    python scripts/sync_env_keys.py

    # 2. 应用 (只动 OUT_OF_SYNC, 需要二次确认)
    python scripts/sync_env_keys.py --apply --yes

    # 3. 详细输出 (每条状态的解密尝试)
    python scripts/sync_env_keys.py --verbose

    # 4. 回滚 (从备份恢复)
    python scripts/sync_env_keys.py --apply --yes --rollback backups/aimc_20260812_xxxxxx.db

安全:
- 默认 dry-run
- --apply 必须配 --yes (二次确认)
- SQLite 单条 UPDATE 事务原子
- 自动备份 db 到 backups/aimc_<timestamp>.db
- 需要 stop service? 不需要, router 5min 自动刷缓存. 但 stats 实时显示的某些
  指标更新不了, --apply 后建议 systemd --user restart aimc

不操作:
- 不创建 channel (那是 manual_import 的活)
- 不创建 account (那是 add_account.py 的活)
- 不动 master key (那是 gen_master_key + reencrypt_accounts 的活)
- 不动 price_snapshots (那是 fix_*_models.py 的活)
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

from dotenv import load_dotenv  # noqa: E402
load_dotenv()  # 读 .env

from sqlalchemy import select, update  # noqa: E402

from config import settings  # noqa: E402
from db.engine import async_session_factory  # noqa: E402
from db.init_db import init_db  # noqa: E402
from db.models import Account as AccountModel, Channel as ChannelModel  # noqa: E402
from security.crypto import try_decrypt, encrypt  # noqa: E402


# === 配置: env_var -> (provider, channel_name) 映射 ===
# 跟 scripts/manual_import.py CHANNELS 保持一致 (V4.6.4 = 10 个渠道)
# 维护: 以后 manual_import 加新渠道, 这里同步加一行
ENV_VAR_TO_PROVIDER = {
    "DEEPSEEK_API_KEY":    ("deepseek",    "deepseek_official"),
    "QWEN_API_KEY":        ("qwen",        "qwen_bailian"),
    "GLM_API_KEY":         ("glm",         "glm_bigmodel"),
    "KIMI_API_KEY":        ("kimi",        "kimi_official"),
    "MINIMAX_API_KEY":     ("minimax",     "minimax_compat"),
    "SILICONFLOW_API_KEY": ("siliconflow", "siliconflow_aggregator"),
    "VOLCENGINE_API_KEY":  ("volcengine",  "volcengine_ark"),
    "HUNYUAN_API_KEY":     ("hunyuan",     "hunyuan_official"),
    "STEPFUN_API_KEY":     ("stepfun",     "stepfun_official"),
    "SENSENOVA_API_KEY":   ("sensenova",   "sensenova_official"),
}


def _clean_env_value(value: str) -> str:
    """跟 manual_import._clean_env_value 保持一致"""
    v = (value or "").strip()
    if v.lstrip().startswith("#"):
        return ""
    if " #" in v:
        v = v.split(" #", 1)[0].rstrip()
    return v


def _backup_db(db_path: Path, backups_dir: Path) -> Path:
    """备份 SQLite db"""
    backups_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backups_dir / f"aimc_{timestamp}.db"
    shutil.copy2(db_path, backup_path)
    return backup_path


async def check_one_channel(
    session, env_var: str, provider: str, channel_name: str, master_key: str,
) -> dict:
    """检查单个 channel 的 sync 状态

    Returns:
        {
            "env_var": str,
            "provider": str,
            "channel_name": str,
            "env_value": str,         # .env 里的明文 (清洗后)
            "env_value_set": bool,    # .env 是否有非空值
            "db_channel_id": int | None,
            "db_account_id": int | None,
            "db_plaintext": str | None,   # 解密后的明文 (or None if fail)
            "status": str,     # OK / OUT_OF_SYNC / MISSING_DB / MASTER_KEY_MISMATCH / MISSING_ENV
            "detail": str,     # 人类可读的说明
        }
    """
    # 1. .env 明文
    raw = os.getenv(env_var, "")
    env_value = _clean_env_value(raw)
    env_value_set = bool(env_value) and not env_value.startswith("sk-your-")

    # 2. db 查 channel
    stmt = select(ChannelModel).where(ChannelModel.provider == provider)
    ch = (await session.execute(stmt)).scalars().first()

    result = {
        "env_var": env_var,
        "provider": provider,
        "channel_name": channel_name,
        "env_value": env_value,
        "env_value_set": env_value_set,
        "db_channel_id": None,
        "db_account_id": None,
        "db_plaintext": None,
        "status": "",
        "detail": "",
    }

    if ch is None:
        # db 没有 channel
        if env_value_set:
            result["status"] = "MISSING_DB"
            result["detail"] = ".env 有 key, 但 db 没该渠道 (请先跑 scripts/manual_import.py)"
        else:
            result["status"] = "OK"
            result["detail"] = ".env 没 key, db 也没渠道 (空状态)"
        return result

    result["db_channel_id"] = ch.id

    # 3. db 查 account
    stmt_acc = select(AccountModel).where(
        AccountModel.channel_id == ch.id,
        AccountModel.status == "active",
    )
    acc = (await session.execute(stmt_acc)).scalars().first()

    if acc is None:
        if env_value_set:
            result["status"] = "MISSING_DB"
            result["detail"] = f".env 有 key, 但 db 渠道 id={ch.id} 没 active account (请跑 manual_import.py 或 add_account.py)"
        else:
            result["status"] = "OK"
            result["detail"] = f".env 没 key, db 渠道 id={ch.id} 也没 active account (空状态)"
        return result

    result["db_account_id"] = acc.id

    # 4. 解密 ciphertext
    db_plaintext = try_decrypt(acc.api_key_ciphertext, master_key)

    if db_plaintext is None and acc.api_key_ciphertext:
        # ciphertext 有但解不开 -> master key mismatch
        result["status"] = "MASTER_KEY_MISMATCH"
        result["detail"] = "ciphertext 解不开 (请跑 scripts/reencrypt_accounts.py)"
        return result

    # 5. 状态判断
    if not env_value_set:
        if db_plaintext:
            result["status"] = "MISSING_ENV"
            result["detail"] = f".env 没值但 db 解出明文 {db_plaintext[:8]}... (db 残留, --apply 不会自动清理)"
        else:
            result["status"] = "OK"
            result["detail"] = ".env 没值, db 也没 ciphertext (空状态)"
        return result

    result["db_plaintext"] = db_plaintext

    if db_plaintext == env_value:
        result["status"] = "OK"
        result["detail"] = ".env 跟 db 一致 (无需操作)"
    else:
        result["status"] = "OUT_OF_SYNC"
        result["detail"] = (
            f".env 明文 != db 解出明文 "
            f"(env={env_value[:8]}..., db={db_plaintext[:8] if db_plaintext else 'None'}...)"
        )

    return result


async def show_state(session, master_key: str) -> list:
    """扫描所有 (env_var, channel) 对"""
    print("=== 扫描所有 _API_KEY (.env vs db accounts) ===\n")
    results = []
    for env_var, (provider, channel_name) in ENV_VAR_TO_PROVIDER.items():
        r = await check_one_channel(session, env_var, provider, channel_name, master_key)
        results.append(r)
        # 打印
        marker = {
            "OK": "  OK ",
            "OUT_OF_SYNC": "DIFF ",
            "MISSING_DB": "  -- ",
            "MASTER_KEY_MISMATCH": "FAIL ",
            "MISSING_ENV": " ORP ",
        }.get(r["status"], " ??  ")
        env_show = "SET" if r["env_value_set"] else "---"
        db_show = "SET" if r["db_plaintext"] else "---"
        print(
            f"  [{marker}] {env_var:22s}  env={env_show}  db={db_show}  {r['detail']}"
        )
    return results


async def apply_updates(session, results: list, master_key: str) -> int:
    """对 OUT_OF_SYNC 的 account 重加密 + UPDATE"""
    need_update = [r for r in results if r["status"] == "OUT_OF_SYNC"]
    if not need_update:
        print("\n[OK] 没有 OUT_OF_SYNC 的 account, 无需更新")
        return 0

    updated = 0
    for r in need_update:
        new_ct = encrypt(r["env_value"], master_key)
        stmt = update(AccountModel).where(
            AccountModel.id == r["db_account_id"],
        ).values(
            api_key_ciphertext=new_ct,
            updated_at=datetime.now(),
        )
        await session.execute(stmt)
        updated += 1
        print(f"  [OK] 重加密 account_id={r['db_account_id']} ({r['env_var']})")
    await session.commit()
    return updated


async def apply(args) -> int:
    """主流程"""
    if not settings.aimc_master_key:
        print("[ERROR] AIMC_MASTER_KEY 未配置. 跑 scripts/gen_master_key.py 生成并填到 .env")
        return 1

    # 回滚路径优先
    if args.rollback:
        backup = Path(args.rollback)
        if not backup.exists():
            print(f"[ERROR] 备份文件不存在: {backup}")
            return 1
        from config import settings as _s
        db_path = Path(_s.db_path)
        print(f"[INFO] 从 {backup} 回滚到 {db_path}")
        if not args.apply:
            print(f"  [DRY-RUN] 加 --apply 才会真回滚")
            return 0
        shutil.copy2(backup, db_path)
        print(f"  [OK] 已回滚. 重启服务: sudo systemctl --user restart aimc")
        return 0

    await init_db()

    async with async_session_factory() as session:
        results = await show_state(session, settings.aimc_master_key)

        # 统计
        status_count = {}
        for r in results:
            status_count[r["status"]] = status_count.get(r["status"], 0) + 1
        print(f"\n=== 汇总 ===")
        for s, n in sorted(status_count.items()):
            marker = {
                "OK": "✓", "OUT_OF_SYNC": "!",
                "MISSING_DB": "?", "MASTER_KEY_MISMATCH": "X",
                "MISSING_ENV": "~",
            }.get(s, " ")
            print(f"  [{marker}] {s}: {n}")

        # 只 OUT_OF_SYNC 可以 --apply
        out_of_sync = [r for r in results if r["status"] == "OUT_OF_SYNC"]
        master_key_mismatch = [r for r in results if r["status"] == "MASTER_KEY_MISMATCH"]
        missing_db = [r for r in results if r["status"] == "MISSING_DB"]
        missing_env = [r for r in results if r["status"] == "MISSING_ENV"]

        if not args.apply:
            print(f"\n[DRY-RUN] 未做任何修改 (加 --apply --yes 才会改 db)")
            if out_of_sync:
                print(f"  + --apply 会重加密 {len(out_of_sync)} 个 OUT_OF_SYNC account:")
                for r in out_of_sync:
                    print(f"    - {r['env_var']} (account_id={r['db_account_id']})")
            return 0

        # 应用阶段
        if not args.yes:
            print(f"\n[ERROR] --apply 必须配 --yes (二次确认)")
            return 1

        # 备份
        from config import settings as _s
        db_path = Path(_s.db_path)
        backups_dir = ROOT / "backups"
        backup = _backup_db(db_path, backups_dir)
        print(f"\n[INFO] 已备份: {backup}")

        # 真正更新 (只动 OUT_OF_SYNC)
        updated = await apply_updates(session, results, settings.aimc_master_key)

        # 给其他状态提示
        if master_key_mismatch:
            print(f"\n[注意] {len(master_key_mismatch)} 个 account ciphertext 解不开 (master key 错):")
            for r in master_key_mismatch:
                print(f"  - {r['env_var']} (account_id={r['db_account_id']})")
            print(f"  修复: 跑 scripts/reencrypt_accounts.py (老 key 还在) 或 add_account.py 重建")

        if missing_db:
            print(f"\n[注意] {len(missing_db)} 个 env key 没 db account (channel/account 缺失):")
            for r in missing_db:
                print(f"  - {r['env_var']}  (provider={r['provider']})")
            print(f"  修复: 跑 scripts/manual_import.py 创建渠道, 或 add_account.py 添加账号")

        if missing_env:
            print(f"\n[注意] {len(missing_env)} 个 channel db 有 ciphertext 但 .env 没 key (db 残留):")
            for r in missing_env:
                print(f"  - {r['env_var']} (db 明文: {r['db_plaintext'][:8] if r['db_plaintext'] else 'None'}...)")
            print(f"  sync_env_keys 不会自动清理 (避免误删), 手动改 db 或跑 add_account.py 重建")

        # 提示
        print(f"\n下一步:")
        print(f"  1. 重启服务: sudo systemctl --user restart aimc  (立即生效)")
        print(f"     或等 5min 自动缓存刷新")
        print(f"  2. 验证: 跑 sync_env_keys (应该全部 OK)")
        print(f"  3. 测试路由:")
        print(f"     curl -X POST http://localhost:8080/v1/chat/completions \\")
        print(f"       -H 'Content-Type: application/json' \\")
        print(f"       -d '{{\"model\":\"...\",\"messages\":[{{\"role\":\"user\",\"content\":\"ping\"}}]}}'")
        print(f"\n回滚 (如需):")
        print(f"  python scripts/sync_env_keys.py --apply --yes --rollback {backup}")

        return 0


def main():
    parser = argparse.ArgumentParser(
        description="同步 .env 的 _API_KEY 到 db accounts (V4.6.4)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/sync_env_keys.py                              # dry-run, 看 .env vs db 状态
  python scripts/sync_env_keys.py --verbose                    # 详细输出
  python scripts/sync_env_keys.py --apply --yes                # 应用 OUT_OF_SYNC (自动备份)
  python scripts/sync_env_keys.py --apply --yes --rollback backups/aimc_xxx.db
        """,
    )
    parser.add_argument("--apply", action="store_true", help="实际修改 db (重加密 OUT_OF_SYNC account)")
    parser.add_argument("--yes", action="store_true", help="二次确认 (必须跟 --apply 一起)")
    parser.add_argument("--verbose", action="store_true", help="详细输出 (每条状态的详细比对)")
    parser.add_argument("--rollback", metavar="BACKUP_PATH", help="从备份回滚 db")
    args = parser.parse_args()
    return asyncio.run(apply(args))


if __name__ == "__main__":
    sys.exit(main())
