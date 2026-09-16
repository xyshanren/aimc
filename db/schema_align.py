"""V6.5+: 迁移机制收敛 — 轻方案 A (已拍板 2026-09-14)

以 db.models ORM 为**单一事实源**的列对齐 + schema_migrations 版本表。

组成:
- `_ALIGN_DDL` — 由 scripts/gen_orm_column_ddl.py 从 ORM 生成的**显式常量**:
  key = 完整 ALTER 语句字面量, value = (表, 列, 版本表登记语句字面量)。
  全部纯字面量, 与 db/init_db.py ensure_migrations 的过审写法同形态。
- `ensure_schema_alignment()` — 对实际库逐条比对 (pragma_table_info 一次
  取全部表全部列), 缺列才执行对应字面量 ALTER 并登记版本表。幂等。
- `mark_legacy_migrations()` — 23 个历史手动迁移脚本登记进版本表
  (仅登记不执行, 内容仍按 docs/ops/migration.md 人工流程),
  解决「无法判断某脚本是否已跑」的审计盲区。
- 不含主键列 / NOT NULL 且无 server_default 的列 (SQLite ALTER 硬限制
  不可 ADD; 这类列只出现在全新库, create_all 已带约束)。

设计决策 (不用 Alembic): 个人项目零运维哲学; 本模块覆盖「新 ORM 列忘写
迁移 → 老库 no such column」这一最高频事故路径; 迁移名格式与 Alembic
兼容, 保留演进空间。

Mimosa 备注: 全模块无 SQL 拼接 — 所有语句都是源码字面量, 动态性仅
存在于「选哪条字面量执行」, 与 f-string/%/bindparams 无关。

用法:
    python -c "import asyncio; from db.schema_align import dry_run; asyncio.run(dry_run())"
ORM 列变更维护流程: 改 db/models.py → 跑生成器替换 _ALIGN_DDL (diff 逐条
可评审) → 提交; 启动时 init_db() 自动对齐老库。
"""

import logging

logger = logging.getLogger(__name__)

MIGRATIONS_TABLE = "schema_migrations"

_VERSION_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "migration TEXT NOT NULL UNIQUE, "
    "note TEXT, "
    "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
)

# 一条字面量 SQL 拿到全部表的全部列名 (SQLite 3.16+ pragma 表函数)
_ALL_COLUMNS_SQL = (
    "SELECT m.name, p.name FROM sqlite_master m "
    "JOIN pragma_table_info(m.name) p WHERE m.type = 'table'"
)

