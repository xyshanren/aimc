"""Phase 4 v4.3 (2026-08-13): model_registry 字段归一化工具

背景:
- seed_model_registry.py 历史上有 MiniMax family 用大写问题 (commit 1005111 引入)
- 修复 commit 4a2cfc5 改成小写, 但未来 commit 还可能再发生大小写漂移
- 应用层归一化: 在写入前 normalize, 检测脚本兜底

约定:
- family: 全小写 (与 provider / channel.name 字段对齐)
- model_id: 全小写 + 去 BOM + 以 family 命名空间开头
- capabilities: 全小写 + 排序去重 (跟 db/models.py:266 命名规范对齐)

不在范围 (用户没选):
- DB CHECK 约束 (lowercase) - 兼容性考虑暂不加
- family 字段改 Enum - 重构风险大
"""

from __future__ import annotations

import re
from typing import Iterable


# model_id 合法字符: 小写字母/数字/点/下划线/连字符, 首字符必须小写字母或数字
# 例: deepseek-v4-pro / qwen3-235b / moonshot-v1-8k
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def normalize_family(s: str) -> str:
    """family 字段归一化: 全小写 + 去空白 + 去 BOM

    Args:
        s: 原始 family 字符串 (可能含大写/空白/BOM)

    Returns:
        归一化后的 family (全小写)

    Examples:
        >>> normalize_family("DeepSeek")
        'deepseek'
        >>> normalize_family("  MiniMax  ")
        'minimax'
        >>> normalize_family("MiniMax\\ufeff")
        'minimax'
    """
    if s is None:
        return ""
    return s.strip().lower().replace("\ufeff", "")


def normalize_model_id(s: str) -> str:
    """model_id 归一化: 全小写 + 去空白 + 去 BOM

    注意: 不验证命名空间 (用什么 family 开头), 用 validate_model_id 校验
    """
    if s is None:
        return ""
    return s.strip().lower().replace("\ufeff", "")


def validate_model_id(model_id: str, family: str) -> bool:
    """model_id 必须以 family 命名空间开头, 且符合字符规范

    V6.x (2026-09-04) 放宽: 除 "{family}-" 外, 也接受 "{family}<版本数字/点>..."
    — qwen 家族的规范名 (qwen3-vl-32b / qwen3.5-4b) 本身以 "qwen" 开头但带版本号,
    旧规则强制 "{family}-" 逼出了 qwen-qwen3-8b 这种双前缀 workaround。

    Args:
        model_id: 归一化后的 model_id (已 normalize_model_id)
        family: 归一化后的 family (已 normalize_family)

    Returns:
        True 表示合法, False 表示不通过

    Examples:
        >>> validate_model_id("deepseek-v4-pro", "deepseek")
        True
        >>> validate_model_id("qwen-max", "qwen")
        True
        >>> validate_model_id("qwen3-vl-32b-instruct", "qwen")   # V6.x 放宽
        True
        >>> validate_model_id("qwen3.5-4b", "qwen")              # V6.x 放宽
        True
        >>> validate_model_id("MiniMax-M3", "minimax")  # 未归一化
        False
        >>> validate_model_id("unknown-model", "deepseek")  # namespace 不匹配
        False
        >>> validate_model_id("qwenx-weird", "qwen")  # family 后跟字母 = 别的词, 拒
        False
    """
    if not model_id or not family:
        return False
    # 完整 "{family}-" 前缀 (原规则)
    if model_id.startswith(f"{family}-"):
        return bool(_MODEL_ID_RE.match(model_id))
    # V6.x 放宽: "{family}" 后紧跟版本数字/点 — family 命名空间仍然编码在名字里
    if model_id.startswith(family):
        rest = model_id[len(family):]
        if rest and (rest[0].isdigit() or rest[0] == "."):
            return bool(_MODEL_ID_RE.match(model_id))
    return False


def normalize_capabilities(caps: Iterable[str]) -> list[str]:
    """capabilities 归一化: 排序去重 + 小写 + 去空白

    允许的取值 (跟 db/models.py 现有惯例对齐):
    - general / reasoning / code / math / vision / tool_use / long_context / agent

    不在此白名单的会被保留 (而非丢弃), 让上层决定. 这里只做格式归一化.
    """
    if not caps:
        return []
    seen: set[str] = set()
    for c in caps:
        if c is None:
            continue
        normalized = c.strip().lower().replace("\ufeff", "")
        if normalized:
            seen.add(normalized)
    return sorted(seen)


__all__ = [
    "normalize_family",
    "normalize_model_id",
    "validate_model_id",
    "normalize_capabilities",
]