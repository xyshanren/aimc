# 部署 AIMC

> 受众：把 AIMC 部署到 Linux 服务器 / LXC 容器 / systemd 守护
> 前提：完成 [user/install.md](../user/install.md)

## 1. systemd user 服务（推荐）

`/home/lxc/aimc/scripts/deploy/aimc.service`：

```ini
[Unit]
Description=AIMC - AI Model Compass Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/lxc/aimc
Environment="PATH=/home/lxc/aimc/.venv/bin:/usr/local/bin:/usr/bin"
EnvironmentFile=/home/lxc/aimc/.env
ExecStart=/home/lxc/aimc/.venv/bin/python /home/lxc/aimc/main.py
Restart=on-failure
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=60

# 资源限制
MemoryMax=2G
LimitNOFILE=65536

# 安全（适度）
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true

# 日志
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aimc

[Install]
WantedBy=default.target
```

**启用 + 启动**：

```bash
mkdir -p ~/.config/systemd/user
cp scripts/deploy/aimc.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable aimc
systemctl --user start aimc
systemctl --user status aimc
```

**关键 systemd user 配置**：
- `WorkingDirectory` / `EnvironmentFile` / `ExecStart` 必须用**绝对路径**
- `Restart=on-failure` + `RestartSec=5`：失败 5s 重启
- `StartLimitBurst=5`：5 分钟内最多 5 次重启（防崩溃循环）
- `MemoryMax=2G`：AIMC 单进程足够

> ⚠️ **`.env` 行内注释陷阱**：systemd 的 `EnvironmentFile=` **不剥离行内注释** —
> `KEY=value  # 注释` 会把注释整段带进值里（pydantic 直读 `.env` 时会剥离，两条路径
> 行为不一致，曾导致 `ALERT_CHANNEL` 值污染、周报推送失败）。**`.env` 生效行的值后
> 不要跟 `#` 注释**，注释一律独立成行。详见 [user/configure.md](../user/configure.md)。

## 1.5 周报定时器（aimc-weekly-report.timer）

每周一 09:00（本地时间）自动生成周报 + 推送（飞书/Bark/Server 酱，走
`ALERT_CHANNEL`）。unit 文件在 `scripts/deploy/aimc-weekly-report.{service,timer}`。

```bash
cp scripts/deploy/aimc-weekly-report.service scripts/deploy/aimc-weekly-report.timer \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aimc-weekly-report.timer

# 确保用户 linger 已开（aimc.service 安装时通常已开）
loginctl enable-linger $USER 2>/dev/null || true

# 验证：下次触发时间应显示下周一 09:00
systemctl --user list-timers | grep weekly

# 手动触发一次验证（不等周一）
systemctl --user start aimc-weekly-report.service
journalctl --user -u aimc-weekly-report.service --no-pager -n 20
ls -la ~/aimc/reports/weekly-latest.md
```

`Persistent=true`：机器在周一宕机、周二恢复时会补跑一次。

## 2. LXC 容器部署

AIMC 已在 Ubuntu 24.04 LXC 容器（`192.168.56.12:8080`）验证：

```bash
# 在 host 上
lxc-start aimc
lxc-attach aimc -- bash -c "systemctl --user status aimc"

# LXC 容器内跟裸机一样用 systemd user 服务
```

LXC 容器**无需 Docker**（无 daemon、无 namespace 开销），比 Docker 更轻量适合个人项目。

## 3. Docker 部署（可选）

`Dockerfile`（项目根目录）：

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080

CMD ["python", "main.py"]
```

```bash
docker build -t aimc:latest .
docker run -d --name aimc -p 8080:8080 --restart unless-stopped \
  -v /path/to/.env:/app/.env:ro \
  -v /path/to/data:/app/data \
  aimc:latest
