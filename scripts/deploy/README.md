# AIMC Ubuntu VM 部署手册 (Phase 4 v4.3)

> 目标: 把 AIMC 部署到 Ubuntu 22.04+ VM, 作为多客户端共享的公共服务
> 架构: `~/aimc` (user home) + user 级 systemd + SQLite 单文件

---

## 1. 部署前检查清单

在 Ubuntu VM 上执行（一次性）:

```bash
# 1. OS
. /etc/os-release
echo "$PRETTY_NAME"  # 期望 Ubuntu 22.04+

# 2. Python ≥ 3.10
python3 --version

# 3. 系统包
sudo apt update
sudo apt install -y git curl sqlite3 python3-venv python3-pip

# 4. 防火墙 (如启用)
sudo ufw allow from 192.168.0.0/16 to any port 8080  # 仅 LAN 内允许
# 或者
sudo ufw allow 8080/tcp  # 任何来源

# 5. 静态 IP / DNS (确保多客户端能稳定找到)
ip addr show  # 记下 vm-ip
```

---

## 2. 一键安装

```bash
# 在 Ubuntu VM 上, 假定 home 在 /home/<user>/
cd ~

# 默认从仓库拉取最新代码
bash <(curl -sSL https://raw.githubusercontent.com/<your-repo>/aimc/master/scripts/deploy/install.sh)

# 或者 clone 后本地跑
git clone https://github.com/<your-repo>/aimc.git ~/aimc
cd ~/aimc
bash scripts/deploy/install.sh
```

`install.sh` 做的事:

| 步骤 | 动作 |
|---|---|
| 1 | 检查 OS / Python / 依赖 |
| 2 | git clone (或 pull) 到 `~/aimc` |
| 3 | 创建 `~/aimc/.venv` |
| 4 | `pip install -r requirements.txt` |
| 5 | 复制 `env.template` → `~/aimc/.env` (仅首次) |
| 6 | 创建 `~/aimc/data/` |
| 7 | 安装 systemd unit: `~/.config/systemd/user/aimc.service` |
| 8 | `sudo loginctl enable-linger $USER` (登出后服务仍跑) |
| 9 | (可选) `AIMC_AUTOSTART=1 bash install.sh` 自动启动 |

---

## 3. 配置 `.env`

```bash
nano ~/aimc/.env
```

填入:
- `AIMC_MASTER_KEY` ← 跑 `python ~/aimc/scripts/gen_master_key.py` 生成
- 9 个厂商 API key (至少 5 个你已有账号的)
- `OPENAI_API_KEY` ← MiniMax token plan 用于 AI 解析
- `AIMC_API_KEY` ← 多客户端共享的网关鉴权 key (留空 = 不鉴权)

**v4.3 验证用的 key 矩阵** (基于你的账号):

| 厂商 | key 状态 |
|---|---|
| deepseek | ✅ 填 |
| qwen | ✅ 填 (如果你用百炼) |
| glm | ✅ 填 (如果你用智谱) |
| kimi | ✅ 填 |
| minimax | ✅ 填 (解析 + 兜底) |
| siliconflow | ✅ 填 |
| volcengine | ⚠️ 填 (查额度) |
| stepfun | ✅ 填 (1月 tokenplan) |
| sensenova | ✅ 填 (公测免费, 提供 sensenova-6.8-flash-lite + 跨渠道免费 glm-5.2) |
| hunyuan | ❌ 留空 (v4.3 跳过) |

---

## 4. 初始化数据库 (全新)

```bash
cd ~/aimc
source .venv/bin/activate

# 1. 建表 (幂等)
python scripts/seed_model_groups.py
python scripts/seed_model_cards.py
python scripts/seed_model_registry.py  # 启动后 scheduler 会重写, 现在只是骨架
python scripts/seed_parse_rules.py

# 2. 创建 channels + accounts (从 .env 读明文 → DB 写密文)
python scripts/manual_import.py
```

---

## 5. 启动服务

```bash
# 启动
systemctl --user enable --now aimc.service

# 状态
systemctl --user status aimc

# 日志 (实时)
journalctl --user -u aimc -f

# 停止
systemctl --user stop aimc

# 重启
systemctl --user restart aimc
```

---

## 6. 健康检查

```bash
# 基础
curl http://127.0.0.1:8080/health

# 模型清单
curl http://127.0.0.1:8080/v1/models | python -m json.tool | head -20

# 跨网 (从 windows / wsl / 其他机器)
curl http://<vm-ip>:8080/health
```

---

## 7. v4.3 真实 9 源 smoke

```bash
cd ~/aimc
source .venv/bin/activate

# 跑全部 9 国内 + 1 千问AI
python scripts/deploy/smoke_v43.py

# 输出示例:
#   [✓] siliconflow   /model_list api   280 models  1234ms
#   [✓] volcengine    /model_list api    45 models   678ms
#   [✗] hunyuan       /model_list api     0 models  5000ms  ⚠ API key 未配置
#   ...

# 把输出保存贴回 (用于 troubleshooting)
python scripts/deploy/smoke_v43.py > smoke_v43.log 2>&1
```

预期成功率: **8/9 或 9/10** (hunyuan 无 key 算可接受)

---

## 8. 多客户端连通性

```bash
# 在 VM 上跑 (4 端点: localhost / 0.0.0.0 绑定 / LAN 模拟)
python scripts/deploy/verify_clients.py --host <vm-ip>
```

从 **windows / wsl / 真实客户端** 验证:

