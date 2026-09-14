"""Playwright 异步页面抓取

支持 JS 渲染的页面抓取，应对 SPA 页面。

改进历史：
- 2026-07-22: 加重试 + 指数退避 (Phase 2A-1, 借鉴 firecrawl)
- 2026-07-22: 加资源拦截 text_mode (Phase 2A-3, 借鉴 crawl4ai)
- 2026-07-22: 修资源拦截 bug — 之前传 list 当 url pattern 触发 'list' not callable 错误
              改成单 glob 字符串
- 2026-07-22: 加 playwright-stealth 反爬 (Phase 2C-3)
  - 屏蔽 navigator.webdriver 等 bot 特征
  - 默认 evasions 全开（成本 < 50ms/context）
"""

import asyncio
import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# Playwright 浏览器实例（延迟初始化）
_browser = None
_pw = None  # V4.8.4: 保存 playwright driver 句柄, close_browser 需调 pw.stop() 释放进程

# V4.8.4: 防并发 init 竞争 (多 source 同时首次 fetch 可触发多个 browser 实例)
_browser_lock = asyncio.Lock()

# V4.8.4: 并发 context 上限 — 防大量 source 同时 fetch 打爆内存
# 延迟初始化: 避免模块 import 时读 settings (测试 mock settings 无此属性会 AttributeError)
_FETCH_SEMAPHORE: Optional[asyncio.Semaphore] = None


def _get_fetch_semaphore() -> asyncio.Semaphore:
    """延迟创建 Semaphore (首次 fetch 时读 settings.browser_max_concurrent)"""
    global _FETCH_SEMAPHORE
    if _FETCH_SEMAPHORE is None:
        _FETCH_SEMAPHORE = asyncio.Semaphore(settings.browser_max_concurrent)
    return _FETCH_SEMAPHORE

# 资源拦截：屏蔽图片/CSS/字体（节省 60-80% 带宽，3-5x 加速）
# Playwright 的 glob 支持 {a,b,c} 语法，单字符串即可
_BLOCKED_RESOURCE_PATTERN = (
    "**/*.{png,jpg,jpeg,gif,webp,svg,ico,bmp,"
    "css,woff,woff2,ttf,otf,eot}"
)
# 常见追踪 / 分析（默认不启用，需要时手动加到 pattern 即可）
# "**/google-analytics.com/**",
# "**/googletagmanager.com/**",

# Stealth 实例（Phase 2C-3）
# 全局复用，script_payload 只算一次。应用在每个 context 上即可对所有 page 生效
_stealth = None


def _get_stealth():
    """延迟初始化 Stealth（避免 import 时的副作用）"""
    global _stealth
    if _stealth is None:
        from playwright_stealth import Stealth
        _stealth = Stealth()
    return _stealth


async def _get_browser():
    """获取或创建浏览器实例

    V4.8.4: double-checked locking 防并发 init 竞争 + 保存 pw 句柄
    """
    global _browser, _pw
    if _browser is not None:
        return _browser
    async with _browser_lock:
        if _browser is not None:
            return _browser
        from playwright.async_api import async_playwright
        _pw = await async_playwright().start()
        launch_args = {"headless": True}
        if settings.proxy_url:
            launch_args["proxy"] = {"server": settings.proxy_url}
        _browser = await _pw.chromium.launch(**launch_args)
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


async def _apply_stealth(context) -> None:
    """在 context 上注入反爬 init script（Phase 2C-3）

    借鉴 playwright-stealth 默认 evasions：
    - navigator.webdriver → false
    - chrome.runtime / chrome.app 等对象模拟
    - navigator.languages / platform / plugins 真实化
    - webgl vendor/renderer 真实化（避免被识别为 SwiftShader）

    注：add_init_script 会在 context 内**所有 page 创建前**执行，
    所以一个 context 应用一次即可（每个 fetch 新建 context 的代价已存在）
    """
    stealth = _get_stealth()
    await stealth.apply_stealth_async(context)


