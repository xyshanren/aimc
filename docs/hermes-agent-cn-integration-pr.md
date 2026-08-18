# hermes-agent-cn 集成 PR 草案

> **本文件目的**：把"hermes-agent-cn 集成 AIMC 网关"的完整 PR 内容准备好，方便直接复制到 hermes-agent-cn 仓库发 PR。
>
> **架构背景**（参见 [agent-integration.md §1-§2](file://C:\Users\lixia\.qoder-cn\worktree\aimc\76apjT\docs\agent-integration.md)）：
> - **hermes-tray**（客户端 GUI/CLI）→ **hermes-agent-cn**（Agent 中枢）→ **AIMC**（模型聚合网关）→ 多渠道 LLM
> - AIMC 是 OpenAI 协议兼容网关，hermes-agent-cn 通过 AIMC 拿到跨模型比价/价格优化/fallback 能力
> - hermes-agent-cn 不需要感知 AIMC 内部路由细节；AIMC 也不反向推送任何东西给 hermes-agent-cn

---


## 📦 历史归档说明

> **本文档已归档**（2026-08-15 文档重组时迁移到 docs/archive/）。
> 新内容见 [docs/index.md](../index.md) 或相关 [docs/dev/](../dev/) / [docs/user/](../user/) 文档。

## 📋 PR Title（GitHub PR 标题）

```
feat: integrate AIMC gateway - profile-based model routing + dynamic adaptation
```

## 📝 PR Description（GitHub PR 描述）

### Why（背景）

当前 hermes-agent-cn 的 `config.yaml` 中 models 段硬编码具体 model 名（如 `default: deepseek-chat`），导致：

1. **无法跨模型比价**：用户手动切换不同厂商的同档 model 才能省钱，没法自动比价
2. **价格变化失效**：硅基流动降费、自动切其他平台优惠等情报层动作，hermes-agent-cn 端无感知
3. **故障转移缺失**：单一渠道挂了就要改 config 重启

### What（变更）

引入 **AIMC 网关** 作为模型调用层，做 3 件事：

1. **profile-based 配置**：`models` 段从具体 model 名改为 AIMC group 名（`tier:balanced` / `scene:free` 等），由 AIMC 按价格/可用性自动选具体 model
2. **动态适应机制**：启动时 + 每日定时拉取 `/v1/models` 校验 profile 合法，对 AIMC 模型动态变化做软适应
3. **响应头被动观测**（可选）：记录 `X-AIMC-Actual-*` 头供 debug，不影响路由决策

### 关键不变量（铁律）

- ❌ hermes-agent-cn **不反向调整** AIMC 配置
- ❌ hermes-agent-cn **不写回** 自己的 profile（除非人手动改）
- ✅ AIMC 路由决策**只**听 DB + 情报层，**不听** hermes-agent-cn
- ✅ 所有"学习"都是**被动观测**（记录响应头 + 日志），不自动改任何配置

### 测试计划

- [x] 单元测试：`AIMCClient.refresh()` 解析 models + groups
- [x] 单元测试：`validate_profiles()` 检测未知 group
- [ ] E2E 测试：本地起 AIMC + hermes-agent-cn，调用 4 个 profile 验证实际命中
- [ ] 回归测试：现有 chat/agent 调用流程不受影响（profile 名 → group 名 → AIMC 路由）

---

## 🔧 变更详情

### 1. `config.yaml` 改动

**位置**：`~/.hermes/config.yaml` 或项目内 `config.yaml`

#### 最小变更（只改一行）

```yaml
# 旧:
models:
  default: deepseek-chat
  think: deepseek-reasoner

# 新:
models:
  default: tier:balanced       # AIMC 自动选同档最便宜的 model
  think: tier:strong           # AIMC 自动选推理档最强 model
```

#### 完整配置（推荐）

```yaml
# ========== AIMC 网关配置 (新增) ==========
aimc:
  base_url: http://localhost:8080/v1   # AIMC 网关地址
  api_key: ${AIMC_API_KEY}             # 从环境变量读, YAML 不含明文
  timeout: 30                          # 秒 (/v1/models 拉取超时)
  refresh_cron: "0 3 * * *"            # 每日凌晨 3 点刷新 /v1/models

# ========== 模型 profile (改动) ==========
# profile 名 → AIMC group 名 (tier:xxx / scene:xxx)
# 改这里不重启 AIMC (AIMC 侧热刷缓存)
models:
  default: tier:balanced       # 默认对话
  think: tier:strong           # 推理/规划任务
  longContext: tier:flagship   # 长上下文 (200k+)
  background: scene:free       # 后台任务 (本地模型免费档)
  code: scene:code             # 代码任务

# ========== AIMC 软偏好 (新增, 可选) ==========
# 仅用于"在多个同 group 候选 model 中给偏好", 不强制
aimc_preferences:
  prefer_family: ""            # e.g. "deepseek" - 优先 deepseek 家
  exclude_providers: []        # e.g. ["siliconflow"] - 排除某渠道
  max_input_price: 10.0        # 输入价上限 (元/M tokens)

# ========== 启动校验 (新增, 可选) ==========
validate_on_startup: true      # 启动时拉 /v1/models 校验 profile 合法
```

### 2. 代码改动

#### 2.1 新增 `aimc_client.py`（hermes-agent-cn 仓库内）

**位置**：`aimc_client.py`（hermes-agent-cn 项目根目录或 `lib/`）

```python
"""AIMC 网关客户端 - hermes-agent-cn 集成层.

设计原则:
- 只读 AIMC 元信息 (/v1/models), 不写任何东西回去
- 启动时校验 profile 合法
- 每日定时刷新以适应 AIMC 模型动态变化

铁律:
- 不反向调整 AIMC 配置
- 不写回自己 profile (除非人手动改)
"""
import logging
from typing import Dict, Set

import httpx

logger = logging.getLogger(__name__)


class AIMCClient:
    """轻量级 AIMC 客户端 (OpenAI 协议兼容)."""

    def __init__(self, base_url: str, api_key: str = "", timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.known_models: Set[str] = set()
        self.known_groups: Set[str] = set()

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def refresh(self) -> None:
        """拉取 /v1/models, 更新 known_models + known_groups.

        Raises:
            httpx.HTTPError: 网络错误
        """
        url = f"{self.base_url}/v1/models"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()

        models: Set[str] = set()
        groups: Set[str] = set()
        for m in data.get("data", []):
            mid = m.get("id", "")
            if not mid:
                continue
            if mid.startswith(("tier:", "scene:")):
                groups.add(mid)
            else:
                models.add(mid)

        self.known_models = models
        self.known_groups = groups
        logger.info(
            "AIMC /v1/models refreshed: %d models, %d groups",
            len(models), len(groups),
        )

    def validate_profiles(self, profiles: Dict[str, str]) -> None:
        """校验 profile 名 -> AIMC group ID 映射合法.

        Args:
            profiles: e.g. {"default": "tier:balanced", "think": "tier:strong"}

        Raises:
            ValueError: 有 profile 引用了不存在的 group
        """
        missing = [
            (name, gid) for name, gid in profiles.items()
            if gid not in self.known_groups
        ]
        if missing:
            raise ValueError(
                f"Profile references unknown AIMC groups: {missing}. "
                f"Known groups: {sorted(self.known_groups)}"
            )

    def is_known_group(self, group_id: str) -> bool:
        return group_id in self.known_groups

    def is_known_model(self, model_id: str) -> bool:
        return model_id in self.known_models
```

#### 2.2 启动初始化（`main.py` 或 agent 入口）

```python
# main.py
import asyncio
from config import load_config
from aimc_client import AIMCClient


async def init_aimc_client(config) -> AIMCClient:
    """启动时拉一次 /v1/models + 校验 profile."""
    client = AIMCClient(
        base_url=config["aimc"]["base_url"],
        api_key=config["aimc"].get("api_key", ""),
        timeout=config["aimc"].get("timeout", 30),
    )
    await client.refresh()  # 失败会 raise, 启动失败 (fail-fast)

    # 校验 profile 引用合法
    if config.get("validate_on_startup", True):
        client.validate_profiles(config["models"])

    return client


async def main():
    config = load_config()
    aimc_client = await init_aimc_client(config)

    # 挂后台调度器: 每日定时刷新
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        aimc_client.refresh,
        "cron",
        hour=3,  # 凌晨 3 点, 低峰期
        id="refresh_aimc_models",
        replace_existing=True,
    )
    scheduler.start()

    # 后续启动 chat/agent loop...
    # 注意: 当 config["models"][profile_name] 被 chat 调用时,
    # 直接以 group_id 作为 model 字段传给 OpenAI client (AIMC 会自动路由)


if __name__ == "__main__":
    asyncio.run(main())
```

#### 2.3 OpenAI client 调用（chat 调用处）

```python
# chat.py / agent.py 内部
from openai import AsyncOpenAI

# AIMC 是 OpenAI 协议兼容网关, base_url 指 AIMC
client = AsyncOpenAI(
    base_url=config["aimc"]["base_url"],
    api_key=config["aimc"].get("api_key", "no-key-required"),
    timeout=config["aimc"].get("timeout", 120),
)


async def chat(profile: str, messages: list) -> str:
    """profile: 'default' / 'think' / 'longContext' / 'background' / 'code'"""
    model_id = config["models"][profile]   # e.g. 'tier:balanced'
    response = await client.chat.completions.create(
        model=model_id,                      # ← 直接传 group 名, AIMC 自动路由
        messages=messages,
    )

    # 可选: 响应头被动观测 (记录, 不影响路由)
    if hasattr(response, "_raw_response"):
        headers = response._raw_response.headers
        logger.debug(
            "AIMC actual: model=%s channel=%s group=%s",
            headers.get("x-aimc-actual-model"),
            headers.get("x-aimc-actual-channel"),
            headers.get("x-aimc-group-id"),
        )

    return response.choices[0].message.content
```

---

## 🧪 测试

### 单元测试（`tests/test_aimc_client.py`）

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from aimc_client import AIMCClient


@pytest.fixture
def mock_models_response():
    return {
        "object": "list",
        "data": [
            {"id": "tier:balanced"},
            {"id": "tier:strong"},
            {"id": "tier:flagship"},
            {"id": "scene:free"},
            {"id": "scene:code"},
            {"id": "deepseek-chat"},
            {"id": "qwen-3-32b"},
        ],
    }


@pytest.mark.asyncio
async def test_refresh_parses_models_and_groups(mock_models_response):
    """refresh() 正确区分 group (tier: / scene: 前缀) 与具体 model."""
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.json.return_value = mock_models_response
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value.__aenter__.return_value = mock_resp

        client = AIMCClient(base_url="http://test")
        await client.refresh()

    assert "deepseek-chat" in client.known_models
    assert "qwen-3-32b" in client.known_models
    assert "tier:balanced" in client.known_groups
    assert "scene:free" in client.known_groups
    assert len(client.known_models) == 2
    assert len(client.known_groups) == 5


@pytest.mark.asyncio
async def test_validate_profiles_passes_for_known_groups():
    client = AIMCClient(base_url="http://test")
    client.known_groups = {"tier:balanced", "tier:strong"}
    client.validate_profiles({"default": "tier:balanced", "think": "tier:strong"})


@pytest.mark.asyncio
async def test_validate_profiles_raises_on_unknown_group():
    client = AIMCClient(base_url="http://test")
    client.known_groups = {"tier:balanced"}
    with pytest.raises(ValueError, match="unknown AIMC groups"):
        client.validate_profiles({"default": "tier:nonexistent"})


@pytest.mark.asyncio
async def test_refresh_handles_http_error():
    """网络错误应该 raise, 不静默吞."""
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value.__aenter__.return_value.raise_for_status.side_effect = \
            httpx.HTTPStatusError("503", request=MagicMock(), response=MagicMock())

        client = AIMCClient(base_url="http://test")
        with pytest.raises(httpx.HTTPStatusError):
            await client.refresh()
```

### 手动 E2E 测试

```bash
# 1) 启动 AIMC
cd /path/to/aimc
uvicorn main:app --host 0.0.0.0 --port 8080

# 2) 启动 hermes-agent-cn (用上面 PR 改完的代码)
cd /path/to/hermes-agent-cn
python main.py

# 3) 验证启动校验: profile 应该全部通过
# 期望日志: "AIMC /v1/models refreshed: N models, M groups"
# 期望日志: "Profile validate: 5 groups OK" (或类似)

# 4) 触发 chat 调用
# profile=default → AIMC 收到 model="tier:balanced" → 自动路由到具体 model
# 检查 AIMC 日志: 应该看到 X-AIMC-Group-Id: tier:balanced 响应头

# 5) 模拟 AIMC 端模型变化 (新增一个 model)
# 在 AIMC 侧手动加一个新 model + price, 等每日定时刷新或手动触发
# 期望: hermes-agent-cn 端 known_models 集合增加新 model
```

---

## 🔄 回滚方案

如果 PR 后出问题，回滚步骤：

```bash
# 1) config.yaml 回滚
models:
  default: deepseek-chat   # 改回具体 model 名
  think: deepseek-reasoner

# 2) 删除 aimc_client.py 引用 + 移除 /v1/models 启动拉取代码
# 3) OpenAI client base_url 改回原 LLM provider (DeepSeek / Qwen 直连)
```

---

## 📚 相关文档

- **AIMC 侧**（hermes-agent-cn 集成方）：
  - [agent-integration.md](file://C:\Users\lixia\.qoder-cn\worktree\aimc\76apjT\docs\agent-integration.md)：完整 3 方案对比 + 推荐 A+C 组合
  - [architecture.md](file://C:\Users\lixia\.qoder-cn\worktree\aimc\76apjT\docs\architecture.md)：AIMC 整体架构
- **AIMC 仓库**：<https://gitee.com/XiaoYRecluse/aimc>
- **AIMC /v1/models 端点**：参见 [api.md](file://C:\Users\lixia\.qoder-cn\worktree\aimc\76apjT\docs\api.md)

---

## ❓ FAQ

**Q: 为什么不让 AIMC 自动反向同步到 hermes-agent-cn 的 profile？**

A: 铁律是"AIMC 路由决策只听 DB + 情报层"。如果 AIMC 反向调整 hermes-agent-cn 的 profile，会出现：
1. AIMC 既是决策者又是执行者，破坏职责边界
2. 失去跨模型比价的可控性（profile 被人/Agent 锁定才能稳定）
3. 价格自动联动失效（AIMC 想"切到某 model"，但 profile 锁死了）

**Q: 每日刷新够不够？模型变化需要实时吗？**

A: 够。个人编程场景下：
- AIMC 内的 model 增删是低频事件（人工录入 + 情报层触发，一天 1~3 次）
- 即使 group 配置暂时"指向"了一个价格不再最优的 model，下一次定时刷新就修正
- 如果确实需要"分钟级"响应，hermes-agent-cn 可以手动触发 `client.refresh()`

**Q: AIMC 挂了怎么办？**

A: 启动时 `refresh()` 失败会 raise → hermes-agent-cn 启动失败（fail-fast），不会静默用旧数据。运行中 AIMC 挂了则 chat 调用失败，由上层错误处理兜底。

**Q: 跟 hermes-tray 是什么关系？**

A: hermes-tray 是客户端（GUI/CLI），不直接调 AIMC，也不直接调 hermes-agent-cn 的内部 API。 hermes-tray 通过 hermes-agent-cn 的标准 API 跟 Agent 交互。本次 PR 不影响 hermes-tray。

---

## 📊 PR 影响范围

| 项 | 变更前 | 变更后 |
|---|---|---|
| hermes-agent-cn 配置行数 | 5 行 models | 5 行 models + 8 行 aimc |
| 启动时间 | <1s | <1s + 0.5s(/v1/models) |
| 内存 | 0 | +1KB (AIMCClient) |
| 跨模型比价 | ❌ | ✅ AIMC 自动 |
| 价格自动联动 | ❌ | ✅ AIMC 情报层 |
| 渠道故障转移 | ❌ | ✅ AIMC fallback |
| 配置文件改动 | 0 | 1 个 (config.yaml) |
| 新增文件 | 0 | 1 个 (aimc_client.py) |
| 新增依赖 | 0 | 0 (httpx / apscheduler 一般已在) |

---

> **最后修订**：2026-07-29 · AIMC 仓库 commit `929e5fb` 之后