"""AIMC 全局配置模块

基于 pydantic-settings 从环境变量 / .env 文件加载配置。
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置项，优先级：环境变量 > .env 文件 > 默认值"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === 服务配置 ===
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    # === 数据库 ===
    db_path: str = "./data/aimc.db"

    # === 网关鉴权 ===
    api_key: str = ""  # 留空则不鉴权

    # === AI 解析引擎 ===
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    parse_model: str = "gpt-4o-mini"

    # === 告警通道（Phase 试用简化）===
    # alert_channel:
    #   "log"        - 只 log（默认，dry-run，不发任何东西）
    #   "bark"       - iOS Bark（国内首选）
    #   "telegram"   - Telegram bot（需翻墙）
    #   "serverchan" - Server 酱（推微信）
    alert_channel: str = "log"
    bark_key: str = ""  # Bark device key（iPhone app 注册时给）
    bark_server: str = ""  # 自建 Bark server URL（留空用官方 https://api.day.app）
    serverchan_key: str = ""  # Server 酱 SendKey
    feishu_webhook_url: str = ""  # 飞书自定义机器人 webhook URL
    # === Telegram 告警（已废弃，国内不实用；保留字段避免破坏旧 .env）===
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # === 代理设置 ===
    proxy_url: str = ""

    # === 路由配置 ===
    route_refresh_interval: int = 300  # 价格缓存刷新间隔（秒）
    max_fallback_retries: int = 2  # 故障转移最大重试次数
    request_timeout: float = 120.0  # 上游请求超时（秒）

    # === 情报采集 ===
    crawl_interval: int = 900  # 默认爬取间隔（秒），15分钟
    diff_similarity_threshold: float = 0.9  # 相似度阈值，低于此值触发解析
    parse_confidence_threshold: float = 0.8  # 自愈置信度阈值
    crawl_enabled: bool = True  # 总开关：False = 跳过所有采集（试用阶段省心）

    @property
    def db_url(self) -> str:
        """SQLAlchemy 异步连接 URL"""
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{db_file}"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)


# 全局单例
settings = Settings()
