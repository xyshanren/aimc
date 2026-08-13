# Model 数据源真相

> Phase 4 v4.3 (2026-08-13) 加入。背景: commit `1005111` 把 MiniMax family 缩到 5 个 + family 字段用大写, 与官方 API 和 price_snapshots 漂移。fix commit `4a2cfc5` 修了这一处, 本文档描述系统性 fix 的策略。

## §1 三层数据源

AIMC 项目里有 3 个数据源都跟"模型"有关, 角色与权威性如下:

| 数据源 | 角色 | 权威性 | 写入入口 |
|--------|------|--------|----------|
| **官方 API 文档** | 最终权威 (产品实际可用) | 最高 | 仅人工事后核对 |
| **`price_snapshots` 表** | 渠道实际支持 (router 选渠道依据) | 中 (反映渠道 key 是否能调通) | `manual_import.py` / `pricing_writer.py` / `seed_*` |
| **`model_registry` 表** | dashboard / group 路由依据 | 最低 (元信息, 不参与实际调用) | `seed_model_registry.py` (仅 1 处) |

权威链: **官方 API 文档** → `price_snapshots` (实际能调) → `model_registry` (展示元信息)

任何 commit 必须保证三者强一致。漂移会导致:
- dashboard 显示能调, 但 router 找不到渠道 → 400
- router 能调, 但 dashboard 不显示 → 用户看不到
- model_registry 字段格式不一致 (大小写) → 数据库查询错位

## §2 数据流

### 2.1 写入入口

| 入口 | 写入表 | 触发时机 |
|------|--------|----------|
| `scripts/seed_model_registry.py` | `model_registry` | 手动 (Phase 2.2 初始化) |
| `scripts/seed_model_groups.py` | `model_group` + `model_group_member` | 手动 (Phase 2.7) |
| `scripts/seed_model_cards.py` | `model_cards` (benchmark 分数) | 手动 (Phase 2.1) |
| `scripts/manual_import.py` | `channels` + `price_snapshots` | 手动 (Phase 2.3) |
| `intelligence/model_sync.py` | `model_registry` (自动入组) | scheduler 周期任务 |
| `intelligence/pricing_writer.py` | `price_snapshots` (AI 解析后 upsert) | scheduler 周期任务 |

### 2.2 应用层归一化约定

Phase 4 v4.3 引入 `db/normalizers.py`, 4 个函数:

| 函数 | 作用 |
|------|------|
| `normalize_family(s)` | 全小写 + 去空白 + 去 BOM |
| `normalize_model_id(s)` | 全小写 + 去空白 + 去 BOM |
| `validate_model_id(model_id, family)` | namespace 校验: 必须以 `{family}-` 开头 |
| `normalize_capabilities(caps)` | 排序去重 + 小写 |

### 2.3 字段约定

| 字段 | 约定 | 示例 |
|------|------|------|
| `family` | 全小写 | `deepseek` / `qwen` / `minimax` |
| `model_id` | 全小写 + 以 `{family}-` 开头 | `deepseek-v4-pro` / `qwen-max` / `minimax-m3` |
| `capabilities` | 全小写 + 排序去重 (JSON 字符串存) | `["code", "general", "reasoning"]` |
| `tier` | T0 / T1 / T2 / T3 / T4 | `T1` |
| `modality` | text / vision / multimodal | `text` |

不在范围 (本版本不做, 后续 plan):
- DB CHECK 约束 (`family GLOB '[a-z0-9_-]*'`)
- family 字段改 Enum
- 自动修复模式

## §3 检测脚本用法

`scripts/check_model_consistency.py` 是兜底机制, 跑 3 种检查:

| 模式 | 检查内容 | exit code |
|------|---------|-----------|
| `--check db` | db 内一致性 (family 小写 / model_id namespace / registry↔price_snapshots 漂移) | 0=PASS, 1=WARN, 2=ERROR |
| `--check official` | 第三方权威源 cross-check (爬 5 家厂商官方 API 文档) | 0=PASS, 1=WARN, 2=ERROR |
| `--check all` | 1 + 2 顺序执行 | 取最大值 |

### 3.1 手动跑

```bash
# 启动前快速验证 (推荐, 5 秒内完成)
python scripts/check_model_consistency.py --check db

# 月度审查官方源 (慢, 5-10 分钟, 5 家厂商爬取)
python scripts/check_model_consistency.py --check official

# 全检
python scripts/check_model_consistency.py --check all

# CI 集成 (JSON 输出)
python scripts/check_model_consistency.py --check db --json

# 单厂商检查
python scripts/check_model_consistency.py --check official --provider minimax
```

### 3.2 CI / pre-commit 接入

`.githooks/pre-commit` 已扩展, 在以下文件被修改时自动跑 `--check db`:

- `scripts/seed_model_registry.py`
- `scripts/seed_model_groups.py`
- `scripts/seed_model_cards.py`
- `db/normalizers.py`
- `db/crud.py`
- `db/models.py`

`--check official` 因为慢 (爬虫) 不在 pre-commit 触发, 留给月度审查。

### 3.3 失败响应流程

1. **db ERROR (exit 2)** → 必须修代码, 不允许 `--no-verify` 跳过
2. **db WARN (exit 1)** → 通常是 registry 与 price_snapshots 漂移 (渠道加了 model 但没加 registry), 需要补 `seed_model_registry.py`
3. **official WARN** → 厂商发新 model (e.g. DeepSeek-V5), 需要人工事后核对 + 更新 registry

## §4 历史教训

- commit `1005111` (2026-08-13): MiniMax family 缩到 5 个 + 用大写 "MiniMax", 引入漂移
- commit `4a2cfc5` (2026-08-13): 修了 MiniMax family 数量, 但没修大小写, 没修 namespace (qwen2.5-* / step-1-8k / moonshot-v1-8k)
- commit `ae4badf` (2026-08-13): 5 厂商价格采集 source, 但没交叉验证 model 集合
- 本次 (Phase 4 v4.3): 引入 normalizers + 检测脚本 + pre-commit hook, 系统性解决

后续 plan 候选:
- family 字段加 DB CHECK 约束 (更严格)
- 自动修复模式 (`--fix` 自动同步, 但风险大)
- model_group_members 一致性检查
- 月度第三方源审查任务 (scheduler 周期跑 + Bark 告警)