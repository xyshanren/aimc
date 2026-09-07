# 失败链路 + 智能探活 + 人工干预 — 实施计划

## 目标
让 dashboard 「🔴 待处理(N)」tab 一眼看到「哪些账号因什么原因需要人工处理」,并支持一键恢复 / 禁用 / 充值,无需查日志。

## 核心原则
1. **融入已有体系**(alerter/cooldown/dashboard/APScheduler),不建平行系统
2. **状态机驱动**:5 个状态迁移有明确触发条件
3. **错误分类优先于状态**:quota/auth/network 用不同策略,而不是统一 60s 冷却
4. **持久化失败事件**(`usage_logs` 加列),重启不丢上下文
5. **智能探活 ≠ 固定轮询**;按连续失败次数 + 错误类型动态决定下次探活时间

---

## 一、错误分类器(新建 `gateway/error_classifier.py`,~120 行)

```python
class ErrorCategory(str, Enum):
    QUOTA_EXHAUSTED = "quota_exhausted"      # 余额/plan 耗尽
    AUTH_FAILED = "auth_failed"               # key 失效
    RATE_LIMITED = "rate_limited"              # 临时限流
    MODEL_UNSUPPORTED = "model_unsupported"    # 当前 model 不支持
    NETWORK_TRANSIENT = "network_transient"    # 超时/连接错误
    UPSTREAM_5XX = "upstream_5xx"              # 上游 5xx
    CLIENT_ERROR = "client_error"              # 4xx 非 retryable
    UNKNOWN = "unknown"

@dataclass
class ErrorInfo:
    category: ErrorCategory
    error_code: str | None        # 上游 error.code(如 "1113" / "insufficient_quota")
    error_msg: str                 # 上游 error.message(截断 500)
    needs_human: bool              # 是否需人工干预
    suggested_cooldown_seconds: int
    suggested_probe_after_seconds: int | None  # None=不主动探活
```

**分类规则(按优先级匹配首个命中)**:

| 类别 | 触发条件 | needs_human | 冷却 | 下次探活 |
|---|---|---|---|---|
| `quota_exhausted` | 429 或 code 匹配 `1113`/`insufficient_quota`/`余额不足`/`plan 用尽` | True | 3600s | 1800s |
| `auth_failed` | 401/403 或 code 匹配 `invalid_api_key`/`unauthorized` | True | 86400s | None(必须人工) |
| `model_unsupported` | 400/404 或 code 匹配 `model_not_found` | True | 1800s | 600s |
| `rate_limited` | 429 但 code 不在 quota 集合 | False | 30s | 60s |
| `network_transient` | exception TimeoutException/ConnectError | False | 15s | 60s |
| `upstream_5xx` | 500/502/503/504 | False | 60s | 300s |
| `client_error` | 其他 4xx | False | 0 | None |

**关键差异**:quota/auth/model_unsupported **needs_human=True,不主动探活**(避免反复撞死 key)。

---

## 二、CooldownManager 升级(`gateway/cooldown.py`,扩展状态)

### 2.1 `_state[aid]` 字段扩展(向后兼容)

现有 `{count, cooldown_until}` 扩展为:
```python
{
    "count": int, "cooldown_until": float | None,           # 现有
    "first_failure_at": float, "last_failure_at": float,    # 新
    "last_error_type": str, "last_error_code": str | None,  # 新
    "last_error_msg": str,                                  # 新
    "needs_human": bool, "next_probe_at": float | None,     # 新
    "probe_attempts": int, "last_success_at": float | None, # 新
    "manual_disabled_at": float | None,                     # 新
}
```

### 2.2 新增方法

- `mark_failure(account_id, error_info: ErrorInfo)` — 新签名,接收 ErrorInfo
- 保留旧 `mark_failure(account_id, reason="")` 签名(自动 classify)
- `mark_manual_disabled(uid)` / `clear_manual_disabled(uid)`
- `probe_result(uid, success, error_info)` — 探活结果回写
- `accounts_needing_human() -> list[int]` — 列出 needs_human
- `accounts_due_for_probe(now=None) -> list[int]` — 列出该探活的
- `snapshot()` 扩展返回所有新字段

---

## 三、智能探活状态机(核心)

### 3.1 5 个状态

| 状态 | 内存标识 | 进入条件 | 退出条件 | 探活策略 |
|---|---|---|---|---|
| `healthy` | `_state[aid]` 不存在 | 任意一次成功 | 请求失败 | 不主动探活 |
| `failing` | count>0, cooldown_until 未来 | mark_failure | cooldown 过期 | cooldown 后路由重试 |
| `degraded` | next_probe_at>now, needs_human=False | 探活失败 | 探活成功/请求成功 | suggested_probe_after 后探 |
| `needs_human` | needs_human=True | 累计失败次数超阈值 | 人工 ack-recover | **不探活**,等人工 |
| `manual_disabled` | manual_disabled_at!=None | 人工禁用按钮 | 人工启用按钮 | 不探活,路由直接排除 |

