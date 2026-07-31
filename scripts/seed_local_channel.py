"""本地模型渠道录入脚本（Ollama / vllm / LM Studio / llama.cpp）

设计目标:
- 让"本地跑个 Ollama 就能走 AIMC"成为 1 分钟级操作
- 不写死任何 base_url — 从 .env 读，默认值对应最常见的本地服务
- 不需要 API key — 本地服务一般不校验，留空占位即可
- 写入 provider_type='local' + billing_mode='free'
- 写入 price_snapshots 让路由能选到（价格 = 0，理论上永远被路由优先选）

支持的本地服务（OpenAI 兼容协议）:

| 服务        | 默认 base_url                  | 模型名示例                  |
|-------------|--------------------------------|-----------------------------|
| Ollama      | http://localhost:11434/v1      | qwen2:7b / deepseek-r1:7b   |
| vllm        | http://localhost:8000/v1       | --served-model-name 指定    |
| LM Studio   | http://localhost:1234/v1       | model loader 名             |
| llama.cpp   | http://localhost:8080/v1       | server 启动时 -m 指定       |

用法:
    1. (可选) 编辑 .env 配置 OLLAMA_BASE_URL / VLLM_BASE_URL 等
    2. python scripts/seed_local_channel.py
    3. 脚本自动检测本地服务是否在跑（GET /v1/models），列出可用 model
    4. 选择要录入的 model，按 y 确认

设计哲学:
- **跟 remote channel 一致的接口**：channel + account + price_snapshots 三表一起写
- **复用 manual_import.py 的 import_one_channel 模式**：渠道元数据 + accounts + 价格
- **价格始终 0 元 / 百万 tokens**：billing_mode=free 让路由时 cheapest 策略永远命中
- **idempotent**：同名 channel 存在就跳过（跟 manual_import.py 一致）

注:
- proxy.py 检测 provider_type=local 时自动跳过 Authorization header
- router.py 通过 billing_mode=free 让本地模型永远在 cheapest 排序第一
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import httpx

from db.engine import async_session_factory
from db.init_db import init_db
from db import crud
from config import settings


# ========== 本地服务配置 ==========
# 每个本地服务配置：name / provider / base_url / 健康检查 path
# base_url 优先从 env 读，默认值是各服务的"开箱即用"端口
LOCAL_CHANNELS = [
    {
        "key": "ollama",
        "name": "ollama_local",
        "provider": "ollama",
        "base_url": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        "health_path": "/models",  # Ollama OpenAI-compat /v1/models
        "discover_path": "/models",  # Ollama 的 OpenAI-compat 路径
    },
    {
        "key": "vllm",
        "name": "vllm_local",
        "provider": "vllm",
        "base_url": os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1"),
        "health_path": "/models",
        "discover_path": "/models",
    },
    {
        "key": "lmstudio",
        "name": "lmstudio_local",
        "provider": "lmstudio",
        "base_url": os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1"),
        "health_path": "/models",
        "discover_path": "/models",
    },
    {
        "key": "llamacpp",
        "name": "llamacpp_local",
        "provider": "llamacpp",
        "base_url": os.getenv("LLAMACPP_BASE_URL", "http://localhost:8080/v1"),
        "health_path": "/models",
        "discover_path": "/models",
    },
]


async def probe_local_service(base_url: str, timeout: float = 2.0) -> dict | None:
    """探测本地服务是否在跑，返回 {"models": [...]} 或 None

    用 /v1/models (OpenAI compat) 做健康检查 + model 发现。
    200 且有 data 字段 → 服务可用；其他 → 服务未起或协议不兼容。
    """
    url = f"{base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
        if r.status_code != 200:
            return None
        data = r.json()
        models = data.get("data", [])
        return {
            "models": [m.get("id") for m in models if m.get("id")],
            "raw": data,
        }
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
        return None
    except Exception:
        return None


async def seed_one_local_channel(session, cfg: dict, model_ids: list[str]) -> bool:
    """录入单个本地 channel（一个或多个 model）。

    设计:
    - provider_type='local'（Phase 2.3 字段，proxy 检测到跳过 Authorization）
    - billing_mode='free'（router cheapest 永远优先）
    - price_snapshots: input_price=0, output_price=0
    - api_key 写占位字符串 "no-key-required"（NOT NULL 约束兼容 + 让人一眼看出是本地）

    Returns:
        True = 创建了 channel + account + price，False = 跳过
    """
    from sqlalchemy import select
    from db.models import Channel as ChannelModel, Account as AccountModel

    # 幂等性: 同名 channel 已存在就 skip
    existing_ch = (await session.execute(
        select(ChannelModel).where(ChannelModel.name == cfg["name"])
    )).scalar_one_or_none()
    if existing_ch is not None:
        print(f"[skip] {cfg['name']}: channel 已存在 (channel_id={existing_ch.id})")
        return False

    # master key 校验（API-Sec-1）— 本地服务用占位 key 也要加密
    if not settings.aimc_master_key:
        print(f"[skip] {cfg['name']}: AIMC_MASTER_KEY 未配置, 无法加密占位 key")
        return False

    # 创建 channel
    from security.crypto import encrypt
    placeholder_key = "no-key-required"  # 本地服务一般不校验 Authorization
    ciphertext = encrypt(placeholder_key, settings.aimc_master_key)

    channel = await crud.create_channel(
        session,
        name=cfg["name"],
        provider=cfg["provider"],
        base_url=cfg["base_url"],
        priority=0,  # 本地 = 高优先级（同价时优先本地，省外网流量）
        provider_type="local",
        billing_mode="free",
    )
    print(f"[+] 创建 channel: {channel.name} (id={channel.id}, provider_type=local)")

    account = await crud.create_account(
        session,
        channel_id=channel.id,
        account_name=f"{cfg['key']}_local_01",
        api_key="",  # deprecated, 占位
        api_key_ciphertext=ciphertext,
        quota_total=None,
    )
    # API-Sec-3: last_rotated_at 写当前（占位 key 也算"已就绪"）
    from datetime import datetime
    account.last_rotated_at = datetime.now()
    await session.commit()
    print(f"[+] 创建 account: {account.account_name} (本地服务, 占位 key 已加密)")

    # 价格: 0 元（free 模型）, 录入每个发现的 model
    for model_id in model_ids:
        await crud.create_price_snapshot(
            session,
            channel_id=channel.id,
            model=model_id,
            price_type="normal",
            time_range="全天",
            input_price=0.0,
            output_price=0.0,
            source="local",
        )
    print(f"[+] 价格已录入: {len(model_ids)} 个 model, 全部 0 元 (billing_mode=free)")

    # 配额: 本地不限速, 写一个超大的 qpm 当软上限
    await crud.create_quota(
        session,
        channel_id=channel.id,
        account_id=account.id,
        quota_type="qpm",
        limit_value=9999,  # 本地无上游限速, 给个超大值
        reset_strategy="rolling_window",
    )
    return True


async def interactive_seed():
    """主流程: 探测所有本地服务, 列出可用 model, 让用户选择录入哪些"""
    print("=" * 60)
    print("AIMC 本地模型渠道录入 (Ollama / vllm / LM Studio / llama.cpp)")
    print("=" * 60)

    await init_db()

    available = []  # 探测到的可用服务
    for cfg in LOCAL_CHANNELS:
        print(f"\n探测 {cfg['name']} ({cfg['base_url']})...", end=" ")
        result = await probe_local_service(cfg["base_url"])
        if result is None:
            print("未响应 (服务未起或 base_url 不对)")
            continue
        models = result["models"]
        if not models:
            print(f"已响应, 但无 model (可能服务起来了但还没 pull 模型)")
            continue
        print(f"✓ 发现 {len(models)} 个 model:")
        for m in models[:5]:  # 只打印前 5 个
            print(f"    - {m}")
        if len(models) > 5:
            print(f"    ... 还有 {len(models) - 5} 个")
        available.append({**cfg, "models": models})

    if not available:
        print("\n[!] 没有可用的本地服务被探测到")
        print("    排查建议:")
        print("    1. Ollama 是否在跑?  → ollama serve (默认 http://localhost:11434)")
        print("    2. vllm 是否在跑?    → vllm serve <model> (默认 :8000)")
        print("    3. LM Studio 是否开了本地 server?  → 默认 :1234")
        print("    4. 自定义 base_url?  → 编辑 .env 设置 OLLAMA_BASE_URL 等")
        return

    print("\n" + "=" * 60)
    print("可选录入的本地 channel:")
    for i, svc in enumerate(available, 1):
        print(f"  {i}. {svc['name']} ({svc['base_url']}) — {len(svc['models'])} 个 model")

    print("\n输入数字录入对应服务 (多选用逗号, 如 '1,3'), 留空直接录入全部:")
    choice = input("> ").strip()

    if choice:
        try:
            indices = [int(x.strip()) - 1 for x in choice.split(",") if x.strip()]
            selected = [available[i] for i in indices if 0 <= i < len(available)]
        except (ValueError, IndexError):
            print("[!] 输入无效, 录入全部")
            selected = available
    else:
        selected = available

    if not selected:
        print("[!] 没有选择, 退出")
        return

    print(f"\n准备录入 {len(selected)} 个本地 channel...\n")
    created = 0
    async with async_session_factory() as session:
        for svc in selected:
            print(f"--- {svc['name']} ---")
            if await seed_one_local_channel(session, svc, svc["models"]):
                created += 1
        await session.commit()

    print(f"\n[OK] 完成: {created} 个本地 channel 已录入")
    if created > 0:
        print("\n下一步:")
        print("  1. 重启 uvicorn 或 POST /dashboard/api/cache/refresh 刷新路由缓存")
        print("  2. curl http://localhost:8080/v1/models 看到本地 model 出现")
        print("  3. 用 Agent 调 model='qwen2:7b' 即可走本地 (group 也支持, 如 tier:light)")


if __name__ == "__main__":
    asyncio.run(interactive_seed())
