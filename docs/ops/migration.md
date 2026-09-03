# 数据库迁移指南

> 受众：AIMC 升级或 reset_db 后跑 migration 的人
> 前提：[dev/setup.md](../dev/setup.md)（本地跑起来）

## 1. 当前 migration 总览

AIMC 当前有 **19 个 migration 脚本**（`db/migrations/`）：

```
2026_07_xx  P0-P0.1  (历史)
2026_08_14_p01_mgm_fk_unique.py               # P0-1 FK + UNIQUE
2026_08_14_p02_price_cache_schema.py         # P0-2 cache 价列 + raw_model_id
2026_08_14_p05_encryption_version.py         # P0-5 encryption_version 列
2026_08_14_p06_account_quotas.py            # P0-6 account_quotas / quota_usage
2026_08_14_p_suggestions_tables.py           # 方案A 建议表
2026_08_14_p112_channel_api_base.py          # P1-12 api_base / api_style
2026_08_14_p03followup_cache_tokens.py       # P0-3 follow-up usage_logs
2026_08_14_p2_scheduler_cleanup.py          # 阶段2 删旧 crawl_* jobs
2026_08_14_p2_job_health_table.py            # 阶段2 job_health 表
2026_08_17_p_disable_thinking.py            # V4.6.4 channels.disable_thinking_pass_through
2026_08_19_v48_multi_user.py                # V4.8 users + api_keys + usage_logs.caller_id
2026_08_20_p2b_drop_params.py               # P2-B channels.drop_params
2026_08_25_add_sensenova_models.py          # V4.9.5 SenseNova 渠道/模型/0元价
2026_08_25_fix_deepseek_pricing.py          # V4.9.5 deepseek 价格修正
2026_08_25_fix_tier_corruption.py           # V4.9.5 registry tier 修复
2026_08_25_request_transcripts.py           # V4.9.3 request_transcripts 表
2026_09_02_v51_m3_tier_t2.py                # V5.1 minimax-m3 tier T1→T2
2026_09_03_v60_glm_flash_vision.py          # V6.0 Phase1 vision 分级 + GLM 免费模型 + P0-3 收窄
2026_09_03_v60_phase2_cost_audit.py         # V6.0 Phase2 标价双轨（registry 标价列 + usage_logs.list_cost_estimate + 回填）
```

> 生产环境（ubuntu2401）只跑幂等 migration，**不要**跑 `seed_model_groups.py` /
> `refresh_siliconflow_models.py`（先清后插会丢同步模型与审计历史）。V6.0 起两个
> `_v60_*` 迁移支持重复执行（已存在列/已有标价自动跳过，不覆盖人工修正）。

## 2. 完整迁移顺序（reset_db 后）

```bash
cd ~/aimc
source .venv/bin/activate

# === 阶段 1: 初始化 (Base.metadata.create_all) ===
python db/init_db.py
# 创建所有表 (Base.metadata.create_all), 但 ALTER 列需 migration

# === 阶段 2: 老的 4 个 (历史) ===
python scripts/seed_model_registry.py
python scripts/seed_model_groups.py
python scripts/manual_import.py
python scripts/refresh_siliconflow_models.py

# === 阶段 3: 21 commit 整改的 7 个 migration (按依赖顺序) ===
# a. FK + UNIQUE (P0-1)
python db/migrations/2026_08_14_p01_mgm_fk_unique.py --apply

# b. price_snapshots 加 cache 价列 + raw_model_id (P0-2)
python db/migrations/2026_08_14_p02_price_cache_schema.py --apply

# c. accounts 加 encryption_version 列 (P0-5)
python db/migrations/2026_08_14_p05_encryption_version.py --apply

# d. account_quotas + account_quota_usage 表 (P0-6)
python db/migrations/2026_08_14_p06_account_quotas.py --apply

# e. 方案A: model_change_suggestions + group_change_suggestions 表
python db/migrations/2026_08_14_p_suggestions_tables.py --apply

# f. channels 加 api_base / api_style / supports_prompt_cache (P1-12)
python db/migrations/2026_08_14_p112_channel_api_base.py --apply

# g. usage_logs 加 cached_tokens 3 列 (P0-3 follow-up)
python db/migrations/2026_08_14_p03followup_cache_tokens.py --apply

# === 阶段 4: 阶段2 scheduler 相关 ===
# h. 删旧 crawl_* jobs (scheduler cleanup)
python db/migrations/2026_08_14_p2_scheduler_cleanup.py --apply

# i. job_health 表 + listener 准备
python db/migrations/2026_08_14_p2_job_health_table.py --apply

# === 阶段 4: V4.6.4 ~ V4.8 新增 migration ===
# j. channels 加 disable_thinking_pass_through (V4.6.4)
python db/migrations/2026_08_17_p_disable_thinking.py --apply

# k. users + api_keys 表 + usage_logs.caller_id (V4.8 多用户)
python db/migrations/2026_08_19_v48_multi_user.py --apply

# l. channels 加 drop_params (P2-B)
python db/migrations/2026_08_20_p2b_drop_params.py --apply

# === 阶段 4b: V4.9.3 ~ V6.0 新增 migration ===
# m. request_transcripts 表 (V4.9.3)
python db/migrations/2026_08_25_request_transcripts.py --apply

# n-o. SenseNova 渠道/模型/价格 + 修复 (V4.9.5, 3 个按需)
python db/migrations/2026_08_25_add_sensenova_models.py --apply
python db/migrations/2026_08_25_fix_deepseek_pricing.py --apply
python db/migrations/2026_08_25_fix_tier_corruption.py --apply

# p. minimax-m3 tier T1→T2 (V5.1)
python db/migrations/2026_09_02_v51_m3_tier_t2.py --apply

# q. vision 分级 + GLM 免费模型 + P0-3 收窄 (V6.0 Phase 1)
python db/migrations/2026_09_03_v60_glm_flash_vision.py --apply

# r. 标价双轨成本审计 (V6.0 Phase 2; 幂等, 可重复跑补新标价)
python db/migrations/2026_09_03_v60_phase2_cost_audit.py --apply

# === 阶段 5: 价格同步 (脚本, 不是 migration) ===
python scripts/sync_siliconflow_cache_prices.py --apply   # 9 行 cache 价 (兼容)
python scripts/sync_siliconflow_prices.py --apply          # 53 行 input/output/cache (阶段B)

# === 阶段 6: 重启 ===
systemctl --user restart aimc

# === 阶段 7: 验证 + 测试 ===
python -m pytest tests/ -q   # 全量绿 (存量 4 个 e2e teardown FK 失败除外, 见 V6 计划 §4)
```

