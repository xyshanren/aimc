"""推送 AIMC 周报关键结论（V4.4.B）

读 reports/weekly-latest.md，提取 Top 5 + 总览 + 总成本，拼成 ≤200 字消息，
根据 ALERT_CHANNEL 走 bark / serverchan / feishu。

不通过 alerter.send_alert 是为了：
- 避免 60s throttle（周报是计划任务，不需要）
- Bark title 用 "AIMC 周报" 而不是 "AIMC 告警"
- 复用 runtime override 机制（API-Sec-2）

用法:
    python scripts/publish_report.py                  # 默认 weekly-latest.md + ALERT_CHANNEL
    python scripts/publish_report.py --channel log    # 只 log 不真发（dry-run）
    python scripts/publish_report.py --report reports/weekly_2026-07-27.md
"""
import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings
from intelligence.alerter import (
    _get_active_channel,
    _get_setting,
    _short,
)

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
NOTIFICATION_TITLE = "AIMC 周报"


def extract_summary(report_path: Path) -> Optional[str]:
    """从周报 Markdown 提取关键结论 ≤ 200 字。

    Returns:
        拼接好的多行文本；None 表示报告不存在或解析失败。
    """
    if not report_path.exists():
        logger.warning("周报不存在: %s", report_path)
        return None

    text = report_path.read_text(encoding="utf-8")

    # 标题行: "# AIMC 周报 (2026-07-27 ~ 2026-08-03)"
    title_match = re.search(r"^# AIMC 周报 \(([^)]+)\)", text, re.MULTILINE)
    title = title_match.group(1) if title_match else ""

    # 总览行
    overview_match = re.search(
        r"^- \*\*总请求\*\*:\s*([\d,]+)\s*（上周\s*([\d,]+)）",
        text,
        re.MULTILINE,
    )
    request_line = ""
    if overview_match:
        cur, prev = overview_match.group(1), overview_match.group(2)
        request_line = f"总请求 {cur}（上周 {prev}）"

    cost_match = re.search(
        r"^- \*\*总成本\*\*:\s*¥([\d.]+)\s*（上周\s*¥([\d.]+)）",
        text,
        re.MULTILINE,
    )
    cost_line = ""
    if cost_match:
        cur, prev = cost_match.group(1), cost_match.group(2)
        cost_line = f"总成本 ¥{cur}（上周 ¥{prev}）"

    # Top 5 表格
    top_models: list[str] = []
    in_top = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("## 🏆"):
            in_top = True
            continue
        if in_top and line.startswith("##"):
            break
        if in_top and line.startswith("|"):
            # 跳过分隔行和表头
            if "排名" in line or "---" in line:
                continue
            # | 1 | `qwen-max` | 92.3 | 100 | 5.20 | 2.0% |
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if len(cells) >= 3:
                rank, model, score = cells[0], cells[1], cells[2]
                top_models.append(f"#{rank} {model} {score}")

    parts: list[str] = []
    if title:
        parts.append(f"📊 AIMC 周报 ({title})")
    if request_line:
        parts.append(f"📈 {request_line}")
    if cost_line:
        parts.append(f"💸 {cost_line}")
    if top_models:
        parts.append("🥇 " + " / ".join(top_models[:3]))

    if not parts:
        return None

    summary = "\n".join(parts)
    return _short(summary, 200)


async def publish_bark(message: str) -> bool:
    """Bark 推送（iOS），title 用 AIMC 周报。"""
    bark_key = _get_setting("bark_key", settings.bark_key)
    if not bark_key:
        logger.warning("[PUBLISH-bark-no-config] %s", _short(message, 50))
        return False
    server = (
        _get_setting("bark_server", settings.bark_server).rstrip("/")
        or "https://api.day.app"
    )
    url = f"{server}/{bark_key}/{NOTIFICATION_TITLE}/{_short(message, 100)}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                logger.info("Bark 周报推送成功")
                return True
            logger.warning(
                "Bark 周报失败: status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception as e:
        logger.error("Bark 周报异常: %s", e)
    return False


async def publish_serverchan(message: str) -> bool:
    """Server 酱推送（微信），title 用 AIMC 周报。"""
    serverchan_key = _get_setting("serverchan_key", settings.serverchan_key)
    if not serverchan_key:
        logger.warning("[PUBLISH-serverchan-no-config] %s", _short(message, 50))
        return False
    url = f"https://sctapi.ftqq.com/{serverchan_key}.send"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                data={"title": NOTIFICATION_TITLE, "desp": _short(message, 500)},
            )
            if resp.status_code == 200:
                logger.info("Server 酱周报推送成功")
                return True
            logger.warning(
                "Server 酱周报失败: status=%s body=%s",
                resp.status_code,
                resp.text[:200],
            )
    except Exception as e:
        logger.error("Server 酱周报异常: %s", e)
    return False


