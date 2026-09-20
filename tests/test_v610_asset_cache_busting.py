# -*- coding: utf-8 -*-
"""V6.10.1 静态资源缓存击穿测试 (dashboard_page ?v= 注入)

背景: app.py 对 /static/* 设 max-age=604800 (7d 强缓存), 但 index.html 引用的是
无版本的 /static/app.js — 发版后浏览器钉住旧 JS (性能卡有骨架无数据实证)。
修复: dashboard_page 按资源 mtime 注入 ?v= 版本 query, 资源变更自动失效。

覆盖:
1. 有 app.js/style.css → index.html 注入 ?v=<mtime>
2. 缺资源文件 → 不注入 (保持原样)
3. mtime 变化 → 新 v (缓存击穿前提)

跑法: python -m pytest tests/test_v610_asset_cache_busting.py -v
"""

import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v6101_")
    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url=f"sqlite+aiosqlite:///{os.path.join(tmp_dir, 'test.db')}",
        db_path=os.path.join(tmp_dir, "test.db"),
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )


_install_mock_env()


async def test_dashboard_page_injects_mtime_version(tmp_path, monkeypatch):
    """有资源文件 → index.html 的 /static/app.js 与 style.css 注入 ?v=<mtime>"""
    import dashboard.routes as dr

    (tmp_path / "index.html").write_text(
        '<script src="/static/app.js"></script>'
        '<link rel="stylesheet" href="/static/style.css">',
        encoding="utf-8",
    )
    (tmp_path / "app.js").write_text("js", encoding="utf-8")
    (tmp_path / "style.css").write_text("css", encoding="utf-8")
    ts = 1700000000
    os.utime(tmp_path / "app.js", (ts, ts))
    os.utime(tmp_path / "style.css", (ts, ts))
    monkeypatch.setattr(dr, "STATIC_DIR", tmp_path)

    resp = await dr.dashboard_page()
    body = resp.body.decode()
    assert f"/static/app.js?v={ts}" in body, "app.js 应带 mtime 版本 query"
    assert f"/static/style.css?v={ts}" in body, "style.css 应带 mtime 版本 query"


async def test_dashboard_page_no_inject_when_assets_missing(tmp_path, monkeypatch):
    """缺资源文件 → 原样输出 (不注入, 不报错)"""
    import dashboard.routes as dr

    (tmp_path / "index.html").write_text(
        '<script src="/static/app.js"></script>', encoding="utf-8")
    monkeypatch.setattr(dr, "STATIC_DIR", tmp_path)

    resp = await dr.dashboard_page()
    body = resp.body.decode()
    assert 'src="/static/app.js"' in body
    assert body.count("/static/app.js") == 1, "无资源文件时不注入版本"
    assert "?v=" not in body


async def test_dashboard_page_version_changes_on_mtime_bump(tmp_path, monkeypatch):
    """mtime 变化 → 版本号变化 (浏览器缓存击穿的充要条件)"""
    import dashboard.routes as dr

    (tmp_path / "index.html").write_text(
        '<script src="/static/app.js"></script>', encoding="utf-8")
    (tmp_path / "app.js").write_text("js", encoding="utf-8")
    os.utime(tmp_path / "app.js", (1700000000, 1700000000))
    monkeypatch.setattr(dr, "STATIC_DIR", tmp_path)

    v1 = (await dr.dashboard_page()).body.decode()
    assert "?v=1700000000" in v1

    os.utime(tmp_path / "app.js", (1700000100, 1700000100))
    v2 = (await dr.dashboard_page()).body.decode()
    assert "?v=1700000100" in v2
    assert v1 != v2