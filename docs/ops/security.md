# 公网部署安全指南

> 受众：把 AIMC 部署到公网云服务器 + 多人共用的运维
> 前提：项目当前在**内网单人**使用，本指南是**未来公网部署**的参考
> 触发条件：需要给团队成员（> 1 人）从外部网络访问 AIMC

---

## 1. 当前安全现状（内网单人）

### 已具备

- ✅ **网关 API Key 鉴权**（`/v1/*`）— `AIMC_API_KEY` 必填，Bearer header 校验
- ✅ **Dashboard Token 鉴权**（`/dashboard/*`）— `AIMC_DASHBOARD_TOKEN`，支持 header/cookie/query
- ✅ **账号 key Fernet 加密**（P0-5）— 厂商 API key 加密入库
- ✅ **多维限额**（P0-6）— 防止单 channel 滥用
- ✅ **547 tests** — 鉴权 / 限额 / cooldown 等核心路径有测试

### 已知缺口（**内网不严重，公网必须修**）

- ❌ **HTTPS / TLS** — AIMC 监听 8080 是明文 HTTP，token / 请求体裸传
- ❌ **Cookie 安全** — `aimc_dashboard_token` 没 `Secure` / `HttpOnly` / `SameSite` 标记
- ❌ **暴力破解防护** — 登录失败无锁定，攻击者可枚举 token
- ❌ **审计日志** — 无"谁在什么时候改了什么"记录
- ❌ **速率限制** — 网关有 fallback 限速，但**没有针对登录的限速**

---

## 2. 公网部署方案对比（按国内团队 + 国内服务器）

### 方案 A：自建 nginx + Let's Encrypt

**适合**：完全控制、0 持续成本、长期方案

**步骤**：
1. 域名注册（国内域名约 ¥50/年，必须 ICP 备案）
2. 云服务器需有公网 IP（阿里云/腾讯云弹性公网 IP，~¥20/月）
3. 安装 nginx + certbot，自动签发 Let's Encrypt SSL 证书
4. nginx 反向代理 AIMC（127.0.0.1:8080）
5. AIMC 内部加 Cookie 安全标记

**成本**：
- 服务器 +1 GB RAM
- 域名 ¥50/年（+ ICP 备案 7-20 天）
- 弹性 IP ¥20/月
- 实施 0.7d

**合规**：✅ 国内服务器 + 国内流量，完全合规

---

### 方案 B：Cloudflare Load Balancer / 反向代理

**适合**：HTTPS 外包给 Cloudflare，但 AIMC 仍 HTTP（依赖外部）

**步骤**：
1. AIMC 服务保持 HTTP（监听 0.0.0.0:8080，公网可达）
2. Cloudflare 配 Origin Server + SSL/TLS 加密模式 "Full"
3. 域名解析走 Cloudflare DNS（自动 SSL）

**成本**：
- 服务器不变
- 域名 ¥50/年（**必须 ICP 备案**）
- Cloudflare 免费层
- 实施 0.1d

**合规**：✅ 但**需要 ICP 备案**（用国内 Cloudflare 节点）

---

### 方案 C2：Cloudflare Tunnel（**推荐**）

**适合**：5-10 人小团队，**0 证书管理、0 公网 IP、0 持续成本**

**原理**：
- AIMC 监听 127.0.0.1（只本地）
- `cloudflared` 客户端建立**出站**连接到 Cloudflare 边缘
- 团队通过 `https://aimc.your-domain.com` 访问
- **不需要公网 IP**、**不需要 ICP 备案**（用 Cloudflare 边缘节点）、**不需要 nginx**

**步骤**：
```bash
# 1. 装 cloudflared
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb

# 2. 登录（浏览器授权）
cloudflared tunnel login

# 3. 创建 tunnel
cloudflared tunnel create aimc
# 记录 tunnel UUID: <TUNNEL_UUID>

# 4. 配置文件 /home/lxc/.cloudflared/config.yml
tunnel: <TUNNEL_UUID>
credentials-file: /home/lxc/.cloudflared/<TUNNEL_UUID>.json
ingress:
  - hostname: aimc.your-domain.com
    service: http://127.0.0.1:8080
  - service: http_status:404

# 5. DNS 路由 + 启动
cloudflared tunnel route dns aimc aimc.your-domain.com
sudo systemctl enable cloudflared
sudo systemctl start cloudflared
```

**成本**：
- 服务器不变
- 域名 ¥50/年（**不需要 ICP 备案**，Cloudflare 海外节点中转）
- Cloudflare Tunnel 免费（个人使用无限流量）
- 实施 10-15 分钟

**合规**：✅ 国内服务器 + 国内流量
**安全**：
- AIMC 仍 0 公网端口暴露
- 自动 HTTPS（Cloudflare 签发）
- DDOS 防护（Cloudflare 边缘）

---

### 方案 C3：Cloudflare Tunnel + 免费子域名（**最简**）

如果连域名钱都不花：
```bash
cloudflared tunnel --url http://127.0.0.1:8080
# 输出: https://xxxx.trycloudflare.com
```

