"""AIMC 全局配置模块

基于 pydantic-settings 从环境变量 / .env 文件加载配置。
"""

import logging
import re
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, Field

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """全局配置项，优先级：环境变量 > .env 文件 > 默认值"""

    # env_prefix="AIMC_" 统一前缀 (2026-08-24 修复 .env 变量名不生效 bug):
    # 之前 .env.example / deploy/env.template 写 AIMC_HOST / AIMC_CRAWL_ENABLED 等,
    # 但 config 没 prefix -> pydantic 按 HOST / CRAWL_ENABLED 匹配 -> AIMC_* 全部静默丢弃。
    # 加 prefix 后 AIMC_HOST / AIMC_PORT / AIMC_CRAWL_ENABLED 等全部正确生效。
    # 例外 (validation_alias 覆盖 prefix, 保持业界标准变量名):
    #   - aimc_master_key -> AIMC_MASTER_KEY (字段名自带 aimc_ 前缀, 加 prefix 会变 AIMC_AIMC_MASTER_KEY)
    #   - api_key / dashboard_token -> AIMC_API_KEY / AIMC_DASHBOARD_TOKEN (prefix 已覆盖, alias 保留显式)
    #   - openai_api_key / openai_base_url -> OPENAI_API_KEY / OPENAI_BASE_URL (OpenAI SDK 业界标准, 不加前缀)
    model_config = SettingsConfigDict(
        env_prefix="AIMC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === 服务配置 ===
    host: str = "0.0.0.0"          # env: AIMC_HOST
    # 默认端口 18080 (2026-10-13): 8080 与各类代理/开发工具高频冲突;
    # 老部署在 .env 显式 AIMC_PORT 的不受默认值变化影响
    port: int = 18080              # env: AIMC_PORT
    log_level: str = "INFO"        # env: AIMC_LOG_LEVEL

    # === 加密 (API-Sec-1, 2026-07-29) ===
    # Fernet master key (44 字符 url-safe base64, 32 bytes 原始)
    # 生成: python scripts/gen_master_key.py
    # 丢了 = 全部 ciphertext 不可解 = 永久性数据丢失, 务必存到密码管理器
    # env: AIMC_MASTER_KEY (validation_alias 覆盖 prefix, 避免变 AIMC_AIMC_MASTER_KEY)
    aimc_master_key: str = Field(default="", validation_alias="AIMC_MASTER_KEY")

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
    db_path: str = "./data/aimc.db"   # env: AIMC_DB_PATH

    # === 网关鉴权 ===
    # BUGFIX (2026-08-17): 加 validation_alias 强制读取 AIMC_API_KEY env var
    # 之前 Pydantic 默认按字段名匹配, 期望 env var "API_KEY" (而不是 "AIMC_API_KEY")
    # 导致 .env 里设了 AIMC_API_KEY 也读不到 -> api_key="" -> auth_enabled=False
    # -> 任意 client 都能无鉴权访问 /v1/* (鉴权旁路)
    # V4.8.5 (2026-08-24): env_prefix="AIMC_" 已覆盖此字段, alias 保留作为显式契约
    # env: AIMC_API_KEY
    api_key: str = Field(default="", validation_alias="AIMC_API_KEY")  # 留空则不鉴权 (/v1/* OpenAI/Anthropic API)

    # P1-7 (2026-08-14): dashboard 鉴权 (跟 api_key 分开, 独立管理)
    # - 未设 = 维持 legacy 行为 (LAN 内裸开, 跟你 brief 里的"已知遗留"一致)
    # - 设了 = /dashboard/* 路径需要 token 校验 (header 或 ?token=)
    # - 跟 api_key 分开的原因: dashboard 数据敏感 (账号/限额/优惠), 应该独立管理
    # env: AIMC_DASHBOARD_TOKEN (env_prefix 已覆盖, alias 保留作为显式契约)
    dashboard_token: str = Field(default="", validation_alias="AIMC_DASHBOARD_TOKEN")

    @property
    def dashboard_auth_enabled(self) -> bool:
        """P1-7: dashboard 是否启用鉴权"""
        return bool(self.dashboard_token)

    # === AI 解析引擎 ===
    # validation_alias 覆盖 prefix: OPENAI_API_KEY / OPENAI_BASE_URL 是 OpenAI SDK 业界标准变量名,
    # 加 AIMC_ 前缀会破坏兼容性 (SDK / 用户肌肉记忆都期望无前缀)
    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", validation_alias="OPENAI_BASE_URL")
    # parse_model 默认走自调用网关的 balanced 组 (OPENAI_API_KEY 留空时生效, parser.py 两档逻辑);
    # 可填其他 tier: 组名或具体模型名; 配了 OPENAI_API_KEY 则按外部模型解析
    parse_model: str = "tier:balanced"   # env: AIMC_PARSE_MODEL

    # === 告警通道（Phase 试用简化）===
    # alert_channel:
    #   "log"        - 只 log（默认，dry-run，不发任何东西）
    #   "bark"       - iOS Bark（国内首选）
    #   "telegram"   - Telegram bot（需翻墙）
    #   "serverchan" - Server 酱（推微信）
    alert_channel: str = "log"         # env: AIMC_ALERT_CHANNEL
    bark_key: str = ""                 # env: AIMC_BARK_KEY (Bark device key)
    bark_server: str = ""              # env: AIMC_BARK_SERVER (留空用官方 https://api.day.app)
    serverchan_key: str = ""           # env: AIMC_SERVERCHAN_KEY (Server 酱 SendKey)
    feishu_webhook_url: str = ""       # env: AIMC_FEISHU_WEBHOOK_URL
    # === Telegram 告警（已废弃，国内不实用；保留字段避免破坏旧 .env）===
    telegram_bot_token: str = ""       # env: AIMC_TELEGRAM_BOT_TOKEN
    telegram_chat_id: str = ""         # env: AIMC_TELEGRAM_CHAT_ID

    # === 代理设置 ===
    proxy_url: str = ""                # env: AIMC_PROXY_URL

    # === 路由配置 ===
    route_refresh_interval: int = 300  # env: AIMC_ROUTE_REFRESH_INTERVAL (价格缓存刷新间隔, 秒)
    max_fallback_retries: int = 2      # env: AIMC_MAX_FALLBACK_RETRIES (故障转移最大重试次数)
    request_timeout: float = 120.0     # env: AIMC_REQUEST_TIMEOUT (上游请求超时, 秒)

    # === V4.9.3 (2026-08-25): 缓存诊断 A/B + 请求转录 ===
    # prompt_cache_inject_enabled: 全局开关 prompt cache 注入 (不影响 channel.supports_prompt_cache)
    #   False = 即使 channel 开了 supports_prompt_cache 也不注入, 用于 A/B 对比 "缓存是否降智"
    prompt_cache_inject_enabled: bool = True    # env: AIMC_PROMPT_CACHE_INJECT_ENABLED
    # transcript_log_enabled: 记录请求/响应转录到 request_transcripts 表 (默认关, 诊断时开)
    transcript_log_enabled: bool = False        # env: AIMC_TRANSCRIPT_LOG_ENABLED
    # transcript_log_max_body: 单条 body 截断上限 (字符数, 防超大 payload 膨胀 DB)
    transcript_log_max_body: int = 50000        # env: AIMC_TRANSCRIPT_LOG_MAX_BODY
    # transcript_log_retention_hours: 转录保留时长 (小时, 超期自动清理)
    transcript_log_retention_hours: int = 72    # env: AIMC_TRANSCRIPT_LOG_RETENTION_HOURS

    # === V4.9 (2026-08-25): Channel 智能探活 ===
    health_probe_enabled: bool = True                  # env: AIMC_HEALTH_PROBE_ENABLED (总开关)
    health_probe_interval: int = 60                    # env: AIMC_HEALTH_PROBE_INTERVAL (调度间隔, 秒; 实际只探到期的)
    health_probe_concurrency: int = 8                  # env: AIMC_HEALTH_PROBE_CONCURRENCY (并发数, 防打爆上游)
    health_probe_timeout: float = 5.0                  # env: AIMC_HEALTH_PROBE_TIMEOUT (单 channel 超时, 秒)
    health_probe_max_backoff: int = 3600               # env: AIMC_HEALTH_PROBE_MAX_BACKOFF (指数退避上限, 秒)
    # V4.9.1 防护: 防止「雪崩」 — 上限每 job 探活数 + 总超时 (防止网络全挂时积压上千个待探活)
    health_probe_max_batch: int = 50                   # env: AIMC_HEALTH_PROBE_MAX_BATCH (单 job 最多探活数)
    health_probe_total_timeout: float = 60.0           # env: AIMC_HEALTH_PROBE_TOTAL_TIMEOUT (job 总超时, 秒)
    # V4.9.1 防护 #3: account_health_log 保留天数 (防表无限增长)
    account_health_log_retention_days: int = 7         # env: AIMC_ACCOUNT_HEALTH_LOG_RETENTION_DAYS

    # === V6.15 (2026-09-28): 配额桶恢复探测 (quota_exhausted 感知上游重置) ===
    # 背景: 探活 GET /models 零 token, 感知不到配额恢复 — 生产实证 glm_01 5h 池
    # 上游 16:44 重置, 固定 1h 冷却空窗 28 分钟; 用户后台手动重置更无法感知。
    # 本探测对隔离中的 (account, model) 桶发 1-token 真聊天, 200 即清桶隔离。
    # 消息级探测有成本 (modelscope 免费次数用完为止), 节拍从 initial 起指数退避。
    quota_recovery_probe_enabled: bool = True          # env: AIMC_QUOTA_RECOVERY_PROBE_ENABLED (总开关)
    quota_recovery_probe_initial_seconds: int = 300    # env: AIMC_QUOTA_RECOVERY_PROBE_INITIAL (首次探测延迟, 秒)
    quota_recovery_probe_max_seconds: int = 1800       # env: AIMC_QUOTA_RECOVERY_PROBE_MAX (退避上限, 秒)

    # === V6.6.x (2026-09-16): 路由体量准入 ===
    # 渠道名 -> 单请求估算输入 token 的适配窗上/下界。窗口外的候选在频限+配额过滤
    # 之后剔除; 全部候选都在窗外则保留原候选 (宁试一次也不 503)。
    # 依据: sensenova 免费公测池=共享吞吐, 大请求基本必拒、小请求可过 → max 挡大;
    # taotoken 按**调用次数**计费 (~2000 次/5h), 小请求烧次数不划算 → min 挡小。
    # 即: 小请求优先 sensenova, 大请求走 taotoken; 一方不可用时另一方兜底。
    # modelscope_inference max (2026-10-25): 免费档吞吐慢 — 6-12k 输入成功也要
    # 80-174s, 130-153s 后上游网关 504 (生产 10-25 实录 2 例, 每例浪费一轮等待后
    # failover 自愈)。>30k 大请求直接走 glm_bigmodel/taotoken, 与 sensenova 同窗。
    # 估算口径见 gateway/router.py:_estimate_input_tokens (~3 字符/token, 图片 1000/张)。
    # env: AIMC_CHANNEL_MAX_INPUT_TOKENS / AIMC_CHANNEL_MIN_INPUT_TOKENS (JSON)
    channel_max_input_tokens: dict = Field(
        default_factory=lambda: {
            "sensenova_official": 30000,
            "modelscope_inference": 30000,
        },
    )
    channel_min_input_tokens: dict = Field(
        default_factory=lambda: {"taotoken_coding": 30000},
    )

    # === V4.9: needs_human 触发阈值 (按 ErrorCategory) ===
    # 当某 error_type 累计失败次数 >= 此阈值时, 标 needs_human=True (等人介入, 不再主动探)
    needs_human_quota_threshold: int = 2               # env: AIMC_NEEDS_HUMAN_QUOTA_THRESHOLD (quota 失败 2 次 → 需人工)
    needs_human_auth_threshold: int = 1                # env: AIMC_NEEDS_HUMAN_AUTH_THRESHOLD (auth 失败 1 次 → 需人工)
    needs_human_model_threshold: int = 2               # env: AIMC_NEEDS_HUMAN_MODEL_THRESHOLD (model 失败 2 次 → 需人工)

    # === 情报采集 ===
    crawl_interval: int = 900          # env: AIMC_CRAWL_INTERVAL (默认爬取间隔, 秒, 15分钟)
    diff_similarity_threshold: float = 0.9  # env: AIMC_DIFF_SIMILARITY_THRESHOLD (低于此值触发解析)
    parse_confidence_threshold: float = 0.8  # env: AIMC_PARSE_CONFIDENCE_THRESHOLD (自愈置信度阈值)
    crawl_enabled: bool = True         # env: AIMC_CRAWL_ENABLED (总开关: False = 跳过所有采集)
    # V4.8.4: Playwright 进程回收防护
    browser_max_concurrent: int = 4    # env: AIMC_BROWSER_MAX_CONCURRENT (并发 page context 上限)
    browser_fetch_timeout: int = 60    # env: AIMC_BROWSER_FETCH_TIMEOUT (单次 fetch 整体超时, 秒)

    # === 价格波动告警 (Phase 4 E2, 2026-07-29) ===
    # input_price 或 output_price 任一方向变动 ≥ 此百分比即触发告警
    # 可被 SettingsRuntime 表 key="price_alert_threshold" 覆盖 (无需重启)
    price_alert_threshold_pct: float = 30.0  # env: AIMC_PRICE_ALERT_THRESHOLD_PCT

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
