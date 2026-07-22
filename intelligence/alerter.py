"""多 channel 告警推送

支持 channel（通过 ALERT_CHANNEL env 选）：
- log         只 log（默认，dry-run）
- bark        iOS Bark push（国内首选，30秒配）——  https://api.day.app
- telegram    Telegram bot（需要翻墙 + 注册）
- serverchan  Server 酱（推送到微信）—— https://sctapi.ftqq.com
- wechat_work 企业微信 webhook
- feishu      飞书 webhook
- dingtalk    钉钉 webhook

借鉴：hermes-tray alerter 设计 — channel 路由 + 单一 send_alert 入口
"""

import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


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


async def _send_telegram(message: str) -> bool:
    """Telegram bot push（需要翻墙）"""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning(f"[ALERT-telegram-no-config] {message}")
        return False

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": f"[AIMC] {_short(message, 500)}",
                    "parse_mode": "HTML",
                },
            )
            if resp.status_code == 200:
                logger.info("Telegram 告警已发送")
                return True
            logger.warning(f"Telegram 发送失败: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Telegram 告警异常: {e}")
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


# 通道路由表（用户加新 channel 就在这里注册）
CHANNEL_HANDLERS = {
    "bark": _send_bark,
    "telegram": _send_telegram,
    "serverchan": _send_serverchan,
    # wechat_work / feishu / dingtalk — 用户可按需加
}


async def send_alert(message: str) -> bool:
    """统一告警入口

    根据 settings.alert_channel 路由：
    - "log"        → 只 log（默认，dry-run）
    - 其他 channel  → 调对应 handler，失败时 fallback 到 log

    Args:
        message: 告警文本

    Returns:
        True = 成功推送；False = 只 log（channel 未配或发送失败）
    """
    channel = settings.alert_channel.lower().strip() or "log"

    if channel == "log":
        logger.warning(f"[ALERT] {message}")
        return True  # log 永远算"成功"（用户看到 log 就行）

    handler = CHANNEL_HANDLERS.get(channel)
    if not handler:
        logger.warning(
            f"[ALERT-fallback] unknown channel={channel!r} "
            f"(available: log/{','.join(CHANNEL_HANDLERS.keys())}), msg: {message}"
        )
        return False

    return await handler(message)
