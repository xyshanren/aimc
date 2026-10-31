# tx-ai 迁移方案 — aliy-hy → 腾讯云广州

> 2026-10-26 定稿。前置评估与硬化记录见记忆 `tx-ai-server-assessment-20261026`。
> 结论依据:aliy-hy 2c/1.8G 内存 61% 占用 + 盘 68% 满;tx-ai 4c/4G/40G SSD;
> 带宽同为 3 Mbps;tx-ai 300GB/月流量配额 ≈ 常态基线(~33 GB/月)的 9 倍余量
> (近期高流量为视觉任务压测,非常态,用户确认数日内结束)。

## 0. 目标架构

| 项 | aliy-hy(现役) | tx-ai(目标) |
|---|---|---|
| 系统 | OpenAnolis 3 / Python 3.11.15 | Ubuntu 24.04.4 / Python 3.12.3 |
| 部署路径 | /opt/aimc | /opt/aimc(同构) |
| 服务 | aimc.service + tailcat-serve.service + aimc-weekly-report-system.timer | 同左 |
| 网关监听 | 127.0.0.1:8080 | 127.0.0.1:8080(不变) |
| 客户端通道 | tailcat P2P(Windows forward)+ WSL ssh 隧道 aimc-tunnel | 重指到 tx-ai 节点 |
| 安全 | fail2ban / 密钥登录 | fail2ban + auditd + ufw(仅 22)✓ 已硬化 |

硬化状态(2026-10-26 已完成并验证):`PermitRootLogin no` / `MaxAuthTries 3` /
`PasswordAuthentication no`;ufw active(deny incoming,仅 22/tcp);fail2ban sshd
jail active;auditd active;apparmor + unattended-upgrades active;时区 Asia/Shanghai
(DB 全库本地时间依赖,已对齐,勿改)。

## 1. 迁移物料清单(aliy-hy 实测)

- 代码:gitee `XiaoYRecluse/aimc`,HEAD `9171e89`(与本地仓库一致)
- DB:`/opt/aimc/data/aimc.db` 3.4G(WAL 模式)+ `scheduler_jobs.db` 76K
- 配置:`/opt/aimc/.env` 21 键(含 `AIMC_MASTER_KEY` 账号密钥解密主钥、
  `AIMC_PROXY_URL`、`AIMC_DASHBOARD_TOKEN`、飞书 webhook、SILICONFLOW_API_KEY 等)
- systemd:`aimc.service`(MemoryMax=1G,ProtectSystem=full,User=aimc)、
  `tailcat-serve.service`(tailcat v0.6.0,`--key=default` + 6 个 nodekey 白名单,
  `TAILCAT_ADDR_FILE=/opt/aimc/tailcat.addr`,8080)、
  `aimc-weekly-report-system.{service,timer}`(周一 09:00,三段 ExecStart:
  aggregate → generate → publish;只读 DB,允许与主服务并发)
- 二进制:`/usr/local/bin/tailcat` v0.6.0
- venv:.venv(Python 3.11)+ Playwright chromium(`~/.cache/ms-playwright`)
- 备份:仅 /opt/aimc/backups 下旧手备;hbrclient 为阿里云自带,tx-ai 无对应物(可放弃)

## 2. 阶段 A — tx-ai 环境预建(T-1 天,不影响生产)

> **✅ 已完成(2026-10-27 01:35 CST)**:系统依赖(git 2.43 / py3.12.3 / vnstat)+ Node
> v20.20.2(npmmirror tarball);aimc 用户 + gitee clone HEAD `9171e89`;venv 43 包 262M;
> playwright chromium 681M;tailcat v0.6.0 + .env(21 键,aimc:600)+ 4 个 systemd 单元
> 已装**未启用**;**pytest 1063 passed / 5:08**(阶段 A 退出标准达成)。rag_demo 同批预建:
> /opt/rag_demo HEAD `542d8ae` + venv 175 包(注意:Linux pip 拉全量 CUDA torch,venv 6.5G,
> 网关模式不加载权重,RSS 不受影响,仅占盘)。`AIMC_PROXY_URL` 实测为空,§8 该风险关闭。
> 遗留小项:割接 enable 时把 aimc.service 的 MemoryMax 1G 调 2G(§9 护栏表)。



