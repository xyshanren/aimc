"""V4.6.1 多账号批量导入脚本

设计目标 (按 docs/development-plan.md §6.1):
- 从 .env 读 `*_API_KEYS=sk-key1,sk-key2,sk-key3` (逗号分隔)
- 同一个 channel 自动建多个账号 (e.g. deepseek_official 下 deepseek_01 / deepseek_02 / ...)
- 配合 gateway/cooldown.py + router least-used: 单 key 失败 → 进冷却, 路由自动切下一个 key

跟 scripts/manual_import.py 的关系:
- manual_import.py 走 `DEEPSEEK_API_KEY=sk-xxx` 单 key 路径 (V4.3 兼容, 仅第一个账号)
- multi_account_import.py 走 `DEEPSEEK_API_KEYS=sk-1,sk-2,sk-3` 多 key 路径 (V4.4+ 推荐)
- 两个脚本互补: 第一次 init 用 manual_import.py (建渠道), 加 key 用 multi_account_import.py

跟 scripts/add_account.py 的关系:
- add_account.py 是 CLI 工具, 一次加一个账号, 适合 dashboard 临时加
- multi_account_import.py 是批量 + 幂等, 适合 CI / 部署时一次性灌入

用法:
    # 1) 先跑 manual_import.py 初始化 channel (建 deepseek_official 等):
    python scripts/manual_import.py

    # 2) .env 里配多 key:
    DEEPSEEK_API_KEYS=sk-key1,sk-key2,sk-key3
    QWEN_API_KEYS=sk-key1,sk-key2

    # 3) 跑 multi_account_import.py 追加账号:
    python scripts/multi_account_import.py

幂等性:
- 同 provider 已存在的 active channel: 复用, 不重建
- 同 channel 下同账号名 (deepseek_01 等): skip
- 新账号命名: deepseek_01 / deepseek_02 / ... (按现有账号数递增)

输出:
- 每个 provider 一个 [import] 段, 显示新建 / 跳过账号
- 末尾 [summary] 统计

环境变量约定 (跟 .env.example 一致):
- 单 key: DEEPSEEK_API_KEY=sk-xxx  (V4.3 兼容, 也支持)
- 多 key: DEEPSEEK_API_KEYS=sk-1,sk-2,sk-3  (V4.4+ 推荐)
- 都填时: 多 key 优先 (覆盖单 key), 避免重复建账号

支持 9 个 channel (跟 manual_import.py 一致):
  deepseek / qwen / glm / kimi / minimax / siliconflow / volcengine / hunyuan / stepfun
"""

import asyncio
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from config import settings as _settings  # noqa: E402  (load_dotenv 之后再读 master key)
from db import crud  # noqa: E402
from db.engine import async_session_factory  # noqa: E402
from db.init_db import init_db  # noqa: E402
from security.crypto import encrypt  # noqa: E402
from sqlalchemy import select  # noqa: E402


# ============================================================
# 9 个 channel 的 provider 元数据 (跟 manual_import.py 对齐)
# ============================================================
# 注意: 只列 import 必需字段. base_url / prices 等复杂字段这里不写 —
# 多账号只解决"账号扩展", 渠道 / 价格由 manual_import.py 首次 init.
PROVIDERS = [
    # (provider_key, env_var_singular, env_var_plural, channel_name_template)
    # channel_name_template 用于在没有 active channel 时创建
    ("deepseek",     "DEEPSEEK_API_KEY",   "DEEPSEEK_API_KEYS",   "deepseek_official"),
    ("qwen",         "QWEN_API_KEY",       "QWEN_API_KEYS",       "qwen_bailian"),
    ("glm",          "GLM_API_KEY",        "GLM_API_KEYS",        "glm_bigmodel"),
    ("kimi",         "KIMI_API_KEY",       "KIMI_API_KEYS",       "kimi_official"),
    ("minimax",      "MINIMAX_API_KEY",    "MINIMAX_API_KEYS",    "minimax_compat"),
    ("siliconflow",  "SILICONFLOW_API_KEY","SILICONFLOW_API_KEYS","siliconflow_aggregator"),
    ("volcengine",   "VOLCENGINE_API_KEY", "VOLCENGINE_API_KEYS", "volcengine_ark"),
    ("hunyuan",      "HUNYUAN_API_KEY",    "HUNYUAN_API_KEYS",    "hunyuan_official"),
    ("stepfun",      "STEPFUN_API_KEY",    "STEPFUN_API_KEYS",    "stepfun_official"),
]


