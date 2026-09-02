# 安全应急响应手册

> 受众：AIMC 运维 / 团队负责人
> 触发：发现可疑活动 / 收到告警 / 用户投诉
> 配套：[ops/security.md](security.md)（防护）/ 本文件（应急）

---

## 0. 应急响应优先级

| 等级 | 触发条件 | 响应时间 |
|------|----------|----------|
| **P0 灾难** | master_key 泄露 / 数据被删 | **5 分钟** |
| **P1 紧急** | dashboard_token 泄露 / 厂商 key 泄露 / 异常大额扣费 | **30 分钟** |
| **P2 高** | AIMC_API_KEY 泄露 / 可疑活动 | **2 小时** |
| **P3 中** | 性能异常 / 个别失败 | **24 小时** |

---

## 1. P0 灾难：AIMC_MASTER_KEY 泄露

### 1.1 识别

```bash
# 任何以下情况视为泄露:
# 1. .env 文件出现在 git history / 公开位置 / 备份
# 2. master_key 在聊天 / 邮件 / 截图中
# 3. 你的密码管理器被入侵
```

### 1.2 立即行动（5 分钟内）

```bash
# 1. 关闭所有渠道 (防厂商 key 被批量盗用)
#    方式 A: dashboard 改 channels.status = 'disabled' (需 dashboard_token)
#    方式 B: 直接 SQL (绕过 dashboard)
ssh aliy-hy "sqlite3 /home/lxc/aimc/data/aimc.db \
  \"UPDATE channels SET status='disabled' WHERE status='active';\""
ssh aliy-hy "systemctl --user restart aimc"

# 2. 通知所有 LLM 厂商 (重置 key)
#    - 阿里云百炼: https://bailian.console.aliyun.com/
#    - DeepSeek: https://platform.deepseek.com/api_keys
#    - 硅基流动: https://cloud.siliconflow.cn/account/ak
#    - 智谱: https://open.bigmodel.cn/usercenter/apikeys
#    - ... 等所有渠道

# 3. 生成新 master_key
NEW_KEY=$(openssl rand -hex 32)
echo "新 master_key: $NEW_KEY"
# 存到密码管理器
```

### 1.3 恢复（30 分钟内）

```bash
# 1. 改 .env
ssh aliy-hy "nano /home/lxc/aimc/.env"
# 替换 AIMC_MASTER_KEY=$NEW_KEY

# 2. 重新加密所有 accounts.api_key_ciphertext
#    AIMC 自动检测 master_key 变化时, 会在第一次解密时失败
#    需要手动 re-encrypt 脚本 (目前没写, 见 TODO)

# 3. 在 dashboard 添加新渠道 (用新厂商 key)
#    dashboard → 渠道管理 → 启用各 channel → 填新 key

# 4. 启用 channels
ssh aliy-hy "sqlite3 /home/lxc/aimc/data/aimc.db \
  \"UPDATE channels SET status='active' WHERE status='disabled';\""
ssh aliy-hy "systemctl --user restart aimc"

# 5. 验证 e2e
ssh aliy-hy "curl -X POST http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{\"model\":\"tier:flagship\",\"messages\":[{\"role\":\"user\",\"content\":\"test\"}]}'"
```

### 1.4 审计

```sql
-- 查 master_key 泄露前后可疑的 usage_logs
SELECT
  strftime('%Y-%m-%d %H:%M', created_at, 'localtime') as local_time,
  channel_id, model, group_id,
  input_tokens, output_tokens, status_code
FROM usage_logs
WHERE created_at BETWEEN '泄露时间 - 1 day' AND '泄露时间 + 1 day'
ORDER BY input_tokens DESC
LIMIT 100;
```

### 1.5 预防

- master_key **不存 .env 文件**（改存密码管理器）
- .env 文件 `chmod 600`（仅 root 可读）
- master_key 不出现在 git / 文档 / 截图
- 定期轮换（**1 年**）

---

## 2. P1 紧急：Dashboard Token 泄露

### 2.1 立即行动（30 分钟内）

