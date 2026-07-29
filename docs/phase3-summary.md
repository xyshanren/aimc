# Phase 3 总结 (2026-07-25)

**范围**：让 AIMC 网关从"能跑"升级到"敢用"——补全日常运维中会触到的几个真实痛点。

## 改动一览

| 编号 | 改动 | commit | 解决什么问题 |
|------|------|--------|------------|
| #1 | 拉硅基流动 91 个 model 入库 | `636c334` | `/v1/models` 只列 2 个，掩盖了"硅基流动 1 key 通吃 100+" 的核心能力 |
| #3 | 告警 throttle (同 key 60s 只发 1 次) | `244f279` | 硅基流动挂掉时飞书会刷屏 |
| #4 | pre-commit hook 防 `.env` 误 commit | local config | 保护真实 API key 不进 git |
| #5 | `POST /dashboard/api/cache/refresh` 手动刷新 | `e50bb7a` | 改完 channel key / model 不用等 5 分钟自动刷新 |

未做：**#2** router 503 告警 — fallback 启用后几乎不返回 503，价值小，留作未来。

---

## #1 — 硅基流动全 model 入库 (`636c334`)

**背景**：`/v1/models` 端点的实现是直接 `select` `price_snapshots.model` 字段，**手填** 2 个 model 意味着用户看到的就是 2 个，看不见 AIMC 网关真实能代理的能力。

**做法**：
- 新增 `scripts/refresh_siliconflow_models.py`，调硅基流动 `/v1/models` 拿 91 个 model
- 给 `siliconflow_aggregator` channel 全部入 `price_snapshots`（`input_price/output_price` 留空，聚合平台无自有价格）
- 已存在的 model 跳过，**idempotent** 可重跑

**验证**：
- `GET /v1/models` 从 2 个 → 91 个
- `zai-org/GLM-4.5V` chat 测试 200 OK
- 重跑脚本 = 0 新增

**用户使用**：
```bash
python scripts/refresh_siliconflow_models.py
# 重启 uvicorn 让 router cache 加载新 model
# 或者 POST /dashboard/api/cache/refresh
```

---

## #3 — 告警 throttle (`244f279`)

**背景**：Phase 2E 加的 proxy 失败告警触发条件是 5xx + 401/403。硅基流动一旦挂掉，1 把 key 反复 401 飞书会被刷屏，1 分钟能收到几十条一样的告警。

**设计**：
- `alerter.send_alert(message, key=None)` 新增 throttle key 参数
- 同 key 在 60s 窗口内只发 1 次
- throttle key 形如 `"{channel_name}:{status_code}"`，不同 status 独立
- **锁内 check+mark 原子完成**，防并发 race（最初没加锁时 3 个并发 401 触发了 2 次飞书）
- `log` 模式 / `key=None` 不 throttle
- 失败也占用 60s 槽位（防 webhook 暂时 503 反复重试刷屏）

**实现位置**：
- `intelligence/alerter.py` — `_try_acquire_throttle(key)` 锁内 check+mark
- `gateway/proxy.py` — `_schedule_alert` 算 `key="{name}:{status}"` 透传

**验证**：
- 单元测试 6/6（同 key / 不同 key / 失败占槽 / log 跳过 / key=None / 并发安全）
- E2E：DB 改假 key → 重启 uvicorn → 连续 3 次 chat → 飞书**只收 1 条**

---

## #4 — pre-commit hook 防 `.env` 误 commit

**背景**：真实 API key 写在 `.env` 里，如果误 commit 进了 git 历史，**1 key 泄露 = 信任丢失**，不可逆。

**已有设施**：
- `.gitignore` 已经排除 `.env` / `.env.*`
- `.githooks/pre-commit`（**这次 commit 之前就存在**，位于 commit `7d28552`）做**第二层防护**

**这次做的**：执行 `git config core.hooksPath .githooks` 让 hook 生效（**local config，不 commit**）。

**验证**：
```
$ git add -f .env
$ git commit -m "test"
[pre-commit] FAIL: 检测到敏感文件, 拒绝 commit:
  - .env  (匹配 pattern: .env)
```

正常文件走 hook 不受影响。

**首次使用**（在新机器上 clone 后）：
```bash
git config core.hooksPath .githooks
```

---

## #5 — 手动 cache refresh (`e50bb7a`)

**背景**：之前 router cache 是启动时从 DB 加载一次，**运行中改 DB 不会自动生效**。原本的 5 分钟自动刷新（`intelligence/scheduler._refresh_route_cache`）已经存在，但改完 channel key 后立即测会被"5 分钟还没到"卡住。

**做法**：
- 新增 `POST /dashboard/api/cache/refresh` endpoint
- 立即调 `route_engine.refresh_cache()`
- 返回 `{ "status": "ok", "channels": N, "models": M }`，让用户看到当前 cache 状态
- 失败 500

**验证**（7 步 E2E）：
1. DB key → fake
2. POST refresh → 200
3. chat → 401 ✓
4. DB key → real
5. chat → 401（cache 还没刷）✓
6. POST refresh → 200
7. chat → 200 ✓

**用户使用**：
```bash
# 改完 .env 跑完 manual_import.py
python scripts/manual_import.py

# 立刻让 router 用新配置（不用重启 uvicorn）
curl -X POST http://127.0.0.1:8080/dashboard/api/cache/refresh
```

---

## 当前能力边界

**改完即生效（不用重启）**：
- channel key、priority、status
- 新增/删除 model
- 新增/删除 account

**仍需重启**：
- 改了 `config.py` 里 Pydantic Settings 字段
- 改了 `gateway/router.py` / `proxy.py` / `intelligence/alerter.py` 等代码

---

## 完整 commit 链（Phase 3 + 关联）

```
e50bb7a feat(dashboard): 手动刷新路由 cache endpoint (#5)
244f279 feat(alerter): throttle 同 key 60s 内只发 1 次 (#3)
636c334 feat(scripts): 拉硅基流动全 model 入库脚本 (#1)
f838549 feat(router): 找不到 model 时 fallback 到 priority 最低 channel (Phase 3 #2, 聚合平台)
b7a2fd5 feat(gateway): proxy 失败时触发 alerter (Phase 2E)
f3e708b fix(env): manual_import 行内注释清洗 + .env.example ALERT_CHANNEL 简化
7d28552 feat(phase2e): ModelCard 21→43 + 5 新 channel + .env 检查机制
```

---

## 下一步候选

- **#2 router 503 告警**（5 min）— fallback 启用后触发率极低，价值小
- **真实跑一次端到端**（10 min）— 改 .env → git commit 看 hook 拒；改 key → POST refresh 看生效
- **Phase 4 规划**（开放）— 你来定方向
