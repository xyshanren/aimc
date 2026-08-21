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

### P2-A: tier:vision 400 专项 (4xx 大头, 先修) ✅ 已完成

67 个 400 几乎全是 vision 场景。3 个复合根因 + 5 项修复 (commit `f29ee1e`):

| Fix | 文件 | 改动 |
|-----|------|------|
| 1 | `router.py:1271` | `require_vision` 过滤器接受 `multimodal` (不再误杀 minimax-m3) |
| 2 | `sync_siliconflow_to_registry.py:89` | `infer_modality` 扩展 vision 关键词 (`4.5v`/`glm-v`/`qwen-v`) + family 前缀去重 |
| 3 | `seed_model_registry.py` + `seed_model_groups.py` | 补充 5 个 vision 模型 (glm-4.5v + 4 qwen-vl-*) + tier:vision 成员改名 |
| 4 | `routes.py:193` | 组路由上游 400 自动 failover (精确路由 400 不重试) |
| 5 | `proxy.py:533` | `_resolve_actual_model` 增加 `_channel_model` fallback (400 不再丢 model 名) |

migration: `init_db.py` 补建 `model_registry.raw_ids` / `supports_prompt_cache` 列
测试: `test_vision_routing.py` 17 tests (全量 442 passed / 4 skipped / 0 failed)
部署: VM 已 git pull + 3 步 re-seed + 服务重启, tier:vision 路由冒烟通过

**遗留**: tier:light 的 `qwen3-8b` / `qwen3.5-4b` 命名不匹配 (registry 实际为 `qwen-qwen3-8b` / `qwen-qwen3.5-4b`), 2 个 member 未入库 — 见 P2-B 修复。

### P2-B: drop_params (LiteLLM 借鉴) + tier:light 命名对齐

4xx 占比 12% (阈值 2%), 条件满足。跨模型转发时自动丢弃目标 channel 不支持的参数:
- channel 维度配置 `supported_params` (或按 provider 预设)
- 转发前过滤 body; 丢弃记 debug 日志 + 响应头 `X-AIMC-Dropped-Params`

tier:light 命名对齐 (小修, 可独立先做):
- `seed_model_groups.py` 里 `qwen3-8b` → `qwen-qwen3-8b`, `qwen3.5-4b` → `qwen-qwen3.5-4b`
- 或反向: 让 `sync_siliconflow_to_registry.py` 不给 qwen family 加重复前缀 (与 Fix 2 的 glm 去重逻辑对齐)

### 验收

- [x] vision 场景 400 率从 ~12% 降到 < 3% (P2-A 完成, 待生产数据验证)
- [x] drop_params 单测 (test_drop_params.py 19 passed; 真实 vision 请求冒烟并入观察期)
- [x] tier:light member 全部入库 (5/5, 与 seed_model_groups.py 一致; 文档原"4/6"是旧设计)

---

## 五、P3: 工程加固 (~1 天)

1. **CI e2e 最小档**: uvicorn 起服 + `httpx.MockTransport` mock 上游, 把 4 个 skip 的
   `test_phase2_e2e` 变成可自动跑 (守住集成点 — 本轮 NULL 价格/cooldown 两个断线 bug 都是单测全绿但链路不通)
2. **周报自动化**: VM 上 cron 每周一 `analyze_usage.py` + `generate_weekly_report.py`,
   报告推送 (数据积累给 V4.7+ 后续决策)

---

## 六、执行顺序

1. **P0 VM 同步** (0.5h, 今天) ✅
2. **P1 V4.8 多用户** (2-3 天) ✅ — 表 + 鉴权 + caller_id + 限额/Dashboard
3. **P2-A vision 400** (1 天) ✅ — 5 fixes + 17 tests + VM 部署验证
4. **P2-B drop_params + tier:light 命名对齐** (0.5-1 天)
5. **P3 CI + 周报** (1 天)

V4.7+ 其余候选 (智能调度/价格趋势) 继续等 `analyze_usage.py` 稳定期信号。

---

## 七、Token cache 三路径评估 (2026-08-21)

### 路径 A: 优化上游 prompt cache 注入 ✅ 已完成 (本轮)

`_inject_prompt_cache` 从"只 hash 第一条 system"升级为**分段 hash 稳定前缀**:
`sha256(全量 system 拼接 + "\x1f" + 首条非 system 消息)[:16]`, 每段截断 4096 字符。
多轮 agent loop 中 system + 首条 user 跨轮不变 → 同 key → 上游同 cache shard → 前缀命中率提升。
同时修复: 无 system 时 fallback 首条 user (消灭全局空串 key)、保留调用方已传的
`prompt_cache_key`、Anthropic 分支 role 校验。零存储成本、零架构改动。

### 路径 B: 响应级 prefix 缓存 (远期 — 有明确触发条件再做)

