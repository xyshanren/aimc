# Windows 便携版 (一键启动)

> 受众：只有 Windows 电脑、不想装 Python 的个人用户
> 形态：解压即用 — 内嵌 Python 3.12 + 预装依赖，首次启动零网络
> 类似 ComfyUI-Portable / MoneyPrinterTurbo-Portable 的分发方式

## 1. 下载与启动

1. 解压 `AIMC-Portable-v<版本>.zip` 到任意目录（**路径不要含中文**，个别依赖对中文路径兼容差）
2. 双击 `start-aimc.bat`
3. 浏览器自动打开看板 `http://127.0.0.1:18080/dashboard/` — 首次启动会直接进入
   **「初始化配置」页**，按引导注册免费渠道并填入 API Key 即可开始使用

> 首次启动会自动生成 `.env`（含随机加密主密钥 `AIMC_MASTER_KEY` 和网关 key
> `AIMC_API_KEY`）与 `data/aimc.db`。**请备份 `.env`** — 主密钥丢失 = 已录入的
> 渠道 key 全部无法解密。

## 2. 停止 / 升级 / 备份

| 操作 | 方式 |
|------|------|
| 停止 | 关闭 start-aimc.bat 的命令行窗口，或双击 `stop-aimc.bat` |
| 备份 | 复制 `data/` + `.env` 两个文件 |
| 升级 | 下载新便携版，用旧包的 `data/` + `.env` 覆盖新包同名文件 |
| 换端口 | 编辑 `.env` 的 `AIMC_PORT=` 后重启 |

## 3. 客户端接入

```
base_url = http://127.0.0.1:18080/v1
api_key  = .env 里的 AIMC_API_KEY（首启自动生成）
```

多用户 key（按人/按工具记账）见 [multi-user-guide.md](multi-user-guide.md)。

## 4. 安全默认值

- 服务只绑定 `127.0.0.1`（`.env` 的 `AIMC_HOST`），局域网其他设备无法访问
- 看板鉴权留空（`AIMC_DASHBOARD_TOKEN=`）— 因为只有本机能访问；如需开放局域网，
  请同时设置 `AIMC_HOST=0.0.0.0` 与 `AIMC_DASHBOARD_TOKEN`，并阅读
  [configure.md](configure.md) §3

## 5. 与 Linux 部署版的差异

- **不含 Playwright 浏览器内核**：网页型情报采集源不可用（模型价格等 httpx 型
  采集源、路由/配额/看板全部功能不受影响）；需要完整情报采集请用 Linux 部署版
  （[install.md](install.md)）
- 停止方式是关窗口（无 systemd 守护）；长期挂机建议 Linux 部署

## 6. 维护者：自己打包

```bash
# 仓库根目录（需 Python 3.11+ 与 git, Windows 10+ 自带 curl）
python portable/build_portable.py                  # 打包当前工作树
python portable/build_portable.py --source head    # 打包已提交内容 (发布用)
python portable/build_portable.py --skip-deps      # 不预装依赖 (包小, 首启需联网)
```

产出 `dist/AIMC-Portable-v<版本>.zip`。步骤：下载 Python embeddable
（npmmirror/huaweicloud/python.org 依次回退）→ 离线引导 pip（wheel 解包法）→
预装 requirements → `git ls-files` 复制源码（天然排除 .env/data/tests）→
依赖导入自检 → 打 zip。
