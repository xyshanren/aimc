"""注册 siliconflow 91 个 model 到 model_registry (V4.6.4 Phase 2a+)

背景:
- aimc 6 渠道中 siliconflow 占比 91+ 个 model, 实际能调度的"长尾 model"都在这
- 但 model_registry 表最初只列了 30 个主流 model, 大量 siliconflow model 缺失
- 价格表 price_snapshots 已经 91 个 (scripts/refresh_siliconflow_models.py 维护),
  但 registry 缺失 → 路由时这些 model 找不到 tier/capabilities, 走 fallback 兜底
- 本脚本把 91 个 model 全部注册到 model_registry, 字段从 model_id 关键词推断

设计:
- 拉 siliconflow /v1/models 91 个 model
- model_id 含 / 的 (e.g. "Qwen/Qwen3-32B") → 转 "qwen-3-32b" (去掉前缀, 全部小写)
- model_id 纯名 (e.g. "Qwen2.5-72B-Instruct") → 转 "qwen-2.5-72b-instruct"
- modality 从关键词推断 (vl → vision, image → image-generation, embedding → embedding, ...)
- tier 从 model_id 关键词推断 (235b/397b/122b → T0, 72b/32b/27b → T1, 9b/8b/4b/7b → T3)
- family 推断 (vendor 前缀: qwen/deepseek/kimi/...)

虚标清理 (--cleanup 参数):
- kimi-k3, kimi-k2 (siliconflow 没这两个)
- qwen-max, qwen-plus, qwen-turbo, qwen-2.5-* (百炼没接)
- hunyuan-pro, hunyuan-standard (腾讯没接)
- doubao-pro-32k, doubao-lite-32k (字节没接)
- moonshot-v1-8k (moonshot 渠道没接)
- llama-3.1-70b, llama-3.1-8b (siliconflow 也没)
- step-1-8k (siliconflow 实际是 stepfun-ai/Step-3.5-Flash)
- glm-4-air, glm-4-9b (glm 渠道只有 glm-4-plus/glm-4-flash)

用法:
    python scripts/sync_siliconflow_to_registry.py             # dry-run
    python scripts/sync_siliconflow_to_registry.py --apply   # 写 db
    python scripts/sync_siliconflow_to_registry.py --apply --cleanup  # 同步 + 清理虚标
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
from db.models import ModelRegistry
from db.normalizers import normalize_model_id, validate_model_id
from sqlalchemy import select
from scripts._siliconflow_sync import fetch_sf_models
from scripts.refresh_siliconflow_models import load_sf_key

# 虚标 model (db 登记了但没真实渠道)
VIRTUAL_MODELS = [
    "kimi-k3", "kimi-k2",
    "qwen-max", "qwen-plus", "qwen-turbo",
    "qwen-2.5-72b", "qwen-2.5-32b", "qwen-2.5-7b",
    "hunyuan-pro", "hunyuan-standard",
    "doubao-pro-32k", "doubao-lite-32k",
    "moonshot-v1-8k",
    "llama-3.1-70b", "llama-3.1-8b",
    "step-1-8k",
    "glm-4-air", "glm-4-9b",
]

FAMILY_HINTS = {
    "qwen": "qwen",
    "deepseek": "deepseek",
    "kimi": "kimi",
    "moonshot": "kimi",
    "baai": "baai",
    "thudm": "glm",
    "zai": "glm",
    "tencent": "hunyuan",
    "byte": "doubao",
    "step": "step",
    "minimax": "minimax",
    "minimaxai": "minimax",  # siliconflow 老命名
    "paddle": "paddleocr",
    "fun": "audio",
    "tongyi": "image",
    "wan": "video",
    "kwai": "image",
    "inclusionai": "inclusion",
    "meituan": "meituan",
    "nex": "nex",
    "deepseek-ai": "deepseek",
    "moonshotai": "kimi",
    "zai-org": "glm",  # P2-A: zai-org/GLM-4.5V -> glm-glm-4.5v
    # V6.4 (2026-09-12): org 名与模型行名不同的厂商 — family 取模型行系,
    # 否则 org 段会被塞进 model_id (paddlepaddle-paddleocr-vl-1.5 实证)
    "paddlepaddle": "paddleocr",
}

def infer_modality(model_id: str) -> str:
    m = model_id.lower()
    if "ocr" in m or "paddleocr" in m or "deepseek-ocr" in m:
        return "ocr"
    # P2-A: 扩展 vision 关键词, 覆盖 glm-4.5v / glm-4v / qwen-v 等命名
    if any(k in m for k in ["-vl-", "-vision-", "vl-", "4.5v", "glm-v", "glm-4v", "qwen-v"]):
        return "vision"
    if any(k in m for k in ["-image", "-t2i", "-i2v", "-t2v", "image-edit", "kolors", "z-image"]):
        return "image-generation"
    if any(k in m for k in ["-tts", "-asr", "ttsd", "speech", "cosyvoice", "sensevoice"]):
        return "audio"
    if any(k in m for k in ["wan", "hunyuanvideo"]):
        return "video"
    if "embedding" in m or "reranker" in m or "embed" in m or "-bge" in m:
        return "embedding"
    return "text"

def infer_tier(model_id: str) -> str:
    """推断新模型的初始 tier (仅用于 registry 中不存在的模型)。

    注意: 此函数基于参数规模关键词匹配, 对现代模型名 (deepseek-v4-flash,
    glm-5.2, minimax-m3 等) 无法正确推断, 会返回默认 T2。
    因此在更新已有模型时绝不应调用此函数覆写 tier —— tier 只应通过
    human-approved 路径 (approve_capability_change) 变更。
    """
    m = model_id.lower()
    if any(k in m for k in ["235b", "397b", "122b", "k2-think", "kimi-k2", "r1", "r2"]):
        return "T0"
    if any(k in m for k in ["72b", "30b-a3b", "32b", "27b", "instruct"]):
        return "T1"
    if any(k in m for k in ["14b", "9b", "8b", "4b", "7b", "1.5b", "1b", "mini"]):
        return "T3"
    return "T2"

def convert_sf_model_id(raw_id: str) -> tuple[str, str]:
    s = raw_id.lower()
    # 特殊: "Pro/Vendor/Model" 去掉 "Pro/" 前缀
    if s.startswith("pro/"):
        s = s[4:]
    parts = s.split("/")
    vendor = parts[0] if len(parts) > 1 else ""
    model_part = parts[-1]
    if vendor and model_part.startswith(vendor + "-"):
        model_part = model_part[len(vendor) + 1:]
    if vendor == "deepseek-ai":
        vendor = "deepseek"
    family = FAMILY_HINTS.get(vendor, vendor if vendor else model_part.split("-")[0])
    new_id = f"{family}-{model_part}" if model_part else family
    # P2-A: 去重 family 前缀 (e.g. glm-glm-4.5v -> glm-4.5v)
    # 当 FAMILY_HINTS 把 vendor 重映射后, new_id 可能变成 "{family}-{family}-{...}"
    prefix = f"{family}-{family}-"
    if new_id.startswith(prefix):
        new_id = new_id[len(family) + 1:]  # 去掉重复的 "{family}-"
    # V6.x (2026-09-04): 去冗余 family 前缀 — model_part 已带 "{family}<版本数字>"
    # 时不再重复 (e.g. qwen + qwen3-8b -> qwen3-8b, 不再是 qwen-qwen3-8b;
    # qwen + qwen2.5-14b-instruct -> qwen2.5-14b-instruct)。
    # validate_model_id 已同步放宽接受 "{family}<数字/点>..." 形态。
    if new_id != model_part and model_part.startswith(family):
        nxt = model_part[len(family):]
        if nxt and (nxt[0].isdigit() or nxt[0] == "."):
            new_id = model_part
    # V6.4 (2026-09-12): model_part 本身以 "{family}-" 开头时直接采用
    # (e.g. family paddleocr + PaddleOCR-VL-1.5 -> paddleocr-vl-1.5,
    # 不再拼成 paddleocr-paddleocr-vl-1.5)
    if new_id != model_part and model_part.startswith(f"{family}-"):
        new_id = model_part
    return family, new_id

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--cleanup", action="store_true", help="同时清理虚标 model")
    args = parser.parse_args()

    await init_db()
    api_key = load_sf_key()

    print("拉取 siliconflow model 列表...")
    raw_models = await fetch_sf_models(api_key)
    print(f"  拿到 {len(raw_models)} 个 model")

    converted = []
    for raw in raw_models:
        fam, mid = convert_sf_model_id(raw)
        mid_norm = normalize_model_id(mid)
        valid = validate_model_id(mid_norm, fam)
        converted.append({
            "raw_id": raw,
            "family": fam,
            "model_id": mid_norm,
            "display_name": mid_norm,
            "modality": infer_modality(mid_norm),
            "tier": infer_tier(mid_norm),
            "is_open_source": 0,
            "valid_namespace": valid,
        })

    print(f"\n=== 转换结果 ===")
    print(f"  total: {len(converted)}")
    by_fam = {}
    for c in converted:
        by_fam.setdefault(c["family"], 0)
        by_fam[c["family"]] += 1
    for fam, n in sorted(by_fam.items()):
        print(f"    [{fam}] {n}")
    invalid = [c for c in converted if not c["valid_namespace"]]
    if invalid:
        print(f"  invalid namespace: {len(invalid)}")
        for c in invalid[:5]:
            print(f"    raw={c['raw_id']!r} -> {c['model_id']!r} family={c['family']!r}")

    if args.cleanup:
        print(f"\n=== 虚标清理计划: {len(VIRTUAL_MODELS)} 个 ===")
        for v in VIRTUAL_MODELS:
            print(f"  - {v}")

    if not args.apply:
        print(f"\n[DRY-RUN] 加 --apply 才写 db")
        return

    # 写 db
    async with async_session_factory() as s:
        # 1. 清理虚标
        if args.cleanup:
            for v in VIRTUAL_MODELS:
                r = await s.execute(select(ModelRegistry).where(ModelRegistry.model_id == v))
                ex = r.scalars().first()
                if ex:
                    await s.delete(ex)
                    print(f"  [DEL] {v}")
        # 2. 插/更新 siliconflow
        for c in converted:
            if not c["valid_namespace"]:
                print(f"  [SKIP] invalid namespace: {c['raw_id']}")
                continue
            r = await s.execute(select(ModelRegistry).where(ModelRegistry.model_id == c["model_id"]))
            ex = r.scalars().first()
            if ex:
                ex.family = c["family"]
                ex.display_name = c["display_name"]
                ex.status = "active"
                print(f"  [UPD] {c['model_id']} (tier/modality preserved)")
            else:
                s.add(ModelRegistry(
                    model_id=c["model_id"],
                    family=c["family"],
                    display_name=c["display_name"],
                    tier=c["tier"],
                    capabilities='["general"]',
                    context_length=None,
                    modality=c["modality"],
                    benchmark_scores=None,
                    is_open_source=c["is_open_source"],
                    release_date=None,
                    status="active",
                    notes=f"synced from siliconflow (raw: {c['raw_id']})",
                ))
                print(f"  [ADD] {c['model_id']}")
        await s.commit()
    print(f"\n[OK] 同步完成")


if __name__ == "__main__":
    asyncio.run(main())