相同 messages prefix 直接回放缓存响应, 跳过上游请求。
- **代价**: 中 (2-3 天) — 新增 `response_cache` 表 (usage_logs 不存原文) + proxy 前置
  缓存判断 + 流式响应回放 (最难: 现架构核心是"原样透传字节", 回放需 buffer 重组 SSE
  或只缓存非流式)
- **收益**: 低-中 — 仅"完全相同的固定 prompt"场景 (FAQ/模板化请求) 有效; 多轮对话
  messages 每轮都变, 命中率极低
- **触发条件**: 出现大量重复固定 prompt 的真实负载 (analyze_usage 数据证实) 再做

### 路径 C: 语义缓存 (不做 — 与项目定位冲突)

embedding + 相似度命中。
- **代价**: 高 (1-2 周+) — 引入 embedding 能力 (sentence-transformers 或上游 API) +
  向量存储 (sqlite-vec/Chroma, SQLite 无原生向量支持) + 原文持久化 + 相似度阈值/风控
- **冲突点**: 2-3 个重依赖与"轻量级、无外部中间件"的项目哲学直接冲突
- **结论**: 除非项目定位从"个人轻量工具"升级, 否则不做

---

## 八、观察期小项清理 (2026-08-21)

- ✅ 删除废弃脚本 `redesign_groups_v2.py` (会重建 scene:free, 与 seed 冲突)
- ✅ `test_usage_log_parse.py` 改 import 生产 `_parse_cache_tokens` (防逻辑漂移) +
  补 group_id 斜杠归一化测试
- ✅ dashboard 缓存统计纳入 Anthropic 风格 `cache_read_input_tokens`
  (cache_creation 是写缓存成本, 不算节省, 不纳入)
- ✅ `batch_crawl_siliconflow` job 已自愈 (healthy, 调度间隔长导致曾 warning)
- ✅ 勾掉过时 TODO (tier:light 5/5, drop_params 单测已有)

**待办 (观察期后)**: 操作审计系统 — 无 audit 表, key 轮换/吊销/用户创建/channel
与 settings 修改均无审计记录 (security.md 已知缺口)。当前 LAN+单用户风险可控,
公网/多用户前必修。约 1 天 (新表 + middleware 钩子)。

---

## 九、当前阶段: 观察期 (2026-08-21 起, 预计 1-2 周)

### 观察清单 (按时间点)

| 时间点 | 检查项 | 预期 |
|--------|--------|------|
| 每隔几天 | Dashboard「缓存节省」栏命中率 | 路径 A 优化后保持高位 (基准 91.8%/¥137) 或上升 |
| 周一 2026-08-24 09:00 | 周报 timer 首次自动触发 | `systemctl --user list-timers` 已就绪; 生成 weekly-latest.md + 飞书推送成功 |
| 本周内 | v44_alerts_check 新 job (每 6h) | job_health 里正常累计 runs, 无异常 error |
| 本周内 | 优惠情报去重效果 | 同一活动不再重复出现; 新采集条目数明显下降 |
| 本周内 | vision 400 率 (P2-A 验证) | 生产数据 < 3% (之前 ~12%) |
| 持续 | daily quota 拦截 (UTC 修复后) | 本地 00:00-08:00 期间限额正常生效 |

### 观察期结束条件

上述各项无异常 → 收尾观察期, 启动下一阶段 (见下)。

---

## 十、观察期后路线 (优先级排序)

1. **操作审计系统** (~1 天, 公网/多用户前必修)
   - audit_log 表: who / when / action / target / old_value / new_value
   - middleware 钩子: dashboard 全部 POST/PUT/DELETE 端点 + CLI 敏感操作
     (key 轮换/吊销/用户创建/channel 修改/settings 修改)
   - 覆盖 security.md 已知缺口 "谁在什么时候改了什么"

2. **V4.7+ 候选** (等周报数据积累信号再定)
   - 智能调度 / 价格趋势分析
   - 依赖 analyze_usage.py + weekly report 的稳定期数据

3. **远期归档** (本文件 §七): 路径 B 响应级 prefix 缓存 (待真实负载触发);
   路径 C 语义缓存 (不做)

### 2026-08-21 补充完成 (commit 483f8b0)

- ✅ V4.4 告警死代码修复: check_all_alerts 从未注册调度, 现补每 6h job
- ✅ 优惠情报去重: 入库 7 天窗口 + 查询取最新 + 前端原文链接
- ✅ 待确认能力变更增强: display_name + 中文 field 标签
- ✅ 采集源自愈: 成功采集恢复 error→active

---

## 附: 上一轮 (2026-08-19) 完成记录

- ✅ P1 测试隔离 (conftest 快照策略修正, 62 污染失败 → 0)
- ✅ P2 真实失败 37 个全清 (含 2 个生产级 bug: NULL 价格路由回归 / `_update_cooldown` hook 断线)
- ✅ phase2_e2e 标 integration marker
- ✅ 最终: 391 passed / 4 skipped (commit `7afa113`)