- 0 元
- 10 秒配置
- 缺点：URL 每次启动变

**适合**：临时测试 / 个人 / 1-2 人

---

## 3. 方案对比表

| 维度 | A 自建 nginx | B CF 反代 | **C2 CF Tunnel (推荐)** | C3 免费子域 |
|------|-------------|-----------|------------------------|-------------|
| 实施成本 | 0.7d | 0.1d | **10-15 分钟** | 10 秒 |
| 服务器变更 | +nginx/certbot | 不变 | **不变** | 不变 |
| 公网 IP | 必须 | 必须 | **不需要** | 不需要 |
| 域名 ICP 备案 | 必须（国内 IP） | 必须 | **不需要** | 不需要 |
| 域名费用 | ¥50/年 | ¥50/年 | **¥50/年**（可选） | 免费 |
| 持续成本 | 0 | 0 | **0** | 0 |
| HTTPS | Let's Encrypt | Cloudflare | **Cloudflare** | Cloudflare |
| DDOS 防护 | 无 | 强 | **强** | 强 |
| 端口暴露 | 80/443 | 80/443 | **0** | 0 |
| 适用规模 | 50+ 人 | 50+ 人 | **5-10 人** | 1-2 人 |
| 维护负担 | 中（证书续期） | 低 | **极低** | 极低 |

---

## 4. 推荐路径

### 短期（部署前 1 周）

**选 C2（Cloudflare Tunnel）**：
- 10-15 分钟配置
- 0 证书管理
- AIMC 仍 0 公网端口暴露（最安全）
- 后续可无缝升级到 A

### 中期（团队 > 10 人 / 需要域名稳定）

**升级到 A（自建 nginx）**：
- 完全控制
- 0 持续成本
- 一次性配置

### 长期（流量大 / 需要自建监控）

**保持 C2 或升级到 B（Cloudflare Load Balancer）**：
- 取决于具体需求

---

## 5. 配合 AIMC 自身的安全改动（无论选哪个方案）

无论用 C2 / B / A，**AIMC 内部**都应该加这些（10-20 行代码）：

```python
# gateway/middleware.py 加 cookie 安全头
response.set_cookie(
    "aimc_dashboard_token",
    token,
    httponly=True,
    secure=True,        # HTTPS only（公网必备）
    samesite="strict",  # CSRF 防护
    max_age=7 * 24 * 3600,
)
```

```python
# gateway/routes.py 加 login 失败锁定
# 5 次错误锁 5 分钟（按 IP 计数）
```

```python
# gateway/middleware.py 加操作审计
# 任何 PUT / POST 写操作都记 audit_log
```

---

## 6. 触发本指南的检查清单

部署前 1 周，按这个顺序：

- [ ] 决定方案（C2 / B / A）
- [ ] 注册域名（如需要）+ ICP 备案（仅 A/B）
- [ ] 装 cloudflared / nginx
- [ ] 配置 SSL 证书
- [ ] AIMC 监听改为 127.0.0.1（C2）/ 0.0.0.0 + 防火墙白名单（A/B）
- [ ] 加 cookie 安全头 + 登录锁定
- [ ] 验证：从外部用 https://aimc.your-domain.com 访问能正常工作
- [ ] 验证：HTTP 访问自动 301 到 HTTPS
- [ ] 验证：错误的 API Key 仍然 401
- [ ] 备份：cloudflared 配置 + AIMC .env + db

---

## 7. 法律合规要点（国内）

**不需要 ICP 备案**的场景：
- AIMC 部署在**国内云服务器**（阿里云/腾讯云/华为云）
- 团队**国内访问**
- 用 **Cloudflare Tunnel**（Cloudflare 边缘节点中转）

**需要 ICP 备案**的场景：
- 域名解析到**国内 IP**（公网）
- AIMC 监听 **0.0.0.0:80/443**

**绝对禁止**：
- ❌ 翻墙（个人 VPN 访问 Google）
- ❌ 未经许可的"VPN 服务"（法律上属于"国际联网服务"）

**不涉及**（你的场景）：
- ✅ 公司用 WireGuard / Tailscale 让员工访问公司内网（合规灰色，但**有先例合法**）
- ✅ 用 Cloudflare Tunnel（CDN 厂商）做安全加速

---

## 8. 决策记录（2026-08-17 用户讨论结果）

### 用户实际情况
- **云服务器**：阿里云河源区域 `aliy-hy`（cn-heyuan），2 vCPU / 1.8 GB RAM / 40 GB 磁盘
- **配置评估**：3-5 人团队，AIMC + cloudflared ≈ 200-300MB RAM，仍有余量
  - **短期不升级**（按需触发，1.8GB 够 3-5 人）
  - 升级触发：> 5 人 / 高频调用 / dashboard 出现 OOM
- **已做防护**：fail2ban SSH + DROP rpcbind 端口 + 公钥认证 + 阿里云安骑士 + 混合云备份
- **决策选 C2（Cloudflare Tunnel）** + 阿里云 .cn 域名（首年 ¥9）

