"""Phase 4 v4.3 — 多客户端 OpenAI 兼容连通性验证

启动 AIMC 服务后, 模拟 4 个客户端实测 base_url:
1. localhost (同机 curl, 验证服务在跑)
2. windows (workbuddy / qclaw / trae 等, base_url=http://<vm-ip>:8080/v1)
3. wsl (hermes-agent 在 wsl 内, base_url=http://<vm-ip>:8080/v1)
4. LAN (其他机器, 同 #2)

每个客户端调 /v1/models, 验证:
- HTTP 200
- 返回 JSON 包含 data 数组
- AIMC_API_KEY 配置正确 (如有)
- 响应头包含 X-AIMC-* (可选, 用于验证路由层)

用法:
    # 在 Ubuntu VM 上跑 (假设服务在 0.0.0.0:8080)
    python scripts/deploy/verify_clients.py
    python scripts/deploy/verify_clients.py --host 192.168.1.100 --port 8080
"""
import argparse
import json
import socket
import sys
import urllib.request
import urllib.error


def get_local_lan_ip() -> str:
    """自动探测本机非 127.0.0.1 的 LAN IP (零依赖, UDP socket 绕路拿地址)

    Returns:
        str 第一个非 127.0.0.1 的网卡 IP, 探测失败返回空串
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 不真发包, 只是让 OS 选路由接口
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()


def check_endpoint(name: str, base_url: str, api_key: str = "") -> dict:
    """单端点连通性测试"""
    result = {
        "name": name,
        "base_url": base_url,
        "http_ok": False,
        "http_code": 0,
        "models_count": 0,
        "response_time_ms": 0,
        "error": None,
        "sample_model": None,
    }

    import time
    start = time.time()
    url = f"{base_url.rstrip('/')}/models"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result["http_code"] = resp.getcode()
            result["http_ok"] = resp.getcode() == 200
            body = json.loads(resp.read().decode("utf-8"))

            # AIMC 兼容 OpenAI /v1/models 格式
            if "data" in body and isinstance(body["data"], list):
                result["models_count"] = len(body["data"])
                if body["data"]:
                    result["sample_model"] = body["data"][0].get("id", "?")
                result["http_ok"] = result["http_ok"] and result["models_count"] > 0
            else:
                result["error"] = f"响应格式异常: keys={list(body.keys())}"

            # 检查 AIMC 响应头 (如果有)
            actual_model = resp.headers.get("X-AIMC-Actual-Model")
            if actual_model:
                result["actual_model"] = actual_model

    except urllib.error.HTTPError as e:
        result["http_code"] = e.code
        if e.code == 401:
            result["error"] = "401 Unauthorized — 检查 AIMC_API_KEY"
        elif e.code == 403:
            result["error"] = "403 Forbidden — 网关拒绝"
        else:
            result["error"] = f"HTTP {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        result["error"] = f"连接失败: {e.reason}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        result["response_time_ms"] = int((time.time() - start) * 1000)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 v4.3 多客户端连通性验证")
    parser.add_argument("--host", default="127.0.0.1", help="AIMC 服务 host (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="AIMC 服务 port (默认 8080)")
    parser.add_argument("--api-key", default="", help="AIMC_API_KEY (如配置)")
    parser.add_argument("--skip-lan", action="store_true", help="跳过 LAN 客户端测试")
    args = parser.parse_args()

    print("=== Phase 4 v4.3 — 多客户端连通性验证 ===\n")
    print(f"目标: {args.host}:{args.port}")
    print(f"鉴权: {'已配置' if args.api_key else '未启用 (AIMC_API_KEY 空)'}")
    print()

    endpoints = [
        ("localhost (本机)", f"http://127.0.0.1:{args.port}/v1", args.api_key),
        ("0.0.0.0 绑定", f"http://{args.host}:{args.port}/v1", args.api_key),
    ]

    # LAN 测试 (v4.3.9: 自动探测本机真实 LAN IP, 不再写死 192.168.1.x)
    if not args.skip_lan:
        lan_ip = get_local_lan_ip()
        if lan_ip and lan_ip != "127.0.0.1":
            endpoints.append((f"LAN (autodetect: {lan_ip})", f"http://{lan_ip}:{args.port}/v1", args.api_key))
        elif args.host not in ("127.0.0.1", "localhost"):
            # 探测失败但 --host 提供了 IP, 退回用 --host
            endpoints.append((f"LAN (--host 兜底)", f"http://{args.host}:{args.port}/v1", args.api_key))
        else:
            # 都拿不到, 提示用户手动跳过或 --lan-host 覆盖
            print(f"  [.] LAN 跳过 (探测不到本机 IP, 用 --lan-host <ip> 手动指定)")

    results = []
    for name, url, key in endpoints:
        r = check_endpoint(name, url, key)
        results.append(r)
        icon = "✓" if r["http_ok"] else "✗"
        line = f"  [{icon}] {name:30s} {r['http_code']:>3d} {r['models_count']:>4d} models  {r['response_time_ms']:>5d}ms"
        if r.get("sample_model"):
            line += f"  (sample: {r['sample_model']})"
        if r["error"]:
            line += f"  ⚠ {r['error'][:60]}"
        print(line)

    # 汇总
    print()
    print("=== 汇总 ===")
    ok = sum(1 for r in results if r["http_ok"])
    print(f"  通过: {ok}/{len(results)}")
    if ok == 0:
        print("\n[FAIL] 所有端点都不通 — 检查:")
        print("  1. 服务是否启: systemctl --user status aimc")
        print("  2. 端口监听: ss -tlnp | grep 8080")
        print("  3. 防火墙: sudo ufw allow 8080/tcp (如启用)")
        return 1
    elif ok < len(results):
        print(f"\n[WARN] 部分端点不通 — 检查网络/firewall/API key 配置")
        return 0
    else:
        print("\n[OK] 所有客户端连通正常")
        return 0


if __name__ == "__main__":
    sys.exit(main())