# 下一步编码计划 (2026-08-20)

> 上一轮 P1 测试隔离 + P2 历史测试债已清零 (commit `7afa113`, 391 passed / 4 skipped)。
> 本轮基于 **VM 真实运行数据** (192.168.56.12 / ubuntu2401, 部署 8 天) + **多用户需求 (3~5 人)** 制定。

---

## 一、VM 实况盘点 (2026-08-20 采集)

| 项 | 状态 |
|----|------|
| 部署方式 | systemd user unit `aimc.service`, enabled 自启, 22h uptime |
| 版本 | commit `1242108` — **落后本地 3 commits** (`f6c841c` P0 修复 / `7afa113` NULL 价格 + cooldown hook) |
| 资源 | 内存 1.1G/3.8G, 磁盘 21G/37G (60%), Playwright 3 driver 进程正常 |
| 数据 | 8 天 677 请求; tier:strong 组路由为主力 (7 天 242 次 / 6.5M tokens), minimax 系列重度使用 |
| 错误分布 | 200=595, **400=67 (12%)**, 429=11, 404=4 |
| 400 来源 | 几乎全是 `tier:vision` / `glm-4.5v` (组路由 vision 场景) |
| 429 恢复 | failover 正常 (429 当天后续 314 次 200) |
| NULL 价 | 38/109 snapshots (siliconflow 1 channel) — NULL 价格 bug 在生产真实影响 |
| 审计缺口 | `usage_logs` 无 caller 字段, 无法区分调用方 (单 key 设计) |

**两个数据驱动的结论**:
1. **4xx 占比 12% ≥ 2%** → V4.7 `drop_params` 触发条件已满足 (原设计阈值)
2. 400 集中在 tier:vision → 不是零散问题, 是 vision 组路由一个点的问题

---

## 二、P0: VM 同步部署 (立即, ~0.5h)

生产在跑已知 bug (NULL 价格误路由 + cooldown hook 断线), 必须先上修复:

```bash
wsl ssh ubuntu2401
cd ~/aimc
cp data/aimc.db data/aimc.db.bak.$(date +%m%d)   # 备份
git pull
systemctl --user restart aimc
systemctl --user status aimc   # 验证
curl -s localhost:8080/health   # 冒烟
```

**验收**: 服务起来 + dashboard 能开 + 按天 usage_logs 继续增长。

---

## 三、P1: V4.8 多用户基础 (3~5 人, ~2-3 天)

原 P3 审计系统 (caller_id) 与 V4.7 "Virtual Key" 候选合并为本阶段。
3~5 人是家庭/小团队场景, 刻意做轻:

### 做

| 项 | 设计 |
|----|------|
| `users` + `api_keys` 表 | 多 key, 每 key 归属一个 user; `aimc_master_key` 降级为 admin key |
| 鉴权改造 | middleware: 单 master key 比对 → 查 `api_keys` 表 (内存缓存, 失效 60s); 未知 key 401 |
| `usage_logs.caller_id` | P3 审计落地; 从 key 推导, `_write_usage_log` 写入 |
| per-user 限额 | 日/月 token 上限 + 余额预警, 复用 `account_quotas` 7 维模式中 daily/monthly 两维 |
| Dashboard | 按人聚合视图 (用量/花费/4xx); admin 视角加 key 管理页 |
| CLI | `scripts/add_user_key.py` / `list_user_keys.py` (admin 手动发 key) |

### 不做 (3~5 人不需要)

- ❌ 注册/自助开通流程 (admin 手动发 key 即可)
- ❌ 组织/团队层级、RBAC
- ❌ 计费/账单 (个人摊算用周报按人聚合即可)
- ❌ 并发写压力优化 (SQLite WAL + 5 人 QPS 远够)

### 验收

- [ ] 2 个 key 并发调用, usage_logs 按 caller_id 正确归属
- [ ] 某 key 日限额打满 → 该 key 429, 其他 key 不受影响
- [ ] admin key 能管理 (增删) 普通 key, 普通 key 打不开管理页
- [ ] 单测: 鉴权矩阵 (无 key/错 key/过期 key/admin key) + 限额触发

---

## 四、P2: 数据驱动修复 (V4.7 首启, ~1-2 天)

### P2-A: tier:vision 400 专项 (4xx 大头, 先修)

67 个 400 几乎全是 vision 场景。排查方向:
1. 组路由是否给纯文本请求选了 vision-only model (或反之, P0-3 逻辑对 vision 的边界)
2. body 参数 (image_url 格式 / max_tokens 上限) 跨厂商不兼容
3. `_get_fuzzy_price` 对 `glm-4.5v` 的误匹配

### P2-B: drop_params (LiteLLM 借鉴)

4xx 占比 12% (阈值 2%), 条件满足。跨模型转发时自动丢弃目标 channel 不支持的参数:
- channel 维度配置 `supported_params` (或按 provider 预设)
- 转发前过滤 body; 丢弃记 debug 日志 + 响应头 `X-AIMC-Dropped-Params`

### 验收

- [ ] vision 场景 400 率从 ~12% 降到 < 3%
- [ ] drop_params 单测 + 真实 vision 请求冒烟

---

## 五、P3: 工程加固 (~1 天)

1. **CI e2e 最小档**: uvicorn 起服 + `httpx.MockTransport` mock 上游, 把 4 个 skip 的
   `test_phase2_e2e` 变成可自动跑 (守住集成点 — 本轮 NULL 价格/cooldown 两个断线 bug 都是单测全绿但链路不通)
2. **周报自动化**: VM 上 cron 每周一 `analyze_usage.py` + `generate_weekly_report.py`,
   报告推送 (数据积累给 V4.7+ 后续决策)

---

## 六、执行顺序

1. **P0 VM 同步** (0.5h, 今天)
2. **P1 V4.8 多用户** (2-3 天) — 表 + 鉴权 + caller_id 先行, 限额/Dashboard 次之
3. **P2-A vision 400** (1 天) — 可与 P1 并行排查
4. **P2-B drop_params** (0.5-1 天)
5. **P3 CI + 周报** (1 天)

V4.7+ 其余候选 (语义缓存/智能调度/价格趋势) 继续等 `analyze_usage.py` 稳定期信号。

---

## 附: 上一轮 (2026-08-19) 完成记录

- ✅ P1 测试隔离 (conftest 快照策略修正, 62 污染失败 → 0)
- ✅ P2 真实失败 37 个全清 (含 2 个生产级 bug: NULL 价格路由回归 / `_update_cooldown` hook 断线)
- ✅ phase2_e2e 标 integration marker
- ✅ 最终: 391 passed / 4 skipped (commit `7afa113`)
