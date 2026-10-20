"""P0-6 (2026-08-14): seed 6 channel 的 quota_config 到 account_quotas 表

设计:
- 每个 account 多个限额维度 (rpm / tpm / daily_tokens / weekly_tokens / 5h_tokens / concurrent / balance_cny)
- 6 channel 的实际限额来自:
  * minimax TokenPlanMax: 用户 brief — 5h rolling + 周 rolling, 2027-06-25 到期
  * sensenova 日日新: 免费试用 + 并发限制 (公开文档)
  * 其他 (deepseek/glm/siliconflow/stepfun): RPM/TPM, 通用默认

注: 实际限额值是估算的, 部署后根据真实使用调整 (P0-6 后 dashboard 提供 quota 编辑 UI)

用法:
    python scripts/seed_account_quotas.py            # dry-run
    python scripts/seed_account_quotas.py --apply   # 实际写 db
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import Account, AccountQuota
from sqlalchemy import select


# (channel_name, account_name, [(dimension, limit_value, window_seconds, reset_strategy, notes), ...])
QUOTA_CONFIG = [
    # === minimax_compat (TokenPlanMax 年度会员, 2027-06-25 到期) ===
    # 你的 brief: 5h rolling + 周 rolling 限额
    (
        "minimax_compat", "minimax_01",
        [
            # 2026-10-20 用户拍板: 不 seed token 门 (5h_tokens/weekly_tokens) 与
            # 估算 rpm — 上游 TokenPlanMax 限额动态不透明 (后台可调/活动期放宽/
            # 重置卡与积分可恢复), 固定门会在上游额度已恢复时把账号假死 (生产
            # 实证: 8/14 估算 5h_tokens=50M 被 3h 真实流量打满; rpm=60 被
            # 62/min 峰值击穿)。上游 429 由冷却+failover 兜底; 月度预算
            # (~18 亿 token) 走用量看板。
            ("concurrent", 10, None, "rolling", "保守并发限制"),
        ]
    ),
    # === sensenova_official (免费试用) ===
    (
        "sensenova_official", "sensenova_01",
        [
            ("daily_tokens", 1_000_000, None, "fixed_daily",
             "免费试用日额度 (公开文档估算)"),
            ("concurrent", 3, None, "rolling", "试用并发限制"),
        ]
    ),
    # === deepseek_official (按量付费) ===
    # 2026-10-20: 余额门激活 — 阈值 5 元 (低于 5 元停止路由, 防打爆);
    # sync 由 scheduler balance_sync job (10min) 维护, 语义见 gateway/quota.py
    (
        "deepseek_official", "deepseek_01",
        [
            ("rpm", 60, None, "rolling", "官方默认"),
            ("balance_cny", 5.0, None, "never",
             "余额下限 (sync 余额 <= 5 元即拦), balance_sync job 维护"),
        ]
    ),
    # === glm_bigmodel / stepfun_official (订阅为主) ===
    # 2026-10-20 用户拍板: 不 seed 余额门 (订阅额度语义不适用余额门);
    # tpm 门曾恒拒死已删 (V6.6)。siliconflow 保留 tpm=500k 作成本闸
    # (用户拍板保留), 其 balance_cny 行因代金券未用完暂不激活 (留 seed 不 sync)。
    (
        "glm_bigmodel", "glm_01",
        [
            ("rpm", 120, None, "rolling", "智谱官方默认"),
        ]
    ),
    # === siliconflow_aggregator (聚合平台) ===
    (
        "siliconflow_aggregator", "siliconflow_01",
        [
            ("rpm", 200, None, "rolling", "硅基流动默认"),
            ("tpm", 500_000, None, "rolling", "硅基流动默认 (成本闸, 用户拍板保留)"),
            ("balance_cny", 20.0, None, "never",
             "代金券耗尽前不激活 (无 sync 即不拦)"),
        ]
    ),
    # === stepfun_official (按量付费) ===
    (
        "stepfun_official", "stepfun_01",
        [
            ("rpm", 60, None, "rolling", "官方默认"),
        ]
    ),
]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    await init_db()

    async with async_session_factory() as s:
        # 读所有账号
        accounts = (await s.execute(
            select(Account, Account.__table__.c.channel_id)
        )).all()
        # Map: (channel_name, account_name) -> Account.id
        ch_acc_map = {}
        for row in accounts:
            acc = row[0]
            # 需要 join channel 才能拿 name, 这里简化
            ch_acc_map[acc.account_name] = acc

        # 用 raw SQL 查 channel_id 关联
        from db.models import Channel
        chans = (await s.execute(select(Channel))).scalars().all()
        chan_name_id = {c.name: c.id for c in chans}

        print(f"=== Seed account_quotas ({len(QUOTA_CONFIG)} channel/account 配置) ===")
        all_quotas = []  # (account_id, dimension, limit_value, window_seconds, reset_strategy, notes)

        for chan_name, acc_name, dims in QUOTA_CONFIG:
            acc = ch_acc_map.get(acc_name)
            if not acc:
                print(f"  [WARN] account {acc_name!r} 不存在, 跳过 {chan_name} 配置")
                continue
            for dim, limit_val, window_sec, reset, notes in dims:
                all_quotas.append((acc.id, dim, limit_val, window_sec, reset, notes))
                print(f"  [ADD] {chan_name}/{acc_name}: {dim}={limit_val} window={window_sec}s reset={reset}")

        if not args.apply:
            print(f"\n[DRY-RUN] 加 --apply 才写 db ({len(all_quotas)} 行)")
            return

        # 实际写入
        from sqlalchemy import delete
        await s.execute(delete(AccountQuota))
        for acc_id, dim, limit_val, window_sec, reset, notes in all_quotas:
            s.add(AccountQuota(
                account_id=acc_id,
                dimension=dim,
                limit_value=limit_val,
                window_seconds=window_sec,
                reset_strategy=reset,
                enabled=1,
                notes=notes,
            ))
        await s.commit()
        print(f"\n[OK] 写入 {len(all_quotas)} 行 account_quotas")


if __name__ == "__main__":
    asyncio.run(main())
