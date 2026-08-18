"""Patch routes.py to add P0-6 quota check."""
import sys

path = "/home/lxc/aimc/gateway/routes.py"
content = open(path).read()

old = """    await route_engine._auto_refresh_if_stale()
    channel = route_engine.select_channel(model, prefs=prefs)
    if channel is None:"""

new = """    await route_engine._auto_refresh_if_stale()
    channel = route_engine.select_channel(model, prefs=prefs)
    # P0-6 2026-08-14: 多维限额检查 (rpm/tpm/concurrent/daily/weekly/5h)
    # post-check: 选了 channel 后判断. 后续可改成 router 内候选过滤
    if channel is not None:
        from gateway.quota import check_account_quota
        from db.engine import async_session_factory
        async with async_session_factory() as qsession:
            allowed, reason = await check_account_quota(
                qsession,
                channel["account_id"],
                estimated_tokens=(body_json.get("max_tokens") or 100) * 2,
            )
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"error": {
                        "message": f"Quota exceeded: {reason}",
                        "type": "rate_limit_error",
                    }},
                    headers={"retry-after": "60"},
                )
    if channel is None:"""

if old not in content:
    print("ERROR: old not found")
    sys.exit(1)
content = content.replace(old, new, 1)
open(path, "w").write(content)
print("OK")
