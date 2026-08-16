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
- [ ] `systemctl --user daemon-reload`（改 service 文件后）
- [ ] `systemctl --user enable aimc`（开机自启）
- [ ] `systemctl --user start aimc`
- [ ] `curl http://localhost:8080/health` 返 `{"status":"ok"}`
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

## 故障排查（部署阶段）

| 症状 | 检查 | 修复 |
|------|------|------|
| `start` 失败 | `journalctl --user -u aimc -n 50` | 看 traceback |
| 端口被占用 | `ss -tlnp \| grep 8080` | 杀进程或换端口 |
| systemd user 不工作 | `loginctl show-user $USER` 看 Linger | `sudo loginctl enable-linger $USER` |
| 文件权限错 | `ls -la /home/lxc/aimc/` | `sudo chown -R $USER:$USER /home/lxc/aimc` |
| 容器内 systemd 不可用 | `lxc-attach aimc -- bash -c "systemctl --user status"` | 检查 `loginctl enable-linger` |
