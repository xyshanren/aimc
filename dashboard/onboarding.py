"""看板「初始化配置」页后端 (2026-10-13)

受众: 全新部署 (无任何渠道账号) 的用户 — 浏览器打开看板即见引导页,
按卡片注册渠道 → 粘贴 API Key → 一键建渠道+账号+价格行, 不再需要
手动跑 manual_import.py / 编辑 .env。

设计:
- 渠道目录 = channels_catalog.py (单一事实源, 与 manual_import 共享)
- 建渠道/账号/价格行/qpm 配额 = 复刻 scripts/manual_import.py import_one_channel
  的服务端等价逻辑 (同加密/同幂等语义)
- setup_done = settings_runtime 里 onboarding_done=="1" OR 已有任意账号
  (老部署升级后不会突然弹出引导页)
- test 端点: 用目录里的 base_url 直连 GET /models 试 key (保存前可选试一下);
  base_url 是服务端目录常量, 不接受用户传入 URL — 无 SSRF 面
- 2026-10-14: 建渠道自动种子 registry+默认组 (db/seed_default.py, ensure 语义);
  + 模型组手动编辑端点 (组创建/启停/成员加移, 软删语义与 P1-2 一致);
  变更后前端应调 POST /api/cache/refresh 立即生效
"""

import ipaddress
import logging
import socket
import urllib.parse
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from channels_catalog import CATEGORY_META, CHANNELS, PR_GUIDE, get_by_key
from db import crud
from db.engine import async_session_factory
from db.models import Account, Channel, ModelGroup, ModelGroupMember, ModelRegistry, PriceSnapshot, SettingsRuntime
from db.seed_default import ensure_registry_and_members, seed_all_catalog_channels

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["onboarding"])

# settings_runtime 里的完成标记 key (前端 "跳过/完成" 时落 "1")
ONBOARDING_DONE_KEY = "onboarding_done"


def get_session_dep():
    """与 dashboard.routes.get_session 同源 (延迟导入避免循环依赖)"""
    from dashboard.routes import get_session
    return get_session


async def _setup_done(session: AsyncSession) -> bool:
    rows = await session.execute(
        select(SettingsRuntime.value).where(
            SettingsRuntime.key == "onboarding_done")
    )
    if rows.scalar_one_or_none() == "1":
        return True
    any_account = (await session.execute(
        select(Account.id).limit(1)
    )).scalar_one_or_none()
    return any_account is not None


def _catalog_view() -> list:
    """给前端的目录视图 (剥掉价格明细等内部字段; hidden 渠道不出现)"""
    view = []
    for cfg in CHANNELS:
        ob = cfg.get("onboarding", {})
        if ob.get("hidden"):
            continue
        view.append({
            "key": cfg["key"],
            "channel_name": cfg["name"],
            "base_url": cfg["base_url"],
            # 卡片展示列表: models_display (策展的最新模型) 优先, 回落价格行名;
            # 价格行按 (model, 时段) 拆分 — 展示层去重, 保持首次出现顺序
            "models": ob.get("models_display")
                        or list(dict.fromkeys(row[0] for row in cfg["prices"])),
            "category": ob.get("category"),
            "display": ob.get("display"),
            "tagline": ob.get("tagline"),
            "description": ob.get("description"),
            "register_url": ob.get("register_url"),
            "key_console_url": ob.get("key_console_url"),
            "invite": ob.get("invite"),
            "setup_note": ob.get("setup_note"),
        })
    return view


@router.get("/api/onboarding/status")
async def onboarding_status(session: AsyncSession = Depends(get_session_dep())):
    """引导页状态 + 渠道目录 (分区元数据 + PR 引导一并返回)"""
    done = await _setup_done(session)
    return {
        "setup_done": done,
        "categories": CATEGORY_META,
        "catalog": _catalog_view(),
        "pr_guide": PR_GUIDE,
    }


@router.post("/api/onboarding/complete")
async def onboarding_complete(session: AsyncSession = Depends(get_session_dep())):
    """用户主动跳过/完成引导 — 落 settings_runtime 标记, 以后不再自动弹"""
    row = (await session.execute(
        select(SettingsRuntime).where(SettingsRuntime.key == "onboarding_done")
    )).scalars().first()
    if row is not None:
        row.value = "1"
    else:
        session.add(SettingsRuntime(key=ONBOARDING_DONE_KEY, value="1"))
    await session.commit()
    return {"ok": True}