## 3. 验证清单

跑完 1-7 步后：

```bash
# 1. 表 + FK 完整性
sqlite3 data/aimc.db "PRAGMA foreign_key_check"
# (空输出 = OK)

# 2. 新表存在
sqlite3 data/aimc.db "
  SELECT name FROM sqlite_master
  WHERE type='table' AND name IN (
    'account_quotas','account_quota_usage',
    'model_change_suggestions','group_change_suggestions',
    'job_health'
  )
"
# 期望: 5 行

# 3. 价格数据
sqlite3 data/aimc.db "
  SELECT channel_id, COUNT(*) FILTER (WHERE input_price IS NOT NULL) AS has_input,
                     COUNT(*) FILTER (WHERE cached_input_price IS NOT NULL) AS has_cache,
                     COUNT(*) AS total
  FROM price_snapshots GROUP BY channel_id
"
# SF (channel 3): has_input=53, has_cache>=53, total=91

# 4. 限额配置
sqlite3 data/aimc.db "SELECT dimension, COUNT(*) FROM account_quotas GROUP BY dimension"
# 期望: balance_cny 6, concurrent 6, rpm/tpm/daily/weekly/5h 组合 ~18 行

# 5. 测试套件
python -m pytest tests/ -v | tail -3
# 期望: 547 passed

# 6. e2e
KEY=$(grep ^AIMC_API_KEY= .env | cut -d= -f2)
for m in tier:flagship tier:strong tier:balanced tier:light tier:code; do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST http://localhost:8080/v1/chat/completions \
    -H "Content-Type: application/json" -H "Authorization: Bearer $KEY" \
    -d "{\"model\":\"$m\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":3}")
  echo "[$m] HTTP=$STATUS"
done
# 期望: 5 行 200
```

## 4. 单个 migration 的 --dry-run / --apply

每个 migration 都支持 dry-run 默认：

```bash
# 看会做什么（不真改）
python db/migrations/2026_08_14_p01_mgm_fk_unique.py

# 真改
python db/migrations/2026_08_14_p01_mgm_fk_unique.py --apply

# 验证
sqlite3 data/aimc.db "SELECT COUNT(*) FROM model_group_members WHERE group_id NOT IN (SELECT group_id FROM model_groups)"
# 期望: 0 (孤儿被清掉)
```

## 5. 回滚策略

AIMC 当前 migration 是**正向**为主，**没有 down() 函数**。回滚靠：

1. **db 备份**（最重要）：迁移前必备份
2. **代码回滚**：git checkout <before-migration-commit>
3. **db 表重建**：Base.metadata.create_all 重建表（会丢数据，**不推荐生产**）

```bash
# 迁移前必做:
sqlite3 data/aimc.db ".backup /backup/aimc-pre-2026_08_15.db"

# 如果迁移失败, 还原:
cp /backup/aimc-pre-2026_08_15.db data/aimc.db
```

## 6. 加新 migration 的规范

详见 [dev/conventions.md §db 迁移规范](../dev/conventions.md#db-迁移规范)。

模板：

```python
# db/migrations/YYYY_MM_DD_p_<scope>.py
"""<一句话描述>

背景:
- <原因>

本改动:
- ALTER TABLE xxx ADD COLUMN ...
- CREATE TABLE yyy (...)

用法:
    python db/migrations/YYYY_MM_DD_p_<scope>.py            # dry-run
    python db/migrations/YYYY_MM_DD_p_<scope>.py --apply   # 实际迁移
"""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))


def get_db_path():
    import config
    return config.settings.db_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db_path = get_db_path()
    print(f"db path: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Inspect + dry-run print
    # ...
    if not args.apply:
        print("[DRY-RUN] 加 --apply 才执行")
        conn.close()
        return
    # Apply
    try:
        conn.execute("BEGIN")
        # ... actual ALTER ...
        conn.execute("COMMIT")
    except Exception as e:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
```

## 故障排查

| 症状 | 检查 |
|------|------|
| 迁移报错 "table already exists" | `Base.metadata.create_all` 已建表, 直接跳过迁移 |
| 迁移报错 "duplicate column" | 列已存在, 检查 `pragma_table_info` |
| 迁移后 test 挂 | `pytest tests/ -v` 看具体哪个; migration 没写完 |
| 迁移后服务启动报错 | `journalctl --user -u aimc -n 30` 看 traceback (可能是 ORM 列对不上) |
| 旧 db 升级 | 跑 12 个 migration; 备份必须先做 |
