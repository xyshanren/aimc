"""V6.5 批次一: S6 周报端点存储型 XSS 修复回归.

背景 (production-review-2026-09-13.md S6): /dashboard/api/reports/latest 与
/api/reports/{date} 把磁盘周报原文直接拼进 <pre> HTML。周报含采集来的第三方
数据 (模型名/URL/公告正文), 被注入 <script> 后 admin 打开即在管理域执行
(可读 localStorage 的 dashboard token)。

修复: 后端 html.escape。前端 app.js 用 DOMParser 取 textContent (浏览器解析
自动反转义), marked 渲染链路零改动 — 本测试同时用 html.unescape 模拟该语义
验证「转义后前端拿到的仍是原文」。
"""

import html
from pathlib import Path

import pytest

import dashboard.routes as dash_routes


PAYLOAD = (
    "# 周报\n\n正常内容\n\n<script>alert(document.cookie)</script>\n"
    "<img src=x onerror=alert(1)> [link](https://example.com)\n"
)


@pytest.fixture
def reports_dir(tmp_path, monkeypatch):
    d = tmp_path / "reports"
    d.mkdir()
    (d / "weekly-latest.md").write_text(PAYLOAD, encoding="utf-8")
    (d / "weekly_2026-09-08.md").write_text(PAYLOAD, encoding="utf-8")
    monkeypatch.setattr(dash_routes, "REPORTS_DIR", d)
    return d


def _body_text(resp) -> str:
    """模拟前端 DOMParser: 解析 HTML 后取 pre.report-md 的 textContent
    (浏览器解析实体自动反转义 ≡ html.unescape)。"""
    raw = resp.body.decode("utf-8")
    inner = raw[raw.index(">") + 1:raw.rindex("</pre>")]
    return html.unescape(inner)


@pytest.mark.asyncio
async def test_latest_report_escapes_html(reports_dir):
    resp = await dash_routes.get_latest_report()
    raw = resp.body.decode("utf-8")
    # 原文中的标签不得以可执行形态出现
    assert "<script>" not in raw
    assert "<img src=x" not in raw
    assert "&lt;script&gt;" in raw
    # 前端链路语义: 反转义后拿到的仍是原文 (marked 渲染不受影响)
    assert _body_text(resp) == PAYLOAD


@pytest.mark.asyncio
async def test_report_by_date_escapes_html(reports_dir):
    resp = await dash_routes.get_report_by_date("2026-09-08")
    raw = resp.body.decode("utf-8")
    assert "<script>" not in raw
    assert "&lt;script&gt;" in raw
    assert _body_text(resp) == PAYLOAD
