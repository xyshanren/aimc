"""Playwright 异步页面抓取

支持 JS 渲染的页面抓取，应对 SPA 页面。

改进历史：
- 2026-07-22: 加重试 + 指数退避 (Phase 2A-1, 借鉴 firecrawl)
- 2026-07-22: 加资源拦截 text_mode (Phase 2A-3, 借鉴 crawl4ai)
- 2026-07-22: 修资源拦截 bug — 之前传 list 当 url pattern 触发 'list' not callable 错误
              改成单 glob 字符串
"""

import asyncio
import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# Playwright 浏览器实例（延迟初始化）
_browser = None

# 资源拦截：屏蔽图片/CSS/字体（节省 60-80% 带宽，3-5x 加速）
# Playwright 的 glob 支持 {a,b,c} 语法，单字符串即可
_BLOCKED_RESOURCE_PATTERN = (
    "**/*.{png,jpg,jpeg,gif,webp,svg,ico,bmp,"
    "css,woff,woff2,ttf,otf,eot}"
)
# 常见追踪 / 分析（默认不启用，需要时手动加到 pattern 即可）
# "**/google-analytics.com/**",
# "**/googletagmanager.com/**",


async def _get_browser():
    """获取或创建浏览器实例"""
    global _browser
    if _browser is None:
        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        launch_args = {"headless": True}
        if settings.proxy_url:
            launch_args["proxy"] = {"server": settings.proxy_url}
        _browser = await pw.chromium.launch(**launch_args)
        logger.info("Playwright 浏览器已启动")
    return _browser


async def _apply_resource_blocking(context) -> None:
    """在 context 上注册全局资源拦截，屏蔽图片/CSS/字体。

    借鉴 crawl4ai text_mode=True 的实现。
    """
    async def _route_handler(route):
        await route.abort()
    # Playwright 的 route() 第二个参数（handler）是 callable，
    # 第一个参数（url pattern）必须是单 string/Pattern，不能是 list
    await context.route(_BLOCKED_RESOURCE_PATTERN, _route_handler)


async def fetch_page(
    url: str,
    wait_until: str = "networkidle",
    timeout: int = 30000,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
) -> Optional[str]:
    """抓取页面内容，带重试 + 指数退避

    Args:
        url: 目标 URL
        wait_until: 等待条件 (load/domcontentloaded/networkidle)
        timeout: 单次请求超时（毫秒）
        max_attempts: 最大尝试次数（默认 3）
        base_delay: 首次重试等待秒数（默认 1.0）
        backoff: 退避倍数（默认 2.0，1s → 2s → 4s）

    Returns:
        页面 HTML 内容，全部失败返回 None

    借鉴：firecrawl apps/api/src/lib/utils/retry.ts
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            content = await _fetch_page_once(url, wait_until, timeout)
            if content is not None:
                if attempt > 1:
                    logger.info(f"fetch 重试成功 (attempt {attempt}/{max_attempts}): {url}")
                return content
            # content is None（异常被吞了）→ 视为失败，继续重试
            last_error = Exception("fetch_page_once returned None")

        except Exception as e:
            last_error = e
            # 4xx 客户端错误不重试（重试无意义）
            # 5xx / Timeout / ConnectError 等才重试
            status = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status", None)
            if status and 400 <= status < 500 and status not in (408, 429):
                logger.warning(f"fetch 客户端错误 {status}，不重试: {url}")
                return None

        if attempt < max_attempts:
            delay = base_delay * (backoff ** (attempt - 1))
            logger.warning(
                f"fetch 失败 (attempt {attempt}/{max_attempts}): {url} "
                f"error={last_error}, {delay:.1f}s 后重试"
            )
            await asyncio.sleep(delay)

    logger.error(f"fetch 全部 {max_attempts} 次失败: {url}, last_error={last_error}")
    return None


async def _fetch_page_once(url: str, wait_until: str, timeout: int) -> Optional[str]:
    """单次抓取（不带重试），使用独立 context 实现资源拦截"""
    browser = await _get_browser()
    context = await browser.new_context()
    await _apply_resource_blocking(context)
    page = await context.new_page()
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout)
        # 等待一小段时间确保动态内容加载
        await page.wait_for_timeout(2000)
        content = await page.content()
        return content
    finally:
        await page.close()
        await context.close()


async def fetch_text(
    url: str,
    css_selector: Optional[str] = None,
    timeout: int = 30000,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
) -> Optional[str]:
    """抓取页面纯文本（可选 CSS 选择器定位），带重试

    Args:
        url: 目标 URL
        css_selector: CSS 选择器，None 则获取 body 全文
        timeout: 单次请求超时（毫秒）
        max_attempts / base_delay / backoff: 同 fetch_page
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            browser = await _get_browser()
            context = await browser.new_context()
            await _apply_resource_blocking(context)
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                await page.wait_for_timeout(1500)

                if css_selector:
                    element = await page.query_selector(css_selector)
                    if element:
                        return await element.inner_text()
                    return None
                else:
                    return await page.inner_text("body")
            finally:
                await page.close()
                await context.close()

        except Exception as e:
            last_error = e
            status = getattr(e, "status", None) or getattr(getattr(e, "response", None), "status", None)
            if status and 400 <= status < 500 and status not in (408, 429):
                logger.warning(f"fetch_text 客户端错误 {status}，不重试: {url}")
                return None

        if attempt < max_attempts:
            delay = base_delay * (backoff ** (attempt - 1))
            logger.warning(
                f"fetch_text 失败 (attempt {attempt}/{max_attempts}): {url} "
                f"error={last_error}, {delay:.1f}s 后重试"
            )
            await asyncio.sleep(delay)

    logger.error(f"fetch_text 全部 {max_attempts} 次失败: {url}, last_error={last_error}")
    return None


async def close_browser():
    """关闭浏览器"""
    global _browser
    if _browser:
        await _browser.close()
        _browser = None
        logger.info("Playwright 浏览器已关闭")