```bash
# 1. 改 .env 新 token
NEW_TOKEN=$(openssl rand -hex 32)
ssh aliy-hy "sed -i 's/^AIMC_DASHBOARD_TOKEN=.*/AIMC_DASHBOARD_TOKEN=$NEW_TOKEN/' /home/lxc/aimc/.env"

# 2. 重启
ssh aliy-hy "systemctl --user restart aimc"

# 3. 通知所有用户新 token
#    通过其他渠道 (邮件 / 钉钉 / 微信) 通知 team
echo "新 dashboard token: $NEW_TOKEN" | mail -s "AIMC Dashboard Token 更新" team@your-domain.com

# 4. SSH IP 白名单 (防止进一步入侵)
#    iptables 只放行你的 IP
```

### 2.2 检查被改内容

```sql
-- 查所有 channels 最近修改时间 (24h 内)
SELECT id, name, status, priority, updated_at
FROM channels
WHERE updated_at > '泄露时间 - 1 day'
ORDER BY updated_at DESC;

-- 查 accounts (新账号 = 攻击者加的)
SELECT * FROM accounts
WHERE created_at > '泄露时间 - 1 day'
ORDER BY created_at DESC;
```

### 2.3 预防

- Dashboard Token **只给人类**，**不给 agent / DSH**
- 启用 SSH 公钥 + 关 PasswordAuth（已有）
- 强密码（≥32 字节）
- 监控 channels / accounts 异常变更（TODO 自动化）

---

## 3. P2 高：AIMC_API_KEY 泄露

### 3.1 立即行动（2 小时内）

```bash
# 1. 改 .env 新 key
NEW_API_KEY=$(openssl rand -hex 32)
ssh aliy-hy "sed -i 's/^AIMC_API_KEY=.*/AIMC_API_KEY=$NEW_API_KEY/' /home/lxc/aimc/.env"

# 2. 重启
ssh aliy-hy "systemctl --user restart aimc"

# 3. 通知所有客户端 (DSH / agent / 用户)
#    必须在 2h 内, 否则攻击者继续用旧 key 消耗 token
```

### 3.2 查可疑请求

```sql
-- 找异常大请求 (攻击者用你 key 跑批量)
SELECT
  strftime('%Y-%m-%d %H:%M', created_at, 'localtime') as local_time,
  channel_id, model, group_id,
  input_tokens, output_tokens, status_code, latency_ms
FROM usage_logs
WHERE created_at > '泄露时间 - 1 day'
ORDER BY input_tokens DESC
LIMIT 100;
```

### 3.3 预估损失

- **DP API 计费**：正常 1k tokens ≈ ¥0.001
- **异常大请求**：100k tokens ≈ ¥0.1/次
- **攻击者跑 1000 次**：≈ ¥100（**很低**）
- 但 Pydantic Settings bug（已修）前可能**完全未鉴权** → 实际损失需要看 usage_logs

### 3.4 预防

- DSH / agent 配置**用 .env**，**不写死在代码里**
- 上传 git 前 `git status` 确认
- 用 `pre-commit` hook 扫描敏感字符串
- 定期轮换（**90 天**）

---

## 4. P2 高：单厂商 API Key 泄露（硅基流动 / DeepSeek 等）

### 4.1 立即行动（1 小时内）

```bash
# 1. 登录厂商控制台, 重置 key
# 2. 改 .env (仅这一家)
#    或通过 dashboard 改 (V4.6.4 之前可能需 restart, 之后 TTL 自动)

# 3. 不影响其他厂商 (继续工作)
```

### 4.2 防护

- P0-6 多维限额**已启用**（防单 channel 滥用）
- 启用厂商侧 key 限额（厂商控制台）

---

## 5. P2 高：SSH 入侵

### 5.1 立即行动

