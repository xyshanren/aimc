# 安装 AIMC

> 受众：第一次部署 AIMC 的用户
> 前提：Linux/macOS 主机 + Python 3.11+
> 耗时：5-10 分钟

## 1. 准备环境

```bash
# Python 3.11+ (推荐 3.12)
python3 --version

# Git
git --version
```

## 2. 克隆项目

```bash
git clone git@gitee.com:XiaoYRecluse/aimc.git
cd aimc
```

## 3. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. 配置环境

```bash
cp .env.example .env
nano .env
```

**最少必填项**：

```ini
AIMC_API_KEY=your-strong-random-key-here  # 客户端调用时使用 (如不填则关闭鉴权)
SILICONFLOW_API_KEY=sk-xxx                 # 硅基流动 (聚合平台, 1 key 通吃 91 model)
DEEPSEEK_API_KEY=sk-xxx                   # DeepSeek 官方 (可选)
```

可选填：`MINIMAX_API_KEY` / `GLM_API_KEY` / `QWEN_API_KEY` / `SENSENOVA_API_KEY` / `STEPFUN_API_KEY` 等。

详见 [configure.md](configure.md)。

## 5. 启动

```bash
# 5.1 录入渠道账号 (一次性, 之后可加 Dashboard 加)
python scripts/manual_import.py

# 5.2 启动网关
python main.py
# 或 systemd (推荐生产):
# sudo systemctl --user enable aimc
# sudo systemctl --user start aimc
```

启动后默认监听 `0.0.0.0:8080`。健康检查：

```bash
curl http://localhost:8080/health
# 期望: {"status":"ok"}
```

## 6. 验证

```bash
# OpenAI SDK (Python)
python -c "
from openai import OpenAI
c = OpenAI(base_url='http://localhost:8080/v1', api_key='your-aimc-key')
print(c.chat.completions.create(model='deepseek-v4-pro',
      messages=[{'role':'user','content':'hi'}], max_tokens=5).choices[0].message.content)
"
```

## 下一步

- 接入多个账号 + Dashboard token: [configure.md](configure.md)
- 日常调用 + tier:xxx 模型组: [usage.md](usage.md)
- Dashboard 看 cache 节省 / 数据新鲜度: [dashboard.md](dashboard.md)
- 周报 / 告警 / shadow log: [operations.md](operations.md)
- 出错怎么办: [troubleshooting.md](troubleshooting.md)

## 故障排查（安装阶段）

| 症状 | 检查 | 修复 |
|------|------|------|
| `pip install` 报错 | Python 版本 ≥ 3.11 | 升级 Python / 用 pyenv |
| 启动报 `ModuleNotFoundError` | venv 没激活 | `source .venv/bin/activate` |
| 启动报 `database is locked` | 多进程启动 | `pkill -f "python main.py"` 后重启 |
| `curl /health` 连不上 | 防火墙/端口 | `ufw allow 8080` 或换端口 |
| `manual_import` 报 `Invalid API key` | .env 没填或错 | 检查 .env 重跑脚本 |