### 3.2 needs_human 阈值(配置化)

```python
NEEDS_HUMAN_THRESHOLDS = {
    "quota_exhausted": 2,    # quota 失败 2 次
    "auth_failed": 1,        # auth 失败 1 次
    "model_unsupported": 2,
}
```

### 3.3 探活间隔智能递增

`next_probe_at = max(now + suggested_probe_after, last_probe_at + 2**probe_attempts * 60)`,上限 1h(指数退避)。

---

## 四、持久化(DB Migration)

### 4.1 `usage_logs` 加列

```sql
ALTER TABLE usage_logs ADD COLUMN error_msg TEXT;
ALTER TABLE usage_logs ADD COLUMN error_type TEXT;
ALTER TABLE usage_logs ADD COLUMN error_code TEXT;
ALTER TABLE usage_logs ADD COLUMN failed_channels TEXT;  -- JSON
ALTER TABLE usage_logs ADD COLUMN needs_human INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_usage_logs_error_type ON usage_logs(error_type);
CREATE INDEX idx_usage_logs_needs_human ON usage_logs(needs_human) WHERE needs_human=1;
```

### 4.2 `accounts` 加列

```sql
ALTER TABLE accounts ADD COLUMN last_success_at DATETIME;
ALTER TABLE accounts ADD COLUMN last_error_at DATETIME;
ALTER TABLE accounts ADD COLUMN last_error_type TEXT;
ALTER TABLE accounts ADD COLUMN manual_disabled_at DATETIME;
-- status 新增取值 manual_disabled
```

### 4.3 新表 `account_health_log`(探活时间序列)

`id / account_id / probe_at / triggered_by(scheduled/user_request/recovery_check) / success / status_code / error_type / error_msg / latency_ms`

---

## 五、`gateway/proxy.py` 集成

### 5.1 `_update_cooldown` 重构

```python
def _update_cooldown(channel, response, exception=None):
    info = classify(response.status_code, response.text, exception)
    if info.category == ErrorCategory.CLIENT_ERROR: return
    cooldown_manager.mark_failure(channel["account_id"], error_info=info)
    if info.needs_human:
        await _schedule_alert_with_info(channel, info)  # V4.8.7 漏了 429
```

### 5.2 `_schedule_alert_with_info` 新增

throttle key `f"channel:{name}:needs_human:{error_type}"`,`silence_seconds=3600` 防刷屏。

### 5.3 网络异常路径

`gateway/proxy.py:592-605` 顶层异常 → `cooldown_manager.mark_failure(error_info=classify(0, None, e))`。

---

## 六、`gateway/routes.py` 集成

### 6.1 failed_channels 增强(L214-219)

新增字段:`error_type / error_code / error_msg / needs_human`。

### 6.2 新增响应头 `X-AIMC-Last-Error-Type`

客户端无需 parse JSON 即可判别。

---

## 七、Dashboard 「🔴 待处理(N)」

### 7.1 后端新 API(`dashboard/routes.py`)

```python
GET  /api/pending-summary                     # 聚合所有 needs_human
POST /api/cooldown/{aid}/ack-recover          # 人工「已恢复」+ 立即探活
POST /api/accounts/{aid}/manual-disable       # 人工禁用
POST /api/accounts/{aid}/manual-enable        # 人工启用
GET  /api/cooldown/needs-human                # 详细列表(弹窗用)
```

`pending-summary` 返回结构:
```json
{
  "total": 7,
  "items": [
    {"category": "needs_human_quota", "count": 2, "severity": "danger",
     "label": "💰 余额不足", "anchor": "accounts-meta", "examples": [...]},
    {"category": "needs_human_auth", "count": 1, "label": "🔑 Key 失效", ...},
    {"category": "cooldown_degraded", "count": 3, "label": "⚠️ 自动冷却中", ...},
    {"category": "data_freshness_failed", "count": 1, "label": "📡 数据采集失败", ...}
  ]
}
```

### 7.2 前端(`dashboard/static/index.html`)

在 `col-right` 顶部新增折叠卡片(open 默认展开):
```html
<details class="card-folding" open>
    <summary>
        <span class="card-title-main">🔴 待处理 <span id="pending-total" class="badge badge-danger">0</span></span>
        <span class="card-title-sub">余额 / Key / 探活</span>
    </summary>
    <div class="card-body">
        <div class="accounts-toolbar">
            <button class="btn btn-sm" onclick="loadPendingSummary()">刷新</button>
            <span id="pending-meta" class="meta"></span>
        </div>
        <div id="pending-list"><div class="loading">加载中…</div></div>
    </div>
</details>
```

### 7.3 前端 JS(`dashboard/static/app.js` 末尾新增)

