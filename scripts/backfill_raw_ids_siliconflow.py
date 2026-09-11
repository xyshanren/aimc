#!/usr/bin/env python3
"""回填 model_registry.raw_ids 中 siliconflow 渠道的上游原始 model_id。

来源：price_snapshots(channel_id=3, effective_to IS NULL) 的 raw_model_id
(硅基流动完整路径, e.g. "deepseek-ai/DeepSeek-V3")。

规范名匹配：SF 快照的 model 列也是完整路径，与 model_registry.model_id
(规范名) 不一致，故按候选名匹配：
  1. 去组织前缀取末段小写   deepseek-ai/DeepSeek-V3 -> deepseek-v3
  2. 全路径小写 "/" 换 "-"  meituan-longcat/LongCat-2.0 -> meituan-longcat-longcat-2.0
  3. ALIAS 显式映射兜底     inclusionAI/Ling-flash-2.0 -> inclusion-ling-flash-2.0

同规范名多行时按优先级取 raw：普通行 > Pro/ 行 > LoRA/ 行
(Pro/ 前缀是计费档不是模型本体, LoRA 是托管服务)。

merge 语义：raw_ids 已有的其他渠道键保留，仅合并 "siliconflow" 键。
UPDATE-only，默认 dry-run，--apply 才提交。
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from db.engine import async_session_factory  # noqa: E402
from db.models import ModelRegistry, PriceSnapshot  # noqa: E402

SILICONFLOW_CHANNEL_ID = 3

# 候选名仍匹配不上时的显式映射 (候选全路径小写 -> 规范名)
ALIAS = {
    "inclusionai-ling-flash-2.0": "inclusion-ling-flash-2.0",
    "inclusionai-ling-mini-2.0": "inclusion-ling-mini-2.0",
}


def row_priority(model: str) -> int:
    """普通行 0 < Pro/ 行 1 < LoRA/ 行 2，越小越优先。"""
    if model.startswith("LoRA/"):
        return 2
    if model.startswith("Pro/"):
        return 1
    return 0


def canonical_candidates(raw_model: str) -> list[str]:
    last = raw_model.split("/")[-1].lower()
    full = raw_model.lower().replace("/", "-")
    cands = [last, full, ALIAS.get(full, "")]
    return [c for c in cands if c]


async def main(apply: bool) -> None:
    async with async_session_factory() as session:
        snapshots = (await session.execute(
            select(PriceSnapshot.model, PriceSnapshot.raw_model_id)
            .where(PriceSnapshot.channel_id == SILICONFLOW_CHANNEL_ID)
            .where(PriceSnapshot.effective_to.is_(None))
            .where(PriceSnapshot.raw_model_id.isnot(None))
            .where(PriceSnapshot.raw_model_id != "")
        )).all()

        registry = (await session.execute(
            select(ModelRegistry)
        )).scalars().all()
        registry_ids = {m.model_id: m for m in registry}

        # SF 快照行 -> registry 行；同规范名按 row_priority 取优
        matched: dict[str, tuple[int, str]] = {}
        unmatched = []
        for sf_model, raw_id in snapshots:
            target = None
            for cand in canonical_candidates(sf_model):
                if cand in registry_ids:
                    target = cand
                    break
            if target is None:
                unmatched.append(sf_model)
                continue
            pri = row_priority(sf_model)
            if target not in matched or pri < matched[target][0]:
                matched[target] = (pri, raw_id)

        changed = 0
        for model_id, (_, raw) in sorted(matched.items()):
            m = registry_ids[model_id]
            current = {}
            if m.raw_ids:
                try:
                    current = json.loads(m.raw_ids)
                except (TypeError, ValueError):
                    current = {}
            old = current.get("siliconflow")
            if old == raw:
                continue
            if apply:
                current["siliconflow"] = raw
                m.raw_ids = json.dumps(current, ensure_ascii=False)
            plan = "APPLY" if apply else "PLAN"
            print(f"[{plan}] {model_id}: {old!r} -> {raw!r}")
            changed += 1

        print(f"\n{'APPLIED' if apply else 'PLAN'}: {changed} rows")
        if unmatched:
            print(f"UNMATCHED ({len(unmatched)}): {sorted(set(unmatched))}")
        if apply and changed:
            await session.commit()


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
