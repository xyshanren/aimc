"""P2-16: 测试 sync_siliconflow_prices.find_sf_prices (阶段B)

覆盖:
- 精确大小写匹配 (Pro/MiniMaxAI/MiniMax-M2.5 -> (2, 1.2))
- 大小写不敏感 (Pro/MINIMAXAI/MiniMax-M2.5 也匹配)
- raw_id 含 '/' 前缀 (Qwen/Qwen3-32B)
- 未匹配 model -> (None, None)
- None / 空字符串输入 -> (None, None)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from scripts.sync_siliconflow_prices import find_sf_prices, SF_PRICES


def test_exact_match():
    ip, op = find_sf_prices("Qwen/Qwen3-32B")
    assert ip == 1.0
    assert op == 4.0


def test_case_insensitive():
    """大小写不敏感"""
    ip, op = find_sf_prices("QWEN/QWEN3-32B")
    assert ip == 1.0
    assert op == 4.0
    ip, op = find_sf_prices("qwen/qwen3-32b")
    assert ip == 1.0
    assert op == 4.0


def test_pro_prefix():
    """Pro/ 前缀 (硅基流动的 Pro 版渠道)"""
    ip, op = find_sf_prices("Pro/deepseek-ai/DeepSeek-R1")
    assert ip == 2.0
    assert op == 8.0


def test_no_match_returns_none():
    ip, op = find_sf_prices("nonexistent/model")
    assert ip is None
    assert op is None


def test_empty_string():
    ip, op = find_sf_prices("")
    assert ip is None
    assert op is None


def test_none():
    ip, op = find_sf_prices(None)
    assert ip is None
    assert op is None


def test_glm():
    ip, op = find_sf_prices("zai-org/GLM-4.5-Air")
    assert ip == 1.0
    assert op == 6.0


def test_bge():
    """BAAI/bge-m3 在 SF_PRICES 里 (免费嵌入, input=0, output=0)"""
    ip, op = find_sf_prices("BAAI/bge-m3")
    assert ip == 0
    assert op == 0


def test_minimax():
    """Pro/MiniMaxAI/MiniMax-M2.5 -> (2.1, 8.4) — 2026-09 校准: 输出价 1.2 实为 8.4"""
    ip, op = find_sf_prices("Pro/MiniMaxAI/MiniMax-M2.5")
    assert ip == 2.1
    assert op == 8.4


def test_free_model():
    """Qwen/Qwen2.5-7B-Instruct: input=0 (免费) -> 解析为 (0, 0)"""
    ip, op = find_sf_prices("Qwen/Qwen2.5-7B-Instruct")
    assert ip == 0
    assert op == 0


def test_dict_size():
    """SF_PRICES 至少 50 个 (覆盖 91 个 SF model 中 53 个有价格)"""
    assert len(SF_PRICES) >= 50, f"只有 {len(SF_PRICES)} 条, 预期 >= 50"
