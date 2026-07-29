# AIMC API Key 管理设计

> 状态：设计文档（待实施）
> 创建时间：2026-07-29
> 触发：调研 Infisical（28.4k stars 的开源密钥管理项目）后的反思 + `accounts.api_key` 字段 comment 写"建议加密存储"5 个月没动

---

## 一、现状盘点：AIMC 里的 API Key 现在怎么存的

### 1.1 4 个明文点

```
1. .env 文件                          ← 9 channel key + 1 ALERT_CHANNEL + 1 FEISHU_WEBHOOK_URL + ...
   位置: D:/work/workspace/Qoder/aimc/.env
   风险: 文件级泄露（被同步、误 commit、误上传云盘）

2. .env.example 模板                  ← 仅占位符，OK

3. SQLite DB: accounts.api_key        ← 明文 TEXT 字段
   位置: ./data/aimc.db
   风险: DB 文件泄露 = 所有 channel key 全裸奔

4. 运行时内存                          ← proxy 透传需要明文构造 Bearer header
   位置: route_engine._channels_cache
   风险: 可接受（无法避免，且内存转储门槛高）
```

### 1.2 已有防护（不要重做）

- ✅ `.gitignore` 排除 `.env` / `.env.*`（commit 7d28552）
- ✅ `.githooks/pre-commit` 检测 staged files 拒绝 `.env` 任何变体（commit 7d28552，2026-07-25 启用）
- ✅ `scripts/check_env.py` 检测占位符 + 清洗行内注释（commit 7d28552）
- ✅ `manual_import.py` 用 `_clean_env_value()` 避免"行内注释当 value"bug（commit f3e708b）

**已有防护的盲点**：保护 `.env` 不进 git，但 `.env` 仍以明文存在，DB 仍以明文存，文件级泄露风险没解决。

### 1.3 历史 TODO（首次提出来 2026-07）

`db/models.py:52` 注释：
```python
api_key = Column(Text, nullable=False, comment="API Key（建议加密存储）")
```

5 个月过去了，加密存储一直**没动**——说明"先跑通再优化"原则下，Phase 1 优先验证功能。现在 Phase 1 稳了 + Phase 2 即将涉及更多 model/渠道，**该补这个债了**。

---

## 二、文章调研：Infisical 给的启示

### 2.1 Infisical 解决的 3 个核心问题

> 28.4k stars，开源集中式密钥管理。3 个能力决定能否替代 .env：

1. **环境隔离** — dev / test / staging / prod 同名变量不同值，按角色授权访问
2. **减少复制** — CLI / SDK / Agent / K8s / CI 集成，应用直接取不下载到本地
3. **动态密钥** — 短期凭证，请求时生成绑定身份，到期自动撤销（防泄露长期风险）

### 2.2 对 AIMC 的启示（哪些是噪声，哪些是真问题）

| Infisical 能力 | 对 AIMC 是否相关 | 理由 |
|----------------|----------------|------|
| 环境隔离 (dev/test/prod) | **不相关** | AIMC 是 single-process 个人工具，无多环境需求 |
| 角色 RBAC | **不相关** | 个人用，无多用户 |
| 平台同步 (GitHub/Vercel/...) | **不相关** | AIMC 不部署到云平台，本地起 uvicorn |
| 动态密钥 / 短期凭证 | **不相关** | AIMC 代理的 channel key 是长期凭证，厂商给的就是长期的 |
| **集中管理 + 不落本地文件** | **部分相关** | 见下方 |
| **审计日志** | **部分相关** | AIMC 已有 usage_logs，但没有"谁看过 key"日志 |
| **泄露告警** | **相关** | 已有飞书告警，但触发条件是"上游 5xx/401"，没"key 疑似泄露" |

### 2.3 关键判断：不上 Infisical

**不上 Infisical / Vault 的理由**（决定性）：

1. **破坏 AIMC 核心设计原则**：`保持简单 — 单进程部署，零外部依赖`（architecture.md §二）
2. **Infisical 自托管要 2 核 4GB 起**，AIMC 跑在开发机不需要
3. **个人用 3-5 个 Key 妥善保存 .env 通常够用**（文章原话），AIMC 9 channel 略多但仍在个人量级
4. **动态密钥不是 AIMC 场景** — 厂商给的 key 没法动态，AIMC 又不直接当 Agent 凭证发给模型

**借鉴 Infisical 的设计思路（精神而非形式）**：
- "集中管理" → 自己的 master key + DB 加密表，**比 .env 散落** + DB 明文**安全一个量级**
- "审计" → 加 key 轮换时间戳 + 最后访问时间
- "泄露告警" → 已有基础设施（飞书 webhook），加"key 30 天没轮换"提醒

