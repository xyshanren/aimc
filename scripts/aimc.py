"""AIMC 统一 CLI (V6.1 架构友好化, docs/next-major-v6-plan.md §3.4 步骤 1)

定位: agent / 运维的统一命令行入口 — 查询与变更全部走 AIMC REST API,
CLI 只做薄封装 (不重实现任何业务逻辑), 与 API/看板天然等价。

鉴权 (二选一, 优先 DASHBOARD_TOKEN):
- AIMC_DASHBOARD_TOKEN — dashboard token
- AIMC_API_KEY         — 网关 key (需 admin key, V6.1 起 dashboard 接受 admin api_key)

用法:
    export AIMC_BASE_URL=http://192.168.56.12:8080
    export AIMC_DASHBOARD_TOKEN=xxxx

    python scripts/aimc.py status                          # 健康检查
    python scripts/aimc.py models                          # /v1/models
    python scripts/aimc.py accounts list                   # 渠道账号列表
    python scripts/aimc.py accounts enable 3
    python scripts/aimc.py accounts rotate 3 --new-key <new-api-key>
    python scripts/aimc.py keys create --username zhangsan --label 工作机 --admin
    python scripts/aimc.py keys revoke 5
    python scripts/aimc.py usage cost-by-model --days 7    # 成本审计 (V6.0)
    python scripts/aimc.py suggestions pending             # 待审建议
    python scripts/aimc.py cache refresh                   # 立即刷路由缓存

全部命令 --help 可查; 输出统一 JSON (stderr 报错, exit 0/1/2)。
"""

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_BASE_URL = "http://127.0.0.1:18080"


# ============================================================
# HTTP 层 (零依赖, urllib)
# ============================================================

def _fail(reason: str, **extra) -> None:
    print(json.dumps({"error": {"reason": reason, **extra}},
                     ensure_ascii=False), file=sys.stderr)
    raise SystemExit(1)


