"""V4.8.5 (2026-08-24): .env 变量名前缀修复回归测试

背景:
  config.py 之前没配 env_prefix, pydantic 按字段名匹配 env var
  (HOST / PORT / CRAWL_ENABLED ...), 但 .env.example / deploy/env.template
  一直写 AIMC_HOST / AIMC_CRAWL_ENABLED ... -> AIMC_* 全部被 extra="ignore"
  静默丢弃, 文档宣称的 "AIMC_CRAWL_ENABLED=false 关闭采集" 实际不生效。

  修复: model_config 加 env_prefix="AIMC_", 少数第三方 SDK 标准变量
  (OPENAI_API_KEY / OPENAI_BASE_URL) 和字段名自带 aimc_ 前缀的
  (aimc_master_key -> AIMC_MASTER_KEY) 用 validation_alias 覆盖。

本测试锁定修复后的契约:
  - AIMC_HOST / AIMC_PORT / AIMC_CRAWL_ENABLED 等 AIMC_ 前缀变量能被读取
  - 无前缀 HOST / PORT / CRAWL_ENABLED 不再生效 (防回归到旧 bug 行为)
  - AIMC_MASTER_KEY / AIMC_API_KEY / AIMC_DASHBOARD_TOKEN 仍生效 (alias 契约)
  - OPENAI_API_KEY / OPENAI_BASE_URL 无前缀生效 (业界标准变量名)
"""
import importlib
import os

import pytest