# 由 scripts/gen_orm_column_ddl.py 从 db/models.py 生成 (2026-09-14 快照)。
# key=ALTER 字面量; value=(表名, 列名, 版本表登记字面量)。
_ALIGN_DDL = {
    "ALTER TABLE account_health_log ADD COLUMN triggered_by TEXT DEFAULT 'scheduled' NOT NULL": ("account_health_log", "triggered_by",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_health_log.triggered_by', 'orm-align')"),
    "ALTER TABLE account_health_log ADD COLUMN success INTEGER DEFAULT '0' NOT NULL": ("account_health_log", "success",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_health_log.success', 'orm-align')"),
    "ALTER TABLE account_health_log ADD COLUMN status_code INTEGER": ("account_health_log", "status_code",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_health_log.status_code', 'orm-align')"),
    "ALTER TABLE account_health_log ADD COLUMN error_type TEXT": ("account_health_log", "error_type",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_health_log.error_type', 'orm-align')"),
    "ALTER TABLE account_health_log ADD COLUMN error_msg TEXT": ("account_health_log", "error_msg",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_health_log.error_msg', 'orm-align')"),
    "ALTER TABLE account_health_log ADD COLUMN latency_ms INTEGER": ("account_health_log", "latency_ms",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_health_log.latency_ms', 'orm-align')"),
    "ALTER TABLE account_quota_usage ADD COLUMN window_start DATETIME": ("account_quota_usage", "window_start",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_quota_usage.window_start', 'orm-align')"),
    "ALTER TABLE account_quota_usage ADD COLUMN used_value FLOAT DEFAULT '0' NOT NULL": ("account_quota_usage", "used_value",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_quota_usage.used_value', 'orm-align')"),
    "ALTER TABLE account_quota_usage ADD COLUMN last_updated DATETIME": ("account_quota_usage", "last_updated",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_quota_usage.last_updated', 'orm-align')"),
    "ALTER TABLE account_quotas ADD COLUMN window_seconds INTEGER": ("account_quotas", "window_seconds",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_quotas.window_seconds', 'orm-align')"),
    "ALTER TABLE account_quotas ADD COLUMN reset_strategy TEXT DEFAULT 'rolling' NOT NULL": ("account_quotas", "reset_strategy",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_quotas.reset_strategy', 'orm-align')"),
    "ALTER TABLE account_quotas ADD COLUMN enabled INTEGER DEFAULT '1' NOT NULL": ("account_quotas", "enabled",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_quotas.enabled', 'orm-align')"),
    "ALTER TABLE account_quotas ADD COLUMN updated_at DATETIME": ("account_quotas", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_quotas.updated_at', 'orm-align')"),
    "ALTER TABLE account_quotas ADD COLUMN notes TEXT": ("account_quotas", "notes",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:account_quotas.notes', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN api_key_ciphertext BLOB": ("accounts", "api_key_ciphertext",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.api_key_ciphertext', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN status TEXT": ("accounts", "status",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.status', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN quota_total INTEGER": ("accounts", "quota_total",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.quota_total', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN quota_used INTEGER": ("accounts", "quota_used",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.quota_used', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN last_rotated_at DATETIME": ("accounts", "last_rotated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.last_rotated_at', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN expires_at DATETIME": ("accounts", "expires_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.expires_at', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN notes TEXT": ("accounts", "notes",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.notes', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN encryption_version INTEGER DEFAULT '1' NOT NULL": ("accounts", "encryption_version",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.encryption_version', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN quota_plan TEXT": ("accounts", "quota_plan",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.quota_plan', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN last_success_at DATETIME": ("accounts", "last_success_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.last_success_at', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN last_error_at DATETIME": ("accounts", "last_error_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.last_error_at', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN last_error_type TEXT": ("accounts", "last_error_type",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.last_error_type', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN manual_disabled_at DATETIME": ("accounts", "manual_disabled_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.manual_disabled_at', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN created_at DATETIME": ("accounts", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.created_at', 'orm-align')"),
    "ALTER TABLE accounts ADD COLUMN updated_at DATETIME": ("accounts", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:accounts.updated_at', 'orm-align')"),
    "ALTER TABLE api_keys ADD COLUMN label TEXT": ("api_keys", "label",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:api_keys.label', 'orm-align')"),
    "ALTER TABLE api_keys ADD COLUMN last_used_at DATETIME": ("api_keys", "last_used_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:api_keys.last_used_at', 'orm-align')"),
    "ALTER TABLE api_keys ADD COLUMN created_at DATETIME": ("api_keys", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:api_keys.created_at', 'orm-align')"),
    "ALTER TABLE api_keys ADD COLUMN updated_at DATETIME": ("api_keys", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:api_keys.updated_at', 'orm-align')"),
    "ALTER TABLE channel_quotas ADD COLUMN account_id INTEGER": ("channel_quotas", "account_id",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channel_quotas.account_id', 'orm-align')"),
    "ALTER TABLE channel_quotas ADD COLUMN reset_strategy TEXT": ("channel_quotas", "reset_strategy",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channel_quotas.reset_strategy', 'orm-align')"),
    "ALTER TABLE channel_quotas ADD COLUMN created_at DATETIME": ("channel_quotas", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channel_quotas.created_at', 'orm-align')"),
    "ALTER TABLE channel_quotas ADD COLUMN updated_at DATETIME": ("channel_quotas", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channel_quotas.updated_at', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN status TEXT": ("channels", "status",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.status', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN priority INTEGER": ("channels", "priority",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.priority', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN provider_type TEXT DEFAULT 'official' NOT NULL": ("channels", "provider_type",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.provider_type', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN billing_mode TEXT DEFAULT 'pay_per_token' NOT NULL": ("channels", "billing_mode",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.billing_mode', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN api_base TEXT": ("channels", "api_base",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.api_base', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN api_style TEXT DEFAULT 'openai'": ("channels", "api_style",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.api_style', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN supports_prompt_cache INTEGER DEFAULT '0' NOT NULL": ("channels", "supports_prompt_cache",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.supports_prompt_cache', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN disable_thinking_pass_through INTEGER DEFAULT '1' NOT NULL": ("channels", "disable_thinking_pass_through",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.disable_thinking_pass_through', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN disable_thinking_inject INTEGER DEFAULT '0' NOT NULL": ("channels", "disable_thinking_inject",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.disable_thinking_inject', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN requires_reasoning_echo INTEGER DEFAULT '0' NOT NULL": ("channels", "requires_reasoning_echo",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.requires_reasoning_echo', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN drop_params TEXT": ("channels", "drop_params",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.drop_params', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN auto_drop_params INTEGER DEFAULT '1' NOT NULL": ("channels", "auto_drop_params",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.auto_drop_params', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN created_at DATETIME": ("channels", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.created_at', 'orm-align')"),
    "ALTER TABLE channels ADD COLUMN updated_at DATETIME": ("channels", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:channels.updated_at', 'orm-align')"),
    "ALTER TABLE cooldown_events ADD COLUMN channel_name TEXT": ("cooldown_events", "channel_name",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cooldown_events.channel_name', 'orm-align')"),
    "ALTER TABLE cooldown_events ADD COLUMN account_name TEXT": ("cooldown_events", "account_name",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cooldown_events.account_name', 'orm-align')"),
    "ALTER TABLE cooldown_events ADD COLUMN error_type TEXT": ("cooldown_events", "error_type",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cooldown_events.error_type', 'orm-align')"),
    "ALTER TABLE cooldown_events ADD COLUMN error_code TEXT": ("cooldown_events", "error_code",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cooldown_events.error_code', 'orm-align')"),
    "ALTER TABLE cooldown_events ADD COLUMN error_msg TEXT": ("cooldown_events", "error_msg",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cooldown_events.error_msg', 'orm-align')"),
    "ALTER TABLE cooldown_events ADD COLUMN remaining_seconds FLOAT": ("cooldown_events", "remaining_seconds",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cooldown_events.remaining_seconds', 'orm-align')"),
    "ALTER TABLE cross_source_discrepancies ADD COLUMN discrepancy_score FLOAT": ("cross_source_discrepancies", "discrepancy_score",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cross_source_discrepancies.discrepancy_score', 'orm-align')"),
    "ALTER TABLE cross_source_discrepancies ADD COLUMN status TEXT": ("cross_source_discrepancies", "status",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cross_source_discrepancies.status', 'orm-align')"),
    "ALTER TABLE cross_source_discrepancies ADD COLUMN resolved_value TEXT": ("cross_source_discrepancies", "resolved_value",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cross_source_discrepancies.resolved_value', 'orm-align')"),
    "ALTER TABLE cross_source_discrepancies ADD COLUMN resolved_note TEXT": ("cross_source_discrepancies", "resolved_note",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cross_source_discrepancies.resolved_note', 'orm-align')"),
    "ALTER TABLE cross_source_discrepancies ADD COLUMN resolved_at DATETIME": ("cross_source_discrepancies", "resolved_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cross_source_discrepancies.resolved_at', 'orm-align')"),
    "ALTER TABLE cross_source_discrepancies ADD COLUMN created_at DATETIME": ("cross_source_discrepancies", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cross_source_discrepancies.created_at', 'orm-align')"),
    "ALTER TABLE cross_source_discrepancies ADD COLUMN updated_at DATETIME": ("cross_source_discrepancies", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:cross_source_discrepancies.updated_at', 'orm-align')"),
    "ALTER TABLE group_change_suggestions ADD COLUMN suggested_config TEXT": ("group_change_suggestions", "suggested_config",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:group_change_suggestions.suggested_config', 'orm-align')"),
    "ALTER TABLE group_change_suggestions ADD COLUMN target_groups TEXT": ("group_change_suggestions", "target_groups",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:group_change_suggestions.target_groups', 'orm-align')"),
    "ALTER TABLE group_change_suggestions ADD COLUMN reason TEXT": ("group_change_suggestions", "reason",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:group_change_suggestions.reason', 'orm-align')"),
    "ALTER TABLE group_change_suggestions ADD COLUMN confidence FLOAT": ("group_change_suggestions", "confidence",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:group_change_suggestions.confidence', 'orm-align')"),
    "ALTER TABLE group_change_suggestions ADD COLUMN based_on TEXT": ("group_change_suggestions", "based_on",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:group_change_suggestions.based_on', 'orm-align')"),
    "ALTER TABLE group_change_suggestions ADD COLUMN status TEXT DEFAULT 'pending' NOT NULL": ("group_change_suggestions", "status",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:group_change_suggestions.status', 'orm-align')"),
    "ALTER TABLE group_change_suggestions ADD COLUMN created_at DATETIME": ("group_change_suggestions", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:group_change_suggestions.created_at', 'orm-align')"),
    "ALTER TABLE group_change_suggestions ADD COLUMN reviewed_at DATETIME": ("group_change_suggestions", "reviewed_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:group_change_suggestions.reviewed_at', 'orm-align')"),
    "ALTER TABLE group_change_suggestions ADD COLUMN reviewed_by TEXT": ("group_change_suggestions", "reviewed_by",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:group_change_suggestions.reviewed_by', 'orm-align')"),
    "ALTER TABLE group_change_suggestions ADD COLUMN applied_at DATETIME": ("group_change_suggestions", "applied_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:group_change_suggestions.applied_at', 'orm-align')"),
    "ALTER TABLE intel_sources ADD COLUMN poll_interval INTEGER": ("intel_sources", "poll_interval",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:intel_sources.poll_interval', 'orm-align')"),
    "ALTER TABLE intel_sources ADD COLUMN status TEXT": ("intel_sources", "status",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:intel_sources.status', 'orm-align')"),
    "ALTER TABLE intel_sources ADD COLUMN last_check_at DATETIME": ("intel_sources", "last_check_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:intel_sources.last_check_at', 'orm-align')"),
    "ALTER TABLE intel_sources ADD COLUMN last_hash TEXT": ("intel_sources", "last_hash",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:intel_sources.last_hash', 'orm-align')"),
    "ALTER TABLE intel_sources ADD COLUMN last_content TEXT": ("intel_sources", "last_content",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:intel_sources.last_content', 'orm-align')"),
    "ALTER TABLE intel_sources ADD COLUMN error_count INTEGER": ("intel_sources", "error_count",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:intel_sources.error_count', 'orm-align')"),
    "ALTER TABLE intel_sources ADD COLUMN consecutive_no_change INTEGER": ("intel_sources", "consecutive_no_change",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:intel_sources.consecutive_no_change', 'orm-align')"),
    "ALTER TABLE intel_sources ADD COLUMN last_changed_at DATETIME": ("intel_sources", "last_changed_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:intel_sources.last_changed_at', 'orm-align')"),
    "ALTER TABLE intel_sources ADD COLUMN created_at DATETIME": ("intel_sources", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:intel_sources.created_at', 'orm-align')"),
    "ALTER TABLE job_health ADD COLUMN last_run_at DATETIME": ("job_health", "last_run_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:job_health.last_run_at', 'orm-align')"),
    "ALTER TABLE job_health ADD COLUMN last_success_at DATETIME": ("job_health", "last_success_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:job_health.last_success_at', 'orm-align')"),
    "ALTER TABLE job_health ADD COLUMN last_error_at DATETIME": ("job_health", "last_error_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:job_health.last_error_at', 'orm-align')"),
    "ALTER TABLE job_health ADD COLUMN last_error_msg TEXT": ("job_health", "last_error_msg",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:job_health.last_error_msg', 'orm-align')"),
    "ALTER TABLE job_health ADD COLUMN last_duration_ms INTEGER": ("job_health", "last_duration_ms",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:job_health.last_duration_ms', 'orm-align')"),
    "ALTER TABLE job_health ADD COLUMN total_runs INTEGER DEFAULT '0' NOT NULL": ("job_health", "total_runs",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:job_health.total_runs', 'orm-align')"),
    "ALTER TABLE job_health ADD COLUMN total_successes INTEGER DEFAULT '0' NOT NULL": ("job_health", "total_successes",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:job_health.total_successes', 'orm-align')"),
    "ALTER TABLE job_health ADD COLUMN total_failures INTEGER DEFAULT '0' NOT NULL": ("job_health", "total_failures",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:job_health.total_failures', 'orm-align')"),
    "ALTER TABLE model_capabilities_log ADD COLUMN old_value TEXT": ("model_capabilities_log", "old_value",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_capabilities_log.old_value', 'orm-align')"),
    "ALTER TABLE model_capabilities_log ADD COLUMN new_value TEXT": ("model_capabilities_log", "new_value",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_capabilities_log.new_value', 'orm-align')"),
    "ALTER TABLE model_capabilities_log ADD COLUMN confidence FLOAT": ("model_capabilities_log", "confidence",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_capabilities_log.confidence', 'orm-align')"),
    "ALTER TABLE model_capabilities_log ADD COLUMN approved INTEGER": ("model_capabilities_log", "approved",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_capabilities_log.approved', 'orm-align')"),
    "ALTER TABLE model_capabilities_log ADD COLUMN provider TEXT": ("model_capabilities_log", "provider",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_capabilities_log.provider', 'orm-align')"),
    "ALTER TABLE model_capabilities_log ADD COLUMN created_at DATETIME": ("model_capabilities_log", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_capabilities_log.created_at', 'orm-align')"),
    "ALTER TABLE model_cards ADD COLUMN score FLOAT": ("model_cards", "score",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_cards.score', 'orm-align')"),
    "ALTER TABLE model_cards ADD COLUMN tier TEXT": ("model_cards", "tier",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_cards.tier', 'orm-align')"),
    "ALTER TABLE model_cards ADD COLUMN source_url TEXT": ("model_cards", "source_url",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_cards.source_url', 'orm-align')"),
    "ALTER TABLE model_cards ADD COLUMN notes TEXT": ("model_cards", "notes",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_cards.notes', 'orm-align')"),
    "ALTER TABLE model_cards ADD COLUMN created_at DATETIME": ("model_cards", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_cards.created_at', 'orm-align')"),
    "ALTER TABLE model_cards ADD COLUMN updated_at DATETIME": ("model_cards", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_cards.updated_at', 'orm-align')"),
    "ALTER TABLE model_change_suggestions ADD COLUMN suggested_value TEXT": ("model_change_suggestions", "suggested_value",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_change_suggestions.suggested_value', 'orm-align')"),
    "ALTER TABLE model_change_suggestions ADD COLUMN current_value TEXT": ("model_change_suggestions", "current_value",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_change_suggestions.current_value', 'orm-align')"),
    "ALTER TABLE model_change_suggestions ADD COLUMN reason TEXT": ("model_change_suggestions", "reason",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_change_suggestions.reason', 'orm-align')"),
    "ALTER TABLE model_change_suggestions ADD COLUMN confidence FLOAT": ("model_change_suggestions", "confidence",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_change_suggestions.confidence', 'orm-align')"),
    "ALTER TABLE model_change_suggestions ADD COLUMN based_on TEXT": ("model_change_suggestions", "based_on",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_change_suggestions.based_on', 'orm-align')"),
    "ALTER TABLE model_change_suggestions ADD COLUMN status TEXT DEFAULT 'pending' NOT NULL": ("model_change_suggestions", "status",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_change_suggestions.status', 'orm-align')"),
    "ALTER TABLE model_change_suggestions ADD COLUMN created_at DATETIME": ("model_change_suggestions", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_change_suggestions.created_at', 'orm-align')"),
    "ALTER TABLE model_change_suggestions ADD COLUMN reviewed_at DATETIME": ("model_change_suggestions", "reviewed_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_change_suggestions.reviewed_at', 'orm-align')"),
    "ALTER TABLE model_change_suggestions ADD COLUMN reviewed_by TEXT": ("model_change_suggestions", "reviewed_by",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_change_suggestions.reviewed_by', 'orm-align')"),
    "ALTER TABLE model_change_suggestions ADD COLUMN applied_at DATETIME": ("model_change_suggestions", "applied_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_change_suggestions.applied_at', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN input_tokens INTEGER": ("model_feedback_summary", "input_tokens",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.input_tokens', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN output_tokens INTEGER": ("model_feedback_summary", "output_tokens",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.output_tokens', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN cost_total FLOAT": ("model_feedback_summary", "cost_total",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.cost_total', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN fail_rate FLOAT": ("model_feedback_summary", "fail_rate",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.fail_rate', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN retry_rate FLOAT": ("model_feedback_summary", "retry_rate",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.retry_rate', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN p50_latency_ms INTEGER": ("model_feedback_summary", "p50_latency_ms",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.p50_latency_ms', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN p95_latency_ms INTEGER": ("model_feedback_summary", "p95_latency_ms",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.p95_latency_ms', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN avg_output_tokens INTEGER": ("model_feedback_summary", "avg_output_tokens",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.avg_output_tokens', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN feedback_score FLOAT": ("model_feedback_summary", "feedback_score",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.feedback_score', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN feedback_score_no_retry FLOAT": ("model_feedback_summary", "feedback_score_no_retry",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.feedback_score_no_retry', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN client_breakdown TEXT": ("model_feedback_summary", "client_breakdown",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.client_breakdown', 'orm-align')"),
    "ALTER TABLE model_feedback_summary ADD COLUMN created_at DATETIME": ("model_feedback_summary", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_feedback_summary.created_at', 'orm-align')"),
    "ALTER TABLE model_group_members ADD COLUMN priority INTEGER": ("model_group_members", "priority",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_group_members.priority', 'orm-align')"),
    "ALTER TABLE model_group_members ADD COLUMN weight FLOAT": ("model_group_members", "weight",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_group_members.weight', 'orm-align')"),
    "ALTER TABLE model_group_members ADD COLUMN max_cost_ratio FLOAT": ("model_group_members", "max_cost_ratio",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_group_members.max_cost_ratio', 'orm-align')"),
    "ALTER TABLE model_group_members ADD COLUMN added_by TEXT": ("model_group_members", "added_by",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_group_members.added_by', 'orm-align')"),
    "ALTER TABLE model_group_members ADD COLUMN created_at DATETIME": ("model_group_members", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_group_members.created_at', 'orm-align')"),
    "ALTER TABLE model_group_members ADD COLUMN removed_at DATETIME": ("model_group_members", "removed_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_group_members.removed_at', 'orm-align')"),
    "ALTER TABLE model_group_members ADD COLUMN removed_reason TEXT": ("model_group_members", "removed_reason",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_group_members.removed_reason', 'orm-align')"),
    "ALTER TABLE model_group_members ADD COLUMN removed_by TEXT": ("model_group_members", "removed_by",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_group_members.removed_by', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN display_name TEXT": ("model_groups", "display_name",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.display_name', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN tier TEXT": ("model_groups", "tier",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.tier', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN capabilities TEXT": ("model_groups", "capabilities",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.capabilities', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN min_context INTEGER": ("model_groups", "min_context",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.min_context', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN require_vision INTEGER": ("model_groups", "require_vision",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.require_vision', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN strategy TEXT": ("model_groups", "strategy",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.strategy', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN fallback_group TEXT": ("model_groups", "fallback_group",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.fallback_group', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN enabled INTEGER": ("model_groups", "enabled",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.enabled', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN sort_order INTEGER": ("model_groups", "sort_order",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.sort_order', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN created_at DATETIME": ("model_groups", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.created_at', 'orm-align')"),
    "ALTER TABLE model_groups ADD COLUMN updated_at DATETIME": ("model_groups", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_groups.updated_at', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN display_name TEXT": ("model_registry", "display_name",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.display_name', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN capabilities TEXT": ("model_registry", "capabilities",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.capabilities', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN context_length INTEGER": ("model_registry", "context_length",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.context_length', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN modality TEXT": ("model_registry", "modality",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.modality', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN benchmark_scores TEXT": ("model_registry", "benchmark_scores",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.benchmark_scores', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN is_open_source INTEGER": ("model_registry", "is_open_source",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.is_open_source', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN release_date TEXT": ("model_registry", "release_date",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.release_date', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN status TEXT": ("model_registry", "status",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.status', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN notes TEXT": ("model_registry", "notes",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.notes', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN created_at DATETIME": ("model_registry", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.created_at', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN updated_at DATETIME": ("model_registry", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.updated_at', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN raw_ids TEXT": ("model_registry", "raw_ids",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.raw_ids', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN supports_prompt_cache INTEGER DEFAULT '0' NOT NULL": ("model_registry", "supports_prompt_cache",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.supports_prompt_cache', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN list_input_price FLOAT": ("model_registry", "list_input_price",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.list_input_price', 'orm-align')"),
    "ALTER TABLE model_registry ADD COLUMN list_output_price FLOAT": ("model_registry", "list_output_price",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:model_registry.list_output_price', 'orm-align')"),
    "ALTER TABLE parse_rules ADD COLUMN url_pattern TEXT": ("parse_rules", "url_pattern",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:parse_rules.url_pattern', 'orm-align')"),
    "ALTER TABLE parse_rules ADD COLUMN css_selector TEXT": ("parse_rules", "css_selector",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:parse_rules.css_selector', 'orm-align')"),
    "ALTER TABLE parse_rules ADD COLUMN ai_strategy TEXT": ("parse_rules", "ai_strategy",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:parse_rules.ai_strategy', 'orm-align')"),
    "ALTER TABLE parse_rules ADD COLUMN confidence FLOAT": ("parse_rules", "confidence",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:parse_rules.confidence', 'orm-align')"),
    "ALTER TABLE parse_rules ADD COLUMN status TEXT": ("parse_rules", "status",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:parse_rules.status', 'orm-align')"),
    "ALTER TABLE parse_rules ADD COLUMN created_at DATETIME": ("parse_rules", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:parse_rules.created_at', 'orm-align')"),
    "ALTER TABLE parse_rules ADD COLUMN updated_at DATETIME": ("parse_rules", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:parse_rules.updated_at', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN price_type TEXT": ("price_snapshots", "price_type",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.price_type', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN time_range TEXT": ("price_snapshots", "time_range",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.time_range', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN input_price FLOAT": ("price_snapshots", "input_price",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.input_price', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN output_price FLOAT": ("price_snapshots", "output_price",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.output_price', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN unit TEXT": ("price_snapshots", "unit",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.unit', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN effective_from TEXT": ("price_snapshots", "effective_from",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.effective_from', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN effective_to TEXT": ("price_snapshots", "effective_to",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.effective_to', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN source TEXT": ("price_snapshots", "source",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.source', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN created_at DATETIME": ("price_snapshots", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.created_at', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN raw_model_id TEXT": ("price_snapshots", "raw_model_id",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.raw_model_id', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN cached_input_price FLOAT": ("price_snapshots", "cached_input_price",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.cached_input_price', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN cache_write_price FLOAT": ("price_snapshots", "cache_write_price",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.cache_write_price', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN currency TEXT DEFAULT 'CNY' NOT NULL": ("price_snapshots", "currency",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.currency', 'orm-align')"),
    "ALTER TABLE price_snapshots ADD COLUMN is_promotion INTEGER DEFAULT '0' NOT NULL": ("price_snapshots", "is_promotion",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:price_snapshots.is_promotion', 'orm-align')"),
    "ALTER TABLE promotion_review_queue ADD COLUMN title TEXT": ("promotion_review_queue", "title",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotion_review_queue.title', 'orm-align')"),
    "ALTER TABLE promotion_review_queue ADD COLUMN summary TEXT": ("promotion_review_queue", "summary",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotion_review_queue.summary', 'orm-align')"),
    "ALTER TABLE promotion_review_queue ADD COLUMN url TEXT": ("promotion_review_queue", "url",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotion_review_queue.url', 'orm-align')"),
    "ALTER TABLE promotion_review_queue ADD COLUMN confidence FLOAT": ("promotion_review_queue", "confidence",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotion_review_queue.confidence', 'orm-align')"),
    "ALTER TABLE promotion_review_queue ADD COLUMN raw_data TEXT": ("promotion_review_queue", "raw_data",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotion_review_queue.raw_data', 'orm-align')"),
    "ALTER TABLE promotion_review_queue ADD COLUMN approved INTEGER": ("promotion_review_queue", "approved",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotion_review_queue.approved', 'orm-align')"),
    "ALTER TABLE promotion_review_queue ADD COLUMN action TEXT": ("promotion_review_queue", "action",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotion_review_queue.action', 'orm-align')"),
    "ALTER TABLE promotion_review_queue ADD COLUMN created_at DATETIME": ("promotion_review_queue", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotion_review_queue.created_at', 'orm-align')"),
    "ALTER TABLE promotion_review_queue ADD COLUMN reviewed_at DATETIME": ("promotion_review_queue", "reviewed_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotion_review_queue.reviewed_at', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN content TEXT": ("promotions", "content",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.content', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN url TEXT": ("promotions", "url",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.url', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN discount_desc TEXT": ("promotions", "discount_desc",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.discount_desc', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN start_date TEXT": ("promotions", "start_date",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.start_date', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN end_date TEXT": ("promotions", "end_date",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.end_date', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN status TEXT": ("promotions", "status",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.status', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN confidence FLOAT": ("promotions", "confidence",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.confidence', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN raw_data TEXT": ("promotions", "raw_data",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.raw_data', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN flagged INTEGER": ("promotions", "flagged",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.flagged', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN flagged_reason TEXT": ("promotions", "flagged_reason",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.flagged_reason', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN created_at DATETIME": ("promotions", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.created_at', 'orm-align')"),
    "ALTER TABLE promotions ADD COLUMN updated_at DATETIME": ("promotions", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:promotions.updated_at', 'orm-align')"),
    "ALTER TABLE request_transcripts ADD COLUMN channel_id INTEGER": ("request_transcripts", "channel_id",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:request_transcripts.channel_id', 'orm-align')"),
    "ALTER TABLE request_transcripts ADD COLUMN model TEXT": ("request_transcripts", "model",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:request_transcripts.model', 'orm-align')"),
    "ALTER TABLE request_transcripts ADD COLUMN request_body TEXT": ("request_transcripts", "request_body",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:request_transcripts.request_body', 'orm-align')"),
    "ALTER TABLE request_transcripts ADD COLUMN response_body TEXT": ("request_transcripts", "response_body",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:request_transcripts.response_body', 'orm-align')"),
    "ALTER TABLE request_transcripts ADD COLUMN status_code INTEGER": ("request_transcripts", "status_code",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:request_transcripts.status_code', 'orm-align')"),
    "ALTER TABLE request_transcripts ADD COLUMN caller_id INTEGER": ("request_transcripts", "caller_id",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:request_transcripts.caller_id', 'orm-align')"),
    "ALTER TABLE request_transcripts ADD COLUMN key_id INTEGER": ("request_transcripts", "key_id",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:request_transcripts.key_id', 'orm-align')"),
    "ALTER TABLE request_transcripts ADD COLUMN created_at DATETIME": ("request_transcripts", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:request_transcripts.created_at', 'orm-align')"),
    "ALTER TABLE settings_runtime ADD COLUMN value TEXT": ("settings_runtime", "value",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:settings_runtime.value', 'orm-align')"),
    "ALTER TABLE settings_runtime ADD COLUMN updated_at DATETIME": ("settings_runtime", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:settings_runtime.updated_at', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN channel_id INTEGER": ("usage_logs", "channel_id",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.channel_id', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN account_id INTEGER": ("usage_logs", "account_id",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.account_id', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN model TEXT": ("usage_logs", "model",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.model', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN caller_id INTEGER": ("usage_logs", "caller_id",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.caller_id', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN key_id INTEGER": ("usage_logs", "key_id",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.key_id', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN group_id TEXT": ("usage_logs", "group_id",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.group_id', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN input_tokens INTEGER": ("usage_logs", "input_tokens",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.input_tokens', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN output_tokens INTEGER": ("usage_logs", "output_tokens",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.output_tokens', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN cost_estimate FLOAT": ("usage_logs", "cost_estimate",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.cost_estimate', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN list_cost_estimate FLOAT": ("usage_logs", "list_cost_estimate",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.list_cost_estimate', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN status_code INTEGER": ("usage_logs", "status_code",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.status_code', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN latency_ms INTEGER": ("usage_logs", "latency_ms",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.latency_ms', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN cached_input_tokens INTEGER": ("usage_logs", "cached_input_tokens",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.cached_input_tokens', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN cache_read_input_tokens INTEGER": ("usage_logs", "cache_read_input_tokens",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.cache_read_input_tokens', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN cache_creation_input_tokens INTEGER": ("usage_logs", "cache_creation_input_tokens",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.cache_creation_input_tokens', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN reasoning_tokens INTEGER": ("usage_logs", "reasoning_tokens",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.reasoning_tokens', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN created_at DATETIME": ("usage_logs", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.created_at', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN error_msg TEXT": ("usage_logs", "error_msg",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.error_msg', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN error_type TEXT": ("usage_logs", "error_type",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.error_type', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN error_code TEXT": ("usage_logs", "error_code",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.error_code', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN failed_channels JSON": ("usage_logs", "failed_channels",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.failed_channels', 'orm-align')"),
    "ALTER TABLE usage_logs ADD COLUMN needs_human BOOLEAN": ("usage_logs", "needs_human",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:usage_logs.needs_human', 'orm-align')"),
    "ALTER TABLE users ADD COLUMN display_name TEXT": ("users", "display_name",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:users.display_name', 'orm-align')"),
    "ALTER TABLE users ADD COLUMN daily_token_limit INTEGER": ("users", "daily_token_limit",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:users.daily_token_limit', 'orm-align')"),
    "ALTER TABLE users ADD COLUMN monthly_token_limit INTEGER": ("users", "monthly_token_limit",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:users.monthly_token_limit', 'orm-align')"),
    "ALTER TABLE users ADD COLUMN notes TEXT": ("users", "notes",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:users.notes', 'orm-align')"),
    "ALTER TABLE users ADD COLUMN created_at DATETIME": ("users", "created_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:users.created_at', 'orm-align')"),
    "ALTER TABLE users ADD COLUMN updated_at DATETIME": ("users", "updated_at",
        "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('align:users.updated_at', 'orm-align')"),
}

# 历史手动迁移脚本登记字面量 (与 LEGACY_MANUAL_MIGRATIONS 清单一一对应,
# 生成器同源产出; 仅登记不执行)
_LEGACY_INSERTS = [
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_14_p01_mgm_fk_unique', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_14_p02_price_cache_schema', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_14_p03followup_cache_tokens', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_14_p05_encryption_version', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_14_p06_account_quotas', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_14_p112_channel_api_base', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_14_p2_job_health_table', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_14_p2_scheduler_cleanup', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_14_p_suggestions_tables', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_17_p_disable_thinking', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_19_v48_multi_user', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_20_p2b_drop_params', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_25_add_sensenova_models', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_25_fix_deepseek_pricing', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_25_fix_tier_corruption', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_08_25_request_transcripts', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_09_02_v51_m3_tier_t2', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_09_03_v60_glm_flash_vision', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_09_03_v60_phase2_cost_audit', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_09_04_v61x_qwen_naming', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_09_05_v6x_reasoning_tokens', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_09_09_v6x_feedback_score_no_retry', 'legacy-manual')",
    "INSERT OR IGNORE INTO schema_migrations (migration, note) VALUES ('2026_09_11_v6x_thinking_inject', 'legacy-manual')",
]

# 版本表清单 (文档/CLI 展示用; 与 _LEGACY_INSERTS 同源)
LEGACY_MANUAL_MIGRATIONS = [
    "2026_08_14_p01_mgm_fk_unique",
    "2026_08_14_p02_price_cache_schema",
    "2026_08_14_p03followup_cache_tokens",
    "2026_08_14_p05_encryption_version",
    "2026_08_14_p06_account_quotas",
    "2026_08_14_p112_channel_api_base",
    "2026_08_14_p2_job_health_table",
    "2026_08_14_p2_scheduler_cleanup",
    "2026_08_14_p_suggestions_tables",
    "2026_08_17_p_disable_thinking",
    "2026_08_19_v48_multi_user",
    "2026_08_20_p2b_drop_params",
    "2026_08_25_add_sensenova_models",
    "2026_08_25_fix_deepseek_pricing",
    "2026_08_25_fix_tier_corruption",
    "2026_08_25_request_transcripts",
    "2026_09_02_v51_m3_tier_t2",
    "2026_09_03_v60_glm_flash_vision",
    "2026_09_03_v60_phase2_cost_audit",
    "2026_09_04_v61x_qwen_naming",
    "2026_09_05_v6x_reasoning_tokens",
    "2026_09_09_v6x_feedback_score_no_retry",
    "2026_09_11_v6x_thinking_inject",
]


def plan_alignment(cols_by_table: dict, existing_tables=None) -> list:
    """纯函数比对: _ALIGN_DDL 白名单 vs 实际列集合 → 待执行清单。

    Args:
        cols_by_table: {表名: set(列名)}
        existing_tables: 实际存在的表名集合; None = 视为全部存在。
            不存在的表整表跳过 (由 create_all 建表, 建后重跑幂等收敛),
            否则其全部列会被误判缺失而对空表执行 ALTER 报 no such table。
    """
    if existing_tables is None:
        existing_tables = {t for t, _c, _i in _ALIGN_DDL.values()}
    return [
        (stmt, table, col, insert)
        for stmt, (table, col, insert) in _ALIGN_DDL.items()
        if table in existing_tables
        and col not in cols_by_table.get(table, ())
    ]


async def ensure_schema_alignment() -> dict:
    """ORM 单一事实源列对齐 (幂等)。init_db() 内自动调用。

    Returns:
        {"added": {表: [列]}, "skipped_tables": [不存在的表]}
    """
    from sqlalchemy import text

    from db.engine import engine
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_VERSION_TABLE_DDL)
        rows = (await conn.exec_driver_sql(_ALL_COLUMNS_SQL)).fetchall()
        cols_by_table: dict = {}
        for tbl, col in rows:
            cols_by_table.setdefault(tbl, set()).add(col)
        all_tables = {
            r[0]
            for r in (
                await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            ).fetchall()
        }
        pending = plan_alignment(cols_by_table, all_tables)
        added: dict = {}
        for stmt, table, col, insert in pending:
            await conn.exec_driver_sql(stmt)
            await conn.exec_driver_sql(insert)
            added.setdefault(table, []).append(col)

    for table, cols in added.items():
        logger.info("schema 对齐: %s 补列 %s", table, cols)
    skipped = sorted({t for t, _c, _i in _ALIGN_DDL.values()} - all_tables)
    if skipped:
        logger.debug("schema 对齐: 以下表不存在, 由 create_all 建表: %s", skipped)
    return {"added": added, "skipped_tables": skipped}


async def dry_run() -> None:
    """打印将执行的 DDL 清单 (不执行)。"""
    from db.engine import engine
    async with engine.connect() as conn:
        rows = (await conn.exec_driver_sql(_ALL_COLUMNS_SQL)).fetchall()
    cols_by_table: dict = {}
    for tbl, col in rows:
        cols_by_table.setdefault(tbl, set()).add(col)
    pending = plan_alignment(cols_by_table)
    if not pending:
        print("[dry-run] schema 已对齐, 无待执行 DDL")
        return
    print("[dry-run] 待执行 %d 条:" % len(pending))
    for stmt, _table, _col, _insert in pending:
        print("  %s" % stmt)


async def get_applied_migrations() -> list:
    """读版本表 (未建表时返回空)。"""
    from db.engine import engine
    async with engine.begin() as conn:
        rows = (
            await conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'schema_migrations'"
            )
        ).fetchall()
        if not rows:
            return []
        rows = (
            await conn.exec_driver_sql(
                "SELECT migration, COALESCE(note, '') FROM schema_migrations "
                "ORDER BY id"
            )
        ).fetchall()
        return [(r[0], r[1]) for r in rows]


async def mark_legacy_migrations() -> int:
    """历史手动迁移脚本登记进版本表 (幂等, 仅登记不执行)。

    生产库已按 docs/ops/migration.md 全量执行过, 2026-09-14 拍板全部
    标记为基线; 新环境由 init_db 全量建表, 同样直接标记。
    """
    from db.engine import engine
    n = 0
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_VERSION_TABLE_DDL)
        for insert in _LEGACY_INSERTS:
            cur = await conn.exec_driver_sql(insert)
            if cur.rowcount:
                n += cur.rowcount
    if n:
        logger.info("版本表登记历史迁移 %d 个", n)
    return n