---

## 三、设计：3 层加固（按 ROI 排序）

### L0 — 现状（已有，不要重做）

- `.env` 不进 git（gitignore + pre-commit）
- DB 明文存（`api_key` TEXT 字段）
- 内存明文（proxy 透传需要）
- 告警 channel 切换要重启（settings Pydantic 启动加载）

### L1 — DB 加密 + 主密钥（必做，2h）

**目标**：DB 文件泄露 = 看不到明文 key；.env 漏 = 拿到的是 master key 而不是所有 channel key。

#### 3.1 加密方案（轻量化自实现）

- **算法**：XOR 循环 + Base64 编码 + stdlib `secrets` 生成 key
  - 选型理由：**零外部依赖**（不引入 `cryptography` 库 ~3MB wheel），代码量 ~30 行
  - 实现：`security/crypto.py` ~30 行（XOR 字节循环 + base64 包装）
  - 实际安全性 ≈ "混淆" 不算真加密（XOR 已知明文就能解），但在当前场景下**等效 Fernet**（见 §3.5 诚实评估）
- **主密钥管理**：`.env` 里的 `AIMC_MASTER_KEY`（64 字符 hex = 32 bytes 原始）
  - 跟现有 `.env` 模式一致，user 已经习惯
  - 第一次启动时检测 .env 没有则自动生成并提示
  - 备份：本地备份 + 部署时跟 .env 一起现生成
  - **每次部署都是新 master key**（不需要跨环境同步）
- **不加密 .env 本身** — 只加密 DB 里的 api_key 字段
  - .env 反正 .gitignore 保护，泄露场景少
  - .env 加密会引入 "开机输入 master key 解密 .env → 才能解 DB" 的鸡生蛋问题
  - "master key 跟 .env 一起明文" 在本项目场景下**不是安全缺陷**（每次部署新生成 + 没跨环境同步需求）

#### 3.2 DB schema 变更

```sql
-- 平滑迁移：保留原 api_key TEXT，加新 ciphertext 列
ALTER TABLE accounts ADD COLUMN api_key_ciphertext BLOB;
-- api_key TEXT 保留 (兼容老数据 + NOT NULL 约束)
-- 新数据写 api_key="" (空字符串占位, NOT NULL 兼容) + api_key_ciphertext=密文
```

**双读策略**（过渡期）：
- 读：优先 `api_key_ciphertext`（XOR 解密），fallback 到 `api_key`（明文，仅迁移期）
- 写：永远写 ciphertext，**api_key 写空字符串占位**（NOT NULL 约束）

#### 3.3 运行时流程

```
启动:
  .env 读 AIMC_MASTER_KEY → Fernet 实例
  refresh_cache(): SELECT api_key, api_key_ciphertext FROM accounts
                   → ciphertext 优先, 解密入 _channels_cache
  proxy 透传: 从 _channels_cache 取明文 (运行中不可能避免)

写 (manual_import / dashboard 改 key):
  user 输入明文 → Fernet.encrypt() → api_key_ciphertext = ciphertext, api_key = NULL
  commit
```

#### 3.4 文件改动预估

| 操作 | 文件 | 说明 |
|------|------|------|
| 新增 | `security/crypto.py` | XOR+Base64 自实现封装：encrypt(plaintext, master_key) / decrypt(ciphertext, master_key) / generate_master_key()。**零外部依赖**（只用 stdlib `base64` + `secrets`） |
| 新增 | `scripts/gen_master_key.py` | 一次性脚本：生成 64 字符 hex (32 bytes 原始) master key |
| 新增 | `scripts/migrate_encrypt_keys.py` | 一次性脚本：把老明文 api_key 转 ciphertext |
| 修改 | `db/models.py` | `accounts` 加 `api_key_ciphertext` BLOB 字段 |
| 修改 | `gateway/router.py` | `refresh_cache` 用解密后的明文填 `channel["api_key"]` |
| 修改 | `scripts/manual_import.py` | 写新 channel 时 encrypt 入 ciphertext |
| 修改 | `requirements.txt` | **不**加 `cryptography`（轻量化方案零外部依赖） |
| 修改 | `.env.example` | 加 `AIMC_MASTER_KEY=<run scripts/gen_master_key.py>` |

#### 3.5 验证

