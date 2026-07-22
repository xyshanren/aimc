"""采集源注册表

管理所有厂商采集源插件。
"""

from intelligence.sources.base import IntelSourceBase
from intelligence.sources.deepseek import DeepSeekSource, DeepSeekAnnouncementSource
from intelligence.sources.qwen import QwenSource, QwenActivitySource
from intelligence.sources.glm import GLMSource, GLMActivitySource


def get_all_sources() -> list[IntelSourceBase]:
    """获取所有已注册的采集源实例"""
    return [
        DeepSeekSource(),
        DeepSeekAnnouncementSource(),
        QwenSource(),
        QwenActivitySource(),
        GLMSource(),
        GLMActivitySource(),
    ]


def get_source_by_provider(provider: str) -> list[IntelSourceBase]:
    """按厂商获取采集源"""
    return [s for s in get_all_sources() if s.provider == provider]
