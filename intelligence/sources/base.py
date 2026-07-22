"""采集源基类

定义插件式采集源的接口协议。
每个厂商实现一个采集源，注册到调度器。
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class IntelSourceBase:
    """采集源基类

    每个厂商的采集源继承此类，定义采集目标 URL 和配置。
    """

    # 厂商标识
    provider: str = ""
    # 采集源 ID（数据库中的 intel_sources.id）
    source_id: int = 0
    # 采集目标 URL
    url: str = ""
    # 来源类型: web_page / api_doc / announcement
    source_type: str = "web_page"
    # 轮询间隔（秒）
    poll_interval: int = 900
    # 上次内容哈希
    last_hash: Optional[str] = None
    # 上次完整内容（截断 50KB，用于相似度计算，避免 99% 一样的页面触发 AI 解析）
    last_content: Optional[str] = None
    # 连续错误计数
    error_count: int = 0
    # CSS 选择器（可选，用于精确定位内容区域）
    css_selector: Optional[str] = None
    # 是否启用
    enabled: bool = True

    def get_fetch_url(self) -> str:
        """获取实际抓取的 URL（子类可覆盖）"""
        return self.url

    def get_css_selector(self) -> Optional[str]:
        """获取 CSS 选择器（子类可覆盖）"""
        return self.css_selector
