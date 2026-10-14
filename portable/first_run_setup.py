"""AIMC Portable 启动引导 (幂等, 每次启动都跑)

职责:
- 确保目录存在 (data/ reports/)
- 首次运行生成 .env: 绑定 127.0.0.1 (仅本机可访问) + 随机 master key +
  随机网关 key; 看板鉴权留空 (便携版默认仅本机开放)
- 往 stdout 打印且只打印一行 dashboard URL (start-aimc.bat 用它打开浏览器)

诊断信息一律走 stderr, 不污染 stdout。
"""
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ENV_PATH = ROOT / ".env"


def _generate_env() -> None:
    # master key 优先复用项目自带的生成器 (security/crypto.py);
    # 导入失败时退化为 os.urandom, 格式同样是 64 字符 hex
    try:
        from security.crypto import generate_master_key
        master_key = generate_master_key()
    except Exception:
        import os
        master_key = os.urandom(32).hex()
    api_key = "aimc-" + secrets.token_urlsafe(24)
    lines = [
        "# AIMC Portable 首次启动自动生成 (2026-10)",
        "# 修改后重启 start-aimc.bat 生效",
        "AIMC_HOST=127.0.0.1",
        "AIMC_PORT=18080",
        "AIMC_LOG_LEVEL=INFO",
        "",
        "# 账号加密主密钥 (丢失 = 已存渠道 key 无法解密, 请备份本文件)",
        f"AIMC_MASTER_KEY={master_key}",
        "",
        "# 网关 API key (客户端调用 /v1/* 时使用)",
        f"AIMC_API_KEY={api_key}",
        "",
        "# 看板鉴权留空 = 仅本机可访问 (便携版默认, HOST 已绑 127.0.0.1)",
        "AIMC_DASHBOARD_TOKEN=",
        "AIMC_DB_PATH=./data/aimc.db",
        "",
        "# 渠道 key 不填这里 — 启动后在看板「初始化配置」页可视化添加",
        "",
        "AIMC_CRAWL_ENABLED=true",
    ]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_env_value(key: str, default: str) -> str:
    try:
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip() or default
    except OSError:
        pass
    return default


def main() -> None:
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "reports").mkdir(exist_ok=True)
    if not ENV_PATH.exists():
        _generate_env()
        print("[first-run] .env 已生成 (master key / 网关 key 均为随机值)", file=sys.stderr)
    host = _read_env_value("AIMC_HOST", "127.0.0.1")
    if host == "0.0.0.0":
        host = "127.0.0.1"  # 打开浏览器永远走本机回环
    port = _read_env_value("AIMC_PORT", "18080")
    print(f"http://{host}:{port}/dashboard/")


if __name__ == "__main__":
    main()