```python
# Python: workbuddy / qclaw / trae / hermes-agent 等
from openai import OpenAI

client = OpenAI(
    base_url="http://<vm-ip>:8080/v1",
    api_key="<你的 AIMC_API_KEY, 或任意字符如未配>"
)

models = client.models.list()
print(f"收到 {len(models.data)} 个 model")
for m in models.data[:3]:
    print(f"  - {m.id}")

# 真实调用
resp = client.chat.completions.create(
    model="deepseek-chat",  # 或 tier:balanced 等组名
    messages=[{"role": "user", "content": "hello"}],
)
print(resp.choices[0].message.content)
```

---

## 9. 备份策略

```bash
# 手动备份
bash ~/aimc/scripts/deploy/backup.sh

# 接入 cron (每天 03:00)
crontab -e
# 加: 0 3 * * * bash /home/<user>/aimc/scripts/deploy/backup.sh >> /home/<user>/aimc/backups/backup.log 2>&1
```

备份位置: `~/aimc/backups/aimc_YYYYMMDD_HHMMSS.db`, 保留 14 天。

恢复:
```bash
# 停服务
systemctl --user stop aimc

# 复制回去
cp ~/aimc/backups/aimc_20260806_030000.db ~/aimc/data/aimc.db

# 启动
systemctl --user start aimc
```

---

## 10. 故障排查

### 服务起不来

```bash
# 查错
journalctl --user -u aimc -n 50 --no-pager

# 常见原因:
# 1. AIMC_MASTER_KEY 未配 → "AIMC_MASTER_KEY 未设置"
# 2. .env 路径不对 → "FileNotFoundError: .env"
# 3. venv 路径不对 → "No such file or directory"
```

### 端口冲突

```bash
# 看谁占 8080
ss -tlnp | grep 8080

# 改 AIMC_PORT=8081 等
nano ~/aimc/.env
systemctl --user restart aimc
```

### Windows / WSL 连不上

```bash
# 1. VM 防火墙
sudo ufw status

# 2. AIMC 是否绑 0.0.0.0
ss -tlnp | grep 8080
# 期望: 0.0.0.0:8080 (不是 127.0.0.1:8080)

# 3. 从 windows ping VM
ping <vm-ip>

# 4. VM 上 listen 服务
sudo netstat -tlnp | grep :8080
```

### scheduler 没跑采集

```bash
# 看 scheduler 启动 log
journalctl --user -u aimc | grep -i scheduler

# 看是否启了
curl http://127.0.0.1:8080/health | python -m json.tool
```

---

## 11. 周报自动化 (systemd timer)

周报每周一 09:00 自动生成 + 推送，由两个 systemd user 单元驱动：

| 单元 | 作用 |
|---|---|
| `aimc-weekly-report.timer` | 每周一 09:00 触发，`Persistent=true` 错过补跑 |
| `aimc-weekly-report.service` | oneshot：跑 `generate_weekly_report.py` 生成 Markdown → `publish_report.py` 推送摘要 |

### 安装

```bash
# 复制 unit 文件到 user systemd 目录
mkdir -p ~/.config/systemd/user/
cp ~/aimc/scripts/deploy/aimc-weekly-report.service ~/.config/systemd/user/
cp ~/aimc/scripts/deploy/aimc-weekly-report.timer ~/.config/systemd/user/

# 重载 + 启用 timer
systemctl --user daemon-reload
systemctl --user enable --now aimc-weekly-report.timer

# 确保 linger 已开（aimc.service 安装时已开，双保险）
loginctl enable-linger $USER

# 验证 timer 已注册
systemctl --user list-timers | grep weekly
```

### 手动触发一次（不等下周一）

```bash
systemctl --user start aimc-weekly-report.service

# 看日志
journalctl --user -u aimc-weekly-report.service --no-pager

# 验证报告已生成
ls -la ~/aimc/reports/weekly-latest.md
```

预期输出：`reports/weekly_YYYY-MM-DD.md` + `reports/weekly-latest.md`，`publish_report.py` 走 `.env` 里的 `AIMC_ALERT_CHANNEL` 推送摘要。

### 配置推送通道

默认 `AIMC_ALERT_CHANNEL=log`（只记日志，不真发）。切换推送通道改 `.env`：

```bash
nano ~/aimc/.env
# 设 AIMC_ALERT_CHANNEL=bark 并填 AIMC_BARK_KEY (iOS Bark app)
# 或 AIMC_ALERT_CHANNEL=feishu 并填 AIMC_FEISHU_WEBHOOK_URL
# 或 AIMC_ALERT_CHANNEL=serverchan 并填 AIMC_SERVERCHAN_KEY
```

改完不需要重启主服务（周报是独立 oneshot，下次触发时读新 `.env`）。

### 卸载周报 timer

```bash
systemctl --user disable --now aimc-weekly-report.timer
rm ~/.config/systemd/user/aimc-weekly-report.{timer,service}
systemctl --user daemon-reload
```

---

## 12. 卸载

```bash
# 停 + 禁服务
systemctl --user disable --now aimc.service
rm ~/.config/systemd/user/aimc.service
systemctl --user daemon-reload

# 移除 linger
sudo loginctl disable-linger $USER

# 删代码 (保留 backup)
rm -rf ~/aimc/data ~/aimc/.venv
# ~/aimc 留着, 备份还在
```

---

## 文件清单

| 文件 | 用途 |
|---|---|
| `aimc.service` | systemd unit (user 级) |
| `install.sh` | 一键安装 |
| `env.template` | 9 key .env 模板 |
| `smoke_v43.py` | 真实 9 源 smoke |
| `verify_clients.py` | 多客户端连通 |
| `backup.sh` | SQLite 热备份 |
| `aimc-weekly-report.timer` | 周报定时器（每周一 09:00） |
| `aimc-weekly-report.service` | 周报生成 + 推送（oneshot） |
| `README.md` | 本手册 |