async def publish_feishu(message: str) -> bool:
    """飞书 webhook 推送，title 用 AIMC 周报。

    对 chat rate limit (code=11233) 自动重试 3 次（60s/120s 退避）。
    """
    feishu_url = _get_setting("feishu_webhook_url", settings.feishu_webhook_url)
    if not feishu_url:
        logger.warning("[PUBLISH-feishu-no-config] %s", _short(message, 50))
        return False
    import asyncio as _aio
    backoffs = [0, 60, 120]  # 首次立即；飞书 chat rate limit 重试 60s/120s
    for attempt, delay in enumerate(backoffs, 1):
        if delay:
            logger.info("[PUBLISH-feishu-retry] attempt %d/%d, waiting %ds", attempt, len(backoffs), delay)
            await _aio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    feishu_url,
                    json={
                        "msg_type": "text",
                        "content": {"text": f"[{NOTIFICATION_TITLE}] {message}"},
                    },
                )
                data = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else {}
                )
                if resp.status_code == 200 and data.get("StatusCode") in (0, "0"):
                    logger.info("飞书周报推送成功 (attempt %d/%d)", attempt, len(backoffs))
                    return True
                feishu_code = data.get("code")
                if feishu_code == 11233 and attempt < len(backoffs):
                    logger.warning("飞书限流 (code=11233), attempt %d/%d, 将重试", attempt, len(backoffs))
                    continue
                logger.warning(
                    "飞书周报失败: status=%s body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
                break
        except Exception as e:
            logger.error("飞书周报异常 (attempt %d/%d): %s", attempt, len(backoffs), e)
            if attempt < len(backoffs):
                continue
            break
    return False


CHANNEL_HANDLERS = {
    "bark": publish_bark,
    "serverchan": publish_serverchan,
    "feishu": publish_feishu,
}


async def publish(
    report_path: Optional[Path] = None,
    channel: Optional[str] = None,
) -> bool:
    """推送周报关键结论。

    Args:
        report_path: 周报 Markdown 路径，默认 reports/weekly-latest.md
        channel: 覆盖 ALERT_CHANNEL（仅本次）

    Returns:
        True = 成功 / log 通道；False = 失败或无报告
    """
    report_path = report_path or (REPORTS_DIR / "weekly-latest.md")
    summary = extract_summary(report_path)
    if not summary:
        logger.warning("周报不存在或无可推送数据: %s", report_path)
        return False

    channel = (channel or _get_active_channel()).lower().strip() or "log"

    if channel == "log":
        logger.info("[PUBLISH-log]\n%s", summary)
        return True

    handler = CHANNEL_HANDLERS.get(channel)
    if not handler:
        logger.warning(
            "[PUBLISH-fallback] unknown channel=%r "
            "(available: log,bark,serverchan,feishu)",
            channel,
        )
        return False

    result = await handler(summary)
    if not result:
        # 推送失败但周报已生成落盘 — 不拖垮退出码（文件在 reports/ 下可查）
        logger.warning(
            "[PUBLISH-best-effort] %s 推送失败，但周报已生成落盘，忽略退出码",
            channel,
        )
        return True
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="推送 AIMC 周报 (V4.4.B)")
    parser.add_argument(
        "--report",
        default=None,
        help="周报 Markdown 路径，默认 reports/weekly-latest.md",
    )
    parser.add_argument(
        "--channel",
        default=None,
        choices=["bark", "serverchan", "feishu", "log"],
        help="推送通道（覆盖 ALERT_CHANNEL 当前次）",
    )
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    args = build_parser().parse_args()
    report_path = Path(args.report) if args.report else None
    success = asyncio.run(publish(report_path=report_path, channel=args.channel))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())