def _assert_safe_target(url: str) -> None:
    """V6.1: SSRF 防护 — 本 CLI 只允许打回环/内网的 AIMC 实例。

    与 scripts/deploy/verify_clients.py 同规则: scheme 限 http/https,
    host 限 loopback / RFC1918 私网 / link-local (典型: 127.0.0.1 或 VM 内网 IP),
    拒绝公网目标与非 IP 主机名。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        _fail("blocked scheme", scheme=parsed.scheme, url=url)
    host = parsed.hostname or ""
    if host.lower() == "localhost":
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        _fail("blocked non-ip host", host=host, url=url)
    if not (ip.is_loopback or ip.is_private or ip.is_link_local):
        _fail("blocked non-private target", host=host, url=url)


def _base_url(args) -> str:
    return (getattr(args, "base_url", None) or os.environ.get("AIMC_BASE_URL")
            or DEFAULT_BASE_URL).rstrip("/")


def _token() -> str:
    return (os.environ.get("AIMC_DASHBOARD_TOKEN")
            or os.environ.get("AIMC_API_KEY")
            or "")


def api_call(args, method: str, path: str, body: dict | None = None) -> dict | list:
    """调 AIMC REST API, 返回解析后的 JSON; 非 2xx 抛 SystemExit(1)"""
    url = _base_url(args) + path
    _assert_safe_target(url)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    token = _token()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        _fail("http error", http_code=e.code, detail=detail)
    except urllib.error.URLError as e:
        _fail("connection failed", detail=str(e.reason), base_url=_base_url(args))


def _output(result) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _require(args, name: str) -> str:
    v = getattr(args, name, None)
    if not v:
        print(json.dumps({"error": {"reason": f"--{name.replace('_', '-')} 必填"}},
                         ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
    return v


# ============================================================
# 子命令实现 (一行一个 REST 映射)
# ============================================================

def cmd_status(args):
    _output(api_call(args, "GET", "/health"))


def cmd_models(args):
    path = "/v1/models"
    if args.include_models:
        path += "?include_models=true"
    _output(api_call(args, "GET", path))


def cmd_auth_status(args):
    _output(api_call(args, "GET", "/dashboard/api/auth/status"))


# ---- accounts ----

def cmd_accounts_list(args):
    _output(api_call(args, "GET", "/dashboard/api/accounts"))


def cmd_accounts_add(args):
    body = {
        "provider": _require(args, "provider"),
        "account_name": _require(args, "name"),
        "api_key": _require(args, "api_key"),
    }
    if args.quota_total is not None:
        body["quota_total"] = args.quota_total
    if args.notes:
        body["notes"] = args.notes
    _output(api_call(args, "POST", "/dashboard/api/accounts", body))


def cmd_accounts_enable(args):
    _output(api_call(args, "POST", f"/dashboard/api/accounts/{args.id}/enable"))


def cmd_accounts_disable(args):
    _output(api_call(args, "POST", f"/dashboard/api/accounts/{args.id}/disable"))


def cmd_accounts_rotate(args):
    _output(api_call(args, "POST", f"/dashboard/api/accounts/{args.id}/rotate",
                     {"api_key": _require(args, "new_key")}))


def cmd_accounts_expire_check(args):
    _output(api_call(args, "POST", "/dashboard/api/accounts/expire-check"))


# ---- keys ----

def cmd_keys_list(args):
    _output(api_call(args, "GET", "/dashboard/api/keys"))


def cmd_keys_create(args):
    body = {"username": _require(args, "username")}
    for src, dst in (("label", "label"), ("display_name", "display_name"),
                     ("notes", "notes")):
        if getattr(args, src, None):
            body[dst] = getattr(args, src)
    if args.admin:
        body["admin"] = True
    if args.daily_limit is not None:
        body["daily_limit"] = args.daily_limit
    if args.monthly_limit is not None:
        body["monthly_limit"] = args.monthly_limit
    _output(api_call(args, "POST", "/dashboard/api/keys", body))


def cmd_keys_revoke(args):
    _output(api_call(args, "POST", f"/dashboard/api/keys/{args.key_id}/revoke"))


# ---- usage / 成本审计 ----

def cmd_usage(args):
    path = f"/dashboard/api/usage/{args.metric}?days={args.days}"
    if args.metric == "summary":
        path = f"/dashboard/api/usage/summary?days={args.days}"
    elif args.metric == "per-user":
        path = f"/dashboard/api/stats/per-user?days={args.days}"
    _output(api_call(args, "GET", path))


# ---- suggestions ----

def cmd_suggestions_pending(args):
    _output(api_call(args, "GET", "/dashboard/api/suggestions?status=pending"))


def cmd_suggestions_approve(args):
    _output(api_call(args, "POST",
                     f"/dashboard/api/suggestions/{args.suggestion_type}/{args.suggestion_id}/approve",
                     {"reviewed_by": "aimc_cli"}))


def cmd_suggestions_reject(args):
    _output(api_call(args, "POST",
                     f"/dashboard/api/suggestions/{args.suggestion_type}/{args.suggestion_id}/reject",
                     {"reviewed_by": "aimc_cli"}))


def cmd_suggestions_apply_all(args):
    _output(api_call(args, "POST", "/dashboard/api/suggestions/apply-all",
                     {"min_confidence": 0.0, "reviewed_by": "aimc_cli"}))


# ---- cooldown ----

def cmd_cooldown_pending(args):
    _output(api_call(args, "GET", "/dashboard/api/cooldown/needs-human"))


def cmd_cooldown_ack(args):
    _output(api_call(args, "POST", f"/dashboard/api/cooldown/{args.account_id}/ack-recover"))


# ---- cache ----

def cmd_cache_stats(args):
    _output(api_call(args, "GET", f"/dashboard/api/cache/stats?days={args.days}"))


def cmd_cache_refresh(args):
    _output(api_call(args, "POST", "/dashboard/api/cache/refresh"))


# ============================================================
# 参数树
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aimc",
        description="AIMC 统一 CLI — 查询/变更全走 REST API (与看板等价)")
    p.add_argument("--base-url", default=None,
                   help="AIMC 服务地址 (默认 AIMC_BASE_URL env 或 " + DEFAULT_BASE_URL + ")")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="健康检查 GET /health").set_defaults(func=cmd_status)
    sub.add_parser("auth", help="dashboard 鉴权状态").set_defaults(func=cmd_auth_status)

    m = sub.add_parser("models", help="模型列表 GET /v1/models")
    m.add_argument("--include-models", action="store_true", help="展开组内全部 model (默认只列组)")
    m.set_defaults(func=cmd_models)

    acc = sub.add_parser("accounts", help="渠道账号管理")
    acc_sub = acc.add_subparsers(dest="action", required=True)
    acc_sub.add_parser("list", help="账号列表").set_defaults(func=cmd_accounts_list)
    a_add = acc_sub.add_parser("add", help="加账号 (POST /api/accounts)")
    a_add.add_argument("--provider", required=True)
    a_add.add_argument("--name", required=True, help="账号名 e.g. deepseek_02")
    a_add.add_argument("--api-key", required=True)
    a_add.add_argument("--quota-total", type=int, default=None)
    a_add.add_argument("--notes", default=None)
    a_add.set_defaults(func=cmd_accounts_add)
    for action, fn in (("enable", cmd_accounts_enable), ("disable", cmd_accounts_disable)):
        s = acc_sub.add_parser(action, help=action + " 账号")
        s.add_argument("id", type=int)
        s.set_defaults(func=fn)
    a_rot = acc_sub.add_parser("rotate", help="轮换 key")
    a_rot.add_argument("id", type=int)
    a_rot.add_argument("--new-key", required=True)
    a_rot.set_defaults(func=cmd_accounts_rotate)
    acc_sub.add_parser("expire-check", help="扫描过期账号").set_defaults(
        func=cmd_accounts_expire_check)

    k = sub.add_parser("keys", help="多用户 key 管理")
    k_sub = k.add_subparsers(dest="action", required=True)
    k_sub.add_parser("list", help="用户 + key 列表 (脱敏)").set_defaults(func=cmd_keys_list)
    k_add = k_sub.add_parser("create", help="创建/复用 user + 发 key (明文只返回一次)")
    k_add.add_argument("--username", required=True)
    k_add.add_argument("--display-name", default=None)
    k_add.add_argument("--label", default=None)
    k_add.add_argument("--admin", action="store_true", help="发 admin key")
    k_add.add_argument("--daily-limit", type=int, default=None)
    k_add.add_argument("--monthly-limit", type=int, default=None)
    k_add.add_argument("--notes", default=None)
    k_add.set_defaults(func=cmd_keys_create)
    k_rev = k_sub.add_parser("revoke", help="吊销 key (立即失效)")
    k_rev.add_argument("key_id", type=int)
    k_rev.set_defaults(func=cmd_keys_revoke)

    u = sub.add_parser("usage", help="用量/成本查询")
    u.add_argument("metric", choices=[
        "summary", "per-user", "cost-by-model", "cost-by-group",
        "cost-by-user", "cost-trend"], help="查询维度 (cost-* = V6.0 成本审计)")
    u.add_argument("--days", type=int, default=7)
    u.set_defaults(func=cmd_usage)

    s = sub.add_parser("suggestions", help="路由建议审批")
    s_sub = s.add_subparsers(dest="action", required=True)
    s_sub.add_parser("pending", help="待审列表").set_defaults(func=cmd_suggestions_pending)
    s_ap = s_sub.add_parser("approve", help="批准建议")
    s_ap.add_argument("suggestion_type", choices=["model", "group"])
    s_ap.add_argument("suggestion_id", type=int)
    s_ap.set_defaults(func=cmd_suggestions_approve)
    s_rj = s_sub.add_parser("reject", help="拒绝建议")
    s_rj.add_argument("suggestion_type", choices=["model", "group"])
    s_rj.add_argument("suggestion_id", type=int)
    s_rj.set_defaults(func=cmd_suggestions_reject)
    s_sub.add_parser("apply-all", help="一键应用已批准").set_defaults(
        func=cmd_suggestions_apply_all)

    c = sub.add_parser("cooldown", help="账号冷却 / 人工干预")
    c_sub = c.add_subparsers(dest="action", required=True)
    c_sub.add_parser("pending", help="needs_human 列表").set_defaults(func=cmd_cooldown_pending)
    c_ack = c_sub.add_parser("ack", help="人工确认恢复")
    c_ack.add_argument("account_id", type=int)
    c_ack.set_defaults(func=cmd_cooldown_ack)

    ca = sub.add_parser("cache", help="prompt cache 统计 / 路由缓存刷新")
    ca_sub = ca.add_subparsers(dest="action", required=True)
    ca_st = ca_sub.add_parser("stats", help="cache 命中/节省统计")
    ca_st.add_argument("--days", type=int, default=7)
    ca_st.set_defaults(func=cmd_cache_stats)
    ca_sub.add_parser("refresh", help="立即刷新路由缓存").set_defaults(func=cmd_cache_refresh)

    return p


def main() -> int:
    args = build_parser().parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