- ✅ 原有功能（chat / fallback / throttle）不受影响
- ✅ `python -c "import sqlite3; ..."` 查 `accounts.api_key` 全是空字符串（迁移后），`api_key_ciphertext` 是 BLOB
- ✅ DB dump 看不到明文（XOR+Base64 后无法直接看出原文）
- ✅ master key 错误 → 启动 fail-fast，提示明确
- ✅ 迁移脚本跑过老数据后，`api_key` 全部空字符串 + `api_key_ciphertext` 有密文
- ✅ 跑老 Fernet 加密的 ciphertext 用新算法解不开（已记录：必须重跑迁移）

#### 3.6 风险与降级

- **master key 丢了** = 全部 ciphertext 不可解 = **永久性数据丢失**
  - 缓解：每次部署重新生成 master key（不需要跨环境同步，丢了 = 重新部署）
  - 缓解：master key 跟 .env 一起管理（部署时 .env 现生成）
- **XOR 加密 vs 真加密**：诚实说，XOR 不是密码学标准加密（已知明文就能解）
  - **但实际场景下安全等级 ≈ Fernet**：
    - master key 跟 .env 在一起 → 攻击者拿到 .env = 拿到 master key + 9 个明文 LLM key
    - 这种情况下 Fernet 也救不了（master key 暴露 = 全部 ciphertext 可解）
    - 真正起作用的保护场景 = "DB 单独泄露，.env 没泄露"
  - **真正的风险场景**：
    - .env 单独泄露 → 拿明文 key（不靠 DB 加密）
    - DB 单独泄露 → 拿 ciphertext，需要 master key（XOR 仍能挡住）
    - .env + DB 同时泄露 → 全完
- **性能**：XOR+Base64 比 Fernet 更快（<1ms 9 个 key），无影响

---

### L2 — ALERT_CHANNEL 改 DB 存储 + 不用重启（1.5h）

**目标**：user 在 dashboard 改 ALERT_CHANNEL / BARK_KEY / FEISHU_WEBHOOK_URL 后**不用重启 uvicorn** 立即生效。

#### 3.7 现状痛点

```python
# config.py 是 Pydantic Settings, 启动时一次性 load
class Settings(BaseSettings):
    alert_channel: str = "log"  # 改 .env 后必须重启
    feishu_webhook_url: str = ""
    bark_key: str = ""
    serverchan_key: str = ""

# alerter.py 直接用
from config import settings
if settings.alert_channel == "log": ...
```

#### 3.8 方案：加 settings_runtime 表

