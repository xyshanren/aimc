"""多 channel 告警推送

支持 channel（通过 ALERT_CHANNEL env 选）：
- log         只 log（默认，dry-run）
- bark        iOS Bark push（国内首选，30秒配）——  https://api.day.app
- serverchan  Server 酱（推送到微信）—— https://sctapi.ftqq.com
- feishu      飞书自定义机器人 webhook（国内办公首选）—— https://open.feishu.cn

不在支持的 channel（已移除）：
- ~~telegram~~    需要翻墙 + 注册，国内用户不实用
- ~~discord~~     国内不稳定
- ~~slack~~       国内不稳定

借鉴：hermes-tray alerter 设计 — channel 路由 + 单一 send_alert 入口

Phase 3 (#3): throttle 机制
- 同 key 60s 内只发 1 次，防飞书刷屏
- log 模式不 throttle（用户主动要 log 噪音）
- 锁内一次性 check+mark, 防并发 race (3 个 401 同时来不会发 3 次)
- 失败也占用槽位 60s, 防止飞书 webhook 暂时 503 时反复重试刷屏
"""
import logging
import time
import asyncio
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


# Phase 3 (#3): throttle 状态
# key (e.g. "siliconflow_aggregator:401") -> 最后一次发送成功的 monotonic 时间
_throttle_state: dict[str, float] = {}
_throttle_lock = asyncio.Lock()
THROTTLE_SECONDS = 60.0


async def _try_acquire_throttle(key: str) -> bool:
    """尝试占用 throttle 槽位 (锁内检查+标记, 防并发 race)

    Returns:
        True  = 已成功标记 (可继续发)
        False = 在窗口内, 已被占用 (跳过)
    """
    async with _throttle_lock:
        last = _throttle_state.get(key)
        if last is not None and (time.monotonic() - last) < THROTTLE_SECONDS:
            return False
        _throttle_state[key] = time.monotonic()
        return True


def reset_throttle() -> None:
    """清空 throttle 状态 (测试用)"""
    _throttle_state.clear()


def _short(msg: str, limit: int = 200) -> str:
    """告警消息截断（避免 Bark/Telegram 长度超限）"""
    if len(msg) > limit:
        return msg[:limit - 3] + "..."
    return msg


async def _send_bark(message: str) -> bool:
    """Bark push（iOS 专属，国内首选）

    配置：BARK_KEY=<你的 device_key>
    可选：BARK_SERVER=https://api.day.app (默认) 或自建 server URL
    """
    if not settings.bark_key:
        logger.warning(f"[ALERT-bark-no-config] {message}")
        return False

    server = settings.bark_server.rstrip("/") or "https://api.day.app"
    # Bark URL: {server}/{key}/{title}/{body}
    url = f"{server}/{settings.bark_key}/AIMC 告警/{_short(message, 100)}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                logger.info("Bark 告警已发送")
                return True
            logger.warning(f"Bark 发送失败: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Bark 告警异常: {e}")
    return False


async def _send_serverchan(message: str) -> bool:
    """Server 酱 push（推送到微信）"""
    if not settings.serverchan_key:
        logger.warning(f"[ALERT-serverchan-no-config] {message}")
        return False

    url = f"https://sctapi.ftqq.com/{settings.serverchan_key}.send"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                data={"title": "AIMC 告警", "desp": _short(message, 500)},
            )
            if resp.status_code == 200:
                logger.info("Server 酱告警已发送")
                return True
            logger.warning(f"Server 酱发送失败: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Server 酱告警异常: {e}")
    return False


async def _send_feishu(message: str) -> bool:
    """飞书自定义机器人 webhook

    配置：在飞书群里添加"自定义机器人" → 拿到 webhook URL → 填到 FEISHU_WEBHOOK_URL

    国内办公首选，签到/工作流通知都用这个
    """
    if not settings.feishu_webhook_url:
        logger.warning(f"[ALERT-feishu-no-config] {message}")
        return False

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                settings.feishu_webhook_url,
                json={
                    "msg_type": "text",
                    "content": {"text": f"[AIMC] {_short(message, 500)}"},
                },
            )
            # 飞书返回 {"StatusCode": 0, "msg": "success"} 或 {"StatusCode": <非零>, "msg": "..."}
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            if resp.status_code == 200 and data.get("StatusCode") in (0, "0"):
                logger.info("飞书告警已发送")
                return True
            logger.warning(f"飞书发送失败: status={resp.status_code} body={resp.text[:200]}")
    except Exception as e:
        logger.error(f"飞书告警异常: {e}")
    return False


# 通道路由表（用户加新 channel 就在这里注册）
CHANNEL_HANDLERS = {
    "bark": _send_bark,
    "serverchan": _send_serverchan,
    "feishu": _send_feishu,
}


async def send_alert(message: str, key: Optional[str] = None) -> bool:
    """统一告警入口

    根据 settings.alert_channel 路由：
    - "log"        → 只 log（默认，dry-run）
    - 其他 channel  → 调对应 handler，失败时 fallback 到 log

    Phase 3 (#3): throttle
    - key 不为 None 且 channel 不是 log → 60s 内同 key 只发 1 次
    - throttle 命中时返回 True (行为对齐"已发过")
    - 锁内 check+mark 一次性原子完成, 防并发 race

    Args:
        message: 告警文本
        key: throttle key (e.g. "channel_name:status_code")，None = 不 throttle

    Returns:
        True = 成功推送 / 被 throttle / log-only；False = 真正发送失败
    """
    channel = settings.alert_channel.lower().strip() or "log"

    # Throttle: 仅非 log channel + key 非 None 时生效
    # 用 lock 包住检查+标记, 防并发 race (3 个 401 同时来不会发 3 次)
    if channel != "log" and key is not None:
        if not await _try_acquire_throttle(key):
            logger.debug(f"[ALERT throttled] key={key}, skip send")
            return True

    if channel == "log":
        logger.warning(f"[ALERT] {message}")
        return True  # log 永远算"成功"（用户看到 log 就行）

    handler = CHANNEL_HANDLERS.get(channel)
    if not handler:
        # 未知 channel (e.g. 用户配了 telegram 但 alerter 已移除) → fallback log
        # 跟"显式配 log"等价：用户至少能在 log 里看到告警
        logger.warning(
            f"[ALERT-fallback] unknown channel={channel!r} "
            f"(available: log/{','.join(CHANNEL_HANDLERS.keys())}), msg: {message}"
        )
        return True  # fallback log 算成功（跟"显式 log"行为对齐）

    success = await handler(message)
    # throttle 已在 _try_acquire_throttle 里 mark, 失败也占用槽位 60s
    return success
