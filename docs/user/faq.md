# FAQ 常见问题

## 安装类

### Q: 必须用 Docker 吗？
**A:** 不必须。AIMC 是单进程 + SQLite，裸 Python 跑即可。Docker 是可选（见 [ops/deploy.md](../ops/deploy.md)）。

### Q: AIMC_MASTER_KEY 是什么？忘了怎么办？
**A:** `AIMC_MASTER_KEY` 是账号 API Key 的加密密钥（Fernet）。如果 `data/aimc.db` 还在（没重建），重启 AIMC 后会自动检测并用现有 key 解密。

如果忘了 + db 是新重建的，所有账号需要**重新加**（Dashboard "账号管理"）。

### Q: Python 版本要求？
**A:** 3.11+（推荐 3.12）。pyproject.toml 写了 `>=3.11`。

## 使用类

### Q: `tier:flagship` 和具体 model 名（"deepseek-v4-pro"）有什么区别？
**A:**
- `tier:flagship`：AIMC 在 "旗舰组"（GLM-5.2 / deepseek-v4-pro 等）里选**最便宜 + 最稳**的 model + channel
- `deepseek-v4-pro`：AIMC 只用 siliconflow_aggregator 渠道的 deepseek-v4-pro（精确路由）

**组路由失败时自动 fallback**；精确路由失败时直接 503。

### Q: 怎么知道 AIMC 选了哪个 model？
**A:** 每个响应都带 `X-AIMC-Actual-Model` / `X-AIMC-Actual-Channel` / `X-AIMC-Actual-Group-Id` / `X-AIMC-Actual-Tier` 头：

```bash
curl -i -X POST http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"tier:flagship","messages":[{"role":"user","content":"hi"}]}' \
  | grep -i "X-AIMC-"
```

### Q: 能用 Anthropic SDK / Claude Code 吗？
**A:** 当前**不支持**。AIMC 只支持 OpenAI Chat Completions 协议。Anthropic Messages API 端点（P2-20）待 0.5d 实现。当前用 Claude Code 需要自己写适配器，或换用 OpenAI 协议 SDK。

### Q: 支持哪些 model？
**A:** AIMC 支持任何在 `model_registry` 表里的 model + 任一 channel 有价格的。SF 渠道（默认启用）支持 91 个 model（Qwen / DeepSeek / GLM / MiniMax / Kimi / Hunyuan 等）。

`sqlite3 data/aimc.db "SELECT model_id, tier FROM model_registry ORDER BY tier, model_id LIMIT 20"` 看完整列表。

### Q: 限速怎么算？会被 AIMC 限速吗？
**A:** AIMC 不主动限速（除非你手动配 account_quotas）。默认转发到上游，限速由上游决定。

AIMC 的多维限额（P0-6）是**保护账号不过快消耗**，不是给客户端限速。

### Q: Prompt Cache 怎么用？
**A:** **自动**。AIMC 检测到 `channel.supports_prompt_cache=1` 时，自动给前 2 条 message 加 `cache_control`（Anthropic）或加 `prompt_cache_key`（OpenAI）。无需客户端改任何东西。

看 cache 命中率 + 节省金额：[Dashboard §Cache 节省](dashboard.md#2-prompt-cache-节省)。

## 性能类

### Q: AIMC 会增加多少延迟？
**A:** 5-20ms（in-memory cache 命中时）。主要延迟来自上游 API 调用。

### Q: 支持流式吗？
**A:** 支持。OpenAI SDK 设 `stream=True` 即可。

### Q: 数据库会无限增长吗？
**A:** `usage_logs` 表会增长（每条请求一行）。建议每周跑 `python scripts/cleanup_old_usage_logs.py`（如果存在）保留最近 30 天。

## 故障类

### Q: 上游报 429 (rate limit)，AIMC 会怎么办？
**A:**
- **AIMC 自己的限额**：返 429 + retry-after header
- **上游 429 quota 耗尽**（如 minimax TokenPlan）：P1-15 自动 failover 到其他 channel（mark cooldown + retry），但**单 channel 的 model**（如 minimax-m3）会透传 429

### Q: AIMC 自己挂了怎么办？
**A:** 启 systemd `Restart=on-failure`（已配）。`journalctl --user -u aimc -n 50` 看错误。

### Q: 升级 AIMC 后 db schema 不兼容？
**A:** 跑 7 个 migration 脚本（按顺序）。见 [ops/migration.md](../ops/migration.md)。

## 路线类

### Q: AIMC 适合什么场景？
**A:**
- ✅ 个人 / 小团队 多厂商 API Key 混用
- ✅ 想用优惠活动省钱
- ✅ 想要 dashboard 看 token 用量
- ✅ 想跑本地模型 + 云端混用
- ❌ 大规模高并发（单进程 SQLite 瓶颈）
- ❌ 团队协作（缺 CI / 多用户权限）

### Q: AIMC vs LiteLLM / One API / OpenRouter？
**A:** AIMC 更**轻量 + 个人**：
- 跟 One API 类似，但 AIMC 加了优惠情报采集 + 多维限额 + 模型调整建议
- 跟 LiteLLM 比，AIMC 不做格式转换（只 OpenAI→OpenAI），但有 dashboard
- 跟 OpenRouter 比，AIMC 让你**自己接入**厂商（保留你的账号），OpenRouter 是聚合商

详见 [archive/competitive-research.md](../archive/competitive-research.md)。

### Q: 什么时候用 `tier:xxx`，什么时候用具体 model 名？
**A:**
- **生产用 `tier:flagship` / `tier:light` 等**：让 AIMC 自动选最便宜 + 灵活 fallback（推荐）
- **开发 / 测试用具体 model 名**：固定行为便于调试

### Q: 怎么贡献代码？
**A:** 见 [dev/contributing.md](../dev/contributing.md)。

### Q: 怎么跑测试？
**A:** `cd aimc && source .venv/bin/activate && python -m pytest tests/ -v`。详见 [dev/testing.md](../dev/testing.md)。

## 反馈

文档不准确？功能建议？提 issue: https://gitee.com/XiaoYRecluse/aimc/issues