```bash
# 系统依赖
sudo apt-get install -y git python3-venv python3-pip vnstat
# 代码
sudo git clone https://gitee.com/XiaoYRecluse/aimc.git /opt/aimc
sudo useradd -r -m -d /opt/aimc aimc 2>/dev/null; sudo chown -R aimc:aimc /opt/aimc
# venv(3.12;本地 conda 3.13 全绿,低风险)
cd /opt/aimc && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium && .venv/bin/playwright install-deps chromium
# tailcat 二进制 + 单元(从 aliy-hy scp,unit 原样复制)
sudo scp aliy-hy:/usr/local/bin/tailcat /usr/local/bin/tailcat
# .env 直传(机对机 scp,不经中转;执行时核对 AIMC_PROXY_URL 语义是否指向 aliy 网内)
```

退出标准:`python -m pytest tests/` 冒烟(全量 1063 项,4c 预计 <10min)+ vnstat 起采。

## 3. 阶段 B — DB 预传(低谷 08:00 时段,rsync 增量法)

> **⏸ 推迟至正式割接前执行(2026-10-27 用户拍板)**:tx-ai 先以配置瘦身库进入联调
> (见 §10),完整 3.4G DB 预传与割接合并到真切换窗口一并做。
> 已核实:3.4G 中 **95% = request_transcripts 3.2G**(72h 滚动,被视觉压测撑大),
> usage_logs 仅 16.7M,**配置表合计 ~3MB / 796 行**——联调不需要用量数据。

3.4G @ aliy-hy 3 Mbps 出口 ≈ 2.5-3h,故分两段,割接窗口只传增量:

```bash
# aliy-hy(服务不停,WAL 快照一致)
sqlite3 /opt/aimc/data/aimc.db ".backup /tmp/stage.db"
rsync -z /tmp/stage.db tx-ai:/tmp/stage.db        # 首传 ~1.5-2.5h
```

## 4. 阶段 C — 割接(低谷窗口,预计停机 10-20 分钟)

```bash
# 1) aliy-hy 停写
sudo systemctl stop aimc aimc-weekly-report-system.timer
sudo -u aimc sqlite3 /opt/aimc/data/aimc.db "PRAGMA wal_checkpoint(TRUNCATE);"
sqlite3 /opt/aimc/data/aimc.db ".backup /tmp/stage.db"      # 重新 stage
rsync -z /tmp/stage.db tx-ai:/tmp/stage.db                  # 增量,通常 <15min
# 2) tx-ai 落位
sudo mv /tmp/stage.db /opt/aimc/data/aimc.db
sudo rsync aliy-hy:/opt/aimc/data/scheduler_jobs.db /opt/aimc/data/
sudo chown -R aimc:aimc /opt/aimc/data
# 3) tailcat 起服 → 读 /opt/aimc/tailcat.addr 取 tx-ai nodekey
#    端口列表给 8080 3800 两个(一个 serve 进程即可,forward 端也是变参多映射);
#    Windows 侧 AIMC-Tailcat-Watchdog 计划任务的 forward 命令加 `3800` 映射
# 4) 客户端重指:WSL aimc-tunnel(ssh 隧道改目标)+ Windows tailcat forward(改 nodekey)
#    Plan B:tailcat P2P 不通则两端临时 ssh -L 直连 tx-ai:22
# 5) 起服
sudo systemctl start aimc && sudo systemctl enable --now tailcat-serve aimc-weekly-report-system.timer
```

## 5. 阶段 D — 验证清单(全绿才算割接完成)

1. `systemctl status aimc` 绿,journal 无 error
2. e2e:经网关发一次真实 chat(AIMC_API_KEY),`usage_logs` 落新行
3. 账号密钥解密正常(`AIMC_MASTER_KEY` 生效)——第 2 步成功即证明
4. 看板经通道可开,dashboard 数据连续(旧 usage_logs 可查)
5. 渠道探活第一轮跑完,无 needs_human 误报
6. `systemctl list-timers` 见周报 timer;transcript 72h 清理调度在跑
7. vnstat 当日流量计数正常

## 6. 阶段 E — 回滚预案(割接后 1 周内随时可用)

aliy-hy 全套不动(仅 stop,不 disable 不删除)。回滚 = 反向:tx-ai stop aimc →
aliy-hy `PRAGMA wal_checkpoint` 后 start → 客户端重指回旧 nodekey/隧道目标。
期间 tx-ai 新增的 usage_logs 若需保留,按同法反向 rsync(通常量小)。

## 7. 阶段 F — 收尾

