"""V6.5 批次三: S7 停机 drain 回归.

S7 (production-review): fire-and-forget 任务 (proxy._pending_tasks) 与定时
采集任务 (scheduler._inflight_tasks 的 batch job) 在关停时不被等待 →
engine.dispose() 打断在途写库, 计量丢失 / 采集半写。

修复:
- proxy._spawn() 登记式 fire-and-forget (替代裸 loop.create_task)
- app.shutdown_drain_tasks() 在 dispose 前 gather
- scheduler._run_provider_batch 自身注册进 _inflight_tasks
"""

import asyncio

import pytest

import gateway.proxy as proxy_mod


@pytest.mark.asyncio
async def test_spawn_registers_and_auto_discards():
    """_spawn 登记任务, 完成后自移除 (集合不膨胀)。"""
    proxy_mod._pending_tasks.clear()

    async def _work():
        await asyncio.sleep(0.01)
        return 42

    task = proxy_mod._spawn(_work())
    assert task in proxy_mod._pending_tasks
    assert await task == 42
    await asyncio.sleep(0)  # 让 done_callback 跑
    assert task not in proxy_mod._pending_tasks


@pytest.mark.asyncio
async def test_shutdown_drain_waits_pending_tasks():
    """drain: 在途任务被等待完成 (不丢); 空集合返回 0。

    全程在测试内 import (含 proxy) — conftest 每个 test 后移除所有项目模块,
    文件顶层 import 拿到的是跨 test 失联的旧模块对象 (pending set 分叉)。
    """
    import gateway.app as app_mod
    import gateway.proxy as proxy_fresh
    proxy_fresh._pending_tasks.clear()

    done_flag = {"usage_log": False}

    async def _write_usage():
        await asyncio.sleep(0.05)
        done_flag["usage_log"] = True

    proxy_fresh._spawn(_write_usage())
    n = await app_mod.shutdown_drain_tasks(timeout=5.0)
    assert n == 1, "drain 必须等到刚 spawn 的任务"
    assert done_flag["usage_log"], "drain 后任务必须已完成 (关停不丢计量)"

    # 空集合
    proxy_fresh._pending_tasks.clear()
    assert await app_mod.shutdown_drain_tasks(timeout=1.0) == 0


@pytest.mark.asyncio
async def test_shutdown_drain_timeout_discards():
    """drain 超时: 不永久挂起关停流程 (超时任务被丢弃并告警)。"""
    import gateway.app as app_mod
    proxy_mod._pending_tasks.clear()

    async def _slow():
        await asyncio.sleep(30)

    task = proxy_mod._spawn(_slow())
    n = await app_mod.shutdown_drain_tasks(timeout=0.05)
    assert n == 0
    assert not task.done()
    task.cancel()
    proxy_mod._pending_tasks.clear()


@pytest.mark.asyncio
async def test_provider_batch_registers_inflight(monkeypatch):
    """定时 batch job 自身登记进 _inflight_tasks (关停可被等待)。"""
    import intelligence.scheduler as sched_mod
    ran = {"sources": 0}

    async def _fake_run_source(source):
        ran["sources"] += 1
        # 模拟正在抓取时被外部观察
        assert task_in_inflight()

    def task_in_inflight():
        cur = asyncio.current_task()
        return cur in sched_mod._inflight_tasks

    monkeypatch.setattr(sched_mod, "_run_source_task", _fake_run_source)
    saved = set(sched_mod._inflight_tasks)
    try:
        await sched_mod._run_provider_batch(
            "test_provider", [SimpleNamespaceSource()]
        )
        assert ran["sources"] == 1
    finally:
        sched_mod._inflight_tasks.clear()
        sched_mod._inflight_tasks.update(saved)


class SimpleNamespaceSource:
    provider = "test_provider"
    source_type = "web_page"


@pytest.mark.asyncio
async def test_proxy_pending_tasks_spawned_by_write_paths():
    """_schedule_usage_log 系列走 _spawn (登记可见) — 抽查源码防回归。"""
    from pathlib import Path
    src = Path("gateway/proxy.py").read_text(encoding="utf-8")
    body = src.split("def _spawn")[1]
    # 请求路径的 fire-and-forget 都应经 _spawn (不含 _spawn 本体内的 loop.create_task)
    assert body.count("_spawn(") >= 6, "存在未登记的裸 create_task 调用点"
    # 除 _spawn 本体外不得再有裸 loop.create_task
    assert "loop.create_task(" not in body.replace(
        "task = loop.create_task(coro)", ""
    ), "存在未登记的裸 loop.create_task 调用点"
