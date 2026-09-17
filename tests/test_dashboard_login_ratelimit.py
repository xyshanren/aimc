"""P2 (2026-09-18): dashboard login 暴力破解防护测试

验证:
- 错误 token 5 次/5 分钟窗口 → 第 6 次 429
- 正确 token 不受限速影响, 且登录成功清零计数
- 不同 IP 独立计数 (CF-Connecting-IP / X-Forwarded-For)
- 登录 cookie 带 Secure + HttpOnly + SameSite
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.middleware import RequestLoggingMiddleware
from dashboard import routes as dashboard_routes


TEST_TOKEN = "test-dashboard-token-p2"


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """每个测试前后清空 login 失败计数 + 固定 dashboard_token"""
    dashboard_routes._LOGIN_FAILS.clear()
    orig = dashboard_routes.settings.dashboard_token if hasattr(dashboard_routes, "settings") else None
    # dashboard_auth_login 内部 from config import settings 取单例, 直接改属性
    from config import settings as global_settings
    old = global_settings.dashboard_token
    global_settings.dashboard_token = TEST_TOKEN
    yield
    global_settings.dashboard_token = old
    dashboard_routes._LOGIN_FAILS.clear()


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(dashboard_routes.router)
    app.add_middleware(RequestLoggingMiddleware)
    return TestClient(app)


class TestLoginRateLimit:
    """P2: /dashboard/api/auth/login 暴力破解防护"""

    def test_6th_wrong_attempt_returns_429(self, client):
        """同一 IP 5 次错误后, 第 6 次 429"""
        for i in range(5):
            resp = client.post(
                "/dashboard/api/auth/login",
                json={"token": "wrong-token"},
            )
            assert resp.status_code == 401, f"attempt {i+1} should be 401, got {resp.status_code}"

        resp = client.post(
            "/dashboard/api/auth/login",
            json={"token": "wrong-token"},
        )
        assert resp.status_code == 429
        assert resp.json()["error"]["type"] == "rate_limit_error"

    def test_success_clears_fail_counter(self, client):
        """登录成功后失败计数清零"""
        for _ in range(4):
            client.post("/dashboard/api/auth/login", json={"token": "wrong"})

        resp = client.post(
            "/dashboard/api/auth/login",
            json={"token": TEST_TOKEN},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # 计数已清零 → 仍是 401 而非 429
        resp = client.post("/dashboard/api/auth/login", json={"token": "wrong"})
        assert resp.status_code == 401

    def test_different_ips_independent(self, client):
        """不同 IP 的失败计数互不影响"""
        for _ in range(5):
            client.post(
                "/dashboard/api/auth/login",
                json={"token": "wrong"},
                headers={"CF-Connecting-IP": "1.2.3.4"},
            )
        resp = client.post(
            "/dashboard/api/auth/login",
            json={"token": "wrong"},
            headers={"CF-Connecting-IP": "1.2.3.4"},
        )
        assert resp.status_code == 429

        # 另一 IP 第一次错误 — 独立计数, 仍 401
        resp = client.post(
            "/dashboard/api/auth/login",
            json={"token": "wrong"},
            headers={"CF-Connecting-IP": "5.6.7.8"},
        )
        assert resp.status_code == 401

    def test_xff_header_fallback(self, client):
        """CF-Connecting-IP 缺失时用 X-Forwarded-For 第一段"""
        for _ in range(5):
            client.post(
                "/dashboard/api/auth/login",
                json={"token": "wrong"},
                headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"},
            )
        resp = client.post(
            "/dashboard/api/auth/login",
            json={"token": "wrong"},
            headers={"X-Forwarded-For": "9.9.9.9, 10.0.0.1"},
        )
        assert resp.status_code == 429

    def test_cookie_has_secure_flag(self, client):
        """登录成功 cookie 带 Secure + HttpOnly + SameSite"""
        resp = client.post(
            "/dashboard/api/auth/login",
            json={"token": TEST_TOKEN},
        )
        assert resp.status_code == 200
        cookie_header = resp.headers.get("set-cookie", "")
        assert "Secure" in cookie_header
        assert "HttpOnly" in cookie_header
        assert "samesite=lax" in cookie_header.lower()

    def test_empty_token_rejected(self, client):
        """空 token 返回 401"""
        resp = client.post(
            "/dashboard/api/auth/login",
            json={"token": ""},
        )
        assert resp.status_code == 401