- 周一 09:00 周报在新机触发成功 → aliy-hy 保留一周热备后下线(先 disable,数据再留 30 天)
- tx-ai 流量告警:vnstat 月累计 >210GB(70%)时人工或脚本经飞书 webhook 提醒
- 阿里云 aegis/备份通道随 aliy-hy 一并退役;tx-ai 无 hbrclient,DB 备份策略需新定
  (建议:cron 每日 sqlite3 .backup + gzip 保留 7 份,另议)

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| tailcat P2P 重指不通(最高风险) | Plan B ssh -L 直连;tailcat 网络发现依赖两端 nodekey 注册,割接窗口内先验证 |
| Python 3.12 兼容 | 本地 conda 3.13 全绿基线;阶段 A 全量测试把关 |
| `AIMC_PROXY_URL` 指向 aliy 网内代理 | 阶段 A 执行时核对值语义,必要时置空/改公网代理 |
| DB 增量比预期大(压测尾巴) | 割接窗口选压测结束后;增量 >1G 则二次 rsync |
| SQLite 版本差(3.45 vs Anolis 旧版) | WAL 正向兼容;tx-ai 侧只读新写,无降级路径 |
| 单 caller 流量再冲高 | vnstat 70% 告警;3 Mbps 峰值两机相同,若常态化再议双活(需先解 SQLite 单写+冷却状态分裂,见记忆 minimax-2056 / group-admission) |

## 9. 同机部署 rag_demo + knowledge-orbit(2026-10-26 增补)

### 9.1 实测资源画像(生产现役数据,2026-10-26)

| 项目 | 运行形态 | 实测 RSS | 磁盘 | 当前部署位 |
|---|---|---|---|---|
| rag_demo | rag-demo-api(uvicorn)+ rag-demo-mcp 双 systemd 服务 | API 104M + MCP 140M ≈ **244M** | venv 2.0G + 项目 457M + chroma_db 24M | ubuntu2401 (/root/rag_demo) |
| knowledge-orbit | Next.js 16 standalone + systemd,端口 3800 | next-server **84M** | 1.7G(含 node_modules;standalone 裸跑 ~0.5G)+ data 6.1M | anolis (/root/workspace) |

**rag_demo 部署版 .env 已坐实纯网关模式**(这是 tx-ai 装得下的前提):
`EMBEDDING_PROVIDER=aimc`(baai-bge-m3)/ `LLM_PROVIDER=aimc` /
`RERANK_PROVIDER=aimc`(baai-bge-reranker-v2-m3)/ `VECTOR_PROVIDER=chroma` /
`GRAPH_PROVIDER=networkx` / `AIMC_API_BASE=http://127.0.0.1:8080/v1`。
Ollama inactive、无 torch/huggingface 本地模型缓存。**tx-ai 上 AIMC 本机跑,
该地址原生成立**(ubuntu2401 现在靠 tailcat forward 才有这个 localhost:8080)。
禁止在 tx-ai 启用 ollama/sentence-transformers 本地模型路径(会把 RSS 抬到 1.5G+)。

### 9.2 tx-ai 容量核算(4c / 4G / 40G / 3Mbps+300GB月)

| 资源 | AIMC | rag_demo | knowledge-orbit | OS+agent | 合计 |
|---|---|---|---|---|---|
| RAM 稳态 | ~0.6G | 0.25G | 0.1G | ~0.5G | **~1.5G / 4G ✓** |
| RAM 峰值(Playwright 活跃) | ~1.0G | 0.3G | 0.15G | 0.5G | ~2.0G ✓ 余 2G |
| 磁盘 | 6.7G(DB 还在涨) | ~2.5G | ~0.5-1.7G | 5.6G | **~15-16G / 40G ✓** |
| CPU | 5.6%(2c 机器实测) | 低 | 低 | — | 4c 富余 ✓ |
| 公网带宽 | AIMC→上游(主头) | 近零(链路全 localhost) | 近零(同左) | — | 配额口径不变 ~9× 余量 ✓ |

带宽侧关键收益:KO→rag_demo→AIMC 链路从现在的
anolis→ubuntu2401→tailcat→aliy-hy 三跳内网+隧道,收敛为 tx-ai 单机 localhost 三连,
**公网带宽账单不变**(上游调用本来就计入 AIMC 的 ~33GB/月),链路延迟反而下降。
文档上传 ≤50MB/文件 @3Mbps ≈ 2-3 分钟,个人使用可接受。

### 9.3 部署要点(并入阶段 A/C)

- **Node 20+ 必须额外装**:Ubuntu 24.04 apt 的 nodejs 是 18.19,不满足 KO 要求
  (NodeSource 或 nvm 固定 v20 LTS)
