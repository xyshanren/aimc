# 使用指南：个人 vs 小团队

> 受众：已经装好 AIMC（见 [install.md](install.md)）的个人用户 / 小团队管理员
> 核心：多用户 Key 体系（V4.8+）—— 用 `username` 区分「人/主体」，用 `label` 区分「工具/场景」

## 1. 一句话原则

| 维度 | username（用户） | label（标签） |
|------|-----------------|--------------|
| 回答的问题 | 这把 key 属于**谁** | 这把 key 用在**哪里** |
| 个人使用 | 固定一个（如 `me`） | 每个工具/场景一个 |
| 团队使用 | 每人一个 | 每人每场景一个 |

Key 是 `username + label` 的组合：用量按 **username** 汇总（谁用了多少），按 **label**
下钻（他的哪个工具在用），吊销按单把 key 粒度进行（只封某个工具不影响其他）。

## 2. 个人用户：一个 username，多个 label

个人本地部署只需要一个身份，把 label 当作「工具/场景」维度：

```bash
# 给 AI 编程工具发一把
python scripts/add_user_key.py --username me --label zcode

# 给聊天客户端发一把
python scripts/add_user_key.py --username me --label chat

# 给定时脚本/自动化发一把
python scripts/add_user_key.py --username me --label cron
```

好处：
- **隔离审计**：用量页能看到「zcode 这周烧了多少 token」而不是混在一起
- **精确吊销**：某个工具泄露/异常，只吊销那一把 label，其他工具不受影响
- **限额到场景**：给 cron 场景单独设日限额防止脚本失控刷量：
  ```bash
  python scripts/add_user_key.py --username me --label cron --daily-limit 100000
  ```

## 3. 小团队：username 区分人，label 区分场景

```bash
# 张三：工作机 + 笔记本各一把
python scripts/add_user_key.py --username zhangsan --label 工作机
python scripts/add_user_key.py --username zhangsan --label 笔记本

# 李四：只发一把，另设个人限额（日 10 万 / 月 100 万 token）
python scripts/add_user_key.py --username lisi --label main \
    --daily-limit 100000 --monthly-limit 1000000
```

管理动作：

```bash
# 看全员 key（脱敏，只显示前缀）
python scripts/list_user_keys.py

# 张三离职 → 只吊销他的 key（用量记录保留，按人统计仍可回溯）
python scripts/aimc.py keys revoke <key_id>

# 谁用得最多？
python scripts/aimc.py usage per-user --days 7
```

## 4. 发 Key 的三种途径

| 途径 | 适合 | 入口 |
|------|------|------|
| 看板（推荐） | 可视化 / Windows 便携版 / 不想上服务器 | 运维页 → Key 管理 → 「+ 新建 Key」 |
| CLI（本文） | 脚本化 / 远程运维 | `scripts/add_user_key.py` |
| REST API | Agent / 自动化 | `POST /dashboard/api/keys`（见 [agent-api.md](../agent-api.md)） |

> ⚠️ key 明文**只在创建时返回一次**，立即保存；丢了就吊销重发。

## 5. 客户端怎么用

所有 OpenAI 兼容客户端都一样，只是 base_url 指向 AIMC：

```bash
curl http://<aimc-host>:18080/v1/chat/completions \
  -H "Authorization: Bearer aimc-xxxxxxxx" \
  -d '{"model": "tier:balanced", "messages": [{"role":"user","content":"hi"}]}'
```

- `model` 推荐用 `tier:` 组名（flagship / balanced / light / vision 等），
  AIMC 自动在已配置的渠道间路由 + failover
- 也可以填具体模型名（如 `deepseek-v4-pro`）走精确路由

## 6. 记账与限额语义

- 每次请求落到 `usage_logs.caller_id`，按 username 汇总、label 细分
- 个人限额在**请求前**检查：已用 + 预估 > 限额 → 返回 429（`daily_token_limit_exceeded`）
- `NULL` = 不限，`0` = 禁用；限额按本地日历日/月计算
- admin key（`--admin`）额外可看看板全局数据，只发给管理员

## 7. 推荐 Key 规划速查

| 场景 | username | label | 备注 |
|------|----------|-------|------|
| 个人自用 | `me` | `zcode` / `chat` / `cron` / `phone` | 每个客户端一把 |
| 团队成员 | 每人一个 | 每人每设备/工具一个 | |
| 共享服务账号 | `svc-ci` / `svc-bot` | 具体服务名 | 服务类主体也当「人」管理 |
| 管理员 | `<名字>` + `--admin` | `admin` | 能看全局看板 |
