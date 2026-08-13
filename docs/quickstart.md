# AIMC 快速开始指南

## 环境要求

- Python 3.11+
- 无需 Docker（可选）
- 无需 Redis / PostgreSQL

## 安装步骤

### 1. 克隆项目

```bash
git clone git@gitee.com:XiaoYRecluse/aimc.git
cd aimc
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

如需使用情报采集层（爬虫），还需安装 Playwright 浏览器：

```bash
playwright install chromium
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```ini
# 服务配置
AIMC_HOST=0.0.0.0
AIMC_PORT=8080

# 数据库路径
AIMC_DB_PATH=./data/aimc.db

# 网关鉴权（留空则不鉴权）
AIMC_API_KEY=your-secret-key

# AI 解析引擎（情报采集用）
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.openai.com/v1
AIMC_PARSE_MODEL=gpt-4o-mini

# Telegram 告警（可选）
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### 4. 初始化数据库并录入渠道

```bash
python scripts/manual_import.py
```

此脚本会创建 DeepSeek 和 Qwen 两个示例渠道。请编辑脚本中的 `api_key` 为你的实际 Key。

### 5. 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 8080
```

## 验证

```bash
# 健康检查
curl http://localhost:8080/health

# 查看可用模型
curl http://localhost:8080/v1/models

# 测试对话（需替换为实际 API Key）
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-aimc-key" \
  -d '{"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "Hello"}]}'
```

## 客户端接入

### OpenAI SDK (Python)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="your-aimc-key",
)

# 非流式
response = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[{"role": "user", "content": "你好"}],
)
print(response.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="qwen-max",
    messages=[{"role": "user", "content": "讲个笑话"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### OpenAI SDK (Node.js)

```javascript
import OpenAI from 'openai';

const client = new OpenAI({
  baseURL: 'http://localhost:8080/v1',
  apiKey: 'your-aimc-key',
});

const response = await client.chat.completions.create({
  model: 'deepseek-v4-flash',
  messages: [{ role: 'user', content: 'Hello' }],
});
```

## Dashboard

启动后访问 `http://localhost:8080/dashboard` 查看：
- 渠道状态面板
- 优惠情报排行
- 价格对比表
- 用量统计图表

## 添加新渠道

编辑 `scripts/manual_import.py`，参照已有格式添加：

```python
# 添加新渠道
channel = await crud.create_channel(
    session,
    name="glm_official",
    provider="glm",
    base_url="https://open.bigmodel.cn/api/paas/v4",
    priority=5,
)

# 添加账号
account = await crud.create_account(
    session,
    channel_id=channel.id,
    account_name="glm_01",
    api_key="your-glm-key",
)

# 添加价格
await crud.create_price_snapshot(
    session,
    channel_id=channel.id,
    model="glm-4",
    input_price=15.0,
    output_price=15.0,
    source="manual",
)
```

## 生产部署（Linux）

```ini
# /etc/supervisor/conf.d/aimc.conf
[program:aimc]
command=/path/to/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8080
directory=/path/to/aimc
user=www-data
autostart=true
autorestart=true
stderr_logfile=/var/log/aimc.err.log
stdout_logfile=/var/log/aimc.out.log
```

```bash
supervisorctl reread
supervisorctl update
supervisorctl start aimc
```

## 数据备份

```bash
# 手动备份
cp data/aimc.db data/aimc_backup_$(date +%Y%m%d).db

# 定时备份（crontab）
0 3 * * * cp /path/to/aimc/data/aimc.db /path/to/backup/aimc_$(date +\%Y\%m\%d).db
```
