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
            ("5h_tokens", 50_000_000, 5 * 3600, "rolling",
             "TokenPlanMax 5h 滚动窗口估算"),
            ("weekly_tokens", 200_000_000, 7 * 86400, "rolling",
             "TokenPlanMax 周滚动窗口估算"),
            ("rpm", 60, None, "rolling", "估算 RPM"),
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
    (
        "deepseek_official", "deepseek_01",
        [
            ("rpm", 60, None, "rolling", "官方默认"),
            ("tpm", 100_000, None, "rolling", "官方默认"),
            ("balance_cny", 50.0, None, "never",
             "估算账户余额, 定期 sync"),
        ]
    ),
    # === glm_bigmodel (按量付费) ===
    (
        "glm_bigmodel", "glm_01",
        [
            ("rpm", 120, None, "rolling", "智谱官方默认"),
            ("tpm", 200_000, None, "rolling", "智谱官方默认"),
            ("balance_cny", 30.0, None, "never",
             "估算账户余额, 定期 sync"),
        ]
    ),
    # === siliconflow_aggregator (聚合平台) ===
    (
        "siliconflow_aggregator", "siliconflow_01",
        [
            ("rpm", 200, None, "rolling", "硅基流动默认"),
            ("tpm", 500_000, None, "rolling", "硅基流动默认"),
            ("balance_cny", 20.0, None, "never",
             "估算账户余额, 定期 sync"),
        ]
    ),
    # === stepfun_official (按量付费) ===
    (
        "stepfun_official", "stepfun_01",
        [
            ("rpm", 60, None, "rolling", "官方默认"),
            ("tpm", 100_000, None, "rolling", "官方默认"),
            ("balance_cny", 30.0, None, "never",
             "估算账户余额, 定期 sync"),
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