@router.post("/api/onboarding/test")
async def onboarding_test_key(payload: dict):
    """保存前试 key: GET {base_url}/models (8s 超时)

    body: {"key": "sensenova", "api_key": "sk-..."}
    base_url 来自服务端目录常量 (不接受用户 URL); 目录渠道都是公网 https,
    仍做 host 公网校验双保险。
    """
    key = (payload or {}).get("key", "")
    api_key = (payload or {}).get("api_key", "")
    cfg = get_by_key(key)
    if not cfg or cfg.get("onboarding", {}).get("hidden"):
        raise HTTPException(status_code=404, detail=f"未知渠道: {key}")
    if not api_key or not isinstance(api_key, str):
        raise HTTPException(status_code=400, detail="api_key required")

    base_url = cfg["base_url"]
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="渠道 base_url 必须 https")
    host = parsed.hostname or ""
    try:
        resolved = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail=f"渠道域名解析失败: {host}")
    for info in resolved:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise HTTPException(status_code=400, detail="渠道 host 非公网地址")

    url = base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as e:
        return {"ok": False, "status": None, "detail": f"网络错误: {e}"}
    if resp.status_code == 200:
        return {"ok": True, "status": 200, "detail": "key 有效 (GET /models 200)"}
    return {"ok": False, "status": resp.status_code,
            "detail": f"上游返回 {resp.status_code}: {resp.text[:200]}"}


@router.post("/api/onboarding/channels/{key}")
async def onboarding_add_channel(key: str, payload: dict):
    """按目录 key 一键建 渠道 + 账号 + 价格行 + qpm 配额

    body: {"api_key": "sk-...", "name": "可选自定义账号名"}
    语义与 scripts/manual_import.py import_one_channel 一致 (幂等: 同渠道
    同名账号已存在 → 409; 价格行只补缺失, 不重复)。
    """
    from config import settings
    from security.crypto import encrypt

    cfg = get_by_key(key)
    if not cfg or cfg.get("onboarding", {}).get("hidden"):
        raise HTTPException(status_code=404, detail=f"未知渠道: {key}")
    api_key = (payload or {}).get("api_key")
    if not api_key or not isinstance(api_key, str):
        raise HTTPException(status_code=400, detail="api_key required")
    if not settings.aimc_master_key:
        raise HTTPException(
            status_code=500,
            detail="AIMC_MASTER_KEY 未配置, 无法加密 API key "
                   "(便携版首启已自动生成; 部署版见 docs/user/install.md)")
    account_name = (payload or {}).get("name") or f"{cfg['key']}_01"

    async with async_session_factory() as session:
        channel = (await session.execute(
            select(Channel).where(Channel.name == cfg["name"])
        )).scalars().first()
        if channel is None:
            channel = await crud.create_channel(
                session, name=cfg["name"], provider=cfg["provider"],
                base_url=cfg["base_url"], priority=cfg["priority"])
            logger.info(f"[onboarding] 创建渠道 {channel.name} (id={channel.id})")

        dup = (await session.execute(
            select(Account.id).where(
                Account.channel_id == channel.id,
                Account.account_name == account_name)
        )).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(
                status_code=409,
                detail=f"账号已存在: {account_name} — 如需换 key 请用「运维 → 账号管理」的轮换")

        ciphertext = encrypt(api_key, settings.aimc_master_key)
        account = await crud.create_account(
            session, channel_id=channel.id, account_name=account_name,
            api_key="", api_key_ciphertext=ciphertext,
            status="active", quota_total=None, quota_used=0)
        account.last_rotated_at = datetime.now()

        # 价格行只补缺失 (与 manual_import 补齐语义一致, 幂等)
        for row in cfg["prices"]:
            model, price_type, time_range, in_p, out_p, *rest = row
            price_exists = (await session.execute(
                select(PriceSnapshot.id).where(
                    PriceSnapshot.channel_id == channel.id,
                    PriceSnapshot.model == model,
                    PriceSnapshot.price_type == price_type,
                    PriceSnapshot.time_range == time_range)
            )).scalar_one_or_none()
            if price_exists is None:
                session.add(PriceSnapshot(
                    channel_id=channel.id, model=model,
                    input_price=in_p, output_price=out_p,
                    cached_input_price=rest[0] if rest else None,
                    source="manual", price_type=price_type,
                    time_range=time_range))

        await crud.create_quota(
            session, channel_id=channel.id, account_id=account.id,
            quota_type="qpm", limit_value=cfg["qpm"],
            reset_strategy="rolling_window")

        # 模型注册表 + 默认组自动种子 (ensure 语义, 只补缺失不覆盖已有;
        # 全新部署因此获得可用的 tier 组路由, 无需手动跑 seed 脚本)
        seed_stats = await ensure_registry_and_members(session, cfg)

        await session.commit()
        logger.info(f"[onboarding] 渠道 {cfg['name']} 新增账号 {account_name}")

    return {
        "ok": True,
        "channel": cfg["name"],
        "account": account_name,
        "prices": len(cfg["prices"]),
        "seed": {
            "registry_created": seed_stats["registry_created"],
            "members_created": seed_stats["members_created"],
        },
        "next": "无需重启 — 路由缓存约 60s 自动刷新; 可在「渠道」页确认",
    }


