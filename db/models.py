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
    Column, Integer, Text, Float, DateTime, ForeignKey, LargeBinary, func
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
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cost_estimate = Column(Float, nullable=True, comment="估算费用（元）")
    status_code = Column(Integer, nullable=True)
    latency_ms = Column(Integer, nullable=True, comment="响应延迟（毫秒）")
    created_at = Column(DateTime, default=func.now())


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