- `loadPendingSummary()` — 30s 自动刷新
- `ackRecover(accountId, btn)` — confirm + prompt(note) + POST
- `manualDisable(accountId, btn)` / `manualEnable(accountId, btn)`

每条 needs_human 卡片显示 3 按钮:**确认恢复** / **禁用账号** / **查看详情**。

### 7.4 CSS(`style.css` 末尾)

新增 `.badge-danger`(背景/边框用 `--color-danger`)+ `.pending-actions` flex 按钮组。

---

## 八、APScheduler 注册

`intelligence/scheduler.py` start_scheduler() 末尾新增 2 个 job:

```python
# V4.9: 智能探活
scheduler.add_job(channel_health_probe,
    IntervalTrigger(seconds=settings.health_probe_interval),
    id="channel_health_probe", replace_existing=True)

# V4.9: cooldown 过期清理(调研发现从未自动调过)
scheduler.add_job(_cooldown_cleanup_task,
    IntervalTrigger(minutes=5), id="cooldown_cleanup", replace_existing=True)
```

新 job 自动获得 `job_health` 表追踪(已有 event listener)。

---

## 九、配置项(`config.py`)

```python
# === Channel 智能探活 (V4.9) ===
health_probe_enabled: bool = True
health_probe_interval: int = 60
health_probe_concurrency: int = 8
health_probe_timeout: float = 5.0
health_probe_max_backoff: int = 3600

# === needs_human 阈值 ===
needs_human_quota_threshold: int = 2
needs_human_auth_threshold: int = 1
needs_human_model_threshold: int = 2
```

---

## 十、测试(5 个)

| 文件 | 覆盖 |
|---|---|
| `tests/test_error_classifier.py`(新) | 8 个 ErrorCategory + 关键字 + status_code + exception 分支 |
| `tests/test_cooldown_state_machine.py`(新) | 5 个状态迁移 + 指数退避 + 人工禁用/恢复 |
| `tests/test_channel_health_probe.py`(新) | mock httpx,scheduled/user-request/recovery 三个路径 |
| `tests/test_pending_api.py`(新) | pending-summary 聚合 + ack-recover / manual-disable 端点 |
| `tests/test_v487_cooldown_429.py`(扩展) | 加 `failed_channels.error_type`/`error_msg` 字段断言 |

---

## 十一、文档

- `docs/agent-integration.md` — 补 `X-AIMC-Last-Error-Type` + `X-AIMC-Failed-Channels` 新 schema
- `docs/admin-manual.md`(或新建) — 「处理余额不足告警」+「手动禁用/启用」+「智能探活工作原理」

---

## 十二、实施顺序(6 个 commit,每步独立可测/可回滚)

**Commit 1**:错误分类器 + 失败链路增强(纯加法)
- 新建 `gateway/error_classifier.py`
- `routes.py` failed_channels 加 error_type/error_msg
- `proxy.py` `_update_cooldown` 用 error_classifier
- `proxy.py` `_schedule_alert` 加 needs_human 告警分支
- 加 X-AIMC-Last-Error-Type 头

**Commit 2**:CooldownManager 状态机化(向后兼容)
- `_state` 字段扩展 + 新方法

**Commit 3**:DB 迁移
- 2 个 migration 脚本 + models.py 加字段 + 新表
- init_db.ensure_migrations 加 ALTER

**Commit 4**:智能探活 job
- 新建 `intelligence/channel_health.py`
- scheduler.py 注册 + cooldown_cleanup

**Commit 5**:Dashboard 「🔴 待处理」tab
- 后端 5 个新 API
- 前端 HTML/JS/CSS 完整

**Commit 6**:文档 + 配置
- config.py 新字段 + docs 更新

---

## 关键风险与缓解

| 风险 | 缓解 |
|---|---|
| 探活打爆上游(余额不足反复探) | needs_human=True 的 account **不主动探活**;指数退避上限 1h |
| 旧 `mark_failure(reason=...)` 调用方丢失 reason | 兼容旧签名,内部自动 classify |
| 关键字匹配误判 | code 字段优先于 message;边界 case 配单测 |
| 探活期间路由又选这个 account | `_is_channel_usable` 已查 cooldown,probe 不阻塞 |
| Dashboard 频繁调 pending-summary(30s) | 全走索引列;不查实时 stream |
| `accounts.status='manual_disabled'` 不识别 | router.py 装载过滤一并加 |

---

## 涉及文件清单

**新建**(8 个):`gateway/error_classifier.py` / `intelligence/channel_health.py` / `db/migrations/2026_08_25_p1_*.py` / `db/migrations/2026_08_25_p2_*.py` / 4 个 test 文件

**修改**(11 个):`gateway/cooldown.py` / `proxy.py` / `routes.py` / `router.py` / `db/models.py` / `db/init_db.py` / `intelligence/scheduler.py` / `dashboard/routes.py` / `dashboard/static/index.html` / `app.js` / `style.css` / `config.py`