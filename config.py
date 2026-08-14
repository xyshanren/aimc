"""AIMC 全局配置模块

基于 pydantic-settings 从环境变量 / .env 文件加载配置。
"""

import logging
import re
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator

logger = logging.getLogger(__name__)


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

    # === 加密 (API-Sec-1, 2026-07-29) ===
    # Fernet master key (44 字符 url-safe base64, 32 bytes 原始)
    # 生成: python scripts/gen_master_key.py
    # 丢了 = 全部 ciphertext 不可解 = 永久性数据丢失, 务必存到密码管理器
    aimc_master_key: str = ""

    @field_validator("aimc_master_key")
    @classmethod
    def _validate_master_key(cls, v: str) -> str:
        """P0-5 (2026-08-14): 启动时校验 master_key 格式, 不通过则抛错

        背景: 历史上 master key 改过但 ciphertext 没重加密, 5/6 账号解密失败
        (POST_V464_FIXES.md issue 1 的下游症状). 启动校验可以:
        - 提早发现 key 配置错 (不是 router 跑到一半才发现)
        - 校验格式 (防止把 placeholder 当真 key 用)

        校验规则 (跟 scripts/migrate_encrypt_keys.py / scripts/gen_master_key.py 一致):
        - 64 字符 hex (Fernet generate_key 返回后 hex 化, d1c7c12 升级)
        - 空字符串允许 (= 不启用加密, 但 db 里 ciphertext 必须也空)
        """
        if not v:
            return v  # 空允许 (没账号就没加密)
        if not re.match(r"^[0-9a-fA-F]{64}$", v):
            raise ValueError(
                f"AIMC_MASTER_KEY 格式错: 期望 64 字符 hex, 实际 {len(v)} 字符 "
                f"(开头 {v[:8]}...). 重新生成: python scripts/gen_master_key.py"
            )
        logger.info(
            f"[P0-5] AIMC_MASTER_KEY 校验通过 (hex 64 字符, "
            f"encryption_version=1 兼容)"
        )
        return v

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

    # === 价格波动告警 (Phase 4 E2, 2026-07-29) ===
    # input_price 或 output_price 任一方向变动 ≥ 此百分比即触发告警
    # 可被 SettingsRuntime 表 key="price_alert_threshold" 覆盖 (无需重启)
    price_alert_threshold_pct: float = 30.0

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
