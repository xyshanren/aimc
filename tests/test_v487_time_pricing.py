"""V4.8.7 P6: 分时段定价测试

覆盖:
- _match_time_range: 工作日/周末/非高峰 全天/小时区间 各组合
- _match_hour_range: 纯 HH:MM-HH:MM 区间
- _get_current_price: peak/offpeak 行在正确时段被选中
- 向后兼容: "全天" / "00:00-08:00" 仍正常工作
- DeepSeek 种子数据结构验证

跑法:
    python tests/test_v487_time_pricing.py
    或 pytest tests/test_v487_time_pricing.py -v
"""
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gateway.router import RouteEngine


# ============================================================
# helpers
# ============================================================

def _make_engine():
    """构造一个空缓存的 RouteEngine (只测 _match_time_range 逻辑)"""
    eng = RouteEngine()
    eng._channels_cache = []
    return eng


def _dt(year, month, day, hour):
    """快捷构造 datetime (用于固定 weekday + hour)"""
    return datetime(year, month, day, hour)


# 已知 weekday:
# 2026-08-24 = 周一 (weekday=0)
# 2026-08-25 = 周二 (weekday=1)
# 2026-08-29 = 周六 (weekday=5)
# 2026-08-30 = 周日 (weekday=6)

WEEKDAY = lambda h: _dt(2026, 8, 24, h)   # 周一
SATURDAY = lambda h: _dt(2026, 8, 29, h)   # 周六
SUNDAY = lambda h: _dt(2026, 8, 30, h)     # 周日


_passed = 0
_failed = 0


def check(desc, got, expected):
    global _passed, _failed
    if got == expected:
        _passed += 1
        print(f"  PASS  {desc}")
    else:
        _failed += 1
        print(f"  FAIL  {desc}  got={got!r}  expected={expected!r}")


# ============================================================
# _match_time_range — 基础格式
# ============================================================

def test_empty_and_all_day():
    print("\n--- test_empty_and_all_day ---")
    eng = _make_engine()
    check("空字符串 → True", eng._match_time_range("", WEEKDAY(10)), True)
    check('"全天" → True', eng._match_time_range("全天", WEEKDAY(10)), True)
    check('"全天" 周末 → True', eng._match_time_range("全天", SATURDAY(10)), True)


def test_plain_hour_range_backward_compat():
    print("\n--- test_plain_hour_range_backward_compat ---")
    eng = _make_engine()
    check('"00:00-08:00" at 3:00 → True', eng._match_time_range("00:00-08:00", WEEKDAY(3)), True)
    check('"00:00-08:00" at 8:00 → False (边界)', eng._match_time_range("00:00-08:00", WEEKDAY(8)), False)
    check('"00:00-08:00" at 10:00 → False', eng._match_time_range("00:00-08:00", WEEKDAY(10)), False)
    check('"00:00-08:00" 周末 3:00 → True (每天)', eng._match_time_range("00:00-08:00", SATURDAY(3)), True)


# ============================================================
# _match_time_range — 工作日 token
# ============================================================

def test_weekday_peak_morning():
    print("\n--- test_weekday_peak_morning ---")
    eng = _make_engine()
    tr = "工作日09:00-12:00"
    check("周一 10:00 → True", eng._match_time_range(tr, WEEKDAY(10)), True)
    check("周一 9:00 → True (边界)", eng._match_time_range(tr, WEEKDAY(9)), True)
    check("周一 12:00 → False (边界)", eng._match_time_range(tr, WEEKDAY(12)), False)
    check("周一 8:00 → False", eng._match_time_range(tr, WEEKDAY(8)), False)
    check("周六 10:00 → False", eng._match_time_range(tr, SATURDAY(10)), False)
    check("周日 10:00 → False", eng._match_time_range(tr, SUNDAY(10)), False)


def test_weekday_peak_afternoon():
    print("\n--- test_weekday_peak_afternoon ---")
    eng = _make_engine()
    tr = "工作日14:00-18:00"
    check("周一 15:00 → True", eng._match_time_range(tr, WEEKDAY(15)), True)
    check("周一 14:00 → True (边界)", eng._match_time_range(tr, WEEKDAY(14)), True)
    check("周一 18:00 → False (边界)", eng._match_time_range(tr, WEEKDAY(18)), False)
    check("周一 13:00 → False", eng._match_time_range(tr, WEEKDAY(13)), False)
    check("周六 15:00 → False", eng._match_time_range(tr, SATURDAY(15)), False)