```bash
# 1. 检查 fail2ban 状态
ssh aliy-hy "fail2ban-client status sshd"

# 2. 看最近登录 (成功 / 失败)
ssh aliy-hy "last -20"
ssh aliy-hy "journalctl -u sshd --since '24 hours ago' | grep -E 'Failed|Accepted'"

# 3. 改 SSH 端口 (避免暴力破解)
ssh aliy-hy "sed -i 's/#Port 22/Port 2222/' /etc/ssh/sshd_config"
ssh aliy-hy "systemctl restart sshd"
# 更新本地 ~/.ssh/config: Port 2222

# 4. 限制 SSH 访问 (只允许你家的 IP)
#    iptables -A INPUT -p tcp --dport 2222 -s 你家IP -j ACCEPT
#    iptables -A INPUT -p tcp --dport 2222 -j DROP
```

### 5.2 审计服务器被入侵迹象

```bash
# 检查异常进程
ssh aliy-hy "ps auxf | head -30"

# 检查新用户 / 后门
ssh aliy-hy "cat /etc/passwd | grep -v 'nologin\|false'"
ssh aliy-hy "cat /etc/sudoers /etc/sudoers.d/* 2>/dev/null | grep -v '^#'"

# 检查 SSH 公钥
ssh aliy-hy "ls -la /root/.ssh/authorized_keys"

# 检查 cron 后门
ssh aliy-hy "crontab -l; ls -la /etc/cron.*"

# 检查最近登录
ssh aliy-hy "lastlog | head -20"
```

---

## 6. P3 中：异常大请求 / 异常流量

### 6.1 识别

```sql
-- 异常大 token 请求
SELECT * FROM usage_logs
WHERE input_tokens > 100000
   OR latency_ms > 60000
ORDER BY created_at DESC LIMIT 20;

-- 异常错误率
SELECT
  strftime('%H', created_at, 'localtime') as hour,
  COUNT(*) as total,
  SUM(CASE WHEN status_code >= 500 THEN 1 ELSE 0 END) as errors
FROM usage_logs
WHERE created_at > datetime('now', '-7 day')
GROUP BY hour
ORDER BY errors DESC;
```

### 6.2 处置

- 如果是用户问题：通知用户
- 如果是攻击：通过 fail2ban / cloudflare WAF 拉黑
- 加速率限制（TODO）

---

## 7. 通用应急检查清单

无论哪种泄露，先做这 5 步：

- [ ] **不要慌**（30 秒冷静）
- [ ] **隔离**：SSH 上 + `systemctl --user stop aimc`（如果 master_key 泄露）
- [ ] **取证**：`usage_logs` / `journalctl` / 厂商后台
- [ ] **轮换**相关 key / token
- [ ] **通知**团队 + 用户（如有损失）

---

## 8. 联系表

| 厂商 | 紧急联系 |
|------|----------|
| 阿里云百炼 | https://bailian.console.aliyun.com/  + 客服工单 |
| DeepSeek | https://platform.deepseek.com/  + support@deepseek.com |
| 硅基流动 | https://cloud.siliconflow.cn/  + 微信群 |
| 智谱 BigModel | https://open.bigmodel.cn/ |
| Cloudflare | https://dash.cloudflare.com/  + 1-650-319-7517 |
| 阿里云 ECS | https://workorder.console.aliyun.com/  + 95187 |

---

## 9. TODO（待实现）

- [ ] **审计日志**（谁在什么时候改了什么）
- [ ] **登录失败锁定**（5 次错锁 5 分钟）
- [ ] **速率限制**（登录 + API）
- [ ] **Cookie Secure / SameSite 标记**
- [ ] **自动监控 channels / accounts 异常变更**
- [ ] **re-encrypt 脚本**（master_key 变化时批量重加密）

---

**作者**：用户 + agent 协作
**创建日期**：2026-08-17
**配套文档**：[ops/security.md](security.md)（防护）

---

## 10. 实测发现 + 修复 (2026-08-17)

### Dashboard Token 实际未启用

**用户问起 dashboard token 状态时审计发现**:
- `.env` 里 `AIMC_DASHBOARD_TOKEN` 没设 (只有注释行)
- `settings.dashboard_auth_enabled = False`
- **任何人访问 `/dashboard/api/channels` 都能拿到完整数据 (200 + JSON)**

