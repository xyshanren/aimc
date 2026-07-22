"""Playwright 异步页面抓取

支持 JS 渲染的页面抓取，应对 SPA 页面。
"""

import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# Playwright 浏览器实例（延迟初始化）
_browser = None


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


async def fetch_page(url: str, wait_until: str = "networkidle", timeout: int = 30000) -> Optional[str]:
    """抓取页面内容

    Args:
        url: 目标 URL
        wait_until: 等待条件 (load/domcontentloaded/networkidle)
        timeout: 超时时间（毫秒）

    Returns:
        页面文本内容，失败返回 None
    """
    try:
        browser = await _get_browser()
        page = await browser.new_page()

        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            # 等待一小段时间确保动态内容加载
            await page.wait_for_timeout(2000)
            content = await page.content()
            return content
        finally:
            await page.close()

    except Exception as e:
        logger.error(f"页面抓取失败: {url} - {e}")
        return None


async def fetch_text(url: str, css_selector: Optional[str] = None, timeout: int = 30000) -> Optional[str]:
    """抓取页面纯文本（可选 CSS 选择器定位）

    Args:
        url: 目标 URL
        css_selector: CSS 选择器，None 则获取 body 全文
        timeout: 超时时间（毫秒）

    Returns:
        纯文本内容
    """
    try:
        browser = await _get_browser()
        page = await browser.new_page()

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

    except Exception as e:
        logger.error(f"文本抓取失败: {url} - {e}")
        return None


async def close_browser():
    """关闭浏览器"""
    global _browser
    if _browser:
        await _browser.close()
        _browser = None
        logger.info("Playwright 浏览器已关闭")