def test_weekday_all_day():
    print("\n--- test_weekday_all_day ---")
    eng = _make_engine()
    tr = "工作日全天"
    check("周一 3:00 → True", eng._match_time_range(tr, WEEKDAY(3)), True)
    check("周一 23:00 → True", eng._match_time_range(tr, WEEKDAY(23)), True)
    check("周六 10:00 → False", eng._match_time_range(tr, SATURDAY(10)), False)


# ============================================================
# _match_time_range — 周末 token
# ============================================================

def test_weekend_all_day():
    print("\n--- test_weekend_all_day ---")
    eng = _make_engine()
    tr = "周末全天"
    check("周六 10:00 → True", eng._match_time_range(tr, SATURDAY(10)), True)
    check("周日 3:00 → True", eng._match_time_range(tr, SUNDAY(3)), True)
    check("周一 10:00 → False", eng._match_time_range(tr, WEEKDAY(10)), False)


def test_weekend_hour_range():
    print("\n--- test_weekend_hour_range ---")
    eng = _make_engine()
    tr = "周末00:00-08:00"
    check("周六 3:00 → True", eng._match_time_range(tr, SATURDAY(3)), True)
    check("周六 10:00 → False", eng._match_time_range(tr, SATURDAY(10)), False)
    check("周一 3:00 → False", eng._match_time_range(tr, WEEKDAY(3)), False)


# ============================================================
# _match_time_range — 非高峰全天
# ============================================================

def test_offpeak_all_day():
    print("\n--- test_offpeak_all_day ---")
    eng = _make_engine()
    tr = "非高峰全天"
    # 工作日非高峰
    check("周一 8:00 → True (非高峰)", eng._match_time_range(tr, WEEKDAY(8)), True)
    check("周一 13:00 → True (午休)", eng._match_time_range(tr, WEEKDAY(13)), True)
    check("周一 20:00 → True (晚上)", eng._match_time_range(tr, WEEKDAY(20)), True)
    check("周一 3:00 → True (凌晨)", eng._match_time_range(tr, WEEKDAY(3)), True)
    # 工作日高峰
    check("周一 10:00 → False (高峰上午)", eng._match_time_range(tr, WEEKDAY(10)), False)
    check("周一 9:00 → False (高峰开始)", eng._match_time_range(tr, WEEKDAY(9)), False)
    check("周一 11:00 → False (高峰)", eng._match_time_range(tr, WEEKDAY(11)), False)
    check("周一 15:00 → False (高峰下午)", eng._match_time_range(tr, WEEKDAY(15)), False)
    check("周一 17:00 → False (高峰)", eng._match_time_range(tr, WEEKDAY(17)), False)
    # 周末全天都是非高峰
    check("周六 10:00 → True", eng._match_time_range(tr, SATURDAY(10)), True)
    check("周六 15:00 → True", eng._match_time_range(tr, SATURDAY(15)), True)
    check("周日 9:00 → True", eng._match_time_range(tr, SUNDAY(9)), True)


# ============================================================
# _match_hour_range — 边界
# ============================================================

def test_hour_range_boundaries():
    print("\n--- test_hour_range_boundaries ---")
    eng = _make_engine()
    check("09:00-12:00 at 9 → True", eng._match_hour_range("09:00-12:00", 9), True)
    check("09:00-12:00 at 11 → True", eng._match_hour_range("09:00-12:00", 11), True)
    check("09:00-12:00 at 12 → False", eng._match_hour_range("09:00-12:00", 12), False)
    check("09:00-12:00 at 8 → False", eng._match_hour_range("09:00-12:00", 8), False)
    check("14:00-18:00 at 14 → True", eng._match_hour_range("14:00-18:00", 14), True)
    check("14:00-18:00 at 18 → False", eng._match_hour_range("14:00-18:00", 18), False)