### 域名策略
- **推荐**：阿里云 `.cn` 域名（如 `aimc.your-domain.cn`），首年 ¥9
- **实名**：需要身份证（个人）/营业执照（企业）
- **Cloudflare 配海外节点中转**（香港/新加坡），免 ICP 备案
- **延迟**：阿里云深圳 → Cloudflare HK 节点 ≈ 30-50ms（用户几乎无感）

### 取消公网 IP 决策
- **暂保留**（建议）：公网 IP 仍提供 SSH + 应急 fallback
- **不取消的理由**：Cloudflare 挂了 / tunnel 配置错 → 完全失联
- **未来 1 个月稳定后**再考虑释放 EIP（设带宽 0）
- **如果保留**：必须用防火墙白名单 Cloudflare IP 段（`https://www.cloudflare.com/ips/`），其他全 deny

### 内存升级 4GB 决策（2026-08-17 调查）

**当前不升级**（按需触发）：
- 1.8GB RAM 当前够 3-5 人
- AIMC + cloudflared ≈ 200-300MB
- 触发：> 5 人 / 高频调用 / dashboard 慢

**河源区域优势**：
- 阿里云 2024 新开区，**价格最便宜**（比老区便宜 50-100%）
- 同账号升降配**不迁移数据**（1 分钟生效）

**4GB 升级方案**：

| 厂商 | 2c4g 月费 | 2c4g 年付 | 适用 |
|------|---------|---------|------|
| **阿里云河源** | ~¥30-50 | ~¥200-400 | **当前，1 分钟升，最划算** |
| 阿里云学生机 | — | ¥9-99/年 | 仅学生认证 |
| 腾讯云新人 | ~¥30-60 | ~¥300-500 | 秒杀价 |
| 华为云 | ~¥50-80 | ~¥400-600 | 一般 |
| UCloud | ~¥25-50 | ~¥200-400 | 小厂更便宜 |

**在线升级步骤**（aliy-hy）：
1. 阿里云控制台 → ECS → aliy-hy → 升降配
2. 选 4GB（保留 2 vCPU）
3. 在线升级，1 分钟生效
4. 付款 ~¥30-50/月
5. 验证：`free -h`

### 实施步骤（部署前 1 周）

```bash
# 1. 阿里云注册 .cn 域名 (aimc.your-domain.cn, ¥9 首年)
#    控制台 → 域名 → 注册 .cn → 实名 (身份证) → 审核 1-3 天

# 2. Cloudflare 加域名 (your-domain.cn), 改 NS 记录到 Cloudflare
#    Cloudflare 控制台 → 添加站点 → 选 Free 计划

# 3. 装 cloudflared 到 aliy-hy
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb
sudo dpkg -i /tmp/cf.deb

# 4. 登录 + 创建 tunnel
cloudflared tunnel login
cloudflared tunnel create aimc
# 记录 TUNNEL_UUID

# 5. 配置 /home/lxc/.cloudflared/config.yml
cat > /home/lxc/.cloudflared/config.yml << EOF
tunnel: <TUNNEL_UUID>
credentials-file: /home/lxc/.cloudflared/<TUNNEL_UUID>.json
ingress:
  - hostname: aimc.your-domain.cn
    service: http://127.0.0.1:8080
  - service: http_status:404
EOF

# 6. 改 AIMC 监听 127.0.0.1 (而不是 0.0.0.0)
#    main.py 或 systemd unit: --host 127.0.0.1

# 7. 配 Cloudflare DNS
cloudflared tunnel route dns aimc aimc.your-domain.cn

# 8. 启动 + 开机自启
sudo systemctl enable cloudflared
sudo systemctl start cloudflared

# 9. 防火墙 (INPUT policy 改 DROP, 白名单 Cloudflare + SSH)
# 阿里云安全组: 入站 22 (SSH) + 80/443 (可选)  (如果用 tunnel 只需要 22)
# iptables INPUT 改 DROP, 加 accept 规则

# 10. 验证
curl https://aimc.your-domain.cn/health
# 应返 200 OK
```

### 成本清单

| 项 | 一次性 | 年度 |
|---|--------|------|
| .cn 域名 | ¥0 | ¥9（续费 ¥30-50/年）|
| Cloudflare Tunnel | ¥0 | ¥0 |
| 阿里云 ECS（当前）| ¥0 | 现有 |
| **合计** | — | **¥9-50/年** |

### 公网 IP 决策时间表

- **部署后 1 个月**（验证 Tunnel 稳定）：考虑释放 EIP（带宽设 0，不花钱）
- **保留 SSH 应急**：把 22 端口 IP 白名单设到你的家 IP / 公司 IP
- **完全无公网**：仅 Tailscale / Cloudflare Tunnel 访问

---

**作者**：用户 + agent 协作
**决策日期**：2026-08-17
**触发时间**：部署到公网云服务器前 1 周
**推荐方案**：C2（Cloudflare Tunnel） + 阿里云 .cn 域名
**实施时间**：~30 分钟（含域名注册 + 实名审核 1-3 天）
**持续成本**：首年 ¥9（域名）+ 续费 ¥30-50/年
