"""V6.1: 登录页 next 参数白名单校验测试

背景 (Mimosa medium, 真问题):
- fallback 登录页把 ?next= 拼进 JS `location.href = '{next_path}'`
- `?next=';alert(1)//` → JS 注入 (XSS)
- `?next=https://evil.com` / `//evil.com` → 登录后开放跳转

修复: dashboard.routes._safe_next_path 白名单 (站内绝对路径, 拒引号/反斜杠/尖括号)。
注: 项目无 static/login.html (生产走 routes.py 内联 fallback), 修复点只有一处。

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


def _login_html(next_param: str) -> str:
    """走真实 HTTP 层拿登录页 HTML (不直调函数, 保证路由行为)"""
    import httpx
    from fastapi import FastAPI
    from dashboard.routes import router as dashboard_router

    async def _run():
        app = FastAPI()
        app.include_router(dashboard_router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/dashboard/login", params={"next": next_param})
        assert resp.status_code == 200
        return resp.text

    return asyncio.run(_run())


def test_safe_next_keeps_site_relative():
    html = _login_html("/dashboard/api/usage/cost-by-model")
    assert "location.href = '/dashboard/api/usage/cost-by-model'" in html


def test_safe_next_rejects_external_url():
    for evil in ("https://evil.com", "http://evil.com/x", "//evil.com", "/\\evil.com"):
        html = _login_html(evil)
        assert "location.href = '/dashboard'" in html, evil
        assert "evil.com'" not in html, evil


def test_safe_next_rejects_js_injection():
    for evil in ("';alert(1)//", '"/><script>x</script>', "/a\";x=", "/a\\b", "/x\r\nSet-Cookie: 1"):
        html = _login_html(evil)
        assert "alert(1)" not in html, evil
        assert "<script>x</script>" not in html, evil
        assert "location.href = '/dashboard'" in html, evil


def test_safe_next_defaults_on_empty():
    html = _login_html("")
    assert "location.href = '/dashboard'" in html


def test_safe_next_path_unit():
    """单元级: 合法站内路径原样保留"""
    from dashboard.routes import _safe_next_path

    assert _safe_next_path("/dashboard") == "/dashboard"
    assert _safe_next_path("/dashboard/api/keys?a=1&b=2") == "/dashboard/api/keys?a=1&b=2"
    assert _safe_next_path("/a%20b") == "/a%20b"