def test_hour_range_invalid():
    print("\n--- test_hour_range_invalid ---")
    eng = _make_engine()
    check("invalid → True (安全兜底)", eng._match_hour_range("abc", 10), True)
    check("no dash → True (安全兜底)", eng._match_hour_range("全天", 10), True)


# ============================================================
# _get_current_price — peak/offpeak 行选择
# ============================================================

def _make_deepseek_channel():
    """模拟 DeepSeek 渠道 (V4.8.7 峰谷 6 行价格)"""
    return {
        "id": 1,
        "name": "deepseek_official",
        "prices": [
            {"model": "deepseek-v4-pro", "price_type": "peak", "time_range": "工作日09:00-12:00", "input_price": 8.0, "output_price": 24.0, "is_promotion": 0},
            {"model": "deepseek-v4-pro", "price_type": "peak", "time_range": "工作日14:00-18:00", "input_price": 8.0, "output_price": 24.0, "is_promotion": 0},
            {"model": "deepseek-v4-pro", "price_type": "offpeak", "time_range": "非高峰全天", "input_price": 4.0, "output_price": 16.0, "is_promotion": 0},
            {"model": "deepseek-v4-flash", "price_type": "peak", "time_range": "工作日09:00-12:00", "input_price": 2.0, "output_price": 4.0, "is_promotion": 0},
            {"model": "deepseek-v4-flash", "price_type": "peak", "time_range": "工作日14:00-18:00", "input_price": 2.0, "output_price": 4.0, "is_promotion": 0},
            {"model": "deepseek-v4-flash", "price_type": "offpeak", "time_range": "非高峰全天", "input_price": 1.0, "output_price": 2.0, "is_promotion": 0},
        ],
    }


def test_get_current_price_peak_morning():
    print("\n--- test_get_current_price_peak_morning ---")
    eng = _make_engine()
    ch = _make_deepseek_channel()
    # 周一 10:00 → 上午高峰
    p = eng._get_current_price(ch, "deepseek-v4-pro", WEEKDAY(10))
    check("周一 10:00 → peak 行", p is not None and p["price_type"] == "peak", True)
    check("周一 10:00 → input=8.0", p["input_price"] if p else None, 8.0)


def test_get_current_price_offpeak_lunch():
    print("\n--- test_get_current_price_offpeak_lunch ---")
    eng = _make_engine()
    ch = _make_deepseek_channel()
    # 周一 13:00 → 午休 (非高峰)
    p = eng._get_current_price(ch, "deepseek-v4-pro", WEEKDAY(13))
    check("周一 13:00 → offpeak 行", p is not None and p["price_type"] == "offpeak", True)
    check("周一 13:00 → input=4.0", p["input_price"] if p else None, 4.0)


def test_get_current_price_offpeak_evening():
    print("\n--- test_get_current_price_offpeak_evening ---")
    eng = _make_engine()
    ch = _make_deepseek_channel()
    # 周一 20:00 → 晚上 (非高峰)
    p = eng._get_current_price(ch, "deepseek-v4-pro", WEEKDAY(20))
    check("周一 20:00 → offpeak 行", p is not None and p["price_type"] == "offpeak", True)


def test_get_current_price_offpeak_weekend():
    print("\n--- test_get_current_price_offpeak_weekend ---")
    eng = _make_engine()
    ch = _make_deepseek_channel()
    # 周六 10:00 → 周末 (非高峰)
    p = eng._get_current_price(ch, "deepseek-v4-pro", SATURDAY(10))
    check("周六 10:00 → offpeak 行", p is not None and p["price_type"] == "offpeak", True)
    check("周六 10:00 → input=4.0", p["input_price"] if p else None, 4.0)


def test_get_current_price_peak_afternoon():
    print("\n--- test_get_current_price_peak_afternoon ---")
    eng = _make_engine()
    ch = _make_deepseek_channel()
    # 周一 15:00 → 下午高峰
    p = eng._get_current_price(ch, "deepseek-v4-pro", WEEKDAY(15))
    check("周一 15:00 → peak 行", p is not None and p["price_type"] == "peak", True)
    check("周一 15:00 → input=8.0", p["input_price"] if p else None, 8.0)