def _clean_env_value(value: str) -> str:
    """清洗 .env 行内注释 ('KEY=  # 注释') + 空白."""
    v = (value or "").strip()
    if v.lstrip().startswith("#"):
        return ""
    if " #" in v:
        v = v.split(" #", 1)[0].rstrip()
    return v


def _extract_keys(provider_key: str, env_var_single: str, env_var_plural: str) -> list[str]:
    """从一个 provider 的两个 env var 中提取 key 列表.

    优先级: _API_KEYS (多 key) > _API_KEY (单 key).
    都填时多 key 优先, 避免重复建账号.
    """
    plural_raw = _clean_env_value(os.getenv(env_var_plural, ""))
    single_raw = _clean_env_value(os.getenv(env_var_single, ""))

    # 多 key 优先
    raw = plural_raw or single_raw
    if not raw:
        return []

    keys = [k.strip() for k in raw.split(",") if k.strip()]
    # 跳过占位符
    keys = [k for k in keys if not k.startswith("sk-your-")]
    return keys


async def _get_active_channel(session, provider: str) -> int | None:
    """查找 provider 的 active channel id.

    设计原则: multi_account_import 不负责创建 channel —
    渠道 / base_url / prices 由 manual_import.py 首次 init 负责 (V4.3 兼容路径).
    本脚本仅向已有 channel 追加账号. 找不到时返回 None, 调用方 skip.
    """
    from db.models import Channel as ChannelModel

    existing = (await session.execute(
        select(ChannelModel).where(
            ChannelModel.provider == provider,
            ChannelModel.status == "active",
        ).order_by(ChannelModel.id)
    )).scalars().first()

    return existing.id if existing else None


async def import_one_provider(
    session, provider_key: str, env_var_single: str, env_var_plural: str, _channel_name: str,
) -> dict:
    """导入一个 provider 的多账号 (向已有 channel 追加).

    Returns:
        {"provider": ..., "created": [names], "skipped": [names], "skipped_dup_keys": int,
         "skipped_no_channel": bool, "skipped_empty": bool}
    """
    keys = _extract_keys(provider_key, env_var_single, env_var_plural)
    result = {
        "provider": provider_key,
        "created": [],
        "skipped": [],
        "skipped_dup_keys": 0,
        "skipped_no_channel": False,
        "skipped_empty": False,
    }

    if not keys:
        result["skipped_empty"] = True
        return result

    # master key 校验
    if not _settings.aimc_master_key:
        print(f"[skip] {provider_key}: AIMC_MASTER_KEY 未配置, 无法加密")
        return result

    # 找已有 channel (不负责创建 — 那是 manual_import.py 的活)
    channel_id = await _get_active_channel(session, provider_key)
    if channel_id is None:
        result["skipped_no_channel"] = True
        print(f"[skip] {provider_key}: 没有 active channel, 请先跑 manual_import.py 初始化")
        return result

    # 列出该 channel 现有账号名 (用于查重)
    from db.models import Account as AccountModel
    used_names: set[str] = set(
        (await session.execute(
            select(AccountModel.account_name).where(AccountModel.channel_id == channel_id)
        )).scalars().all()
    )

    # 去重 (同一个 env 里重复 key 只算一次)
    seen_keys: set[str] = set()
    for i, raw_key in enumerate(keys, start=1):
        if raw_key in seen_keys:
            result["skipped_dup_keys"] += 1
            continue
        seen_keys.add(raw_key)

        # 位置式命名: env 第 i 个 key → provider_key_{i:02d}
        # 这样保证幂等性: 同一 env 重跑永远得到相同 slot
        # (缺点: 中间删 key 会留洞, 但多 key 场景基本不会)
        candidate_name = f"{provider_key}_{i:02d}"
        if candidate_name in used_names:
            # 已存在 → skip (幂等)
            result["skipped"].append(candidate_name)
            continue

        if i > 99:
            print(f"[warn] {provider_key}: env key 超过 99 个, 跳过剩余")
            break

        # 加密入库
        from datetime import datetime
        account = await crud.create_account(
            session,
            channel_id=channel_id,
            account_name=candidate_name,
            api_key="",  # API-Sec-1: 空字符串占位
            api_key_ciphertext=encrypt(raw_key, _settings.aimc_master_key),
            status="active",
            quota_total=None,
            quota_used=0,
            last_rotated_at=datetime.now(),
        )
        result["created"].append(account.account_name)
        used_names.add(account.account_name)  # 同步给本轮后续调用

    return result


