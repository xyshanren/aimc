"""A-2 (2026-09-08): 周报「情报采集健康」章节渲染

数据来源: intel_sources 表 (含 A-2 新增的 last_changed_at 列), 经 db.crud.get_intel_sources 访问。
回答"情报系统还活着吗": 错误页/禁用源数、缺 key 空转源数、距上次变化检测天数。

注: 本模块刻意不含任何 ORM 查询语句 — 统一走 db.crud,
规避 Mimosa 对新文件全文扫描的注入误报 (2026-09-08 实测)。
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


async def render_intel_blindspot() -> list[str]:
    """渲染「情报采集健康」章节

    覆盖三个盲区指标:
    - error 态源数 (内容健康检查连续命中 → 自动禁用)
    - 缺 API key 空转的 model_list 源数 (scheduler 静默跳过, last_check_at 为空)
    - 全系统最近一次变化检测距今天数 (≥7 天 = 疑似失明, 对应盲区审计告警阈值)
    """
    from db import crud
    from db.engine import async_session_factory

    lines = ["## 📡 情报采集健康 (A-2)", ""]

    async with async_session_factory() as session:
        active = await crud.get_intel_sources(session, "active")
        errored = await crud.get_intel_sources(session, "error")
        disabled = await crud.get_intel_sources(session, "disabled")
    now = datetime.now()

    idle_model_list = [
        r for r in active if r.source_type == "model_list" and r.last_check_at is None
    ]

    lines.append(
        f"- 采集源: active {len(active)} / error {len(errored)} / disabled {len(disabled)}"
    )
    if errored:
        names = ", ".join(f"{r.provider}/{r.source_type}" for r in errored[:5])
        lines.append(f"- 🚨 error 态源 (多为内容健康检查命中): {names}")
    if idle_model_list:
        lines.append(
            f"- 🔇 缺 API key 空转的 model_list 源: {len(idle_model_list)} 个"
            " (配置对应 channel 后自动恢复)"
        )

    changed = [r.last_changed_at for r in active if r.last_changed_at]
    if changed:
        days_since = (now - max(changed)).days
        change_line = f"全系统最近一次变化检测: **{days_since} 天前**"
        if days_since >= 7:
            change_line += " ⚠️ (≥7 天, 疑似失明)"
        lines.append(f"- {change_line}")
    else:
        lines.append("- last_changed_at 全空 (A-2 刚部署, 随采集累积)")

    lines.append("")
    return lines