@router.post("/api/onboarding/seed-groups")
async def onboarding_seed_groups():
    """「初始化默认模型组」按钮: 按渠道目录全量补种 registry + 默认组成员

    幂等 (只补缺失, 不删除/覆盖任何已有行 — 已策展的生产组不受影响)。
    适用: 渠道已建但模型组为空的存量部署 (如升级前已配渠道的便携版)。
    """
    async with async_session_factory() as session:
        summary = await seed_all_catalog_channels(session)
        await session.commit()
    logger.info(f"[onboarding] seed-groups: registry +{summary['registry_created']}, "
                f"members +{summary['members_created']}")
    return {
        "ok": True,
        "registry_created": summary["registry_created"],
        "members_created": summary["members_created"],
        "channels_touched": summary["channels"],
        "next": "路由缓存约 60s 自动刷新; 已有组成员不会被改动",
    }


# ============================================================
# 模型组手动编辑 (看板 Model Registry + Groups 卡, 2026-10-14)
# 语义: 组创建=INSERT; 成员移出=软删 (is_active=0 + 审计); 成员加回=复活旧行
# (model_group_members 有严格 UNIQUE(group_id, model_id), 软删行占位);
# 变更后前端调 POST /api/cache/refresh 立即生效。
# ============================================================

async def _get_group_or_404(session, group_id: str) -> "ModelGroup":
    group = (await session.execute(
        select(ModelGroup).where(ModelGroup.group_id == group_id)
    )).scalars().first()
    if group is None:
        raise HTTPException(status_code=404, detail=f"组不存在: {group_id}")
    return group


async def _get_registry_model_or_404(session, model_id: str) -> "ModelRegistry":
    row = (await session.execute(
        select(ModelRegistry).where(ModelRegistry.model_id == model_id)
    )).scalars().first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"模型不在注册表: {model_id} (手动加组前需先有 registry 行, "
                   f"注册表来自渠道目录/价格行同步)")
    return row


@router.post("/api/onboarding/groups")
async def create_group(payload: dict):
    """创建模型组 body: {group_id, display_name, tier?, fallback_group?, require_vision?}"""
    from db.models import ModelGroup as MG

    group_id = ((payload or {}).get("group_id") or "").strip()
    if not group_id or not group_id.replace(":", "").replace("-", "").isalnum() \
            or not (group_id.startswith("tier:") or group_id.startswith("scene:")):
        raise HTTPException(status_code=400,
                            detail="group_id 须以 tier:/scene: 开头 (如 tier:code-review)")
    async with async_session_factory() as session:
        dup = (await session.execute(
            select(MG.id).where(MG.group_id == group_id)
        )).scalar_one_or_none()
        if dup is not None:
            raise HTTPException(status_code=409, detail=f"组已存在: {group_id}")
        fb = (payload or {}).get("fallback_group")
        if fb:
            await _get_group_or_404(session, fb)  # fallback 目标必须存在
        group = MG(
            group_id=group_id,
            display_name=(payload or {}).get("display_name") or group_id,
            tier=(payload or {}).get("tier"),   # None=不限 (prod 教训: 精确 tier 过滤易误杀)
            capabilities=None,
            min_context=int((payload or {}).get("min_context") or 0),
            require_vision=1 if (payload or {}).get("require_vision") else 0,
            strategy="cheapest",
            fallback_group=fb,
            sort_order=int((payload or {}).get("sort_order") or 60),
        )
        session.add(group)
        await session.commit()
    logger.info(f"[onboarding] 创建模型组 {group_id}")
    return {"ok": True, "group": group_id}


