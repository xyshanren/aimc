"""Phase 4 E2 (2026-07-29) — 价格大幅波动告警

监听 `PriceChange` 实例，当 input_price 或 output_price 任一方向变动幅度
超过阈值（默认 30%）时，调用 `alerter.send_alert` 推送。

设计要点：
- 复用 Phase 3 (#3) alerter.send_alert，已有 bark/feishu/serverchan/log 通道
- throttle key = "price_{provider}_{model}"，60s 内同 model 不重复
- 异常吞掉，告警失败不阻断主流程（pricing_sync 已 commit 价格）
- 阈值可被 SettingsRuntime 覆盖 (price_alert_threshold)，未覆盖时用 settings 默认值

典型用例：
- 硅基流动 qwen-plus 涨价 50%（0.8 → 1.2 元/百万 token）
- DeepSeek 限时优惠结束，价格回到原价（+200%）
- 新模型上架原价 vs 已存在模型降价
"""

import logging
from typing import Optional

from intelligence.alerter import send_alert
from intelligence.pricing_sync import PriceChange

logger = logging.getLogger(__name__)

# 默认告警阈值：30% (与 Phase 4 E2 spec 对齐)
DEFAULT_THRESHOLD_PCT = 30.0

# SettingsRuntime 表的覆盖 key
SETTINGS_RUNTIME_KEY = "price_alert_threshold"


def calc_change_pct(old: Optional[float], new: Optional[float]) -> Optional[float]:
    """计算价格百分比变动。

    Returns:
        - 正数 = 涨价 (new > old)
        - 负数 = 降价
        - None = 无法计算（old 为 None / 0 / 任一为 None）

    公式: (new - old) / old * 100
    - old=0 时退化为 None（避免除零 + 表示"从免费涨到 X"语义模糊）
    - 任一为 None 返 None（新增或消失的价格不视为"波动"）
    """
    if old is None or new is None:
        return None
    if old == 0:
        # 0 -> X 视作"新增条目"而非波动；caller 决定是否走其它路径
        return None
    return (new - old) / old * 100.0


def _get_threshold() -> float:
    """读告警阈值：优先 SettingsRuntime 覆盖, fallback config 默认 30%。

    P2-15 fix: 走 alerter.get_runtime_override public accessor, 不再 import 私有 _settings_cache.
    P2-16 fix: runtime override 转 float 失败时 logger.warning + 走 fallback, 不静默吞掉异常.
    """
    from config import settings

    try:
        from intelligence.alerter import get_runtime_override
        runtime_val = get_runtime_override(SETTINGS_RUNTIME_KEY)
        if runtime_val:
            try:
                return float(runtime_val)
            except (TypeError, ValueError) as e:
                # 覆盖值被人手动设成 "abc" / "30%" 之类, 不阻断主流程, 走 fallback
                logger.warning(
                    "价格告警阈值 override 解析失败: key=%s value=%r err=%s, fallback 默认 %.1f%%",
                    SETTINGS_RUNTIME_KEY, runtime_val, e, DEFAULT_THRESHOLD_PCT,
                )
    except Exception as e:
        # alerter 模块本身异常 (e.g. import 失败), 同样走 fallback
        logger.warning(
            "价格告警阈值读取异常: err=%s, fallback 默认 %.1f%%", e, DEFAULT_THRESHOLD_PCT,
        )
    return getattr(settings, "price_alert_threshold_pct", DEFAULT_THRESHOLD_PCT)


async def maybe_alert_price_change(change: PriceChange) -> bool:
    """检测价格变动幅度，超过阈值则发告警。

    Args:
        change: 由 sync_price_change 返回的 PriceChange 实例
                (含 channel_id / model / old/new / source)

    Returns:
        True  = 已发送（或被 throttle，或低于阈值但记录了）
        False = 异常未送达 (实际几乎不会返 False，alerter 已兜底)

    注意：
    - change.changed == False 时直接返回，不告警
    - 任一方向 (input 或 output) 变动 ≥ 阈值即触发一次告警（不是叠加）
    - 告警失败被吞掉，主流程不受影响
    """
    if not change.changed:
        return False

    threshold = _get_threshold()
    in_pct = calc_change_pct(change.old_input_price, change.new_input_price)
    out_pct = calc_change_pct(change.old_output_price, change.new_output_price)

    triggered = []
    if in_pct is not None and abs(in_pct) >= threshold:
        triggered.append(("input", in_pct))
    if out_pct is not None and abs(out_pct) >= threshold:
        triggered.append(("output", out_pct))

    if not triggered:
        logger.debug(
            "价格变动未达告警阈值: model=%s in_pct=%s out_pct=%s threshold=%.1f%%",
            change.model, in_pct, out_pct, threshold,
        )
        return False

    # 构造告警消息
    parts = [f"🔔 价格波动告警 (≥{threshold:.0f}%)"]
    parts.append(f"模型: {change.model}")
    if change.provider:
        parts.append(f"渠道: {change.provider}")
    if "input" in [t[0] for t in triggered]:
        old = change.old_input_price or 0
        new = change.new_input_price or 0
        direction = "↑涨价" if (new > old) else "↓降价"
        parts.append(
            f"输入价: {old:.4f} → {new:.4f} ({in_pct:+.1f}% {direction})"
        )
    if "output" in [t[0] for t in triggered]:
        old = change.old_output_price or 0
        new = change.new_output_price or 0
        direction = "↑涨价" if (new > old) else "↓降价"
        parts.append(
            f"输出价: {old:.4f} → {new:.4f} ({out_pct:+.1f}% {direction})"
        )
    parts.append(f"来源: {change.source}")

    message = "\n".join(parts)
    # P1-8 fix: throttle key 包含 provider, 不同渠道同名 model 互不抑制.
    # e.g. qwen-plus 在 siliconflow 涨价 → 发告警; aliyun 涨价 → 另发告警.
    # 没 provider 补 "__" 避免 key 空拼接 (e.g. price___qwen-plus).
    provider_key = change.provider or "__"
    throttle_key = f"price_{provider_key}_{change.model}"

    try:
        await send_alert(message, key=throttle_key)
        logger.info(
            "价格告警已触发: model=%s threshold=%.1f%% in_pct=%s out_pct=%s",
            change.model, threshold, in_pct, out_pct,
        )
        return True
    except Exception as e:
        # 告警失败不阻断主流程
        logger.error(f"价格告警发送异常: {e}")
        return False