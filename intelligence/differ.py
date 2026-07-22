"""HTML/文本 Diff 检测

通过内容哈希和相似度比较判断页面是否发生变化。
变化超过阈值时触发 AI 解析。
"""

import hashlib
import logging
from difflib import SequenceMatcher
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)


def compute_hash(content: str) -> str:
    """计算内容 SHA256 哈希"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_similarity(old_content: str, new_content: str) -> float:
    """计算两段文本的相似度（0-1）

    使用 SequenceMatcher，对长文本截断比较以提升性能。
    """
    # 截断到前 5000 字符比较（避免超长页面性能问题）
    max_len = 5000
    old_truncated = old_content[:max_len]
    new_truncated = new_content[:max_len]

    return SequenceMatcher(None, old_truncated, new_truncated).ratio()


async def has_changed(source, new_content: str) -> bool:
    """判断采集源内容是否发生变化

    Args:
        source: 采集源对象（含 last_hash 属性）
        new_content: 新抓取的页面内容

    Returns:
        True 表示有变化，需要触发解析
    """
    new_hash = compute_hash(new_content)

    # 首次采集（无历史哈希）
    if not source.last_hash:
        await _update_source_hash(source, new_hash)
        return True

    # 哈希完全相同 -> 无变化
    if source.last_hash == new_hash:
        return False

    # 哈希不同，进一步比较相似度
    # 注意：这里简化处理，直接用哈希判断
    # 如果需要更精细的相似度比较，可以存储上次内容
    await _update_source_hash(source, new_hash)
    return True


async def _update_source_hash(source, new_hash: str):
    """更新采集源的内容哈希和检查时间"""
    try:
        from db.engine import async_session_factory
        from db import crud

        async with async_session_factory() as session:
            await crud.update_intel_source(
                session,
                source.source_id,
                last_hash=new_hash,
                last_check_at=datetime.now(),
                error_count=0,
            )
            await session.commit()

        source.last_hash = new_hash
    except Exception as e:
        logger.warning(f"更新采集源哈希失败: {e}")
