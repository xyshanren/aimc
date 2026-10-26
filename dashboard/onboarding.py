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
from dashboard.group_members import resolve_add_priority

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
            "invite_code": ob.get("invite_code"),
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


@router.get("/api/pricing/unpriced")
async def pricing_unpriced(session: AsyncSession = Depends(get_session_dep())):
    """待补价盘点 (渠道页「待补价」卡, 2026-10-15)

    model_list 源自动同步进来的新模型价格行是 NULL — 本端点把它们盘出来:
    - chat_pending: 疑似聊天模型待补价 (按命名关键词排除嵌入/语音/生图等
      非聊天类), 人工到官方定价页核对后 UPDATE price_snapshots
    - non_chat: 非聊天类 (语音/生图/OCR 等, 无按 token 计价语义, 通常无需补)
    """
    rows = (await session.execute(
        select(Channel.name, PriceSnapshot.model)
        .join(PriceSnapshot, PriceSnapshot.channel_id == Channel.id)
        .where(PriceSnapshot.input_price.is_(None))
    )).all()

    # 非聊天模型关键词 —— 与 gateway/group_rules.NON_CHAT_KW 同源 (V6.17d)
    from gateway.group_rules import is_chat_model

    def _is_chat(model: str) -> bool:
        return is_chat_model(model)

    chat_pending, non_chat = [], []
    for chname, model in rows:
        entry = {"model": model, "channel": chname}
        (chat_pending if _is_chat(model) else non_chat).append(entry)
    return {
        "total": len(rows),
        "chat_pending": chat_pending,
        "non_chat_count": len(non_chat),
        "hint": "去对应渠道官方定价页核对后, 用 SQL UPDATE 补价 (价格表卡为只读, 无编辑入口)",
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
    """加成员 body: {model_id, priority?} — 已软删的成员复活 (is_active=1)

    priority 语义 (V6.17d, 见 dashboard/group_members.py):
    - 显式 priority → 用它 (UI 可填);
    - 同一 model_id 的软删行 → 复活并**保留其原 priority** (当初的策展排位);
    - 同族旧行 (改名/预览后缀不同, 如 minimax-m3.1-flash ← -preview) → 继承其 priority;
    - 否则 → 组内现有最大值 +5 (追加队尾, 不抢占现有成员位次)。
    已是活跃成员且带了 priority → 直接改 priority (幂等更新, 不再 409)。
    """
    from datetime import datetime as dt

    model_id = ((payload or {}).get("model_id") or "").strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="model_id 必填")
    priority = (payload or {}).get("priority")

    async with async_session_factory() as session:
        await _get_group_or_404(session, group_id)
        await _get_registry_model_or_404(session, model_id)

        # 任意状态 (active/软删) 查旧行 — 软删行复活, active 行改 priority
        member = (await session.execute(
            select(ModelGroupMember).where(
                ModelGroupMember.group_id == group_id,
                ModelGroupMember.model_id == model_id)
        )).scalars().first()
        if member is not None and member.is_active == 1:
            if priority is None:
                raise HTTPException(status_code=409, detail="成员已存在")
            # 已是成员 + 显式 priority → 幂等改位次 (看板"改 priority"同一入口)
            member.priority = int(priority)
            await session.commit()
            logger.info(f"[onboarding] 组 {group_id} 成员 {model_id} 改 priority={priority}")
            return {"ok": True, "group": group_id, "model": model_id,
                    "priority": member.priority, "updated": True,
                    "next": "前端将自动刷新路由缓存"}
        if member is not None:  # 复活: 保留原 priority (除非显式指定)
            member.is_active = 1
            member.removed_at = None
            member.removed_reason = None
            member.removed_by = None
            member.added_by = "manual"
            if priority is not None:
                member.priority = int(priority)
        else:
            priority = await resolve_add_priority(session, group_id, model_id, priority)
            session.add(ModelGroupMember(
                group_id=group_id, model_id=model_id,
                priority=int(priority), weight=1.0, added_by="manual",
            ))
        await session.commit()
    logger.info(f"[onboarding] 组 {group_id} 加成员 {model_id}")
    return {"ok": True, "group": group_id, "model": model_id,
            "next": "前端将自动刷新路由缓存"}


