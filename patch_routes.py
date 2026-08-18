"""Patch routes.py to add _auto_refresh_if_stale call."""
import sys

path = "/home/lxc/aimc/gateway/routes.py"
content = open(path).read()

old = '''    # 路由决策: select_channel (Phase 2.4 入口, 精确模型 + 模型组)
    # - "deepseek-chat" → 走原精确模型路由
    # - "tier:balanced" / "scene:code" → 走组路由 + fallback chain
    channel = route_engine.select_channel(model, prefs=prefs)'''

new = '''    # 路由决策: select_channel (Phase 2.4 入口, 精确模型 + 模型组)
    # - "deepseek-chat" → 走原精确模型路由
    # - "tier:balanced" / "scene:code" → 走组路由 + fallback chain
    # P0-4 2026-08-14: 路由前先 auto-refresh stale cache
    # 避免 db 改了必须 systemctl restart 的运维陷阱
    await route_engine._auto_refresh_if_stale()
    channel = route_engine.select_channel(model, prefs=prefs)'''

if old not in content:
    print("ERROR: old not found")
    sys.exit(1)
content = content.replace(old, new, 1)
open(path, "w").write(content)
print("OK")