async def run_import():
    """主入口: 遍历 9 个 provider, 导入多 key."""
    await init_db()

    results = []
    async with async_session_factory() as session:
        for provider_key, env_single, env_plural, channel_name in PROVIDERS:
            r = await import_one_provider(
                session, provider_key, env_single, env_plural, channel_name,
            )
            results.append(r)
            await session.commit()  # 每个 provider 一次 commit, 失败隔离

    # ============== 打印报告 ==============
    print()
    print("=" * 60)
    print("V4.6.1 multi_account_import — 报告")
    print("=" * 60)

    total_created = 0
    total_skipped_existing = 0
    total_skipped_empty = 0
    total_skipped_no_channel = 0

    for r in results:
        provider = r["provider"]
        if r["skipped_empty"]:
            print(f"\n[skip-empty] {provider}: .env 未配 (单 key / 多 key 都缺)")
            total_skipped_empty += 1
            continue
        if r["skipped_no_channel"]:
            # import_one_provider 已 print 过, 这里只计数
            total_skipped_no_channel += 1
            continue
        if not r["created"] and not r["skipped"]:
            continue

        created_str = ", ".join(r["created"]) if r["created"] else "(无)"
        skipped_str = ", ".join(r["skipped"]) if r["skipped"] else "(无)"
        print(f"\n[append] {provider}:")
        print(f"  新建账号 ({len(r['created'])}): {created_str}")
        print(f"  跳过账号 ({len(r['skipped'])}, 同名已存在): {skipped_str}")
        if r["skipped_dup_keys"]:
            print(f"  .env 内重复 key 跳过: {r['skipped_dup_keys']}")
        total_created += len(r["created"])
        total_skipped_existing += len(r["skipped"])

    print()
    print("=" * 60)
    print(f"[summary] 新建账号: {total_created}  /  "
          f"跳过 (同名): {total_skipped_existing}  /  "
          f"env 缺配: {total_skipped_empty}  /  "
          f"无 channel: {total_skipped_no_channel}")
    print("=" * 60)

    if total_created == 0 and total_skipped_existing == 0:
        print()
        print("! 没有账号被处理 — 可能原因:")
        print("  1. .env 里所有 *_API_KEY / *_API_KEYS 都没填 / 填了占位符")
        print("  2. AIMC_MASTER_KEY 未配置 (运行 python scripts/gen_master_key.py)")
        print("  3. 对应 provider 没有 active channel (请先跑 manual_import.py 初始化)")
        print()
        print("示例配置 (复制到 .env):")
        print("  DEEPSEEK_API_KEYS=sk-key1,sk-key2,sk-key3")
        print("  QWEN_API_KEYS=sk-key1,sk-key2")
        print("  SILICONFLOW_API_KEYS=sk-key1")
    elif total_created > 0:
        print()
        print("[提示] 重启 gateway 让 RouteEngine 重新加载缓存, 或等待 600s 自动刷新.")
        print("       后续可手动触发刷新:")
        print("       curl -X POST http://127.0.0.1:8080/admin/refresh-cache")


if __name__ == "__main__":
    print("=" * 60)
    print("AIMC multi_account_import.py — V4.6.1 多账号批量导入")
    print("=" * 60)
    asyncio.run(run_import())