```

**注意**：Docker volume 必须挂载 `data/` 目录（db + reports）+ `.env`（只读）。

## 4. 反向代理（可选）

AIMC 默认监听 `0.0.0.0:8080`。生产建议套 nginx：

```nginx
server {
    listen 80;
    server_name aimc.example.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # 流式响应需要禁用 buffering
        proxy_buffering off;
    }

    # Dashboard 子路径（可选, 加 basic auth）
    location /dashboard {
        auth_basic "AIMC Dashboard";
        auth_basic_user_file /etc/nginx/.htpasswd;
        proxy_pass http://127.0.0.1:8080;
    }
}
```

## 5. 防火墙

```bash
# UFW (Ubuntu)
sudo ufw allow from 192.168.0.0/16 to any port 8080
sudo ufw enable

# 验证
sudo ufw status verbose

# 或 iptables (通用)
sudo iptables -A INPUT -p tcp --dport 8080 -s 192.168.0.0/16 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 8080 -j DROP
```

**只允许 LAN** 访问（192.168.0.0/16），不暴露公网（除非有 HTTPS 反向代理）。

## 6. 启动检查清单

- [ ] `.env` 填了至少 1 个渠道 API Key
- [ ] `.env` 生效行**没有行内 `#` 注释**（systemd 陷阱，见 §1 警告块）
- [ ] `systemctl --user daemon-reload`（改 service 文件后）
- [ ] `systemctl --user enable aimc`（开机自启）
- [ ] `systemctl --user start aimc`
- [ ] `curl http://localhost:8080/health` 返 `{"status":"ok"}`
- [ ] 周报 timer 已启用（`systemctl --user list-timers | grep weekly`，见 §1.5）
- [ ] 防火墙放行 8080（或用 reverse proxy）
- [ ] Dashboard token 设了（生产环境）
- [ ] AIMC_API_KEY 设了（生产环境）
- [ ] 备份脚本 cron 设了（见 [monitor.md](monitor.md)）

## 7. 升级流程

```bash
ssh <user>@<host>
cd /home/lxc/aimc
git pull
source .venv/bin/activate
pip install -r requirements.txt
# 如果 schema 变了, 跑 migration
for m in db/migrations/2026_08_*.py; do python $m --apply; done
systemctl --user restart aimc
# 跑测试确认
python -m pytest tests/ -v
```

**版本注意事项**：
- 升级到 ≥ `3f90d5e`（2026-08-21）：`usage_logs.created_at` 从 UTC 改为本地时间，
  **存量行需一次性迁移**（否则历史统计窗口偏 8 小时）：
  ```bash
  cp data/aimc.db data/aimc.db.bak.$(date +%Y%m%d_%H%M%S)   # 先备份
  .venv/bin/python -c "
  import asyncio, sys
  from datetime import datetime
  from sqlalchemy import text
  sys.path.insert(0, '.')
  from db.engine import async_session_factory
  async def m():
      off = round((datetime.now() - datetime.utcnow()).total_seconds() / 3600)
      async with async_session_factory() as s:
          r = await s.execute(text(\"UPDATE usage_logs SET created_at = datetime(created_at, :o)\"), {\"o\": f'+{off} hours'})
          await s.commit()
          print('migrated', r.rowcount, 'rows')
  asyncio.run(m())
  "
  ```
- 升级到 ≥ `041000e`（优惠情报治理）：建议清理历史"未知活动"垃圾条目：
  `sqlite3 data/aimc.db "DELETE FROM promotions WHERE title='未知活动' OR flagged=1"`

## 故障排查（部署阶段）

| 症状 | 检查 | 修复 |
|------|------|------|
| `start` 失败 | `journalctl --user -u aimc -n 50` | 看 traceback |
| 端口被占用 | `ss -tlnp \| grep 8080` | 杀进程或换端口 |
| systemd user 不工作 | `loginctl show-user $USER` 看 Linger | `sudo loginctl enable-linger $USER` |
| 文件权限错 | `ls -la /home/lxc/aimc/` | `sudo chown -R $USER:$USER /home/lxc/aimc` |
| 容器内 systemd 不可用 | `lxc-attach aimc -- bash -c "systemctl --user status"` | 检查 `loginctl enable-linger` |
