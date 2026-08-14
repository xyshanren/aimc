"""Phase 4 v4.3.1 (2026-08-13) price_snapshot migration 测试

覆盖:
- find_migrations: 扫描大写行 (MiniMax-M*)
- find_migrations: 跳过已归一化行
- find_migrations: --rename 语义替换 (deepseek-chat -> deepseek-v4-flash)
- find_migrations: rename 优先于 normalize
- find_migrations: channel_name filter
- find_migrations: 幂等
- find_migrations: 空 db
- execute_migrations: dry_run=True 不修改
- execute_migrations: dry_run=False INSERT + DELETE
- execute_migrations: existing 已存在 → 跳过 INSERT
- _parse_rename_args: 解析 + 错误处理

跑法:
    .venv/Scripts/python.exe tests/test_migrate_price_snapshots.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class _MockChannel:
    def __init__(self, id, name, provider):
        self.id = id
        self.name = name
        self.provider = provider


class _MockPriceSnapshot:
    def __init__(self, id, channel_id, model, price_type="normal", time_range="全天",
                 input_price=0.0, output_price=0.0, source="manual"):
        self.id = id
        self.channel_id = channel_id
        self.model = model
        self.price_type = price_type
        self.time_range = time_range
        self.input_price = input_price
        self.output_price = output_price
        self.source = source


class _MockResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        class _S:
            def __init__(s, r): s._rows = r
            def all(s): return s._rows
            def scalar_one_or_none(s):
                for r in s._rows:
                    if isinstance(r, _MockPriceSnapshot):
                        return r
                return None
        return _S(self._rows)

    def scalar_one_or_none(self):
        for r in self._rows:
            if isinstance(r, _MockPriceSnapshot):
                return r
        return None


class _MockSession:
    """按 call 次数返回不同 mock 结果 (跟 find_migrations 实际跑法对应).

    call 0: select(Channel)
    call 1: select(PriceSnapshot) IN (channel_ids)
    call 2+: select(PriceSnapshot) existing lookup
    """
    def __init__(self, channels, price_snapshots, existing_normalized=None):
        self._channels = channels
        self._price_snapshots = price_snapshots
        self._existing_normalized = existing_normalized or []
        self._call_count = 0
        self._add_calls = []
        self._committed = False

    async def execute(self, stmt):
        idx = self._call_count
        self._call_count += 1
        if idx == 0:
            return _MockResult(self._channels)
        if idx == 1:
            return _MockResult(self._price_snapshots)
        # existing lookup
        if self._existing_normalized:
            return _MockResult(self._existing_normalized)
        return _MockResult([])

    async def commit(self):
        self._committed = True

    def add(self, obj):
        self._add_calls.append(obj)


# ============================================================
# find_migrations 测试
# ============================================================

def test_find_migrations_dirty():
    """扫描大写行"""
    from scripts.migrate_price_snapshots_normalize import find_migrations

    channels = [
        _MockChannel(1, "minimax_compat", "minimax"),
        _MockChannel(2, "deepseek_official", "deepseek"),
    ]
    price_snapshots = [
        # minimax_compat: 大写 (待归一化)
        _MockPriceSnapshot(1, 1, "MiniMax-M3", input_price=4.0, output_price=8.0),
        _MockPriceSnapshot(2, 1, "MiniMax-M2.7"),
        # deepseek_official: 已经是小写 (不会被归一化找到)
        _MockPriceSnapshot(10, 2, "deepseek-chat"),
        _MockPriceSnapshot(11, 2, "deepseek-reasoner"),
        # 已经归一化的
        _MockPriceSnapshot(100, 1, "minimax-m2.7-highspeed"),
    ]
    session = _MockSession(channels, price_snapshots)

    migrations = asyncio.run(find_migrations(session))
    # 不带 --rename: 仅大写归一化被识别, deepseek-* 小写被跳过
    assert len(migrations) == 2, f"应 2 条 (仅 MiniMax-M*), 实际 {len(migrations)}"
    old_models = {m["old_model"] for m in migrations}
    assert old_models == {"MiniMax-M3", "MiniMax-M2.7"}
    # 全是 normalize 原因
    assert all(m["reason"] == "normalize" for m in migrations)
    print(f"  [OK] find_migrations 扫描到 2 条大写行 (deepseek-chat 跳过, 因为已归一化)")


def test_find_migrations_with_rename():
    """--rename 触发 deepseek-chat -> deepseek-v4-flash"""
    from scripts.migrate_price_snapshots_normalize import find_migrations

    channels = [
        _MockChannel(1, "minimax_compat", "minimax"),
        _MockChannel(2, "deepseek_official", "deepseek"),
    ]
    price_snapshots = [
        _MockPriceSnapshot(1, 1, "MiniMax-M3"),
        _MockPriceSnapshot(10, 2, "deepseek-chat"),
        _MockPriceSnapshot(11, 2, "deepseek-reasoner"),
        _MockPriceSnapshot(100, 1, "minimax-m2.7-highspeed"),  # 已归一化
    ]
    renames = {
        "deepseek-chat": "deepseek-v4-flash",
        "deepseek-reasoner": "deepseek-v4-pro",
    }
    session = _MockSession(channels, price_snapshots)

    migrations = asyncio.run(find_migrations(session, renames=renames))
    # 1 个 normalize + 2 个 rename = 3 条
    assert len(migrations) == 3, f"应 3 条 (1 normalize + 2 rename), 实际 {len(migrations)}"

    # MiniMax-M3 -> normalize
    minmax = next(m for m in migrations if m["old_model"] == "MiniMax-M3")
    assert minmax["new_model"] == "minimax-m3"
    assert minmax["reason"] == "normalize"

    # deepseek-chat -> rename
    ds_chat = next(m for m in migrations if m["old_model"] == "deepseek-chat")
    assert ds_chat["new_model"] == "deepseek-v4-flash"
    assert ds_chat["reason"] == "rename"

    # deepseek-reasoner -> rename
    ds_reason = next(m for m in migrations if m["old_model"] == "deepseek-reasoner")
    assert ds_reason["new_model"] == "deepseek-v4-pro"
    assert ds_reason["reason"] == "rename"

    # 已归一化的 minimax-m2.7-highspeed 不在列表
    assert not any(m["old_model"] == "minimax-m2.7-highspeed" for m in migrations)
    print(f"  [OK] --rename: 1 normalize + 2 rename 共 3 条")


def test_find_migrations_channel_filter():
    """channel_name filter"""
    from scripts.migrate_price_snapshots_normalize import find_migrations

    channels = [
        _MockChannel(1, "minimax_compat", "minimax"),
        _MockChannel(2, "deepseek_official", "deepseek"),
    ]
    price_snapshots = [
        _MockPriceSnapshot(1, 1, "MiniMax-M3"),
        _MockPriceSnapshot(10, 2, "deepseek-chat"),
    ]
    session = _MockSession(channels, price_snapshots)

    migrations = asyncio.run(find_migrations(session, channel_name="minimax_compat"))
    assert len(migrations) == 1
    assert migrations[0]["channel_name"] == "minimax_compat"
    assert migrations[0]["old_model"] == "MiniMax-M3"
    print(f"  [OK] channel_name filter 仅扫 minimax_compat")


def test_find_migrations_idempotent():
    """重复调用结果相同"""
    from scripts.migrate_price_snapshots_normalize import find_migrations

    channels = [_MockChannel(1, "minimax_compat", "minimax")]
    price_snapshots = [_MockPriceSnapshot(1, 1, "MiniMax-M3")]
    s1 = _MockSession(channels, price_snapshots)
    s2 = _MockSession(channels, price_snapshots)

    m1 = asyncio.run(find_migrations(s1))
    m2 = asyncio.run(find_migrations(s2))
    assert len(m1) == len(m2)
    print(f"  [OK] find_migrations 幂等")


def test_find_migrations_empty():
    """无 price_snapshot 时返回 []"""
    from scripts.migrate_price_snapshots_normalize import find_migrations

    channels = [_MockChannel(1, "minimax_compat", "minimax")]
    session = _MockSession(channels, [])

    migrations = asyncio.run(find_migrations(session))
    assert migrations == []
    print(f"  [OK] 空 db 无迁移行")


# ============================================================
# execute_migrations 测试
# ============================================================

def test_execute_migrations_dry_run():
    """dry_run=True 不修改 db"""
    from scripts.migrate_price_snapshots_normalize import execute_migrations

    migrations = [{
        "old_id": 1, "old_model": "MiniMax-M3",
        "new_model": "minimax-m3",
        "channel_id": 1, "channel_name": "minimax_compat",
        "provider": "minimax",
        "price_type": "normal", "time_range": "全天",
        "input_price": 0.0, "output_price": 0.0,
        "source": "manual", "new_id": None, "reason": "normalize",
    }]
    session = _MockSession([], [])
    result = asyncio.run(execute_migrations(session, migrations, dry_run=True))
    assert result["would_sync"] == 1
    assert len(session._add_calls) == 0
    assert session._committed is False
    print(f"  [OK] dry_run=True 不修改 db")


def test_execute_migrations_normal():
    """dry_run=False INSERT + DELETE"""
    from scripts.migrate_price_snapshots_normalize import execute_migrations

    migrations = [
        {  # new_id=None → INSERT
            "old_id": 1, "old_model": "MiniMax-M3",
            "new_model": "minimax-m3",
            "channel_id": 1, "channel_name": "minimax_compat",
            "provider": "minimax",
            "price_type": "normal", "time_range": "全天",
            "input_price": 0.0, "output_price": 0.0,
            "source": "manual", "new_id": None, "reason": "normalize",
        },
        {  # new_id=999 → 跳过 INSERT
            "old_id": 2, "old_model": "deepseek-chat",
            "new_model": "deepseek-v4-flash",
            "channel_id": 2, "channel_name": "deepseek_official",
            "provider": "deepseek",
            "price_type": "normal", "time_range": "全天",
            "input_price": 1.0, "output_price": 2.0,
            "source": "manual", "new_id": 999, "reason": "rename",
        },
    ]
    session = _MockSession([], [])
    result = asyncio.run(execute_migrations(session, migrations, dry_run=False))
    assert result["synced"] == 1
    assert result["deleted"] == 2
    assert session._committed is True
    assert len(session._add_calls) == 1
    added = session._add_calls[0]
    assert added.model == "minimax-m3"
    print(f"  [OK] INSERT 1 + DELETE 2 (new_id 存在跳过 INSERT)")


# ============================================================
# _parse_rename_args 测试
# ============================================================

def test_parse_rename_args():
    """--rename 参数解析"""
    from scripts.migrate_price_snapshots_normalize import _parse_rename_args

    # 单个
    assert _parse_rename_args(["deepseek-chat=deepseek-v4-flash"]) == {
        "deepseek-chat": "deepseek-v4-flash"
    }
    # 多个
    assert _parse_rename_args([
        "deepseek-chat=deepseek-v4-flash",
        "deepseek-reasoner=deepseek-v4-pro",
    ]) == {
        "deepseek-chat": "deepseek-v4-flash",
        "deepseek-reasoner": "deepseek-v4-pro",
    }
    # 空 list
    assert _parse_rename_args([]) == {}
    # 带空格
    assert _parse_rename_args([" a = b "]) == {"a": "b"}
    print(f"  [OK] _parse_rename_args 解析正常")


def test_parse_rename_args_invalid():
    """--rename 格式错误"""
    from scripts.migrate_price_snapshots_normalize import _parse_rename_args

    # 没有 =
    try:
        _parse_rename_args(["deepseek-chat"])
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "格式错误" in str(e)

    # 空 old
    try:
        _parse_rename_args(["=deepseek-v4-flash"])
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "不能为空" in str(e)

    # 空 new
    try:
        _parse_rename_args(["deepseek-chat="])
        assert False, "应抛 ValueError"
    except ValueError as e:
        assert "不能为空" in str(e)
    print(f"  [OK] _parse_rename_args 错误处理正确")


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Phase 4 v4.3.1 price_snapshot migration 测试")
    print("=" * 60)

    print("\n=== find_migrations ===")
    test_find_migrations_dirty()
    test_find_migrations_with_rename()
    test_find_migrations_channel_filter()
    test_find_migrations_idempotent()
    test_find_migrations_empty()

    print("\n=== execute_migrations ===")
    test_execute_migrations_dry_run()
    test_execute_migrations_normal()

    print("\n=== _parse_rename_args ===")
    test_parse_rename_args()
    test_parse_rename_args_invalid()

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)