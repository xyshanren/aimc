"""内容健康检查 (A-2, 2026-09-08)

背景: 2026-09-08 排查发现情报系统"失明"两周 — 多个采集源抓回的是错误页
(Chromium 网络错误页 182B / 404 页 / Next.js SSR 失败页 / 未渲染 SPA),
错误页内容字节级稳定 → differ 每次判定"无变化" → 全系统 14 天零变化检测,
且全程无任何告警 (静默死亡)。

本模块在 fetch 之后、differ 之前按特征库分类内容健康度:
- 不健康内容不写入 last_content (不污染 hash 基线, 源恢复后能正常检测到变化)
- 走既有 handle_fetch_error 通道 (error_count+1, 连续 3 次禁用源+告警)
- 源修复后由 differ._update_source_hash 既有的 error→active 恢复路径自动恢复

设计约束: 特征只收录低误报模式 — "enable javascript" 这类 noscript 常见文案
不收录 (正常渲染成功的 SPA 页面也会带 <noscript> 提示, 会误杀健康内容)。
"""

import logging

logger = logging.getLogger(__name__)

# 最短健康长度: 低于此值视为错误页/空壳。
# 实测: Chromium 网络错误页 182B, 404 页 151B; 正常采集内容最小 ~3.5KB
_MIN_HEALTHY_LEN = 500

# 错误页特征 (lowercase 匹配; 全部来自 2026-09-08 生产实证样本)
# 只查前 4KB — 这些特征都出现在页面头部区域
_ERROR_SIGNATURES = (
    "__next_error__",                          # Next.js SSR 失败 (siliconflow 实证)
    "doesn't work properly without javascript",  # Vue CLI 未渲染 SPA (glm 9/8 实证; 注意是 without, with 匹配不到真实文案)
    "this site can’t be reached",              # Chromium 网络错误页标题
    "this site can't be reached",              # 同上 (直引号变体)
    "this site cannot be reached",
    "<title>404",                              # 404 页 title (sensenova 实证)
    "err_name_not_resolved",                   # Chromium DNS 失败
    "err_connection_refused",
    "err_connection_reset",
    "err_timed_out",
)

# 特征匹配窗口 (字符)
_SIGNATURE_SCAN_LEN = 4096


def classify_content(content: str) -> tuple[bool, str]:
    """判断抓取内容是否健康

    Args:
        content: 抓取到的原始内容 (HTML / 文本)

    Returns:
        (healthy, reason) — reason 仅在 unhealthy 时有值, 供告警/日志使用
    """
    if not content:
        return False, "空内容"

    if len(content) < _MIN_HEALTHY_LEN:
        return False, f"内容过短({len(content)}B, 疑似错误页)"

    head = content[:_SIGNATURE_SCAN_LEN].lower()
    for sig in _ERROR_SIGNATURES:
        if sig in head:
            return False, f"命中错误页特征: {sig!r}"

    return True, ""
