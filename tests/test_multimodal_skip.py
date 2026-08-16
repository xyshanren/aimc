"""P2-16: 测试 _has_multimodal_content (P0-3 纯文本跳 multimodal)

覆盖场景:
- None / [] (空) -> False
- 纯字符串 content -> False
- 纯 list of text parts -> False
- 含 image_url / image / input_audio / audio / file -> True
- 混合 (text + image) -> True
- Anthropic content format [{type:image, source:...}] -> True (type="image")
- 非 dict msg 跳过 (异常输入)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

from gateway.router import _has_multimodal_content


def test_empty():
    assert _has_multimodal_content(None) is False
    assert _has_multimodal_content([]) is False


def test_string_content():
    msgs = [{"role": "user", "content": "hello"}]
    assert _has_multimodal_content(msgs) is False


def test_text_only_parts():
    msgs = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": [
            {"type": "text", "text": "hi"},
        ]},
    ]
    assert _has_multimodal_content(msgs) is False


def test_image_url_openai():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
    ]}]
    assert _has_multimodal_content(msgs) is True


def test_image_anthropic():
    """Anthropic content format: type=image + source dict"""
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "xxx"}},
    ]}]
    assert _has_multimodal_content(msgs) is True


def test_input_audio():
    msgs = [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": "xxx", "format": "wav"}},
    ]}]
    assert _has_multimodal_content(msgs) is True


def test_audio_anthropic():
    msgs = [{"role": "user", "content": [
        {"type": "audio", "source": {"type": "base64", "media_type": "audio/wav", "data": "xxx"}},
    ]}]
    assert _has_multimodal_content(msgs) is True


def test_file_type():
    msgs = [{"role": "user", "content": [
        {"type": "file", "file": {"file_id": "file_123"}},
    ]}]
    assert _has_multimodal_content(msgs) is True


def test_non_dict_msg_skipped():
    """非 dict msg (异常输入) 跳过, 不崩"""
    msgs = [
        "not a dict",  # 异常输入
        {"role": "user", "content": "ok"},
    ]
    assert _has_multimodal_content(msgs) is False


def test_part_not_dict():
    """part 不是 dict 也跳过"""
    msgs = [{"role": "user", "content": [
        "raw string in list",  # 异常 part
        {"type": "text", "text": "hi"},
    ]}]
    assert _has_multimodal_content(msgs) is False


def test_multimodal_in_later_msg():
    """多轮对话: 第 2 轮才有 multimodal"""
    msgs = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]},
    ]
    assert _has_multimodal_content(msgs) is True
