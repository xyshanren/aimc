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
    """价格快照表（支持峰谷时段）"""
    __tablename__ = "price_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    model = Column(Text, nullable=False, comment="模型名: deepseek-chat/qwen-max")
    price_type = Column(Text, default="normal", comment="normal/peak/offpeak/cache_hit/free")
    time_range = Column(Text, default="全天", comment='时段: "00:00-08:00" 或 "全天"')
    input_price = Column(Float, nullable=True, comment="输入价格（元/百万tokens）")
    output_price = Column(Float, nullable=True, comment="输出价格（元/百万tokens）")
    unit = Column(Text, default="元/百万tokens")
    effective_from = Column(Text, nullable=True, comment="生效日期")
    effective_to = Column(Text, nullable=True, comment="失效日期，NULL=长期有效")
    source = Column(Text, nullable=True, comment="来源: manual/crawler/api")
    created_at = Column(DateTime, default=func.now())

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
    不强制 FK (model_id 用 soft join, 允许"还没入库的新 model"占位)。

    P1-2 (2026-08-06) 软删除设计:
    - is_active=1 (默认) 参与路由; is_active=0 仍保留行, 仅路由跳过
    - removed_at / removed_reason / removed_by 记录出组审计字段
    - 业务查询 path (gateway/router._load_groups_cache, dashboard 等)
      需主动 .where(is_active=1) 过滤, 否然会跳走 deprecated model
    - 路由 cache 严格不应出现 is_active=0 行
    """
    __tablename__ = "model_group_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Text, nullable=False, comment="引用 model_groups.group_id (soft FK)")
    model_id = Column(Text, nullable=False, comment="引用 model_registry.model_id (soft FK)")
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

    # 索引: 路由查询以 (group_id, is_active) 为主过滤, 顺序访问
    __table_args__ = (
        Index("idx_mgm_group_active", "group_id", "is_active"),
        Index("idx_mgm_model_active", "model_id", "is_active"),
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