@router.put("/api/onboarding/groups/{group_id}")
async def update_group(group_id: str, payload: dict):
    """编辑组 (最小集): display_name / fallback_group / enabled / sort_order"""
    async with async_session_factory() as session:
        group = await _get_group_or_404(session, group_id)
        p = payload or {}
        if "display_name" in p:
            group.display_name = p["display_name"]
        if "fallback_group" in p:
            if p["fallback_group"]:
                await _get_group_or_404(session, p["fallback_group"])
                if p["fallback_group"] == group_id:
                    raise HTTPException(status_code=400, detail="fallback 不能指向自己")
            group.fallback_group = p["fallback_group"] or None
        if "enabled" in p:
            group.enabled = 1 if p["enabled"] else 0
        if "sort_order" in p:
            group.sort_order = int(p["sort_order"])
        await session.commit()
    return {"ok": True, "group": group_id}


@router.post("/api/onboarding/groups/{group_id}/members")
async def add_group_member(group_id: str, payload: dict):
    """加成员 body: {model_id, priority?} — 已软删的成员复活 (is_active=1)"""
    from datetime import datetime as dt

    model_id = ((payload or {}).get("model_id") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="model_id 必填")
    priority = (payload or {}).get("priority")

    async with async_session_factory() as session:
        await _get_group_or_404(session, group_id)
        await _get_registry_model_or_404(session, model_id)

        # 任意状态 (active/软删) 查旧行 — 软删行复活, active 行 409
        member = (await session.execute(
            select(ModelGroupMember).where(
                ModelGroupMember.group_id == group_id,
                ModelGroupMember.model_id == model_id)
        )).scalars().first()
        if member is not None and member.is_active == 1:
            raise HTTPException(status_code=409, detail="成员已存在")
        if member is not None:  # 复活
            member.is_active = 1
            member.removed_at = None
            member.removed_reason = None
            member.removed_by = None
            member.added_by = "manual"
            if priority is not None:
                member.priority = int(priority)
        else:
            if priority is None:
                pris = (await session.execute(
                    select(ModelGroupMember.priority).where(
                        ModelGroupMember.group_id == group_id,
                        ModelGroupMember.is_active == 1)
                )).scalars().all()
                priority = (max(pris) + 5) if pris else 100
            session.add(ModelGroupMember(
                group_id=group_id, model_id=model_id,
                priority=int(priority), weight=1.0, added_by="manual",
            ))
        await session.commit()
    logger.info(f"[onboarding] 组 {group_id} 加成员 {model_id}")
    return {"ok": True, "group": group_id, "model": model_id,
            "next": "前端将自动刷新路由缓存"}


@router.delete("/api/onboarding/groups/{group_id}/members/{model_id}")
async def remove_group_member(group_id: str, model_id: str):
    """移出成员 (软删: is_active=0 + 审计字段, 与 P1-2 语义一致)"""
    async with async_session_factory() as session:
        await _get_group_or_404(session, group_id)
        member = (await session.execute(
            select(ModelGroupMember).where(
                ModelGroupMember.group_id == group_id,
                ModelGroupMember.model_id == model_id,
                ModelGroupMember.is_active == 1)
        )).scalars().first()
        if member is None:
            raise HTTPException(status_code=404, detail="成员不存在或已出组")
        member.is_active = 0
        member.removed_at = datetime.now()
        member.removed_reason = "manual"
        member.removed_by = "dashboard"
        await session.commit()
    logger.info(f"[onboarding] 组 {group_id} 移出成员 {model_id} (软删)")
    return {"ok": True, "group": group_id, "model": model_id}