@router.patch("/api/onboarding/groups/{group_id}/members/{model_id}")
async def update_group_member_priority(group_id: str, model_id: str, payload: dict):
    """改成员位次 body: {priority} — 只改 priority, 不动其它字段"""
    priority = (payload or {}).get("priority")
    if priority is None:
        raise HTTPException(status_code=400, detail="priority 必填")
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="priority 须为整数")

    async with async_session_factory() as session:
        await _get_group_or_404(session, group_id)
        member = (await session.execute(
            select(ModelGroupMember).where(
                ModelGroupMember.group_id == group_id,
                ModelGroupMember.model_id == model_id)
        )).scalars().first()
        if member is None or member.is_active != 1:
            raise HTTPException(status_code=404, detail="成员不存在或已出组")
        old = member.priority
        member.priority = priority
        await session.commit()
    logger.info(f"[onboarding] 组 {group_id} 成员 {model_id} priority {old} → {priority}")
    return {"ok": True, "group": group_id, "model": model_id, "priority": priority,
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


# ============================================================
# 价格表手动编辑 (渠道页 价格表卡, 2026-10-25)
# 语义: 补价/改时段/退役清理此前只能生产库 SQL (价格表卡只读)。
# 护栏:
# - price_type 枚举 + time_range 格式校验 + 价格 >=0 (null=未定价/不支持)
# - 全天行与时段行并存 409: 取价按 prices 列表 id 序「首个时段命中」,
#   normal+全天 行恒命中 → 时段行 (峰谷) 永远选不中 (2026-10-24 补分时段
#   价踩过: 必须把 NULL 全天行就地转成时段行)。拒绝的是「保留全天且定价」
#   同时存在时段行; 全天行改成时段行 / 删时段行后再定价都放行。
# - 四元组 (channel,model,price_type,time_range) 唯一冲突 → 409
# - 严格 raw id 渠道 (siliconflow) 的规范名行必须带 raw_model_id: 路由取价
#   精确命中后 _channel_model = raw_model_id or model, 缺失时与请求同名 →
#   body 改写被跳过 → 规范名裸发上游 400 "Model does not exist" (20012)。
#   2026-10-26 生产实证: 10-14 手工补的 glm-5.3-flash 规范名行压住 raw 行,
#   该模型在此渠道 20 天零成功全 400。同渠道已存在 raw 兄弟行 → 服务端自动
#   回填; 没有 → 422 (先让每日同步/手工把 raw id 行建起来再补规范名行)。
# 写后 route_engine.invalidate_price(channel_id) 精准刷新, 免重启。
# ============================================================

_PRICE_TYPES = {"normal", "peak", "offpeak", "cache_hit", "free"}
_PRICE_NUM_FIELDS = ("input_price", "output_price", "cached_input_price", "cache_write_price")

# 上游严格按 raw id (org/name) 寻址、不认规范名的渠道。按需扩充 (须有
# "规范名裸发上游即 400" 的生产证据, modelscope/minimax 实测接受规范名)。
_RAW_ID_STRICT_PROVIDERS = frozenset({"siliconflow"})


def _valid_time_range(tr: str) -> bool:
    """与 gateway/router._match_time_range 支持集对齐"""
    if tr in ("全天", "非高峰全天"):
        return True
    for prefix in ("周末", "工作日"):
        if tr.startswith(prefix):
            rest = tr[len(prefix):]
            return rest in ("", "全天") or _valid_hhmm(rest)
    return _valid_hhmm(tr)


def _valid_hhmm(tr: str) -> bool:
    parts = tr.split("-")
    if len(parts) != 2:
        return False
    try:
        h1, h2 = int(parts[0].split(":")[0]), int(parts[1].split(":")[0])
    except (ValueError, IndexError):
        return False
    return 0 <= h1 < 24 and 0 < h2 <= 24 and h1 < h2


def _parse_price_fields(p: dict, out: dict) -> None:
    """价格字段: 接受 number/null/空串(=null)/缺省(不动); 拒绝负数与非法数字"""
    for f in _PRICE_NUM_FIELDS:
        if f not in p:
            continue
        v = p[f]
        if v is None or v == "":
            out[f] = None
        else:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                raise HTTPException(400, detail=f"{f} 须为数字或留空: {v!r}")
            if fv < 0:
                raise HTTPException(400, detail=f"{f} 不能为负数")
            out[f] = fv


def _parse_type_fields(p: dict, out: dict, *, partial: bool) -> None:
    if not partial or "price_type" in p:
        pt = p.get("price_type", "normal")
        if pt not in _PRICE_TYPES:
            raise HTTPException(400, detail=f"price_type 须为 {'/'.join(sorted(_PRICE_TYPES))}")
        out["price_type"] = pt
    if not partial or "time_range" in p:
        tr = p.get("time_range", "全天")
        if not _valid_time_range(tr):
            raise HTTPException(400, detail="time_range 格式不支持 (全天/非高峰全天/工作日HH:MM-HH:MM/周末…/HH:MM-HH:MM)")
        out["time_range"] = tr


async def _guard_all_day_conflict(session, row: PriceSnapshot) -> None:
    """normal+全天 且任一价格非空 时, 同 (channel,model) 不允许存在时段行"""
    if row.price_type != "normal" or row.time_range != "全天":
        return
    if all(getattr(row, f) is None for f in _PRICE_NUM_FIELDS):
        return  # 未定价的 NULL 全天行 = 待补价占位, 与时段行无冲突
    sibling = (await session.execute(
        select(PriceSnapshot.id).where(
            PriceSnapshot.channel_id == row.channel_id,
            PriceSnapshot.model == row.model,
            PriceSnapshot.id != row.id,
            PriceSnapshot.time_range != "全天")
        .limit(1)
    )).scalar_one_or_none()
    if sibling is not None:
        raise HTTPException(
            409,
            detail="全天 normal 行与时段行并存时, 取价按 id 序恒命中全天行, "
                   "峰/谷时段行永远不会被选中 — 请把本行 time_range 改成时段 "
                   "(如 00:00-02:00) 或先删时段行",
        )


async def _guard_raw_model_id(session, row: PriceSnapshot, provider: str) -> None:
    """严格 raw id 渠道: 规范名行缺 raw_model_id 时自动回填, 无兄弟行则拒绝"""
    if provider not in _RAW_ID_STRICT_PROVIDERS:
        return
    if "/" in row.model:
        return  # 本身就是 raw id 行
    if (row.raw_model_id or "").strip():
        return
    model_lower = row.model.lower()
    candidates = (await session.execute(
        select(PriceSnapshot.raw_model_id).where(
            PriceSnapshot.channel_id == row.channel_id,
            PriceSnapshot.raw_model_id.isnot(None),
            PriceSnapshot.id != row.id,
        ).order_by(PriceSnapshot.id)
    )).scalars().all()
    for raw in candidates:
        raw_lower = raw.lower()
        if raw_lower == model_lower or raw_lower.endswith("/" + model_lower):
            row.raw_model_id = raw
            return
    raise HTTPException(
        422,
        detail=f"{provider} 渠道上游只认 raw id (org/name): 规范名行 {row.model} "
               f"缺 raw_model_id 时路由会把规范名裸发上游 (400 Model does not "
               f"exist)。请请求带 raw_model_id, 或先确保同渠道已建对应 raw id 行 "
               f"(有则自动回填)",
    )


async def _invalidate_route_price(channel_id: int) -> None:
    try:
        from gateway.router import route_engine
        await route_engine.invalidate_price(channel_id)
    except Exception as e:  # 刷新失败不影响落库 (cache ~60s 也会自动过期)
        logger.warning(f"[prices-admin] invalidate_price({channel_id}) 失败: {e}")


@router.put("/api/prices/{row_id}")
async def update_price_row(row_id: int, payload: dict):
    """编辑价格行 body: 价格字段/null 清空 + price_type/time_range; source 置 manual:dashboard"""
    p = payload or {}
    fields: dict = {}
    if "model" in p:
        m = (p.get("model") or "").strip()
        if not m:
            raise HTTPException(400, detail="model 不能为空")
        fields["model"] = m
    if "raw_model_id" in p:
        fields["raw_model_id"] = (p.get("raw_model_id") or "").strip() or None
    _parse_price_fields(p, fields)
    _parse_type_fields(p, fields, partial=True)
    if not fields:
        raise HTTPException(400, detail="没有可更新字段")
    async with async_session_factory() as session:
        row = await session.get(PriceSnapshot, row_id)
        if row is None:
            raise HTTPException(404, detail=f"价格行不存在: id={row_id}")
        ch = await session.get(Channel, row.channel_id)
        old = {f: getattr(row, f) for f in fields}
        for f, v in fields.items():
            setattr(row, f, v)
        row.source = "manual:dashboard"
        await _guard_raw_model_id(session, row, ch.provider if ch else "")
        await _guard_all_day_conflict(session, row)
        try:
            await session.commit()
        except Exception as e:
            await session.rollback()
            raise HTTPException(409, detail=f"保存失败 (唯一约束冲突?): {e}")
        await _invalidate_route_price(row.channel_id)
    logger.info(f"[prices-admin] 编辑价行 {row_id}: {old} -> {fields}")
    return {"ok": True, "id": row_id, "updated": fields}


@router.post("/api/prices")
async def create_price_row(payload: dict):
    """新增价格行 body: {channel_id, model, price_type?, time_range?, 价格字段?}"""
    p = payload or {}
    fields: dict = {}
    _parse_price_fields(p, fields)
    _parse_type_fields(p, fields, partial=False)
    channel_id = p.get("channel_id")
    model = (p.get("model") or "").strip()
    if not isinstance(channel_id, int) or channel_id <= 0:
        raise HTTPException(400, detail="channel_id 须为正整数")
    if not model:
        raise HTTPException(400, detail="model 不能为空")
    async with async_session_factory() as session:
        ch = await session.get(Channel, channel_id)
        if ch is None:
            raise HTTPException(404, detail=f"渠道不存在: {channel_id}")
        row = PriceSnapshot(
            channel_id=channel_id, model=model,
            raw_model_id=(p.get("raw_model_id") or "").strip() or None,
            price_type=fields.get("price_type", "normal"),
            time_range=fields.get("time_range", "全天"),
            source="manual:dashboard",
            **{f: fields.get(f) for f in _PRICE_NUM_FIELDS},
        )
        await _guard_raw_model_id(session, row, ch.provider)
        await _guard_all_day_conflict(session, row)
        session.add(row)
        try:
            await session.commit()
        except Exception as e:
            await session.rollback()
            raise HTTPException(409, detail=f"新增失败 (四元组 (渠道,模型,类型,时段) 已存在?): {e}")
        await _invalidate_route_price(channel_id)
    logger.info(f"[prices-admin] 新增价行: {ch.name}/{model} {row.price_type} {row.time_range} (id={row.id})")
    return {"ok": True, "id": row.id, "model": model, "channel_id": channel_id}


@router.delete("/api/prices/{row_id}")
async def delete_price_row(row_id: int):
    """删除价格行 (退役模型清理 / 误建行)"""
    async with async_session_factory() as session:
        row = await session.get(PriceSnapshot, row_id)
        if row is None:
            raise HTTPException(404, detail=f"价格行不存在: id={row_id}")
        channel_id, model = row.channel_id, row.model
        await session.delete(row)
        await session.commit()
        await _invalidate_route_price(channel_id)
    logger.info(f"[prices-admin] 删除价行 {row_id} (channel={channel_id} model={model})")
    return {"ok": True, "id": row_id, "model": model}
