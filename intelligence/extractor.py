"""CSS selector 提取器

Phase 2B-2: AI 解析失败时的降级方案。
用 ParseRule 表里存的 css_selector 提取主内容，构造 ParseResult 喂给 healer。

借鉴 crawl4ai 多 extraction strategy fallback 思路。
"""

import logging
from typing import Optional

from schemas.intel import ParseResult

logger = logging.getLogger(__name__)


def _infer_title_from_css(target, text: str, selectors: list[str]) -> str:
    """V4.8.5: 从 CSS 命中节点推断非空标题，修复降级路径被 _save_promotion 丢弃的断链。

    优先级:
    1. 首个 heading 元素 (h1-h6) 文本
    2. target 的 title / aria-label 属性
    3. 文本首行截断 80 字符
    4. selector + 字符数兜底 (保证非空)
    """
    # 1. heading 元素
    heading = target.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    if heading:
        t = heading.get_text(strip=True)[:100]
        if t:
            return t
    # 2. 属性
    t = target.get("title") or target.get("aria-label")
    if t and t.strip():
        return t.strip()[:100]
    # 3. 文本首行
    if text:
        first_line = text.split("\n", 1)[0].strip()
        if first_line:
            return first_line[:80]
    # 4. 兜底
    return f"[CSS降级] {selectors[0]} ({len(text)}字符)"


def extract_by_css(
    html: str,
    css_selector: str,
    confidence: float = 0.5,
    provider: Optional[str] = None,
    url: Optional[str] = None,
) -> Optional[ParseResult]:
    """用 CSS selector 从 HTML 提取主内容

    Args:
        html: 原始 HTML
        css_selector: CSS 选择器（支持逗号分隔多个 selector，逗号分隔 = 多个候选）
        confidence: 该规则的置信度（默认 0.5，比 AI 解析 0.8 阈值低）
        provider: 厂商标识
        url: 来源 URL

    Returns:
        ParseResult 或 None（提取失败时）

    Note:
        - 只填 content 字段，其它字段（title/discount_desc/date）留 None
        - 用 ParseRule 的 confidence（不是 AI 给的），作为降级标识
    """
    if not html or not css_selector:
        return None

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 未安装，CSS 降级失败")
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.warning(f"BeautifulSoup 解析失败: {e}")
        return None

    # CSS selector 支持逗号分隔多个候选（firecrawl 模式）
    selectors = [s.strip() for s in css_selector.split(",")]
    target = None
    for sel in selectors:
        target = soup.select_one(sel)
        if target:
            break

    if not target:
        logger.debug(f"CSS 降级失败：所有 selector {selectors} 都没匹配")
        return None

    # 提取文本（保留段落结构）
    text = target.get_text("\n", strip=True)
    # 截断到 2000 字符（避免太长）
    if len(text) > 2000:
        text = text[:2000] + "..."

    return ParseResult(
        is_promotion=True,  # 降级路径默认当作 promotion 入库
        confidence=confidence,
        title=_infer_title_from_css(target, text, selectors),  # V4.8.5: 不再硬编码 None
        provider=provider,
        discount_desc=None,
        content=text,
        start_date=None,
        end_date=None,
        url=url,
        summary=f"[CSS 降级] selector={selectors[0]!r} 提取 {len(text)} 字符",
    )
