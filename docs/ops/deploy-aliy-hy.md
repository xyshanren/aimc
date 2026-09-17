# AIMC 部署到 aliy-hy 执行方案

> 目标：从 ubuntu2401 (本地 VM) 迁移到 aliy-hy (阿里云河源 ECS)，公网可访问
> 前提：代码缺口已修复（Cookie Secure + Login 限速），Cloudflare Tunnel 方案已定
> 执行时机：用户确认后开始（本文件仅规划，不含执行）

---

## 资源基线

| 维度 | 当前值 | AIMC 需求 | 余量 | 结论 |
|------|--------|-----------|------|------|
| CPU | 2 vCPU / load 0.02 | ~5% | 充足 | ✅ |
| 内存 | 1.8 GiB / 已用 1.1G / 可用 776M | ~350MB | ~420MB | ⚠️ 紧，必须加 swap |
| Swap | 0 | — | — | ❌ **P0 必须先加** |
| 磁盘 | 40G / 已用 24G / 剩 14G | ~1.5G | ~12G | ✅ |
| 带宽 | 3 Mbps | 日常 <0.1 Mbps | — | ✅ 常规用 |

---

## Phase 0: 前置准备（aliy-hy 上）

### 0.1 添加 swap（必需，防 OOM）

```bash
# 2GB swapfile，物理内存 1.8G 的 111%，足以防止 OOM killer 杀 AIMC
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
swapon --show  # 验证: /swapfile file 2G 0B -1
```

### 0.2 确认 Python 环境

```bash
python3 --version  # 需要 ≥ 3.10，aliy-hy 已有 3.11.15 (pyenv)
pip3 --version
```

### 0.3 创建运行用户 + 目录

```bash
useradd -r -s /usr/sbin/nologin -d /opt/aimc -m aimc
mkdir -p /opt/aimc/data
chown -R aimc:aimc /opt/aimc
```

---

## Phase 1: 数据迁移（ubuntu2401 → aliy-hy）

### 1.1 在 ubuntu2401 上准备

```bash
# 停服务，确保 WAL 合并完成
systemctl --user stop aimc

# SQLite checkpoint — 把 WAL 写入主库，得到一个干净 .db 文件
sqlite3 /home/lxc/aimc/data/aimc.db "PRAGMA wal_checkpoint(TRUNCATE); PRAGMA wal_checkpoint(FULL);"
ls -lh /home/lxc/aimc/data/aimc.db  # 确认 WAL 已清空 (0 bytes)

# 压缩（420M 压缩后约 60-100M，加快传输）
gzip -c /home/lxc/aimc/data/aimc.db > /tmp/aimc.db.gz
```

### 1.2 传到 aliy-hy

```bash
scp /tmp/aimc.db.gz root@47.113.200.130:/opt/aimc/data/aimc.db.gz
ssh root@47.113.200.130 "cd /opt/aimc/data && gunzip aimc.db.gz && chown aimc:aimc aimc.db && chmod 640 aimc.db"
```

### 1.3 .env 迁移（密钥分发）

```bash
# 从 ubuntu2401 读生产 .env，传到 aliy-hy（不经过本机终端历史）
# 注意: 修改 AIMC_HOST 和 AIMC_PORT
scp ubuntu2401:/home/lxc/aimc/.env /tmp/aimc.env
# 手工编辑: AIMC_HOST=127.0.0.1, AIMC_PORT=8080 (或保持不变, 加防火墙限制)
scp /tmp/aimc.env root@47.113.200.130:/opt/aimc/.env
ssh root@47.113.200.130 "chown aimc:aimc /opt/aimc/.env && chmod 600 /opt/aimc/.env"
```

---

## Phase 2: 代码部署（aliy-hy 上）

### 2.1 安装

```bash
# 代码来源: gitee 私有仓库 (AIMC 托管在 gitee 非 GitHub)
# 方案 A: git clone (推荐, 后续更新方便)
su -s /bin/bash aimc -c 'git clone <gitee-repo-url> /opt/aimc && cd /opt/aimc && git checkout master'

# 方案 B: rsync 从 ubuntu2401 (如果 gitee 不方便)
# rsync -avz --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
#   ubuntu2401:/home/lxc/aimc/ /opt/aimc/
```