def test_get_current_price_flash_model():
    print("\n--- test_get_current_price_flash_model ---")
    eng = _make_engine()
    ch = _make_deepseek_channel()
    # 周一 10:00 → flash 高峰
    p = eng._get_current_price(ch, "deepseek-v4-flash", WEEKDAY(10))
    check("周一 10:00 flash → peak", p is not None and p["price_type"] == "peak", True)
    check("周一 10:00 flash → input=2.0", p["input_price"] if p else None, 2.0)
    # 周六 10:00 → flash 低谷
    p2 = eng._get_current_price(ch, "deepseek-v4-flash", SATURDAY(10))
    check("周六 10:00 flash → offpeak", p2 is not None and p2["price_type"] == "offpeak", True)
    check("周六 10:00 flash → input=1.0", p2["input_price"] if p2 else None, 1.0)


def test_get_current_price_no_match():
    print("\n--- test_get_current_price_no_match ---")
    eng = _make_engine()
    ch = _make_deepseek_channel()
    # 不存在的 model
    p = eng._get_current_price(ch, "nonexistent-model", WEEKDAY(10))
    check("不存在 model → None", p, None)


# ============================================================
# DeepSeek 种子数据结构验证
# ============================================================

def test_manual_import_deepseek_prices():
    print("\n--- test_manual_import_deepseek_prices ---")
    # 直接验证 manual_import.py 中的 DeepSeek 价格数据结构
    # (不跑真实 import, 只检查数据形状)
    try:
        from scripts.manual_import import CHANNELS
    except Exception:
        # manual_import 可能依赖环境, 手动查找
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "manual_import", PROJECT_ROOT / "scripts" / "manual_import.py"
        )
        mod = importlib.util.module_from_spec(spec)
        # 可能 import 时有副作用, 包一层 try
        try:
            spec.loader.exec_module(mod)
        except Exception:
            print("  SKIP  manual_import 无法直接导入 (环境依赖), 跳过结构验证")
            return
        CHANNELS = mod.CHANNELS

    ds = None
    for ch in CHANNELS:
        if ch.get("key") == "deepseek":
            ds = ch
            break
    check("找到 deepseek 渠道", ds is not None, True)
    if not ds:
        return

    prices = ds["prices"]
    check("DeepSeek 价格行数 = 6", len(prices), 6)

    # 检查每行: (model, price_type, time_range, input, output)
    models = {p[0] for p in prices}
    check("包含 deepseek-v4-pro", "deepseek-v4-pro" in models, True)
    check("包含 deepseek-v4-flash", "deepseek-v4-flash" in models, True)

    types = {p[1] for p in prices}
    check("包含 peak", "peak" in types, True)
    check("包含 offpeak", "offpeak" in types, True)
    check("不含 normal (已改峰谷)", "normal" not in types, True)

    # 检查 time_range 用了新格式
    time_ranges = {p[2] for p in prices}
    check("含 工作日09:00-12:00", "工作日09:00-12:00" in time_ranges, True)
    check("含 工作日14:00-18:00", "工作日14:00-18:00" in time_ranges, True)
    check("含 非高峰全天", "非高峰全天" in time_ranges, True)
    check("不含旧 00:00-08:00", "00:00-08:00" not in time_ranges, True)


# ============================================================
# main
# ============================================================

def main():
    print("=" * 60)
    print("V4.8.7 P6: 分时段定价测试")
    print("=" * 60)

    test_empty_and_all_day()
    test_plain_hour_range_backward_compat()
    test_weekday_peak_morning()
    test_weekday_peak_afternoon()
    test_weekday_all_day()
    test_weekend_all_day()
    test_weekend_hour_range()
    test_offpeak_all_day()
    test_hour_range_boundaries()
    test_hour_range_invalid()
    test_get_current_price_peak_morning()
    test_get_current_price_offpeak_lunch()
    test_get_current_price_offpeak_evening()
    test_get_current_price_offpeak_weekend()
    test_get_current_price_peak_afternoon()
    test_get_current_price_flash_model()
    test_get_current_price_no_match()
    test_manual_import_deepseek_prices()

    print("\n" + "=" * 60)
    print(f"结果: {_passed} passed, {_failed} failed")
    print("=" * 60)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
