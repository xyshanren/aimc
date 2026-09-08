"""HTML/文本 Diff 检测

通过内容哈希和相似度比较判断页面是否发生变化。
变化超过阈值时触发 AI 解析。

改进历史：
- 2026-07-22: 实际用上相似度 (Phase 2A-2, 借鉴 crawl4ai)
  - 加 last_content 字段，存上次截断内容
  - 哈希不同但相似度 ≥ 阈值 → 不解析（避免白花 token）
"""

import hashlib
import logging
from difflib import SequenceMatcher
from datetime import datetime
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# 相似度比较时截断长度（前 N 字符），避免超长页面性能问题
_SIMILARITY_MAX_LEN = 5000
# 存储 last_content 时截断长度（避免单行存 1MB 进 SQLite）
_LAST_CONTENT_MAX_LEN = 51200  # 50KB


def compute_hash(content: str) -> str:
    """计算内容 SHA256 哈希"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_similarity(old_content: str, new_content: str) -> float:
    """计算两段文本的相似度（0-1）

    使用 SequenceMatcher，对长文本截断比较以提升性能。
    """
    old_truncated = old_content[:_SIMILARITY_MAX_LEN]
    new_truncated = new_content[:_SIMILARITY_MAX_LEN]
    return SequenceMatcher(None, old_truncated, new_truncated).ratio()


async def has_changed(source, new_content: str) -> bool:
    """判断采集源内容是否发生变化

    三级检测：
    1. 首次采集（无历史哈希）→ True
    2. 哈希完全相同 → False
    3. 哈希不同但有 last_content → 算相似度
       - 相似度 ≥ 阈值（settings.diff_similarity_threshold）→ False（小幅修改，忽略）
       - 相似度 < 阈值 → True
    4. 哈希不同但没有 last_content（旧数据迁移场景）→ True（保守：宁可错杀）

    V4.8.2: 从 DB 读 last_hash/last_content, 不依赖 in-memory source 对象。
    原因: APScheduler 用 SQLAlchemyJobStore (pickle) 持久化 job args,
    每次 tick 反序列化出全新的 source 副本 → in-memory last_hash 永远为 None →
    每次采集都判定 has_changed=True → 自愈+告警刷屏。

    Returns:
        True 表示有变化，需要触发 AI 解析
    """
    new_hash = compute_hash(new_content)

    # V4.8.2: 从 DB 读权威 last_hash/last_content, 覆盖 in-memory 默认值
    db_hash, db_content = await _load_hash_from_db(source)
    if db_hash:
        source.last_hash = db_hash
        source.last_content = db_content

    # 1. 首次采集
    if not source.last_hash:
        await _update_source_hash(source, new_hash, new_content)
        return True

    # 2. 哈希完全相同
    if source.last_hash == new_hash:
        await _increment_no_change(source)
        return False

    # 3. 哈希不同 → 算相似度（如果有 last_content）
    if source.last_content:
        similarity = compute_similarity(source.last_content, new_content)
        threshold = settings.diff_similarity_threshold
        if similarity >= threshold:
            # 相似度足够高，只是小幅修改（CSS/广告/时间戳），不触发 AI 解析
            logger.debug(
                f"hash 不同但相似度 {similarity:.3f} ≥ {threshold}，跳过 AI 解析: "
                f"{getattr(source, 'provider', '?')}"
            )
            # 仍要更新 last_content（保持最新），但跳过 AI 解析
            await _update_source_hash(source, new_hash, new_content)
            await _increment_no_change(source)
            return False
        else:
            logger.info(
                f"hash 不同且相似度 {similarity:.3f} < {threshold}，触发 AI 解析: "
                f"{getattr(source, 'provider', '?')}"
            )
    # else: hash 不同 + 没 last_content → 保守返回 True（首次迁移场景）

    await _update_source_hash(source, new_hash, new_content)
    return True


async def _update_source_hash(source, new_hash: str, new_content: Optional[str] = None):
    """更新采集源的哈希、last_content 和检查时间

    同步：
    1. 内存 dataclass（source.last_hash / last_content）
    2. 数据库 intel_sources 表（如果 source_id 有效）
    """
    # 1. 内存更新
    source.last_hash = new_hash
    if new_content is not None:
        source.last_content = new_content[:_LAST_CONTENT_MAX_LEN]

    # 2. DB 更新（best-effort，失败不影响主流程）
    # Phase 2C-2: 用 (provider, url) 替代 source_id（dataclass.source_id=0 不可靠）
    try:
        from db.engine import async_session_factory
        from db import crud

        async with async_session_factory() as session:
            update_kwargs = {
                "last_hash": new_hash,
                "last_check_at": datetime.now(),
                "error_count": 0,
                "consecutive_no_change": 0,  # V4.8.3: 有变化时归零
                # A-2 (2026-09-08): 记录变化检测时间, 供系统级盲区审计判"失明"
                # 本函数覆盖所有"看到过新内容"的路径 (首次/真变化/相似度噪声跳过),
                # 只有 hash 完全相同的无变化路径不经过这里
                "last_changed_at": datetime.now(),
            }
            if new_content is not None:
                update_kwargs["last_content"] = new_content[:_LAST_CONTENT_MAX_LEN]

            db_source = await crud.get_intel_source_by_provider_url(
                session, source.provider, source.url
            )
            if db_source:
                for key, value in update_kwargs.items():
                    setattr(db_source, key, value)
                # 成功采集时恢复被 handle_fetch_error 禁用的 source (status error→active)
                # V4.8.1: 恢复时发一条恢复告警 (用独立 key 避免跟失败告警互相 throttle)
                if db_source.status == "error":
                    db_source.status = "active"
                    logger.info(
                        f"采集源恢复: {db_source.provider} (status error→active)"
                    )
                    from intelligence.alerter import send_alert
                    await send_alert(
                        f"[采集恢复] {db_source.provider} 已恢复采集\n"
                        f"URL: {db_source.url}\n"
                        f"此前连续失败已自动恢复。",
                        key=f"fetch_recover:{db_source.provider}:{db_source.url}",
                        silence_seconds=3600,
                    )
                await session.commit()
            else:
                # DB 没对应行（dataclass 跟 DB 没关联）→ 自动 upsert 一条
                # 修复 V4.8.2: 不能传 status="active" — upsert_intel_source 内部
                # 已 hardcode status="active", 重复传会触发 "got multiple values
                # for keyword argument 'status'" → DB hash 永远写不进去 →
                # 每次采集都判定 has_changed=True → 自愈+告警刷屏
                await crud.upsert_intel_source(
                    session,
                    provider=source.provider,
                    url=source.url,
                    source_type=getattr(source, "source_type", "web_page"),
                    poll_interval=getattr(source, "poll_interval", 900),
                    **update_kwargs,
                )
                await session.commit()
    except Exception as e:
        logger.warning(f"更新采集源哈希失败: {e}")


async def _load_hash_from_db(source) -> tuple[Optional[str], Optional[str]]:
    """从 DB 读 last_hash / last_content

    V4.8.2: APScheduler SQLAlchemyJobStore 每次反序列化 job args 时
    创建全新 source 副本, in-memory last_hash 永远为 None。
    本函数在 has_changed 入口处从 DB 加载权威值, 覆盖内存默认值。

    Returns:
        (last_hash, last_content) — DB 无行时返回 (None, None)
    """
    try:
        from db.engine import async_session_factory
        from db import crud

        async with async_session_factory() as session:
            db_source = await crud.get_intel_source_by_provider_url(
                session, source.provider, source.url
            )
            if db_source:
                return db_source.last_hash, db_source.last_content
    except Exception as e:
        logger.debug(f"从 DB 加载 last_hash 失败 (非阻断): {e}")
    return None, None


async def _increment_no_change(source):
    """递增连续无变化计数 (V4.8.3 自适应降频)

    无变化时调用: DB consecutive_no_change += 1。
    有变化时由 _update_source_hash 归零。
    scheduler._run_source_task 根据此值决定是否跳过本次采集。
    """
    try:
        from db.engine import async_session_factory
        from db import crud

        async with async_session_factory() as session:
            db_source = await crud.get_intel_source_by_provider_url(
                session, source.provider, source.url
            )
            if db_source:
                db_source.consecutive_no_change = (db_source.consecutive_no_change or 0) + 1
                db_source.last_check_at = datetime.now()
                await session.commit()
                logger.debug(
                    f"无变化计数: {source.provider} → {db_source.consecutive_no_change}"
                )
    except Exception as e:
        logger.debug(f"递增 consecutive_no_change 失败 (非阻断): {e}")


async def load_no_change_count(source) -> int:
    """从 DB 读 consecutive_no_change (供 scheduler 自适应降频用)

    Returns:
        连续无变化次数, DB 无行时返回 0
    """
    try:
        from db.engine import async_session_factory
        from db import crud

        async with async_session_factory() as session:
            db_source = await crud.get_intel_source_by_provider_url(
                session, source.provider, source.url
            )
            if db_source:
                return db_source.consecutive_no_change or 0
    except Exception:
        pass
    return 0