**已修复**:
1. 添加 `AIMC_DASHBOARD_TOKEN=<redacted-64-hex-from-vm-only>` 到 `.env`
2. 重启服务
3. 验证: 错 token 401, 无 token 401, 对 token 200

**已部署**: VM 已运行, 但需要同步 Windows 本地 + 通知所有 dashboard 用户新 token

### master_key 轮换 — TODO 写脚本

**当前没 scripts/rotate_master_key.py**, 轮换需要手动:
1. 写脚本 (1-2h): 读所有 ciphertext, 用旧 key 解密, 用新 key 重加密
2. 测试 (1h): 准备测试环境, 跑 1 轮, 验证 e2e
3. 走流程 (30min): 关所有渠道 -> 改 .env -> 跑脚本 -> 重启 -> 测试每个 channel

**估时**: 首次 0.5-1 天 (含测试), 后续 1-2 小时

**触发条件**:
- master_key 出现在任何截图 / chat / 邮件 / git history
- 你的密码管理器被入侵
- 离职 / 设备转让
- 年度主动轮换 (建议 1 年)

---

## 11. strong 组请求被客户端切换到 balanced + `_channel_model` 共享 dict 污染 (2026-09-02)

### 现象

用户反馈"使用 strong 组被路由到 balanced"。12:11:58 起 usage_logs 全部是
`tier:balanced`, 而此前 (12:07–12:12) 是 `tier:strong`。

### 排查结论 (两个独立问题)

**问题 1 — 客户端 429 触发切换 (网关无责)**:

- strong 组当时内部退化: deepseek-v4-flash@sensenova 上游 429 (12:11:03/12:11:40,
  account 5 进 cooldown) + glm-4-plus 无可用渠道 (glm_bigmodel 渠道当时仍是禁用态,
  不在路由缓存 — 12:50 前缓存一直是"5 个可用渠道账号", 13:10 起 6 个)
- 12:08:24 和 12:11:04 两次全部重试失败, 429 返回客户端
- 12:11:04 的 429 后 0.8s, 第一个 `model=tier:balanced` 请求到达 (同一 key) —
  客户端 (hermes-agent-cn) 收到 429 后自行切换备用模型
- 网关全天 0 条"组 tier:strong 全部不可用 fallback"警告, 组路由重试保持原组名,
  **网关没有做 strong→balanced 降级**

**问题 2 — `_channel_model` 共享 dict 污染 (真 bug, 已修)**:

```
12:11:58,465 [INFO] 组路由: tier:balanced -> minimax-m2.5 via minimax_compat
12:11:58,592 [INFO] [V4.6.5 body model 替换] tier:balanced -> minimax-m3  ← 串模型!
```

- 根因: `select_best_channel` 旧逻辑只在 `price_model != model` 时写
  `ch["_channel_model"]`, 同名精确匹配不清旧值; channel entry 是
  `_channels_cache` 共享 dict, strong 请求残留的 `minimax-m3` 泄漏给随后的
  balanced 请求, proxy body 替换读共享 dict (`proxy.py`) 发错模型
- usage 1994/2001 (group_id=tier:balanced) 上游响应 model=minimax-m3 实证
- 同源问题: `proxy.py` Phase 2.10 只修了 `_actual_model` 响应头污染, body 路径漏了

### 修复 (V4.9.7)

1. `router.select_best_channel`: 精确/fuzzy 匹配均**无条件覆写** `_channel_model`
2. `routes`: `select_channel` 返回后 `dict(channel)` 浅拷贝, 隔离 per-request 字段
   (`_channel_model/_registry_*/_group_id`), 消除并发竞态
3. 回归测试: `tests/test_v497_channel_model_pollution.py` (3 用例复现事故时序)
4. 顺带: 补 `GET /v1/models/{model_id}` 端点 (此前客户端探测组 id 必 404)

### 教训

- 客户端有 429 自动切模型行为, 网关侧 429 = 把流量永久赶到备用组, 频限类失败要尽量
  在组内 failover 消化掉
- 共享缓存 dict 上挂 per-request 字段是反模式, 新字段一律走请求级快照