- rag_demo:**✅ 已部署并验收(2026-10-27)**——/opt/rag_demo(rag 用户,HEAD `542d8ae`,
  venv 175 包)+ chroma_db 425M 生产数据自 ubuntu2401 拷贝 + .env(MCP_HOST 改 127.0.0.1)+
  rag-demo-api/mcp/target 三单元(User=rag,MemoryMax 1G/512M,全 127.0.0.1 绑定)。
  验收:`/api/v1/health` 200(ChromaDB 挂载+embedding baai-bge-m3)、MCP tools/list 200
  (⚠️ `MCP_API_KEYS` 是 JSON dict 非 CSV,REST 面另有用户体系鉴权,KO 只走 MCP)。
  ubuntu2401 原实例未动,双实例并行联调
- 跨项目部署规范已固化共用卡:**D:/work/workspace/zcode/tx-ai-deploy-spec.md**
  (端口注册表/用户与 unit 模式/MemoryMax 预算/凭据规则/验收清单,KO 按卡自主部署)
- knowledge-orbit:本机 `npm run build` → `scripts/deploy-standalone.sh`
  传 standalone 产物 → systemd unit 固化版 `scripts/knowledge-orbit.anolis.service`
  改路径复用;`RAG_API_URL=http://127.0.0.1:1817/mcp`
- **MemoryMax 护栏(必设,防互相挤压 AIMC)**:
  `rag-demo-api.service` 1G / `rag-demo-mcp.service` 512M /
  `knowledge-orbit.service` 768M / `aimc.service` 保持 2G(§2 计划值)
  —— 全部护栏同时打满 = 4.3G 理论超 4G,实际峰值组合 ~2G,护栏只兜异常泄漏
- 访问面:三服务全部绑 localhost / 走 tailcat / ssh 隧道,**ufw 维持仅 22 不动**;
  rag_demo 自带 JWT+API Key,KO MCP 端点配 `KO_MCP_API_KEY`
- 备份(§7 F 阶段扩列):rag_demo chroma_db+项目配置、KO data/(官方
  `scripts/backup-db.sh` + `ko-backup.timer` 现成,14 天保留)一并纳入每日备份
- 割接顺序建议:AIMC 先行(§4),rag_demo/KO 随后低风险窗口单独切
  (两者都带降级/重试,切错可立即回退,无数据写入风险窗口)

### 9.4 候选清理项(tx-ai 存储紧张时再动手)

| 项 | 体积 | 说明 |
|---|---|---|
| rag_demo venv 的 CUDA torch | ~4.5G(venv 6.5G vs ubuntu2401 纯 CPU 2.0G) | 网关模式不加载权重,RSS 无影响;要清则 `pip install torch --index-url https://download.pytorch.org/whl/cpu` 重装 CPU 版 |
| playwright 浏览器缓存 | 681M | AIMC 情报采集在用,**勿清** |
| /opt/aimc.bak | 16M | 已清(2026-10-27) |
| pip 缓存(aimc 74M + rag 3.2G)+ apt 缓存 231M | ~3.5G | **已清(2026-10-27)** |
| docker/containerd(空载) | RAM ~150M + 盘 | **已停用**(无容器无镜像,systemctl enable 可恢复) |

当前盘况:14G 已用 / 24G 余(2026-10-27 清理后)。

## 10. 联调阶段运行态(2026-10-27 起生效)

**用户拍板:跳过 B/C,tx-ai 先供本地项目联调,不对外服务;完整 DB 预传 + 割接推迟到真切换窗口。**

- AIMC 运行于配置瘦身库:`/opt/aimc/data/aimc.db`(2.0M,全 schema + 22 张配置表
  796 行,数据表留空;accounts 密文 key 靠已迁移的 AIMC_MASTER_KEY 解密)
- `aimc.service` **enabled + active**,127.0.0.1:8080,MemoryMax 已调 2G;
  健康检查 200 / key 鉴权 / 1-token e2e(glm-4.7-flash)/ usage_logs 落库 全部实测通过
- **tailcat-serve 与周报 timer 保持 disabled**(联调用 `ssh -L 8080:127.0.0.1:8080 tx-ai`
  接入;正式割接时才启用 tailcat 并重指客户端)
- 联调期已知代价(可接受,正式割接时消解):
  1. 与 aliy-hy 生产**共享上游账号但冷却状态各自独立**——联调流量务必压低
  2. 配置快照冻结于 2026-10-27 05:47,生产的渠道/价格每日同步不跟随——联调看到的价格/渠道可能与生产有偏差
  3. **联调期间 tx-ai 的 usage_logs 等数据在正式割接时会被生产 DB 覆盖丢弃**,需要留存的话割接前先导出
  4. `AIMC_CRAWL_ENABLED=true` 从 .env 原样带来——tx-ai 会与生产双跑情报采集;联调期无害,介意可置 false 重启
  5. deepseek 余额门 sync(每 1min)双实例轮询上游——量极小,已知即可