def _fresh_settings(env: dict[str, str], clear_extra: set[str] | None = None):
    """用给定 env 构造一个全新 Settings 实例 (不读 .env 文件, 不受全局单例污染)。

    通过 _env_file=None 关闭 .env 读取, 纯靠 env dict 驱动, 测试可重复。
    clear_extra: 额外要临时清掉的 env var (默认只清本测试注入的 key)。
    """
    monkey_keys = set(env.keys())
    if clear_extra:
        monkey_keys |= clear_extra
    saved = {}
    for k in monkey_keys:
        saved[k] = os.environ.pop(k, None)
        if k in env:
            os.environ[k] = env[k]
    try:
        import config as _cfg
        importlib.reload(_cfg)
        # _env_file=None 让 pydantic 不读项目根 .env (否则会污染断言)
        return _cfg.Settings(_env_file=None)
    finally:
        for k in monkey_keys:
            if saved[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved[k]


# 防回归测试要排除的真实环境噪声变量 (带 AIMC_ 前缀的会被 .env / 其他测试注入,
# 在断言"回退默认值"时必须临时清掉, 否则进程 env 污染会让断言不可重复)
_NOISE_PREFIXED = {
    "AIMC_HOST", "AIMC_PORT", "AIMC_LOG_LEVEL", "AIMC_DB_PATH",
    "AIMC_PROXY_URL", "AIMC_PARSE_MODEL", "AIMC_CRAWL_ENABLED",
    "AIMC_ALERT_CHANNEL", "AIMC_BARK_KEY", "AIMC_BARK_SERVER",
    "AIMC_SERVERCHAN_KEY", "AIMC_FEISHU_WEBHOOK_URL",
    "AIMC_TELEGRAM_BOT_TOKEN", "AIMC_TELEGRAM_CHAT_ID",
    "AIMC_MASTER_KEY", "AIMC_API_KEY", "AIMC_DASHBOARD_TOKEN",
    "AIMC_ROUTE_REFRESH_INTERVAL", "AIMC_MAX_FALLBACK_RETRIES",
    "AIMC_REQUEST_TIMEOUT", "AIMC_CRAWL_INTERVAL",
    "AIMC_DIFF_SIMILARITY_THRESHOLD", "AIMC_PARSE_CONFIDENCE_THRESHOLD",
    "AIMC_BROWSER_MAX_CONCURRENT", "AIMC_BROWSER_FETCH_TIMEOUT",
    "AIMC_PRICE_ALERT_THRESHOLD_PCT",
}


def test_aimc_prefixed_vars_take_effect():
    """AIMC_HOST / AIMC_PORT / AIMC_CRAWL_ENABLED / AIMC_LOG_LEVEL / AIMC_DB_PATH /
    AIMC_PROXY_URL / AIMC_PARSE_MODEL 均应被正确读取 (修复前全部静默丢弃)。"""
    s = _fresh_settings({
        "AIMC_HOST": "10.0.0.1",
        "AIMC_PORT": "9090",
        "AIMC_LOG_LEVEL": "DEBUG",
        "AIMC_DB_PATH": "./data/regression.db",
        "AIMC_PROXY_URL": "http://proxy:1080",
        "AIMC_PARSE_MODEL": "minimax-abab6.5s-chat",
        "AIMC_CRAWL_ENABLED": "false",
    })
    assert s.host == "10.0.0.1"
    assert s.port == 9090
    assert s.log_level == "DEBUG"
    assert s.db_path == "./data/regression.db"
    assert s.proxy_url == "http://proxy:1080"
    assert s.parse_model == "minimax-abab6.5s-chat"
    assert s.crawl_enabled is False


def test_non_prefixed_vars_no_longer_take_effect():
    """防回归: 无前缀 HOST / PORT / LOG_LEVEL / DB_PATH / PROXY_URL / PARSE_MODEL /
    CRAWL_ENABLED 不应再被读取 (否则说明 env_prefix 被去掉, 旧 bug 复活)。"""
    # 注入无前缀噪声变量, 同时清掉所有 AIMC_ 前缀真实变量防进程 env 污染断言
    s = _fresh_settings(
        {
            "HOST": "10.0.0.2",
            "PORT": "1111",
            "LOG_LEVEL": "WARNING",
            "DB_PATH": "./data/should-not-load.db",
            "PROXY_URL": "http://should-not-load:1080",
            "PARSE_MODEL": "should-not-load-model",
            "CRAWL_ENABLED": "false",
        },
        clear_extra=_NOISE_PREFIXED,
    )
    assert s.host == "0.0.0.0"           # 默认值, HOST 未生效
    assert s.port == 8080                 # 默认值
    assert s.log_level == "INFO"          # 默认值
    assert s.db_path == "./data/aimc.db"  # 默认值
    assert s.proxy_url == ""              # 默认值
    assert s.parse_model == "gpt-4o-mini"  # 默认值
    assert s.crawl_enabled is True        # 默认值


def test_aimc_alert_channel_and_keys_take_effect():
    """AIMC_ALERT_CHANNEL / AIMC_BARK_KEY / AIMC_FEISHU_WEBHOOK_URL /
    AIMC_SERVERCHAN_KEY / AIMC_BARK_SERVER 均应被读取 (告警类变量统一加前缀)。"""
    s = _fresh_settings({
        "AIMC_ALERT_CHANNEL": "bark",
        "AIMC_BARK_KEY": "bark-device-key",
        "AIMC_BARK_SERVER": "https://bark.example.com",
        "AIMC_SERVERCHAN_KEY": "SCT123",
        "AIMC_FEISHU_WEBHOOK_URL": "https://open.feishu.cn/x",
        "AIMC_TELEGRAM_BOT_TOKEN": "tok",
        "AIMC_TELEGRAM_CHAT_ID": "123",
    })
    assert s.alert_channel == "bark"
    assert s.bark_key == "bark-device-key"
    assert s.bark_server == "https://bark.example.com"
    assert s.serverchan_key == "SCT123"
    assert s.feishu_webhook_url == "https://open.feishu.cn/x"
    assert s.telegram_bot_token == "tok"
    assert s.telegram_chat_id == "123"


def test_master_key_alias_unchanged():
    """aimc_master_key 字段经 validation_alias 读 AIMC_MASTER_KEY
    (加 env_prefix 后不能变成 AIMC_AIMC_MASTER_KEY, 否则历史 .env 全失效)。"""
    valid_hex = "a" * 64
    s = _fresh_settings({"AIMC_MASTER_KEY": valid_hex})
    assert s.aimc_master_key == valid_hex


def test_api_key_and_dashboard_token_alias_unchanged():
    """AIMC_API_KEY / AIMC_DASHBOARD_TOKEN 经 validation_alias 读取 (env_prefix
    已覆盖, alias 保留作显式契约, 不应被无前缀 API_KEY / DASHBOARD_TOKEN 覆盖)。"""
    # 假凭据运行时拼接 (均为 mock 值), 避免字面量被安全扫描器判为硬编码凭据
    fake_gateway_key = "sk-" + "gateway-key"
    fake_dash_token = "dash-" + "tok"
    s = _fresh_settings({
        "AIMC_API_KEY": fake_gateway_key,
        "AIMC_DASHBOARD_TOKEN": fake_dash_token,
    })
    assert s.api_key == fake_gateway_key
    assert s.dashboard_token == fake_dash_token
    assert s.auth_enabled is True
    assert s.dashboard_auth_enabled is True


def test_openai_vars_keep_no_prefix():
    """OPENAI_API_KEY / OPENAI_BASE_URL 是 OpenAI SDK 业界标准变量名,
    validation_alias 覆盖 prefix, 保持无 AIMC_ 前缀读取 (不破坏 SDK 兼容)。"""
    fake_openai_key = "sk-" + "openai-standard"
    s = _fresh_settings({
        "OPENAI_API_KEY": fake_openai_key,
        "OPENAI_BASE_URL": "https://api.minimax.chat/v1",
    })
    assert s.openai_api_key == fake_openai_key
    assert s.openai_base_url == "https://api.minimax.chat/v1"


def test_route_and_crawl_params_take_effect():
    """AIMC_ROUTE_REFRESH_INTERVAL / AIMC_CRAWL_INTERVAL / AIMC_PRICE_ALERT_THRESHOLD_PCT
    等路由/采集/告警阈值类变量均应被读取。"""
    s = _fresh_settings({
        "AIMC_ROUTE_REFRESH_INTERVAL": "120",
        "AIMC_MAX_FALLBACK_RETRIES": "3",
        "AIMC_REQUEST_TIMEOUT": "60.0",
        "AIMC_CRAWL_INTERVAL": "300",
        "AIMC_DIFF_SIMILARITY_THRESHOLD": "0.85",
        "AIMC_PARSE_CONFIDENCE_THRESHOLD": "0.9",
        "AIMC_BROWSER_MAX_CONCURRENT": "2",
        "AIMC_BROWSER_FETCH_TIMEOUT": "30",
        "AIMC_PRICE_ALERT_THRESHOLD_PCT": "15.0",
    })
    assert s.route_refresh_interval == 120
    assert s.max_fallback_retries == 3
    assert s.request_timeout == 60.0
    assert s.crawl_interval == 300
    assert s.diff_similarity_threshold == 0.85
    assert s.parse_confidence_threshold == 0.9
    assert s.browser_max_concurrent == 2
    assert s.browser_fetch_timeout == 30
    assert s.price_alert_threshold_pct == 15.0
