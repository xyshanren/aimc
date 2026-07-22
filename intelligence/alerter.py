"""Telegram Bot 告警推送

免费、稳定、API 友好，适合个人自用告警。
"""

import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)


async def send_alert(message: str):
    """发送 Telegram 告警消息

    如果未配置 Telegram，则仅记录日志。

    Args:
        message: 告警消息内容
    """
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning(f"[告警-未推送] {message}")
        return

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": f"[AIMC] {message}",
                    "parse_mode": "HTML",
                },
            )
            if response.status_code == 200:
                logger.info("Telegram 告警已发送")
            else:
                logger.warning(f"Telegram 发送失败: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Telegram 告警异常: {e}")
