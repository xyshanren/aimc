"""V6.5+ (2026-09-14): 免费渠道扩容回归 (next-stage-research §2.6).

- 方案 A: ModelScope 渠道配置块 (manual_import.CHANNELS 结构/幂等形状)
- 方案 B: FreeLLMCollectionSource 注册 (38 源, price_aggregator, 1 天)
"""

import pytest


# ============================================================
# 方案 A: ModelScope 渠道配置块
# ============================================================

def test_manual_import_modelscope_config():
    from scripts.manual_import import CHANNELS

    ms = [c for c in CHANNELS if c["key"] == "modelscope"]
    assert len(ms) == 1, "modelscope 配置块应恰好 1 个"
    cfg = ms[0]
    assert cfg["provider"] == "modelscope"
    assert cfg["name"] == "modelscope_inference"
    assert cfg["base_url"] == "https://api-inference.modelscope.cn/v1"
    assert cfg["env_var"] == "MODELSCOPE_API_KEY"
    assert cfg["priority"] == 10, "免费兜底应最低档 (priority 大 = 靠后)"
    assert cfg["qpm"] == 30
    # 价格行: raw 名直录 (同 siliconflow 聚合模式), 全 0 元
    # 模型行来自 2026-09-14 用户全量实测 (只录大参数直接可用行)
    models = [p[0] for p in cfg["prices"]]
    assert "deepseek-ai/DeepSeek-V4.1-Flash" in models
    assert "deepseek-ai/DeepSeek-V4-Pro" in models
    assert "ZhipuAI/GLM-5.2" in models
    assert "Qwen/Qwen3-Next-80B-A3B-Instruct" in models
    # 实测不可用/需流式的行不应录入
    assert "deepseek-ai/DeepSeek-V4-Flash" not in models  # ModelScope 无此 id
    assert "MiniMax/MiniMax-M3" not in models  # has no provider
    assert "ZhipuAI/GLM-4.7-Flash" not in models  # 1305 过载
    for row in cfg["prices"]:
        assert row[1] == "normal" and row[2] == "全天"
        assert row[3] == 0.0 and row[4] == 0.0


def test_manual_import_no_duplicate_channel_names():
    from scripts.manual_import import CHANNELS

    names = [c["name"] for c in CHANNELS]
    assert len(names) == len(set(names)), "channel name 应唯一 (幂等前提)"


# ============================================================
# 方案 B: FreeLLMCollectionSource
# ============================================================

def test_free_llm_collection_source_registered():
    from intelligence.sources import get_all_sources

    srcs = [s for s in get_all_sources() if s.provider == "free_llm_collection"]
    assert len(srcs) == 1
    s = srcs[0]
    assert s.source_type == "price_aggregator"
    assert s.url.startswith("https://raw.githubusercontent.com/for-the-zero/")
    assert s.url.endswith("Free-LLM-Collection/main/README.md")
    assert s.poll_interval == 86400
    assert s.wait_until == "domcontentloaded"


def test_free_llm_collection_source_count_grew():
    """注册表应恰好 +1 (38 = 37 既有 + 1 新)。"""
    from intelligence.sources import get_all_sources

    assert len(get_all_sources()) == 38


# ============================================================
# get_source_by_provider 通道 (scheduler 按 provider 分组依赖)
# ============================================================

def test_get_source_by_provider_works():
    from intelligence.sources import get_source_by_provider

    got = get_source_by_provider("free_llm_collection")
    assert len(got) == 1
    assert got[0].provider == "free_llm_collection"
    # 不存在的 provider 返回空列表
    assert get_source_by_provider("nonexistent-provider-x") == []
