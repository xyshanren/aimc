"""模型消失检测 (intelligence/model_absence.py) 单元测试

覆盖:
1. 首次快照: 全量插入, 零 suggestion
2. 消失一次: miss_count=1, 未达阈值无 suggestion
3. 连续第二次消失: 达阈值 + 有活跃组员 → suggestion 生成
4. 模型回归: miss_count 清零
5. 列骤降守卫: <50% 跳过检测 (快照不动)
6. 幂等: 已有 pending suggestion 不重复建
7. 无组员模型: 达阈值也不建 suggestion

用例按 provider (p1..p7) 隔离共享临时库, 无需清表。
跑法: python tests/test_model_absence.py 或 pytest tests/test_model_absence.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_absence_")
    db_path = os.path.join(tmp_dir, "test.db")
    import types

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        aimc_master_key="a" * 64,
        db_url=f"sqlite+aiosqlite:///{db_path}",
        db_path=db_path,
    )


def _ensure_db():
    from db.init_db import init_db
    asyncio.run(init_db())


_install_mock_env()
_ensure_db()  # 模块级建表 (pytest 不走 __main__ 分支)


async def _seed_group_member(model_id: str, group_id: str = "tier:code"):
    from db.engine import async_session_factory
    from db.models import ModelGroupMember
    async with async_session_factory() as s:
        s.add(ModelGroupMember(group_id=group_id, model_id=model_id,
                               priority=100, weight=1.0, added_by="test"))
        await s.commit()


async def _pending_suggestions():
    from db.engine import async_session_factory
    from db.models import ModelChangeSuggestion
    from sqlalchemy import select
    async with async_session_factory() as s:
        return list((await s.execute(select(
            ModelChangeSuggestion.model_id).where(
            ModelChangeSuggestion.change_type == "remove_from_group",
            ModelChangeSuggestion.status == "pending"))).scalars().all())


async def _snapshot_state(provider: str):
    from db.engine import async_session_factory
    from db.models import ModelListSnapshot
    from sqlalchemy import select
    async with async_session_factory() as s:
        rows = list((await s.execute(select(
            ModelListSnapshot.model, ModelListSnapshot.miss_count)
            .where(ModelListSnapshot.provider == provider))).all())
        return {m: c for m, c in rows}


from intelligence.model_absence import detect_missing_models  # noqa: E402


def test_first_snapshot_inserts_all():
    async def run():
        stats = await detect_missing_models("p1", ["m-a", "m-b", "m-c"])
        assert stats["suggestions_created"] == []
        assert stats["tracked"] == 0
        state = await _snapshot_state("p1")
        assert state == {"m-a": 0, "m-b": 0, "m-c": 0}
    asyncio.run(run())


def test_missing_once_below_threshold():
    async def run():
        await detect_missing_models("p2", ["m-a", "m-b", "m-c"])   # 首次
        stats = await detect_missing_models("p2", ["m-a", "m-b"])  # m-c 消失第 1 次
        assert stats["suggestions_created"] == []
        state = await _snapshot_state("p2")
        assert state["m-c"] == 1
        assert state["m-a"] == 0 and state["m-b"] == 0
    asyncio.run(run())


def test_missing_twice_creates_suggestion_with_member():
    async def run():
        await _seed_group_member("m-old")
        await detect_missing_models("p3", ["m-old", "m-new"])
        await detect_missing_models("p3", ["m-new"])               # 第 1 次缺失
        stats = await detect_missing_models("p3", ["m-new"])       # 第 2 次缺失
        assert stats["suggestions_created"] == ["m-old"]
        pend = await _pending_suggestions()
        assert "m-old" in pend
    asyncio.run(run())


def test_model_return_resets_miss():
    async def run():
        await detect_missing_models("p4", ["m-a", "m-b"])
        await detect_missing_models("p4", ["m-a"])         # m-b miss 1
        await detect_missing_models("p4", ["m-a", "m-b"])  # 回归
        state = await _snapshot_state("p4")
        assert state["m-b"] == 0
    asyncio.run(run())


def test_shrink_guard_skips():
    async def run():
        await detect_missing_models("p5", ["m-%d" % i for i in range(10)])
        stats = await detect_missing_models("p5", ["m-0", "m-1", "m-2", "m-3"])  # 4/10 < 50%
        assert stats["shrunk_skipped"] is True
        state = await _snapshot_state("p5")
        assert len(state) == 10  # 快照未动
    asyncio.run(run())


def test_idempotent_suggestion():
    async def run():
        await _seed_group_member("m-x")
        await detect_missing_models("p6", ["m-x", "m-y"])
        for _ in range(4):
            await detect_missing_models("p6", ["m-y"])
        pend = await _pending_suggestions()
        assert "m-x" in pend
        assert pend.count("m-x") == 1  # 幂等: 只一条
    asyncio.run(run())


def test_no_member_no_suggestion():
    async def run():
        await detect_missing_models("p7", ["m-lonely", "m-k"])
        await detect_missing_models("p7", ["m-k"])
        stats = await detect_missing_models("p7", ["m-k"])
        assert stats["missing_now"] == ["m-lonely"]
        assert stats["suggestions_created"] == []  # 无组员不建
        state = await _snapshot_state("p7")
        assert state["m-lonely"] == 2
    asyncio.run(run())


if __name__ == "__main__":
    _ensure_db()
    test_first_snapshot_inserts_all()
    test_missing_once_below_threshold()
    test_missing_twice_creates_suggestion_with_member()
    test_model_return_resets_miss()
    test_shrink_guard_skips()
    test_idempotent_suggestion()
    test_no_member_no_suggestion()
    print("all model_absence tests passed")
