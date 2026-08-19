"""SQLAlchemy ORM 模型定义

包含全部 8 张核心表：
1. channels - 渠道表
2. accounts - 账号表（多账号兜底）
3. price_snapshots - 价格快照表（峰谷时段）
4. promotions - 优惠活动表（情报核心）
5. channel_quotas - 频限配额表
6. usage_logs - 用量日志表
7. parse_rules - 解析规则表（自愈循环）
8. intel_sources - 情报采集源配置表
"""

from datetime import datetime

from sqlalchemy import (
    Column, Integer, Text, Float, DateTime, ForeignKey, LargeBinary, func,
    Index, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Channel(Base):
    """渠道表（厂商接入点）"""
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False, comment="渠道名称，如 deepseek_official")
    provider = Column(Text, nullable=False, comment="厂商标识: deepseek/qwen/glm/doubao/hunyuan")
    base_url = Column(Text, nullable=False, comment="API 基础地址")
    status = Column(Text, default="active", comment="active/disabled/error")
    priority = Column(Integer, default=0, comment="手动优先级调整，越大越优先")
    # Phase 2.3 (2026-07-29): 渠道类型 + 计费模式
    provider_type = Column(
        Text, nullable=False, server_default="official",
        comment="official(厂商官方) / third_party(第三方托管) / aggregator(聚合) / local(本地)",
    )
    billing_mode = Column(
        Text, nullable=False, server_default="pay_per_token",
        comment="pay_per_token(按量) / subscription(订阅制) / free(免费)",
    )
    # P1-12 (2026-08-14): 实际 base_url (覆盖 db.base_url, 改 dashboard 即可生效不需要 restart)
    # 优先级: db.api_base ?? db.base_url ?? SENSENOVA_BASE_URL env (router/proxy 实现)
    # 用例: dashboard 临时切到备份 base_url 不需要重启 service
    api_base = Column(
        Text, nullable=True,
        comment="实际 base_url (覆盖 db.base_url, NULL=用 db.base_url)",
    )
    # P1-12: 协议风格 — 决定 router/proxy 走 OpenAI 格式还是 Anthropic 格式
    # 后续 P1-8 用这个字段决定是否注入 cache_control headers
    api_style = Column(
        Text, nullable=True, server_default="openai",
        comment="协议风格: openai / anthropic (决定 cache_control headers 格式)",
    )
    # P1-12: 是否支持 prompt cache — router/proxy 用这个字段决定是否注入 cache headers
    # 硅基流动/DeepSeek 支持, qwen/智谱/sensenova/minimax 不支持
    supports_prompt_cache = Column(
        Integer, nullable=False, server_default="0",
        comment="0=不支持 prompt cache, 1=支持 (router 据此注入 cache headers)",
    )
    # V4.6.4+: 剥离请求里的 thinking 相关字段 (默认启用, 个人项目偏好)
    # 某些上游 (minimax deepthink) 即使不传 enable_thinking 也强制 thinking,
    # 关掉后: agent loop 更快 + token 减少
    disable_thinking_pass_through = Column(
        Integer, nullable=False, server_default="1",
        comment="0=不剥离, 1=剥离 body 里的 thinking/enable_thinking/reasoning_effort (proxy 层)",
    )
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # 关系
    accounts = relationship("Account", back_populates="channel", lazy="selectin")
    price_snapshots = relationship("PriceSnapshot", back_populates="channel", lazy="selectin")
    quotas = relationship("ChannelQuota", back_populates="channel", lazy="selectin")


