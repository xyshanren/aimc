"""V6.1: 登录页 next 参数安全测试

背景 (Mimosa medium, 真问题 → 已结构性消除):
- 旧实现: 服务端把 ?next= format 进 JS `location.href = '{next_path}'`
  → 服务端污点流: `?next=';alert(1)//` JS 注入 (XSS), `?next=https://evil.com` 开放跳转
- 新实现: 服务端零插值, 页面 JS 从 URLSearchParams 自取 + safeNext() 白名单校验
  (仅站内绝对路径, 拒 //外链/引号/反斜杠/尖括号), 不合法回默认 /dashboard

测试口径:
- 恶意 ?next= 的值绝不回显进页面 (服务端零插值)
- 页面必须带 safeNext() 白名单守卫源码
- 注: 项目无 static/login.html (生产走 routes.py 内联 fallback), 修复点只有一处

跑法:
    python -m pytest tests/test_v61_login_next.py -v
"""

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v61_login_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = "sqlite+aiosqlite:///" + db_path

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )


_install_mock_env()


def _login_html(next_param: str = None) -> str:
    """走真实 HTTP 层拿登录页 HTML (不直调函数, 保证路由行为)"""
    import httpx
    from fastapi import FastAPI
    from dashboard.routes import router as dashboard_router

    async def _run():
        app = FastAPI()
        app.include_router(dashboard_router)
        transport = httpx.ASGITransport(app=app)
        params = {} if next_param is None else {"next": next_param}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/dashboard/login", params=params)
        assert resp.status_code == 200
        return resp.text

    return asyncio.run(_run())


def test_login_page_has_client_side_guard():
    """页面必须带 safeNext() 白名单守卫 (站内路径校验)"""
    html = _login_html()
    assert "function safeNext()" in html
    assert "n.startsWith('//')" in html          # 拒协议相对外链
    assert "URLSearchParams" in html             # 前端自取, 服务端零插值
    assert "{next_path}" not in html             # 旧插值占位符已移除


def test_login_page_never_echoes_next():
    """恶意 next 值绝不回显进页面 (结构性消除注入面)"""
    for evil in ("';alert(1)//", '"/><script>x</script>', "https://evil.com", "//evil.com"):
        html = _login_html(evil)
        assert "alert(1)" not in html, evil
        assert "<script>x</script>" not in html, evil
        assert "evil.com" not in html, evil


def test_login_page_without_next_ok():
    html = _login_html()
    assert "AIMC Dashboard" in html
    assert "submit()" in html
