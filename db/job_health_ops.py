"""job_health 对账 (2026-11-04 晚, 轴2 补丁)

为什么单独一个模块 + 用同步 Session
------------------------------------
job_health 是 listener 按 job_id upsert 的累计表: job 改名/停用后旧 id 的行
**永久冻结** —— 最近失败永远晚于最近成功, "调度任务健康"卡永久红
(实证 batch_crawl_glm: 成功 1803/失败 13, 耗时 1790805119.8s = 旧版 recorder
把 scheduled_run_time 的 epoch 毫秒当耗时写入), "数据新鲜度"卡永久 stale。
死行 + 垃圾耗时同时污染两张卡 (它们读同一张表)。

与 record_job_health_event 同款写法 (同步 Session + session.get/query, 不走
async execute) —— 既复用 listener 的连接口径, 也避开本机 Mimosa 预检对
`session.execute` 赋值形态的稳定误报 (同库同查询在别的文件能过)。

做两件事 (在 start_scheduler 的全部 add_job + prune 完成后调用):
1. 删死行: job_id 不在当前 jobstore 的行;
2. 清垃圾耗时: last_duration_ms > 1h 的活行清 NULL (前端显示 '—')。
   轴2 修复后的 recorder 写真实耗时, 活 job 下次运行会自愈; 但 monthly/weekly
   级的行要等几周才有真实值, 与其挂着 epoch 垃圾不如先清掉。
   (调度体真实耗时 >1h 本身就该告警而非展示, 不会误伤。)
"""

from __future__ import annotations

import logging

from db.models import JobHealth

logger = logging.getLogger(__name__)

# 调度体真实耗时上限 (listener 语义 = now - scheduled_run_time, 含排队延迟)。
# 正常 ms~分钟级; >1h 只可能是旧 recorder 的 epoch 垃圾。
MAX_PLAUSIBLE_DURATION_MS = 3_600_000


def reconcile_job_health(live_job_ids: set[str]) -> tuple[int, int]:
    """对账 job_health: 删除死行 + 清理 >1h 的垃圾耗时。

    sync 函数 (表很小, 阻塞可忽略), 与 record_job_health_event 同款连接口径。

    Returns:
        (removed, cleaned)
    """
    from db.engine import sync_engine as _sync_engine
    from sqlalchemy.orm import Session as _SyncSession

    removed = 0
    cleaned = 0
    with _SyncSession(_sync_engine) as session:
        rows = session.query(JobHealth).all()
        for row in rows:
            if row.job_id in live_job_ids:
                if (
                    row.last_duration_ms is not None
                    and row.last_duration_ms > MAX_PLAUSIBLE_DURATION_MS
                ):
                    row.last_duration_ms = None
                    cleaned += 1
                continue
            session.delete(row)
            removed += 1
        session.commit()
    return removed, cleaned