class Account(Base):
    """账号表（多账号兜底）"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    account_name = Column(Text, nullable=False, comment="标识: deepseek_01/glm_02")
    api_key = Column(Text, nullable=False, comment="API Key 明文 (deprecated, 迁移期保留, 新数据写空字符串占位)")
    api_key_ciphertext = Column(LargeBinary, nullable=True, comment="API Key 密文 (Fernet, API-Sec-1)")
    status = Column(Text, default="active", comment="active/exhausted/expired/error")
    quota_total = Column(Integer, nullable=True, comment="总配额（tokens）")
    quota_used = Column(Integer, default=0, comment="已用配额")
    # API-Sec-3 (2026-07-29): key 轮换审计
    last_rotated_at = Column(DateTime, nullable=True, comment="最后轮换时间 (manual_import.py 写入)")
    expires_at = Column(DateTime, nullable=True, comment="用户自定过期时间 (NULL=无)")
    notes = Column(Text, nullable=True, comment="用户备注 (V4.5 fix: add_account.py --notes)")
    # P0-5 (2026-08-14): encryption_version 标记 ciphertext 用哪个 master key 加密
    # - 1 = 当前 AIMC_MASTER_KEY (Fernet, 64 hex chars) — 现状
    # - 2 = 未来 AIMC_MASTER_KEY_V2 — 预留, reencrypt 后用
    # 让 reencrypt_accounts.py 可以区分新旧密文, 启动时校验可定位"key 版本错"
    encryption_version = Column(
        Integer, nullable=False, server_default="1",
        comment="密文用的 master key 版本 (1=current, 2=next, ...)",
    )
    # P0-6 (2026-08-14): quota_plan 标识账户所属的 plan 类型
    # 跟 account_quotas 表联动 — 不同 plan 有不同的限额维度配置
    # 值: 'token_plan_max' (minimax 年度) / 'free_trial' (sensenova 试用) /
    #     'pay_as_you_go' (其他按量付费) — 后续可扩展
    quota_plan = Column(
        Text, nullable=True,
        comment="账户 plan 类型 (跟 account_quotas 配置联动)",
    )
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # 关系
    channel = relationship("Channel", back_populates="accounts")
    quotas = relationship("ChannelQuota", back_populates="account", lazy="selectin")

    @property
    def quota_remaining(self) -> int | None:
        if self.quota_total is None:
            return None
        return max(0, self.quota_total - (self.quota_used or 0))


class PriceSnapshot(Base):
    """价格快照表（支持峰谷时段 + 缓存价格 + 优惠期）"""
    __tablename__ = "price_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    model = Column(Text, nullable=False, comment="归一化 model_id (e.g. glm-5.2, 跟 model_registry.model_id 对齐)")
    price_type = Column(Text, default="normal", comment="normal/peak/offpeak/cache_hit/free")
    time_range = Column(Text, default="全天", comment='时段: "00:00-08:00" 或 "全天"')
    input_price = Column(Float, nullable=True, comment="输入价格（默认货币元/百万tokens）")
    output_price = Column(Float, nullable=True, comment="输出价格")
    unit = Column(Text, default="元/百万tokens")
    effective_from = Column(Text, nullable=True, comment="生效日期 (ISO date 或 datetime)")
    effective_to = Column(Text, nullable=True, comment="失效日期，NULL=长期有效")
    source = Column(Text, nullable=True, comment="来源: manual/crawler/api")
    created_at = Column(DateTime, default=func.now())

    # P0-2 (2026-08-14): 缓存价格 + 优惠期 + raw_model_id 双轨
    raw_model_id = Column(
        Text, nullable=True,
        comment="上游原始 model_id (e.g. zai-org/GLM-5.2), proxy 用这个发请求",
    )
    cached_input_price = Column(
        Float, nullable=True,
        comment="缓存命中输入价 (元/百万tokens), NULL=不支持或未定价. "
                "硅基流动/DeepSeek 等支持 prompt cache 的渠道用这个",
    )
    cache_write_price = Column(
        Float, nullable=True,
        comment="缓存写入价 (元/百万tokens), NULL=不收费. 部分渠道缓存命中免费但写入收费",
    )
    currency = Column(
        Text, nullable=False, server_default="CNY",
        comment="货币单位 (默认 CNY), 跨渠道价格比较时归一化用",
    )
    is_promotion = Column(
        Integer, nullable=False, server_default="0",
        comment="0=日常价格, 1=优惠期价格 (跟 effective_from/to 联动, 优惠期过自动失效)",
    )

    # P0-2: UNIQUE 索引 (channel_id, model) — 避免重复录入
    __table_args__ = (
        Index("uq_ps_channel_model", "channel_id", "model", unique=True),
    )

    # 关系
    channel = relationship("Channel", back_populates="price_snapshots")


class Promotion(Base):
    """优惠活动表（情报核心）"""
    __tablename__ = "promotions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(Text, nullable=False, comment="厂商标识")
    title = Column(Text, nullable=False, comment="活动标题")
    content = Column(Text, nullable=True, comment="活动摘要")
    url = Column(Text, nullable=True, comment="活动链接")
    discount_desc = Column(Text, nullable=True, comment='优惠描述: "5折"/"免费100万token"')
    start_date = Column(Text, nullable=True)
    end_date = Column(Text, nullable=True)
    status = Column(Text, default="active", comment="active/expired/upcoming")
    confidence = Column(Float, nullable=True, comment="AI 解析置信度 0-1")
    raw_data = Column(Text, nullable=True, comment="JSON 原始数据备份")
    # Phase 2D: 异常标记（2026-07-24）
    # 不阻止入库，但人工关注 - LLM 可能编造看似合理的错误优惠
    flagged = Column(Integer, default=0, comment="0=正常, 1=异常（人工确认）")
    flagged_reason = Column(Text, nullable=True, comment="异常原因: low_confidence/abnormal_discount/...")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class ChannelQuota(Base):
    """频限配额表"""
    __tablename__ = "channel_quotas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    quota_type = Column(Text, nullable=False, comment="qpm/tpm/daily/total/concurrent")
    limit_value = Column(Integer, nullable=False, comment="限制值")
    reset_strategy = Column(Text, default="rolling_window", comment="rolling_window/fixed_daily/never")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # 关系
    channel = relationship("Channel", back_populates="quotas")
    account = relationship("Account", back_populates="quotas")


class AccountQuota(Base):
    """P0-6 (2026-08-14): 多维限额定义 (一行 = 一个限额维度)

    替代 ChannelQuota 的简单 RPM/TPM 表达, 支持厂商实际的多维限额:
    - rpm / tpm: 每分钟请求数 / token 数
    - daily_tokens / weekly_tokens / 5h_tokens: 日/周/5小时窗口 token 数
    - concurrent: 并发请求数 (in-flight count)
    - balance_cny: 余额 (元, 永久, 定期 sync)

    每个 (account_id, dimension) 只允许 1 行 (UNIQUE INDEX)
    """
    __tablename__ = "account_quotas"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    dimension = Column(
        Text, nullable=False,
        comment="rpm / tpm / daily_tokens / weekly_tokens / 5h_tokens / concurrent / balance_cny",
    )
    limit_value = Column(Float, nullable=False, comment="限制值 (token 数 / 请求数 / 余额元)")
    window_seconds = Column(
        Integer, nullable=True,
        comment="窗口长度 (秒), NULL=永久 (余额) 或 N/A (rpm 并发)",
    )
    reset_strategy = Column(
        Text, nullable=False, server_default="rolling",
        comment="rolling (滑动窗口) / fixed_daily / fixed_weekly / never (永久, 余额)",
    )
    enabled = Column(Integer, nullable=False, server_default="1", comment="0=禁用")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    notes = Column(Text, nullable=True, comment="人工备注: 来源 URL / 厂商文档引用")


class AccountQuotaUsage(Base):
    """P0-6 (2026-08-14): 实时限额使用量

    (account_id, dimension) 联合主键, 一行 = 一个账户一个维度的当前窗口

    rolling window: window_start = 最早一次未超时的请求时间
    fixed_daily/weekly: window_start = 周期起点
    never (余额): used_value 不变 (单独维护, 不在 usage 表)

    router pre-check 时读 + 累加; proxy 成功后再次累加 (input + output tokens)
    """
    __tablename__ = "account_quota_usage"

    account_id = Column(Integer, ForeignKey("accounts.id"), primary_key=True, nullable=False)
    dimension = Column(Text, primary_key=True, nullable=False)
    window_start = Column(DateTime, nullable=True)
    used_value = Column(Float, nullable=False, server_default="0")
    last_updated = Column(DateTime, default=func.now(), onupdate=func.now())


class UsageLog(Base):
    """用量日志表（归档查账）"""
    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(Integer, nullable=True)
    account_id = Column(Integer, nullable=True)
    model = Column(Text, nullable=True)
    # Phase 3.A (2026-07-29): 记录请求时的 group_id
    # - tier:xxx / scene:xxx 前缀时填 group_id (e.g. "tier:balanced")
    # - 精确模型路由时为 NULL
    # - 供 Dashboard 命中率聚合查询 GROUP BY group_id, model
    group_id = Column(Text, nullable=True, comment="请求时的 group_id (NULL=精确模型路由)")
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cost_estimate = Column(Float, nullable=True, comment="估算费用（元）")
    status_code = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True, comment="响应延迟（毫秒）")
    # P0-3 follow-up (2026-08-14): prompt cache token 统计
    # - cached_input_tokens: OpenAI 风格的 cache hit tokens
    #   (usage.prompt_tokens_details.cached_tokens)
    # - cache_read_input_tokens: Anthropic 风格的 cache read tokens
    #   (usage.cache_read_input_tokens)
    # - cache_creation_input_tokens: Anthropic 风格的 cache write tokens
    #   (usage.cache_creation_input_tokens)
    # - NULL = 上游不支持 prompt cache 或 0 cache tokens
    cached_input_tokens = Column(
        Integer, nullable=True,
        comment="OpenAI-style: 缓存命中 token 数 (usage.prompt_tokens_details.cached_tokens)",
    )
    cache_read_input_tokens = Column(
        Integer, nullable=True,
        comment="Anthropic-style: 缓存读取 token 数 (usage.cache_read_input_tokens)",
    )
    cache_creation_input_tokens = Column(
        Integer, nullable=True,
        comment="Anthropic-style: 缓存写入 token 数 (usage.cache_creation_input_tokens)",
    )
    created_at = Column(DateTime, default=func.now())


class ModelFeedbackSummary(Base):
    """按周聚合的模型隐式反馈（V4.4.A）"""
    __tablename__ = "model_feedback_summary"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Text, nullable=False, comment="usage_logs.model")
    family = Column(Text, nullable=False, comment="模型族")
    period_start = Column(DateTime, nullable=False, comment="统计周期起点（周一）")
    period_end = Column(DateTime, nullable=False, comment="统计周期终点（下周一，开区间）")
    request_count = Column(Integer, nullable=False)
    success_count = Column(Integer, nullable=False)
    retry_count = Column(Integer, nullable=False)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cost_total = Column(Float, nullable=True)
    fail_rate = Column(Float, nullable=True)
    retry_rate = Column(Float, nullable=True)
    p50_latency_ms = Column(Integer, nullable=True)
    p95_latency_ms = Column(Integer, nullable=True)
    avg_output_tokens = Column(Integer, nullable=True)
    feedback_score = Column(Float, nullable=True)
    client_breakdown = Column(Text, nullable=True, comment="JSON: 按 account_id 统计请求数")
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("model_id", "period_start", name="uq_feedback_model_period"),
        Index("idx_feedback_period", "period_start"),
        Index("idx_feedback_score", "feedback_score"),
    )


class ParseRule(Base):
    """解析规则表（自愈循环支撑）"""
    __tablename__ = "parse_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(Text, nullable=False, comment="厂商标识")
    url_pattern = Column(Text, nullable=True, comment="URL 匹配模式")
    css_selector = Column(Text, nullable=True, comment="CSS 选择器")
    ai_strategy = Column(Text, nullable=True, comment="AI 解析策略描述")
    confidence = Column(Float, nullable=True, comment="规则置信度")
    status = Column(Text, default="active", comment="active/failed/manual")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class IntelSource(Base):
    """情报采集源配置表"""
    __tablename__ = "intel_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(Text, nullable=False, comment="厂商标识")
    source_type = Column(Text, nullable=False, comment="web_page/api_doc/announcement")
    url = Column(Text, nullable=False, comment="采集目标 URL")
    poll_interval = Column(Integer, default=900, comment="轮询间隔（秒），默认15分钟")
    status = Column(Text, default="active", comment="active/error/disabled")
    last_check_at = Column(DateTime, nullable=True)
    last_hash = Column(Text, nullable=True, comment="上次内容哈希（Diff 用）")
    last_content = Column(Text, nullable=True, comment="上次完整内容（截断 50KB，用于相似度计算）")
    error_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=func.now())


class ModelCard(Base):
    """模型评测卡 (Phase 2D-4, 2026-07-24)

    静态引用公开 benchmark 分数（OpenCompass / LMSYS Arena），
    让 dashboard / 路由决策有依据。

    设计哲学：
    - 不自己做 benchmark（成本高、个人没必要）
    - 引用第三方公开数据，标 source 即可
    - 路由时按 score + tier 选模型
    """
    __tablename__ = "model_cards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(Text, nullable=False, comment="模型名 (e.g. deepseek-chat)")
    provider = Column(Text, nullable=False, comment="厂商标识")
    benchmark = Column(Text, nullable=False, comment="benchmark 来源 (OpenCompass 2024-Q3)")
    score = Column(Float, nullable=True, comment="benchmark 分数 (0-100)")
    tier = Column(Text, nullable=True, comment="适配场景: code/chinese/general/multimodal")
    source_url = Column(Text, nullable=True, comment="benchmark 数据来源链接")
    notes = Column(Text, nullable=True, comment="备注 (e.g. 'snapshot 2024-10')")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class ModelRegistry(Base):
    """模型注册表 (Phase 2.1, 2026-07-29)

    存模型的 **路由元信息**（tier / capabilities / context_length），
    跟 model_cards（存 benchmark 分数）双轨，model_id 字符串 soft join。

    设计：
    - tier/capabilities 是路由"硬"字段（决策依据）
    - benchmark_scores 仅 Dashboard 展示用，不参与路由
    - capabilities 是 JSON 字符串，SQLAlchemy 不解析，应用层 json.loads
    - model_id 规范: {family}-{version}[-{variant}]，如 deepseek-v4-pro

    P0-2 (2026-08-14): 加 raw_ids JSON + supports_prompt_cache
    - raw_ids: 各渠道上游原始 model_id 映射, proxy 用这个反查发请求
      (e.g. {"siliconflow": "zai-org/GLM-5.2", "deepseek": "glm-5.2"})
    - supports_prompt_cache: 是否支持 prompt cache (0/1), router/proxy
      看到这个字段才注入 cache_control headers
    """
    __tablename__ = "model_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Text, nullable=False, unique=True, comment="规范ID: deepseek-v4-pro / qwen3-235b")
    family = Column(Text, nullable=False, comment="模型族: deepseek/qwen/glm/kimi")
    display_name = Column(Text, nullable=True, comment="展示名: DeepSeek V4 Pro")
    tier = Column(Text, nullable=False, default="T2", comment="能力档: T0/T1/T2/T3/T4")
    capabilities = Column(Text, nullable=True, comment='JSON数组: ["reasoning","code","math"]')
    context_length = Column(Integer, nullable=True, comment="最大上下文窗口")
    modality = Column(Text, default="text", comment="text/vision/multimodal")
    benchmark_scores = Column(Text, nullable=True, comment="JSON: 展示用, 不参与路由")
    is_open_source = Column(Integer, default=1, comment="是否开源权重 (0/1)")
    release_date = Column(Text, nullable=True, comment="发布日期")
    status = Column(Text, default="active", comment="active/deprecated/preview")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # P0-2 扩展
    raw_ids = Column(
        Text, nullable=True,
        comment="JSON: 各渠道上游原始 model_id, {\"siliconflow\": \"zai-org/GLM-5.2\", ...}. "
                "proxy 发请求前从 model_id 反查 raw_ids[channel.name]",
    )
    supports_prompt_cache = Column(
        Integer, nullable=False, server_default="0",
        comment="0=不支持 prompt cache, 1=支持 (e.g. siliconflow/deepseek 支持, qwen 不支持)",
    )


class ModelGroup(Base):
    """模型组定义 (Phase 2.1, 2026-07-29)

    Agent 请求 "tier:balanced" / "scene:code" 时，路由在组内选最优 (model, channel, account)。

    组过滤条件：tier (限定能力档) / capabilities (能力交集) / min_context / require_vision
    组策略：cheapest (默认) / priority / weighted (Phase 3) / latency (Phase 4)
    降级：fallback_group 指向下一个组；visited set 防死循环
    """
    __tablename__ = "model_groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Text, nullable=False, unique=True, comment="组ID: tier:balanced / scene:code")
    display_name = Column(Text, nullable=True, comment="展示名: 均衡性价比组")
    tier = Column(Text, nullable=True, comment="限定能力档 (NULL=不限)")
    capabilities = Column(Text, nullable=True, comment="JSON: 要求能力标签的交集")
    min_context = Column(Integer, default=0, comment="最低上下文要求")
    require_vision = Column(Integer, default=0, comment="是否要求视觉能力 (0/1)")
    strategy = Column(Text, default="cheapest", comment="cheapest/priority/weighted/latency")
    fallback_group = Column(Text, nullable=True, comment="降级目标组ID (NULL=无降级)")
    enabled = Column(Integer, default=1, comment="0/1")
    sort_order = Column(Integer, default=0, comment="Dashboard 展示排序")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class ModelGroupMember(Base):
    """组成员关联 (Phase 2.1, 2026-07-29)

    多对多: group_id ↔ model_id。

    P0-1 (2026-08-14): 加 FK + UNIQUE 强约束
    - group_id FK -> model_groups.group_id (db 层拒绝孤儿)
    - (group_id, model_id) UNIQUE WHERE is_active=1 (db 层拒绝重复 member)
    - 软删除 (is_active=0) 行保留, 但不参与 UNIQUE 约束
    - 历史脏数据 (孤儿行 + 重复行) 已通过 db/migrations/2026_08_14_p01_mgm_fk_unique.py
      软删除

    P1-2 (2026-08-06) 软删除设计:
    - is_active=1 (默认) 参与路由; is_active=0 仍保留行, 仅路由跳过
    - removed_at / removed_reason / removed_by 记录出组审计字段
    - 业务查询 path (gateway/router._load_groups_cache, dashboard 等)
      需主动 .where(is_active=1) 过滤, 否然会跳走 deprecated model
    - 路由 cache 严格不应出现 is_active=0 行

    router 层防御:
    - 即便 db 已有 FK, router._load_groups_cache 仍做防御性 group_id 归一化
      (0afd0a2, C1 of fix-branch) — 容忍启动前 db 短暂不一致
    """
    __tablename__ = "model_group_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # P0-1: FK 强制 model_groups.group_id 对齐, db 层拒绝 int-string 污染
    group_id = Column(
        Text, nullable=False,
        comment="FK -> model_groups.group_id (db 强约束)",
    )
    model_id = Column(Text, nullable=False, comment="soft FK -> model_registry.model_id (允许未入库 model)")
    priority = Column(Integer, default=100, comment="组内优先级 (越小越优先, 同价时生效)")
    weight = Column(Float, default=1.0, comment="weighted 策略时的权重 (Phase 3)")
    max_cost_ratio = Column(Float, nullable=True, comment="价格超过组内最低 N 倍时排除 (NULL=不限)")
    added_by = Column(Text, default="manual", comment="manual/intel_auto")
    created_at = Column(DateTime, default=func.now())
    # P1-2 软删除: 出组不改 DELETE, 设 is_active=0 + 记录审计
    is_active = Column(Integer, default=1, nullable=False, comment="1=参与路由, 0=已出组 (软删除)")
    removed_at = Column(DateTime, nullable=True, comment="出组时间")
    removed_reason = Column(Text, nullable=True, comment="出组原因: deprecated+tier_change+manual")
    removed_by = Column(Text, nullable=True, comment="出组执行人: intel_auto/manual_approve")

    # 索引 + 约束
    # P0-1: UNIQUE 部分索引 (SQLite only) — (group_id, model_id) 在 is_active=1 行内唯一
    #       软删除行不参与 UNIQUE, 保留审计历史
    __table_args__ = (
        Index("idx_mgm_group_active", "group_id", "is_active"),
        Index("idx_mgm_model_active", "model_id", "is_active"),
        UniqueConstraint(
            "group_id", "model_id",
            name="uq_mgm_group_model",
        ),  # 严格 UNIQUE (跟 migration 的 partial index 等效, 简化 ORM 层)
    )


class ModelCapabilitiesLog(Base):
    """能力标签变更审计 (Phase 2.1, 2026-07-29)

    tier 调整永远人工确认 (architecture.md §十)：
    - AI 推断的变更 default approved=0，必须 dashboard 人工批准才生效
    - manual 写入的变更直接 approved=1
    - 完整 audit trail 留底
    """
    __tablename__ = "model_capabilities_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Text, nullable=False, comment="soft FK → model_registry.model_id")
    field_changed = Column(Text, nullable=False, comment="tier / capabilities / status / benchmark_scores")
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    source = Column(Text, nullable=False, comment="manual / intel_crawler / ai_infer")
    confidence = Column(Float, nullable=True, comment="AI 推断时的置信度 (manual 时为 NULL)")
    approved = Column(Integer, default=0, comment="是否已人工确认 (0/1)")
    created_at = Column(DateTime, default=func.now())


class CrossSourceDiscrepancy(Base):
    """跨源交叉验证不一致记录 (Phase 4 E3, 2026-07-29)

    同一个 model_id 在多个采集源 (model_list / price_aggregator / benchmark) 中
    出现 tier / capabilities / context_length 等关键字段不一致时落库，供人工确认。

    设计：
    - 每个 (model_id, field_changed) 在 status='pending' 时只保留 1 条 (upsert)
    - resolved 后保留 audit trail (resolved_at / resolved_value / resolved_note)
    - sources_json 存各源值: [{"source": "siliconflow", "value": "T3", "ts": "..."},
                              {"source": "openrouter", "value": "T2", "ts": "..."}]
    """
    __tablename__ = "cross_source_discrepancies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Text, nullable=False, comment="被对比的 model_id")
    field_changed = Column(Text, nullable=False, comment="tier / capabilities / context_length / modality")
    sources_json = Column(Text, nullable=False, comment='JSON: [{"source":..., "value":..., "ts":..., "confidence":...}]')
    discrepancy_score = Column(Float, default=0.0, comment="不一致程度 (字段差异量化, 越高越值得注意)")
    status = Column(Text, default="pending", comment="pending / resolved / ignored")
    resolved_value = Column(Text, nullable=True, comment="人工拍板后的最终值 (JSON for capabilities)")
    resolved_note = Column(Text, nullable=True, comment="人工备注: 取信源X / 忽略 / 其他")
    resolved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    # P0-7 fix: 防止并发扫描产生重复 pending 行.
    # 业务语义是 “同 (model_id, field_changed, status) 只保留 1 条”,
    # 抽到 schema 层补齐状态机不变量. SQLite / PostgreSQL 都支持该约束.
    # (resolved / ignored 状态依然计入唯一键, 人为拍板后允许同 (model, field)
    # 在不同 status 下各保留一行历史 — 如果业务上希望 resolved 后才能重新
    # 入 pending, 后续可改为 partial unique 索引.)
    __table_args__ = (
        UniqueConstraint(
            "model_id", "field_changed", "status",
            name="uq_cross_source_key",
        ),
    )


Index("idx_cross_source_status", CrossSourceDiscrepancy.status, CrossSourceDiscrepancy.model_id)


class ModelChangeSuggestion(Base):
    """方案 A (2026-08-14): 单 model 调整建议

    - tier_change: tier 升降 (基于 model_cards.score 跟当前 tier 平均比较)
    - add_to_group: model 应该加进某个 group
    - remove_from_group: model 应该从 group 移除 (长期不用)

    流程:
    1. intelligence/analytics/suggest_changes.py 写入 status='pending'
    2. dashboard \"建议中心\" 人工 review, 点批准/拒绝
    3. scripts/apply_suggestions.py 直接 UPDATE db + 写 applied_at

    不直接改 model_group_members 或 model_registry.tier (那是 seed_model_groups.py 的事),
    让 db 状态最新 + seed 脚本可重新生成 (如果需要 git-tracked).
    """
    __tablename__ = "model_change_suggestions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Text, nullable=False)
    change_type = Column(
        Text, nullable=False,
        comment="tier_change / add_to_group / remove_from_group",
    )
    suggested_value = Column(Text, nullable=True, comment="新 tier / group_id")
    current_value = Column(Text, nullable=True, comment="当前 tier / group_id (NULL=未加入)")
    reason = Column(Text, nullable=True, comment="markdown 解释, 含置信度逻辑")
    confidence = Column(
        Float, nullable=True,
        comment="0-1, 越高越可信 (基于 model_cards / usage / price / cache 4 数据源)",
    )
    based_on = Column(
        Text, nullable=True,
        comment="JSON: {\"score\": 85, \"old_tier\": \"T2\", \"avg_tier_T0_score\": 80}",
    )
    status = Column(
        Text, nullable=False, server_default="pending",
        comment="pending / approved / rejected / applied",
    )
    created_at = Column(DateTime, default=func.now())
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(Text, nullable=True)
    applied_at = Column(DateTime, nullable=True)


class GroupChangeSuggestion(Base):
    """方案 A (2026-08-14): 组级别调整建议

    - create: 新增 group (基于 usage_logs fallback 503 比例)
    - merge: 合并 2 个 group (低使用率)
    - delete: 删除 group (0 使用率)
    - rename: 重命名 (跟实际 model 不匹配)

    同 model_change_suggestions 流程
    """
    __tablename__ = "group_change_suggestions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Text, nullable=False)
    change_type = Column(
        Text, nullable=False,
        comment="create / merge / delete / rename",
    )
    suggested_config = Column(
        Text, nullable=True,
        comment="JSON: fallback_group / strategy / require_vision 等",
    )
    target_groups = Column(
        Text, nullable=True,
        comment="merge 时: \"tier:strong,tier:balanced\"; delete 时: NULL",
    )
    reason = Column(Text, nullable=True)
    confidence = Column(Float, nullable=True)
    based_on = Column(Text, nullable=True, comment="JSON: {\"usage_pct\": {...}}")
    status = Column(Text, nullable=False, server_default="pending")
    created_at = Column(DateTime, default=func.now())
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(Text, nullable=True)
    applied_at = Column(DateTime, nullable=True)


Index("idx_mcs_status", ModelChangeSuggestion.status, ModelChangeSuggestion.created_at)
Index("idx_mcs_model", ModelChangeSuggestion.model_id, ModelChangeSuggestion.change_type)
Index("idx_gcs_status", GroupChangeSuggestion.status, GroupChangeSuggestion.created_at)
Index("idx_gcs_group", GroupChangeSuggestion.group_id, GroupChangeSuggestion.change_type)


class JobHealth(Base):
    """阶段2 (2026-08-14): APScheduler job 健康状态

    用途:
    - dashboard 显示 \"数据新鲜度\" (last_success_at 距今多久)
    - 失败告警 (last_error_at + last_error_msg)
    - 累计统计 (total_runs / successes / failures)

    跟 apscheduler_jobs 表独立 (后者由 apscheduler 自己管理, 只存 schedule)
    """
    __tablename__ = "job_health"

    job_id = Column(Text, primary_key=True)
    last_run_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_error_at = Column(DateTime, nullable=True)
    last_error_msg = Column(Text, nullable=True)
    last_duration_ms = Column(Integer, nullable=True)
    total_runs = Column(Integer, nullable=False, server_default="0")
    total_successes = Column(Integer, nullable=False, server_default="0")
    total_failures = Column(Integer, nullable=False, server_default="0")


Index("idx_job_health_last_success", JobHealth.last_success_at)


class SettingsRuntime(Base):
    """Runtime settings override (API-Sec-2, 2026-07-29)

    k-v 存储, 存运行时可改的配置 (e.g. ALERT_CHANNEL / BARK_KEY / FEISHU_WEBHOOK_URL).
    dashboard PUT 即时生效, 不需要重启 uvicorn.

    跟 .env 的关系 (跟 api-key-security.md §2 一致):
    - .env: 启动时 Pydantic 加载 (immutable, 重启才变)
    - settings_runtime: dashboard 可改, 优先于 .env
    - alerter 读时: 先查 override, fallback env

    设计决策 (2026-07-29):
    - value 字段明文存, 跟 .env 模式一致
    - 加密留 TODO (Phase 3 收尾) — master key 跟 .env 一起泄露时加密救不了,
      加密主要是 "DB 单独泄露" 的 defense in depth
    - 不在表里的 key 表示 "用 .env 的值" (NULL = 用 env)
    """
    __tablename__ = "settings_runtime"

    key = Column(Text, primary_key=True, comment="设置名: alert_channel / bark_key / feishu_webhook_url / serverchan_key")
    value = Column(Text, nullable=True, comment="override 值, NULL = 删除 override 回退 .env")
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