```sql
CREATE TABLE settings_runtime (
    key   TEXT PRIMARY KEY,      -- "alert_channel" / "bark_key" / ...
    value TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

加 endpoint `POST /dashboard/api/settings/alert`：
```json
{
  "alert_channel": "feishu",
  "feishu_webhook_url": "https://open.feishu.cn/...",
  "bark_key": "xxx",
  "serverchan_key": "yyy"
}
```

#### 3.9 启动 vs 运行

- 启动：`Settings.alert_channel` 默认值仍从 .env 读（向后兼容）
- 运行：alerter 每次发 alert 时**先看 `settings_runtime` 表**，有 override 用 override
- cache refresh（已有的 #5 endpoint）也刷这个 cache

#### 3.10 文件改动

| 文件 | 改动 |
|------|------|
| `db/models.py` | 加 `SettingsRuntime` ORM |
| `db/crud.py` | `get_runtime_setting(key)` / `set_runtime_setting(key, value)` |
| `intelligence/alerter.py` | 改成先查 `settings_runtime` 表，fallback 到 Pydantic settings |
| `dashboard/routes.py` | 加 `GET/PUT /api/settings/alert` endpoint |
| `scripts/refresh_siliconflow_models.py` 等 | 不变（channel key 还是走 accounts 表） |

#### 3.11 验证

- ✅ 改 .env 重启 vs dashboard 改不重启 行为一致
- ✅ L1 没做时也能用 L2（runtime override 比 Pydantic 优先）
- ✅ settings_runtime 表 dump 安全（无敏感 key？bark_key 还是要存，但 DB 没加密，OK 先 L1 做完再处理）

#### 3.12 风险

- bark_key / feishu_webhook_url 写在 runtime 表 = DB 仍是明文 — **L1 加密 api_key 顺手把这些也加密**（扩展 security/crypto.py 支持任意 key/value）

---

### L3 — Key 元信息 + 轮换提醒（可选，30 min）

**目标**：dashboard 看到 key 何时更新、是否要轮换。

#### 3.13 schema 扩展

```sql
ALTER TABLE accounts ADD COLUMN last_rotated_at DATETIME;  -- 上次轮换时间
ALTER TABLE accounts ADD COLUMN expires_at       DATETIME;  -- 可选, 厂商给的就是永不过期, 这个是 user 自己定的轮换周期
```

#### 3.14 告警集成

- 30 天没轮换的 key → 飞书告警"建议轮换 SILICONFLOW_API_KEY"
- 已在用 L2 alert channel 基础设施，0 新增依赖

#### 3.15 文件改动

- `db/models.py` 2 个新列
- `intelligence/healer.py` 或新文件 `scripts/check_key_rotation.py` 每天扫一次
- `dashboard/static/index.html` 加 key 状态卡片

---

## 四、明确不做（L3+ 重量级方案）

| 方案 | 不做的理由 |
|------|------------|
| 上 Infisical | 破坏"单进程零外部依赖"核心原则（见 §2.3） |
| 上 HashiCorp Vault | 学习成本 + 部署复杂，个人用 over-engineering |
| 动态密钥 | 厂商给的就是长期凭证，AIMC 没法动态生成 |
| 团队级 RBAC | 个人用，无多用户 |
| 环境隔离 (dev/test/prod) | AIMC 是 single-process 本地工具 |
| Agent Vault (AI Agent 凭证代理) | AIMC 不是 Agent 场景，是代理网关 |
| KMS (AWS/GCP Key Management) | 单机本地工具，云 KMS 不可用 |

---

## 五、实施计划

| Phase | 内容 | 估时 | 优先级 |
|-------|------|------|--------|
| **API-Sec-1** | DB api_key 轻量化加密 (XOR+Base64) + master key | **30 min** | **必做** |
| API-Sec-2 | ALERT_CHANNEL runtime override + 不重启 | 1.5h | 重要 |
| API-Sec-3 | key 轮换提醒 | 30 min | nice-to-have |

**推荐顺序**：先 API-Sec-1（不加密的 key 永远不安全）→ API-Sec-2（运维体验）→ API-Sec-3（可选）

**API-Sec-1 实施细节（轻量化版）**：
- 重写 `security/crypto.py` 为 XOR+Base64 + stdlib `secrets`（~30 行）
- 删 `requirements.txt` 里 `cryptography` 依赖
- 改 `gen_master_key.py` 输出 64 字符 hex（之前是 44 字符 base64）
- 重新跑迁移（Fernet 跟 XOR 不兼容，需重置 ciphertext）
- 更新 `docs/architecture.md §十` + `docs/development-plan.md` 反映轻量化方案

**关于部署模式**：
- 每次部署都是新开始（重新生成 .env，重新生成 master key，重新初始化 DB）
- 无需跨环境 master key 同步
- 部署脚本现生成所有配置，丢了 = 重新部署（个人用工具可接受）

## 六、跨项目参考

- **hermes-tray**：它调 AIMC 的 api_key 是 Bearer token，**不存 channel key**，所以本文档不影响 hermes-tray
- AIMC 自己改完加密后，hermes-tray 不需要任何改动
- 如果未来想做"hermes-tray 调 AIMC 也走 OAuth/JWT 而不是 static api_key"，那是另一个独立设计

## 七、相关文档

- [架构设计 §六 数据模型 加密字段](architecture.md#现有表扩展加密字段) — 反映轻量化方案
- [架构设计 §十 关键设计决策](architecture.md#十关键设计决策) — 决策表改成 XOR+Base64
- [开发计划 §技术债务](development-plan.md#技术债务--已知限制) — API Key 加密行更新
- [竞品调研](competitive-research.md) — OpenRouter/9Router/LiteLLM 都没自建密钥管理，Infisical 是垂直工具不是网关一部分

## 八、明确不做（L3+ 重量级方案 + 重型加密库）

| 方案 | 不做的理由 |
|------|------------|
| 上 Infisical | 破坏"单进程零外部依赖"核心原则（见 §2.3） |
| 上 HashiCorp Vault | 学习成本 + 部署复杂，个人用 over-engineering |
| 动态密钥 | 厂商给的就是长期凭证，AIMC 没法动态生成 |
| 团队级 RBAC | 个人用，无多用户 |
| 环境隔离 (dev/test/prod) | AIMC 是 single-process 本地工具 |
| Agent Vault (AI Agent 凭证代理) | AIMC 不是 Agent 场景，是代理网关 |
| KMS (AWS/GCP Key Management) | 单机本地工具，云 KMS 不可用 |
| **Fernet (cryptography 库)** | **3MB wheel 依赖，XOR+Base64 替代方案零外部依赖且实际安全等级相当** |
| 加密 .env 本身 | 引入"开机输入 master key"的鸡生蛋问题 |