### 2.2 虚拟环境 + 依赖

```bash
su -s /bin/bash aimc -c '
cd /opt/aimc
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install --with-deps chromium  # 装 Chromium + 系统依赖 (约 500M)
'
```

> **内存预警**: `playwright install --with-deps` 会拉约 200-500M 系统包 + 656M 浏览器二进制。
> 磁盘剩 14G 够用，但 apt 下载时注意观察。

### 2.3 验证 DB 和代码匹配

```bash
# 只读检查，不动数据
su -s /bin/bash aimc -c '
source /opt/aimc/.venv/bin/activate
python -c "
import sqlite3
conn = sqlite3.connect(\"/opt/aimc/data/aimc.db\")
cur = conn.cursor()
for table in [\"channels\",\"accounts\",\"api_keys\",\"usage_logs\",\"model_registry\"]:
    c = cur.execute(f\"SELECT COUNT(*) FROM {table}\").fetchone()[0]
    print(f\"{table}: {c} rows\")
conn.close()
"
'
# 预期: channels >0, accounts >0, api_keys >0
```

---

## Phase 3: systemd 服务

### 3.1 复制 systemd unit

```bash
# 从 ubuntu2401 拿现成 unit，改两处:
#   - WorkingDirectory=%h/aimc → /opt/aimc
#   - MemoryMax=2G → MemoryMax=1G (aliy-hy 只有 1.8G 物理内存)
#   - ExecStartPre 的 pkill 路径适配 (chrome-linux64)

cat > /etc/systemd/system/aimc.service <<'EOF'
[Unit]
Description=AIMC - AI Model Compass Gateway
Documentation=https://github.com/<your-repo>
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=aimc
Group=aimc
WorkingDirectory=/opt/aimc
EnvironmentFile=/opt/aimc/.env
ExecStartPre=/bin/bash -c 'pids=$(pgrep -f "[c]hrome-linux64/chrome" 2>/dev/null); [ -n "$pids" ] && kill $pids 2>/dev/null; true'
ExecStart=/opt/aimc/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=60
TimeoutStopSec=30
KillMode=control-group
StandardOutput=journal
StandardError=journal
LimitNOFILE=65536
MemoryMax=1G

NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=/opt/aimc/data /opt/aimc/.venv

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable aimc
```

### 3.2 启动 + 验证

```bash
systemctl start aimc
sleep 5
curl -s http://127.0.0.1:8080/health
# 预期: {"status":"ok","service":"aimc"}

curl -s -H "Authorization: Bearer <AIMC_API_KEY>" http://127.0.0.1:8080/v1/models
# 预期: {"object":"list","data":[...]}
```

---

## Phase 4: Cloudflare Tunnel

### 4.1 安装 cloudflared

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb
dpkg -i /tmp/cf.deb
cloudflared --version
```

### 4.2 配置 Tunnel

```bash
# 浏览器登录 (在本地浏览器完成授权)
cloudflared tunnel login

# 创建 tunnel
cloudflared tunnel create aimc
# 记录 TUNNEL_UUID

# 配置 /root/.cloudflared/config.yml
cat > /root/.cloudflared/config.yml <<EOF
tunnel: <TUNNEL_UUID>
credentials-file: /root/.cloudflared/<TUNNEL_UUID>.json
ingress:
  - hostname: aimc.your-domain.cn
    service: http://127.0.0.1:8080
  - service: http_status:404
EOF

# DNS 路由
cloudflared tunnel route dns aimc aimc.your-domain.cn

# 启动 + 开机自启
systemctl enable cloudflared
systemctl start cloudflared
```

### 4.3 验证

```bash
# 本机 (应该 200, 127.0.0.1 直连)
curl -s http://127.0.0.1:8080/health

# 公网 (应该 200, 通过 Cloudflare HTTPS)
curl -s https://aimc.your-domain.cn/health
# 预期: {"status":"ok","service":"aimc"}

curl -s https://aimc.your-domain.cn/v1/models
# 预期: 401 (未带 key)

