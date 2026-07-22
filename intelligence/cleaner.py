"""HTML → 干净 Markdown 净化

借鉴 crawl4ai `DefaultMarkdownGenerator` + `PruningContentFilter` 的思路：
- 移除噪音标签（script/style/nav/footer/aside/header/广告）
- 优先保留 main/article 等语义化容器
- 输出 Markdown 而非原始 HTML，AI 解析更准 / token 更省

为什么需要：
- 现在 parser.py 直接截断 3000 字符 HTML 喂 LLM
- 含 script/style/nav/footer 等无关内容
- 净化后 → 1500 字符 Markdown 等价信息
- 收益：token 成本 ↓50%、解析质量 ↑、抗广告/CSS 干扰

改进历史：
- 2026-07-22: Phase 2B-1 初版 (借鉴 crawl4ai)
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 优先保留的语义化容器（按优先级排列，找到第一个就用）
_CONTENT_SELECTORS = [
    "main",
    "article",
    "[role=main]",
    "#content",
    "#main",
    ".main-content",
    ".article-content",
    ".post-content",
]

# 完全移除的标签（噪音）
_REMOVE_TAGS = [
    "script", "style", "noscript", "iframe", "embed", "object",
    "nav", "footer", "aside", "header",
    "form", "button", "input", "select", "textarea",
    "svg", "canvas", "video", "audio", "source", "track",
    "ins",
]

_REMOVE_ATTR_SELECTORS = [
    "[role=ad]",
    "[role=banner]",
    "[role=navigation]",
    "[aria-hidden=true]",
]

# 常见遮罩 / cookie 同意框
_OVERLAY_SELECTORS = [
    ".cookie-banner", ".cookie-notice", ".cookie-consent", ".cookie",
    ".modal", ".dialog", ".popup", ".overlay",
    ".gdpr", ".cc-window",
]


def _find_content_root(soup):
    """找到主内容根节点（优先 main / article）"""
    for selector in _CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node:
            return node
    return soup.body or soup


def _strip_noise(soup) -> None:
    """移除噪音标签和遮罩元素（in-place）"""
    for tag in _REMOVE_TAGS:
        for node in soup.find_all(tag):
            node.decompose()
    for selector in _REMOVE_ATTR_SELECTORS + _OVERLAY_SELECTORS:
        for node in soup.select(selector):
            node.decompose()


def _node_to_markdown(node, out: list) -> None:
    """递归把 BS4 节点转成 Markdown 片段（追加到 out）"""
    # NavigableString（裸文本节点）的 .name 是 None
    if not getattr(node, "name", None):
        text = str(node).strip()
        if text:
            out.append(text)
        return

    name = node.name.lower()
    if name in ("script", "style", "noscript", "svg", "iframe", "img"):
        return

    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        text = node.get_text(" ", strip=True)
        if text:
            out.append(f"\n\n{'#' * level} {text}\n\n")
    elif name == "br":
        out.append("\n")
    elif name in ("strong", "b"):
        text = node.get_text(" ", strip=True)
        if text:
            out.append(f"**{text}**")
    elif name in ("em", "i"):
        text = node.get_text(" ", strip=True)
        if text:
            out.append(f"*{text}*")
    elif name == "code":
        # 如果父节点是 pre，跳过（pre 自己处理）
        parent = node.parent
        if parent and getattr(parent, "name", "") == "pre":
            return
        text = node.get_text()
        if text:
            out.append(f"`{text}`")
    elif name == "pre":
        code_node = node.find("code")
        text = (code_node.get_text() if code_node else node.get_text()).rstrip()
        if text:
            # 检测语言（class 里有 language-xxx）
            lang = ""
            if code_node:
                for c in (code_node.get("class") or []):
                    if c.startswith("language-"):
                        lang = c[len("language-"):]
                        break
            out.append(f"\n\n```{lang}\n{text}\n```\n\n")
    elif name == "ul":
        for li in node.find_all("li", recursive=False):
            # li 内部要保留 strong/em/code 等，所以递归
            li_md: list[str] = []
            for child in li.children:
                _node_to_markdown(child, li_md)
            text = "".join(li_md).strip()
            if text:
                out.append(f"- {text}\n")
        out.append("\n")
    elif name == "ol":
        for idx, li in enumerate(node.find_all("li", recursive=False), 1):
            li_md: list[str] = []
            for child in li.children:
                _node_to_markdown(child, li_md)
            text = "".join(li_md).strip()
            if text:
                out.append(f"{idx}. {text}\n")
        out.append("\n")
    elif name == "table":
        for tr in node.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                out.append("| " + " | ".join(cells) + " |\n")
        out.append("\n")
    elif name == "a":
        text = node.get_text(" ", strip=True)
        href = node.get("href", "")
        if text and href and not href.startswith("#") and not href.startswith("javascript:"):
            out.append(f"[{text}]({href})")
        elif text:
            out.append(text)
    else:
        # 容器节点（p/div/section/article/main/...）：递归处理子节点
        # 不用 get_text() — 那样会丢掉子节点的 markdown 标记（**/[]() 等）
        for child in node.children:
            _node_to_markdown(child, out)
        # 段落/容器后面加换行
        if name in ("p", "div", "section", "article", "main"):
            out.append("\n\n")


def clean_html(html: str, max_chars: int = 1500) -> str:
    """HTML → 干净 Markdown

    Args:
        html: 原始 HTML（来自 fetcher 的 page.content()）
        max_chars: 输出最大字符数（默认 1500，比之前 HTML 截断 3000 省 50% token）

    Returns:
        净化后的 Markdown 字符串（空字符串如果输入为空）
    """
    if not html:
        return ""

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 未安装，跳过 HTML 净化（直接截断返回）")
        return html[:max_chars]

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        logger.warning(f"BeautifulSoup 解析失败: {e}")
        return html[:max_chars]

    # 找主内容根 + 移除噪音
    root = _find_content_root(soup)
    _strip_noise(root)

    # 递归转 Markdown
    out: list[str] = []
    for child in root.children:
        _node_to_markdown(child, out)

    result = "".join(out).strip()
    # 规范化空白
    import re
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = re.sub(r" {2,}", " ", result)

    # 截断
    if len(result) > max_chars:
        result = result[:max_chars] + "..."
    return result
