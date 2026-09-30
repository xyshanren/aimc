"""2026-10-01: embeddings/rerank image_tokens 计量修复测试

背景: Qwen3-VL-Embedding/Reranker-8B 的 image_tokens 与 input_tokens
分列返回, _extract_tokens 旧逻辑只取 input_tokens → 图片部分漏计
(成本审计低估; RAG 侧游戏帧检索 432 image_tokens/帧, 占大头)。
修复: image_tokens 合入 input_tokens (同价 0.7 元/M, pricing 页单一定价)。

跑法:
    python tests/test_embeddings_image_tokens.py
    pytest tests/test_embeddings_image_tokens.py -v
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_openai_embeddings_image_tokens_merged():
    """OpenAI 形态: usage.prompt_tokens + usage.image_tokens 合入"""
    from gateway.embeddings import _extract_tokens
    data = {"usage": {"prompt_tokens": 18, "completion_tokens": 0,
                      "total_tokens": 22, "image_tokens": 4}}
    assert _extract_tokens(data) == (22, 0)


def test_openai_embeddings_text_only_no_regression():
    """纯文本 embedding 无 image_tokens, 不回归"""
    from gateway.embeddings import _extract_tokens
    data = {"usage": {"prompt_tokens": 10, "completion_tokens": 0,
                      "total_tokens": 10}}
    assert _extract_tokens(data) == (10, 0)


def test_rerank_meta_tokens_image_merged():
    """rerank 形态: meta.tokens.input_tokens + image_tokens 合入"""
    from gateway.embeddings import _extract_tokens
    data = {"meta": {"tokens": {"input_tokens": 141, "output_tokens": 0,
                                "image_tokens": 8},
                     "billed_units": {"input_tokens": 141,
                                      "image_tokens": 8}}}
    assert _extract_tokens(data) == (149, 0)


def test_rerank_billed_units_fallback():
    """meta.tokens 无 image_tokens 时落 billed_units 兜底"""
    from gateway.embeddings import _extract_tokens
    data = {"meta": {"tokens": {"input_tokens": 50},
                     "billed_units": {"input_tokens": 50, "image_tokens": 12}}}
    assert _extract_tokens(data) == (62, 0)


def test_rerank_text_only_no_regression():
    """纯文本 rerank 不回归"""
    from gateway.embeddings import _extract_tokens
    data = {"meta": {"tokens": {"input_tokens": 100, "output_tokens": 0}}}
    assert _extract_tokens(data) == (100, 0)


def test_empty_usage():
    """无 usage 返回 0, 不抛异常"""
    from gateway.embeddings import _extract_tokens
    assert _extract_tokens({}) == (0, 0)


def main():
    print("=" * 60)
    print("image_tokens 计量修复测试")
    print("=" * 60)

    tests = [
        ("openai embeddings image 合入", test_openai_embeddings_image_tokens_merged),
        ("openai 纯文本不回归", test_openai_embeddings_text_only_no_regression),
        ("rerank meta.tokens image 合入", test_rerank_meta_tokens_image_merged),
        ("rerank billed_units 兜底", test_rerank_billed_units_fallback),
        ("rerank 纯文本不回归", test_rerank_text_only_no_regression),
        ("空 usage 不抛异常", test_empty_usage),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  [OK] {name}")
            passed += 1
        except AssertionError as e:
            print(f"  [FAIL] {name}: {e}")
            failed += 1

    print("=" * 60)
    print(f"Result: {passed} passed, {failed} failed, {passed + failed} total")
    return failed


if __name__ == "__main__":
    sys.exit(main())