curl -s -H "Authorization: Bearer <AIMC_API_KEY>" https://aimc.your-domain.cn/v1/models
# 预期: {"object":"list",...}
```

---

## Phase 5: 端口加固（趁部署窗口顺手做）

| 端口 | 现状 | 建议 | 优先级 |
|------|------|------|--------|
| 3306 (mysql8) | 0.0.0.0 | 安全组已限 IP，**保持现状**（改 bind 会破坏容器内服务名解析） | — |
| 5432 (pg16) | 0.0.0.0 (host network) | 安全组已挡 | ⚠️ 长期建议改 `pg_hba.conf` 只监听 localhost |
| 5000 (registry) | 0.0.0.0 | 安全组已限 116.205.244.181 | ⚠️ 建议加 Docker registry auth (htpasswd) |
| 8080 (AIMC) | 127.0.0.1 | ✅ 部署后只在 localhost | — |

> 安全组已挡 3306/5000/5432，这些不急修。但建议在下次维护窗口处理。

---

## Phase 6: 验证清单

```bash
# 1. 健康检查
curl -s https://aimc.your-domain.cn/health

# 2. API 鉴权
curl -s https://aimc.your-domain.cn/v1/models
# 401 ✓

curl -s -H "Authorization: Bearer $AIMC_API_KEY" https://aimc.your-domain.cn/v1/models
# 200 ✓

# 3. Dashboard
curl -s https://aimc.your-domain.cn/dashboard
# 302 → /dashboard/login ✓

# 4. 登录 + Cookie Secure
curl -s -X POST https://aimc.your-domain.cn/dashboard/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"token\":\"$AIMC_DASHBOARD_TOKEN\"}" -v
# 200 + Set-Cookie 带 Secure 标记 ✓

# 5. Login 限速
for i in {1..6}; do
  curl -s -X POST https://aimc.your-domain.cn/dashboard/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"token":"wrong"}' -o /dev/null -w "%{http_code}\n"
done
# 前5次: 401, 第6次: 429 ✓

# 6. 系统资源
free -h   # Mem available > 300MB, Swap used > 0
docker stats --no-stream  # AIMC + cloudflared 均 < 500MB
```

---

## Phase 7: 回滚方案

如果部署后发现问题，回滚到 ubuntu2401：

```bash
# 1. 停 aliy-hy 的 aimc + cloudflared
systemctl stop aimc cloudflared

# 2. 在 ubuntu2401 恢复原服务 (服务未停，只是不在 .env 中切换)
# 确认 ubuntu2401 的 aimc 仍在跑:
ssh ubuntu2401 "systemctl --user status aimc"

# 3. 修改 DNS 记录 / tunnel 指向回 ubuntu2401 (如果用了 CNAME 策略)
# 或直接在 cloudflare dashboard 删除 tunnel
cloudflared tunnel delete aimc
```

> 回滚只影响云端，ubuntu2401 是原生产，一直跑着。数据不丢。

---

## 部署后观察（第 1 周）

| 指标 | 正常范围 | 检查方式 |
|------|----------|----------|
| AIMC 内存 | 200-500 MB | `docker stats` / `systemctl status aimc` |
| Swap 使用 | 缓慢增长 ≤ 500MB | `free -h` |
| cloudflared 内存 | 30-80 MB | `docker stats cloudflared` |
| 响应延迟 | <2s (正常请求) / <5s (大请求) | `curl -w "%{time_total}" ...` |
| 429 错误 | 0 (Cloudflare 层) / 0 (AIMC 层) | `journalctl -u aimc \| grep 429` |
| 登录失败 | 偶尔 401 正常，无连续 401 爆发 | `journalctl -u aimc \| grep login` |

---

## 执行总耗时估算

| 阶段 | 预估时间 | 备注 |
|------|----------|------|
| Phase 0 (swap + 用户) | 5 min | |
| Phase 1 (数据迁移) | 15 min | DB 压缩 + scp |
| Phase 2 (代码部署) | 20 min | pip install + playwright |
| Phase 3 (systemd) | 10 min | 含首次启动调试 |
| Phase 4 (Cloudflare) | 15 min | 含浏览器登录 + DNS |
| Phase 5 (端口加固) | 10 min | 可选 |
| Phase 6 (验证) | 10 min | |
| **合计** | **~1.5 小时** | 不含域名实名审核 (1-3 天) |
