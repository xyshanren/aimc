"""诊断 accounts 表 ciphertext 健康度 (V4.6.4)

背景:
- router 启动时如果 _resolve_account_key 返回 None, 会跳过该 account
- 老代码警告 "既无 ciphertext 也无明文 key" 误导排查
- 真实根因常常是: ciphertext 存在但用当前 AIMC_MASTER_KEY 解密失败
  (典型场景: master key 跟 .env 不一致 / 备份恢复时 master key 错)

用法:
    # 1. 默认 dry-run (打印诊断, 不动 db)
    python scripts/check_ciphertext_health.py

    # 2. JSON 输出 (便于脚本处理)
    python scripts/check_ciphertext_health.py --json

    # 3. 列出具体失败原因 (展开每个 account)
    python scripts/check_ciphertext_health.py --verbose

输出:
- master key 健康度 (长度 / 是否 hex / 解密一段 test ciphertext)
- 每个 active account 的 ciphertext 状态 (长度 / 解密是否成功 / 失败原因)
- 汇总: 总数 / 健康 / 损坏 / 明文 / 空

常见失败原因:
1. "master key 格式错" - AIMC_MASTER_KEY 不是 64 字符 hex
2. "decrypt 失败 (UTF-8 decode 失败)" - master key 跟加密时用的不一致
3. "ciphertext 不是有效 Base64" - 加密格式不兼容 (例如 Fernet 老格式 vs XOR+Base64 新格式)
4. "ciphertext 为空" - 该 account 没 ciphertext 也没明文 (旧数据残留)
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import Account, Channel


def check_master_key_health() -> dict:
    """检查 AIMC_MASTER_KEY 的健康度"""
    from config import settings
    mk = settings.aimc_master_key or ""
    result = {
        "master_key_set": bool(mk),
        "master_key_length": len(mk),
        "master_key_is_hex": False,
        "master_key_status": "ok",
    }
    if not mk:
        result["master_key_status"] = "MISSING"
        return result
    if len(mk) != 64:
        result["master_key_status"] = f"WRONG_LENGTH (got {len(mk)}, expected 64 hex chars)"
        return result
    try:
        bytes.fromhex(mk)
        result["master_key_is_hex"] = True
    except ValueError:
        result["master_key_status"] = "NOT_HEX"
        return result
    result["master_key_status"] = "ok"
    return result


async def check_accounts(verbose: bool = False) -> dict:
    """诊断每个 active account 的 ciphertext"""
    await init_db()

    summary = {
        "total": 0,
        "healthy": 0,
        "plaintext_only": 0,
        "empty": 0,
        "decrypt_failed": 0,
        "by_failure_reason": {},
        "accounts": [],
    }

    async with async_session_factory() as session:
        rows = (await session.execute(
            select(Account, Channel)
            .join(Channel, Account.channel_id == Channel.id)
            .where(Account.status == "active")
        )).all()

        summary["total"] = len(rows)

        from security.crypto import decrypt
        from config import settings

        for account, channel in rows:
            ct = account.api_key_ciphertext
            plaintext = account.api_key or ""

            entry = {
                "channel_id": channel.id,
                "channel_name": channel.name,
                "provider": channel.provider,
                "account_id": account.id,
                "account_name": account.account_name,
                "ciphertext_length": len(ct) if ct else 0,
                "ciphertext_exists": ct is not None and len(ct) > 0,
                "plaintext_exists": bool(plaintext),
                "decrypt_status": "n/a",
                "decrypt_error": None,
            }

            if not entry["ciphertext_exists"] and not entry["plaintext_exists"]:
                entry["decrypt_status"] = "EMPTY"
                summary["empty"] += 1
            elif not entry["ciphertext_exists"]:
                entry["decrypt_status"] = "PLAINTEXT_ONLY"
                summary["plaintext_only"] += 1
            else:
                try:
                    pt = decrypt(ct, settings.aimc_master_key)
                    entry["decrypt_status"] = "OK"
                    summary["healthy"] += 1
                except ValueError as e:
                    err_msg = str(e)
                    entry["decrypt_status"] = "FAILED"
                    entry["decrypt_error"] = err_msg
                    summary["decrypt_failed"] += 1
                    if "Base64" in err_msg:
                        reason = "invalid_base64"
                    elif "UTF-8" in err_msg or "decrypt 失败" in err_msg:
                        reason = "master_key_mismatch"
                    elif "格式错误" in err_msg or "长度错误" in err_msg:
                        reason = "master_key_format"
                    elif "ciphertext 不能为 None" in err_msg:
                        reason = "empty_ciphertext"
                    else:
                        reason = "unknown"
                    summary["by_failure_reason"][reason] = \
                        summary["by_failure_reason"].get(reason, 0) + 1
                except Exception as e:
                    entry["decrypt_status"] = "ERROR"
                    entry["decrypt_error"] = str(e)
                    summary["decrypt_failed"] += 1
                    summary["by_failure_reason"]["exception"] = \
                        summary["by_failure_reason"].get("exception", 0) + 1

            summary["accounts"].append(entry)

    return summary


def format_human(mk_health: dict, accounts: dict, verbose: bool = False) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append("AIMC accounts ciphertext 健康度检查 (V4.6.4)")
    lines.append("=" * 70)

    lines.append("\n=== AIMC_MASTER_KEY 健康度 ===")
    lines.append(f"  已设置: {mk_health['master_key_set']}")
    lines.append(f"  长度: {mk_health['master_key_length']} (期望 64 字符 hex)")
    lines.append(f"  是否 hex: {mk_health['master_key_is_hex']}")
    lines.append(f"  状态: {mk_health['master_key_status']}")
    if mk_health["master_key_status"] != "ok":
        lines.append("  [警告] master key 异常, 所有 ciphertext 解密都会失败")

    lines.append("\n=== Account 汇总 ===")
    lines.append(f"  总数: {accounts['total']}")
    lines.append(f"  健康 (ciphertext 解密成功): {accounts['healthy']}")
    lines.append(f"  仅明文 (无 ciphertext): {accounts['plaintext_only']}")
    lines.append(f"  损坏 (ciphertext 解密失败): {accounts['decrypt_failed']}")
    lines.append(f"  空 (都无): {accounts['empty']}")

    if accounts["by_failure_reason"]:
        lines.append("\n=== 失败原因分布 ===")
        for reason, count in accounts["by_failure_reason"].items():
            reason_desc = {
                "invalid_base64": "ciphertext 不是有效 Base64 (可能是 Fernet 老格式)",
                "master_key_mismatch": "master key 不匹配 (用了新 key 解老 ciphertext)",
                "master_key_format": "master key 格式错 (长度/非 hex)",
                "empty_ciphertext": "ciphertext 为 None",
                "exception": "未知异常",
                "unknown": "未知",
            }.get(reason, reason)
            lines.append(f"  {reason}: {count} - {reason_desc}")

    if verbose:
        lines.append("\n=== 详细 account 列表 ===")
        for a in accounts["accounts"]:
            status_icon = {
                "OK": "[OK] ",
                "EMPTY": "[EMPTY]",
                "PLAINTEXT_ONLY": "[PLAIN]",
                "FAILED": "[FAIL]",
                "ERROR": "[ERR] ",
            }.get(a["decrypt_status"], "[?]   ")
            ct_info = f"ct={a['ciphertext_length']}B"
            pt_info = "pt=yes" if a["plaintext_exists"] else "pt=no"
            err = f" [{a['decrypt_error']}]" if a["decrypt_error"] else ""
            lines.append(
                f"  {status_icon} {a['channel_name']:25s} {a['account_name']:20s} "
                f"{ct_info:12s} {pt_info:6s}{err}"
            )
    else:
        lines.append("\n=== 问题 accounts (--verbose 看全部) ===")
        issues = [a for a in accounts["accounts"] if a["decrypt_status"] not in ("OK", "PLAINTEXT_ONLY")]
        if not issues:
            lines.append("  [OK]  无问题")
        else:
            for a in issues:
                err = f" - {a['decrypt_error']}" if a["decrypt_error"] else ""
                lines.append(
                    f"  [{a['decrypt_status']:8s}] {a['channel_name']}/{a['account_name']}{err}"
                )

    lines.append("\n=== 修复建议 ===")
    if mk_health["master_key_status"] != "ok":
        lines.append("  1. master key 异常: 重新生成 + 重新加密所有 account")
        lines.append("     python scripts/gen_master_key.py")
        lines.append("     # 把新 key 写到 .env")
        lines.append("     python scripts/fix_sensenova_endpoint.py --rollback <old_backup>  # 如有备份")
    elif accounts["decrypt_failed"] > 0:
        lines.append("  1. 部分 ciphertext 解密失败 (master key 可能改过):")
        lines.append("     a. 如果 .env 改了 AIMC_MASTER_KEY, 恢复老 key 即可")
        lines.append("     b. 如果 .env 是对的, 说明 db 是用另一个 key 加密的:")
        lines.append("        - 找到原 master key 恢复")
        lines.append("        - 或: 删除失败 account 用新 key 重建")
        lines.append("          python scripts/add_account.py --channel <provider> --name <name> --api-key <key>")
        lines.append("          python scripts/cleanup_inactive_members.py  # 清理老 account")
    elif accounts["empty"] > 0:
        lines.append("  1. 有空 account (老数据残留):")
        lines.append("     python scripts/cleanup_inactive_members.py")
    else:
        lines.append("  [OK]  全部 account 健康")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="诊断 accounts 表 ciphertext 健康度 (V4.6.4)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 详细")
    parser.add_argument("--verbose", "-v", action="store_true", help="列出每个 account 详情")
    args = parser.parse_args()

    mk_health = check_master_key_health()
    accounts = asyncio.run(check_accounts(verbose=args.verbose))

    if args.json:
        print(json.dumps({"master_key": mk_health, "accounts": accounts}, ensure_ascii=False, indent=2))
    else:
        print(format_human(mk_health, accounts, verbose=args.verbose))

    return 0 if accounts["decrypt_failed"] == 0 and accounts["empty"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