async def _fetch_page_once(
    url: str,
    wait_until: str,
    timeout: int,
    wait_for: Optional[str] = None,
    wait_for_timeout: int = 10000,
) -> Optional[str]:
    """单次抓取（不带重试），使用独立 context 实现资源拦截

    Phase 2B-3: 支持 wait_for CSS selector
    - 优先级：wait_until → wait_for(可选) → page.content()
    - 不再有硬编码的 wait_for_timeout(2000)，用 wait_for 替代

    Phase 2C-3: 加 stealth 反爬
    - 顺序很重要：先 new_context → 后 apply_stealth（init script）

    V4.8.4: 加 Semaphore (并发 context 上限) + 不再在此层包 wait_for
    (wait_for 由调用方 fetch_page_with_error 统一包)
    """
    async with _get_fetch_semaphore():
        browser = await _get_browser()
        context = await browser.new_context()
        await _apply_stealth(context)
        await _apply_resource_blocking(context)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            # Phase 2B-3: 等关键元素出现（可选）
            # 比硬编码 sleep 2s 更精准 — 元素出现立即继续，没出现等到超时
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=wait_for_timeout)
                    logger.debug(f"wait_for 命中: {wait_for!r} ({url})")
                except Exception as e:
                    # wait_for 超时 — 这是告警级事件，但仍然返回当前 content
                    logger.warning(f"wait_for 超时 ({wait_for!r}): {e}")
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
        max_attempts / base_delay / backoff: 同 fetch_page_with_error
    """
    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            async with _get_fetch_semaphore():
                browser = await _get_browser()
                context = await browser.new_context()
                await _apply_stealth(context)
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
    """关闭浏览器 + playwright driver (V4.8.4)

    旧 bug: pw 句柄是局部变量未保存 → close_browser 只关 browser, 不关 driver →
    driver 进程泄漏. 现在保存 _pw 全局句柄, close 后调 pw.stop() 彻底释放.
    """
    global _browser, _pw
    if _browser:
        try:
            await _browser.close()
        except Exception as e:
            logger.warning(f"关 browser 异常: {e}")
        _browser = None
    if _pw:
        try:
            await _pw.stop()
        except Exception as e:
            logger.warning(f"关 playwright driver 异常: {e}")
        _pw = None
    logger.info("Playwright 浏览器已关闭")


async def fetch_page_with_error(
    url: str,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    wait_for: Optional[str] = None,
    wait_for_timeout: int = 10000,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
) -> tuple[Optional[str], Optional[str]]:
    """HTML 抓取（带重试 + 指数退避）：返回 (content, error) tuple

    Phase 2C-1: 让 _run_source_task 拿到错误原因，告警能显示具体问题

    Returns:
        (content, None) 成功
        (None, error_str) 失败（含具体原因）

    与已删除的 fetch_page 不同，本函数返回 (content, error)，调用方知道为什么失败
    (content=None, error=None 表示重试后仍拿不到内容且无异常对象)。
    """
    last_error: Optional[Exception] = None
    last_error_status: Optional[int] = None

    for attempt in range(1, max_attempts + 1):
        try:
            content = await asyncio.wait_for(
                _fetch_page_once(
                    url, wait_until, timeout, wait_for, wait_for_timeout
                ),
                timeout=settings.browser_fetch_timeout,
            )
            if content is not None:
                if attempt > 1:
                    logger.info(f"fetch 重试成功 (attempt {attempt}/{max_attempts}): {url}")
                return content, None
            last_error = Exception("fetch_page_once returned None")

        except Exception as e:
            last_error = e
            last_error_status = (
                getattr(e, "status", None)
                or getattr(getattr(e, "response", None), "status", None)
            )
            if last_error_status and 400 <= last_error_status < 500 and last_error_status not in (408, 429):
                logger.warning(f"fetch 客户端错误 {last_error_status}，不重试: {url}")
                return None, f"HTTP {last_error_status} 客户端错误（不重试）: {type(e).__name__}: {e}"

        if attempt < max_attempts:
            delay = base_delay * (backoff ** (attempt - 1))
            logger.warning(
                f"fetch 失败 (attempt {attempt}/{max_attempts}): {url} "
                f"error={last_error}, {delay:.1f}s 后重试"
            )
            await asyncio.sleep(delay)

    # 全部失败
    if last_error_status:
        reason = f"HTTP {last_error_status} 重试 {max_attempts} 次失败: {type(last_error).__name__}: {last_error}"
    else:
        reason = f"重试 {max_attempts} 次失败: {type(last_error).__name__}: {last_error}"
    logger.error(f"fetch 全部 {max_attempts} 次失败: {url}, reason={reason}")
    return None, reason
