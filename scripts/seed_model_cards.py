"""Seed ModelCard 表 (Phase 2D-4, 2026-07-24 重写)

引用 OpenCompass 公开榜单分数，让 dashboard / 路由决策有依据。

数据来源（2026-07-25 playwright 抓取 + 用户截图校对）：
  - LLM 官方评测榜   2026-05-13 更新  https://rank.opencompass.org.cn/leaderboard/llm?class=official
  - LLM 开源评测榜   2026-03-09 更新  https://rank.opencompass.org.cn/leaderboard/llm?class=open_source
  - VLM 官方评测榜   2026-06-01 更新  https://rank.opencompass.org.cn/leaderboard/image-vlm?class=official
  - VLM 开源评测榜   2026-05-26 更新  https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source

设计：
- 不自己做 benchmark（成本高、个人没必要）
- 引用 OpenCompass 公开榜单分数
- **同 model 跨榜 → 分两行**（model+benchmark 联合唯一键已天然支持 upsert）
- score 字段：0.0 = 未收录（保留作为 dashboard 占位）；>0 = 榜单分数

设计原则（用户偏好：国内 + 个人用）：
- **只列 5 家国内厂商**（先打基础）：DeepSeek / Qwen / Zhipu/GLM / Kimi / MiniMax
- **不列国外**（GPT-5/Claude/Gemini 等）—— 不用做选型参考
- **保留老版本**（新版 ≠ 替换，老版同服务照样可用）
- **未收录 model**：score=0.0 + notes 说明原因（"未收录"/"发布日 晚于榜单"）

用法：
    python scripts/seed_model_cards.py
    重复运行会全删后重建（crud.upsert 备份此行为）

总规模：45 张 cards / 46 行（Kimi-K2.6 跨 LLM + VLM 2 榜 → 2 行）

覆盖厂商（5 家 + 扩展）:
- 优先级高: DeepSeek / Qwen / GLM / Kimi / MiniMax / 字节 Doubao / 腾讯 Hy+Hunyuan / 蚂蚁 Ring / 商汤 SenseNova / 阶跃 StepFun
- 优先级低: 移动 JT / 电信 TeleMM / 云从 CongRong / Ovis / 上海 AI Lab InternVL / 面壁 MiniCPM / 百应 Bailing
- 排除: 小米 MiMo（用户偏好不上小米）

数据来源（2026-07-25 playwright 抓取 4 榜 top 20-30）:
- LLM 官方 2026-05-13  https://rank.opencompass.org.cn/leaderboard/llm?class=official
- LLM 开源 2026-03-09  https://rank.opencompass.org.cn/leaderboard/llm?class=open_source
- VLM 官方 2026-06-01  https://rank.opencompass.org.cn/leaderboard/image-vlm?class=official
- VLM 开源 2026-05-26  https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.engine import async_session_factory
from db.init_db import init_db
from db import crud
from db.models import ModelCard


# 字段：model, provider, benchmark, score (0-100), tier, source_url, notes
# benchmark 命名："OpenCompass {LLM|VLM} {官方|开源} {更新日期}"
# tier 候选：code / chinese / general / multimodal
MODEL_CARDS = [
    # ========== DeepSeek (3 张) ==========
    # V4-Pro: 1.6T 总参, 49B 激活, 1M ctx, 编程追平闭源
    ("deepseek-v4-pro", "deepseek", "OpenCompass LLM 官方 2026-05-13", 65.1, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "1.6T 参数, 49B 激活, 1M ctx"),
    # V4-Flash: 284B 总参, 13B 激活, 1M ctx, 性价比路线
    ("deepseek-v4-flash", "deepseek", "OpenCompass LLM 官方 2026-05-13", 60.4, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "284B 参数, 13B 激活, 1M ctx, 极便宜"),
    # V3.2: 上一代旗舰, 仍在服务, 671B
    ("deepseek-v3.2", "deepseek", "OpenCompass LLM 开源 2026-03-09", 75.3, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "671B 参数, 2025-12 发布, 上一代旗舰"),

    # ========== Qwen (5 张) ==========
    # 3.6-Max-Preview: 2026-04-20 预览版, 榜单上是 Qwen 最新主力
    ("qwen3.6-max-preview", "qwen", "OpenCompass LLM 官方 2026-05-13", 60.8, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "2026-04-20 预览版, 1.2T MoE"),
    # 3.7-Plus: 2026-05-20 阿里最新, VLM 官方榜 #1
    ("qwen3.7-plus", "qwen", "OpenCompass VLM 官方 2026-06-01", 72.96, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=official", "2026-05-20 发布, VLM 官方榜 #1"),
    # 3.5-397B-A17B: 397B MoE, 跨榜都有分（LLM 开源 78.4 / VLM 官方 72.3）
    # 这里只放 LLM 开源榜分数（VLM 行可后续补）
    ("qwen3.5-397b-a17b", "qwen", "OpenCompass LLM 开源 2026-03-09", 78.4, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "397B MoE, 2026-02-16, 跨 LLM+VLM 都有分"),
    # 3.7-Flash: 轻量多模态, 榜单未收录
    ("qwen3.7-flash", "qwen", "OpenCompass LLM 官方 2026-05-13", 0.0, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "未收录, 轻量多模态, 等下个榜"),
    # 3.8-Max-Preview: 2026-07 预览版, 榜单未收录
    ("qwen3.8-max-preview", "qwen", "OpenCompass LLM 官方 2026-05-13", 0.0, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "2026-07 预览版, 2.4T 参数, 榜单晚于发布日未收录"),

    # ========== GLM (6 张) ==========
    # GLM-5: 2026-02-12, 744B, LLM 开源榜 #1
    ("glm-5", "glm", "OpenCompass LLM 开源 2026-03-09", 79.0, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "744B, 2026-02-12, LLM 开源榜 #1"),
    # GLM-4.7: 2025-12-23, 355B, 替代旧 4.6 在开榜首
    ("glm-4.7", "glm", "OpenCompass LLM 开源 2026-03-09", 77.6, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "355B, 2025-12-23, 上一代旗舰仍在榜"),
    # GLM-5.1: 2026-04-08, 744B, LLM 官方榜
    ("glm-5.1", "glm", "OpenCompass LLM 官方 2026-05-13", 59.0, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "744B, 2026-04-08, 官方闭源评测"),
    # GLM-5-Turbo: 高吞吐代理变体, 未收录
    ("glm-5-turbo", "glm", "OpenCompass LLM 开源 2026-03-09", 0.0, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "未收录, 高吞吐代理变体"),
    # GLM-5.2: 2026-06-17, 753B MIT 开源, 1M ctx, 晚于所有榜单
    ("glm-5.2", "glm", "OpenCompass LLM 开源 2026-03-09", 0.0, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "753B MIT 开源, 2026-06-17 发布, 晚于榜单 3-4 个月未收录"),
    # GLM-5V-Turbo: 多模态版, VLM 官方榜
    ("glm-5v-turbo", "glm", "OpenCompass VLM 官方 2026-06-01", 62.78, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=official", "2026-04-01, 多模态版"),

    # ========== Kimi (3 model, 4 cards 因为 K2.6 跨 2 榜) ==========
    # K2.5: 2026-01-27, 1000B, LLM 开源榜主力
    ("kimi-k2.5", "kimi", "OpenCompass LLM 开源 2026-03-09", 78.2, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "1000B, 2026-01-27, 开源版"),
    # K2.6: 2026-04-20, 1T, 跨 LLM 官方 + VLM 官方 2 榜
    ("kimi-k2.6", "kimi", "OpenCompass LLM 官方 2026-05-13", 63.4, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "1T, 2026-04-20, LLM 官方榜 (Multimodal 部分 backbone 标 DeepSeek-V3)"),
    # K2.6 第 2 行: VLM 官方榜
    ("kimi-k2.6", "kimi", "OpenCompass VLM 官方 2026-06-01", 70.6, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=official", "1T, 2026-04-20, VLM 官方榜, 多模态分数"),
    # K3: 2026-07-16, 2.8T 参数, 1M ctx, 7-27 开源, 晚于所有榜单
    ("kimi-k3", "kimi", "OpenCompass LLM 开源 2026-03-09", 0.0, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "2.8T 参数, 1M ctx, 2026-07-16 发布, 7-27 完整权重开源, 晚于榜单未收录"),

    # ========== MiniMax (3 张) ==========
    # M2.7: 2026-03-18, 230B, LLM 官方榜
    ("MiniMax-M2.7", "minimax", "OpenCompass LLM 官方 2026-05-13", 53.7, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "230B, 2026-03-18, 自我进化 Agent 强"),
    # M2.7-Highspeed: 极速版, 榜单未收录
    ("MiniMax-M2.7-highspeed", "minimax", "OpenCompass LLM 官方 2026-05-13", 0.0, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "未收录, TPS 100 极速版"),
    # M3: 2026-06-01, 196B, 1M ctx 多模态, VLM 官方榜
    ("MiniMax-M3", "minimax", "OpenCompass VLM 官方 2026-06-01", 51.6, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=official", "196B, 2026-06-01, 1M ctx, 原生多模态"),

    # ========== 字节 Doubao (4 张) ==========
    # Doubao-Seed-2-0-Pro (High): LLM 官方 #3, 火山方舟头部
    ("Doubao-Seed-2-0-Pro", "doubao", "OpenCompass LLM 官方 2026-05-13", 63.5, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "2026-02-15, 字节旗舰, 高 quality"),
    # Doubao-Seed-2-0-Lite: LLM 官方 #9
    ("Doubao-Seed-2-0-Lite", "doubao", "OpenCompass LLM 官方 2026-05-13", 60.4, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "2026-02-15, 轻量版"),
    # Doubao-Seed-1.6-Thinking-250615: LLM 开源 #29
    ("Doubao-Seed-1.6-Thinking", "doubao", "OpenCompass LLM 开源 2026-03-09", 66.0, "code", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "2025-06-15, 思考链版本"),
    # Doubao-1.5-Pro: VLM 开源 #18
    ("Doubao-1.5-Pro", "doubao", "OpenCompass VLM 开源 2026-05-26", 69.36, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source", "2025 上半年发布, 多模态"),

    # ========== 腾讯 (2 张) ==========
    # Hy3-Preview (High): 腾讯混元 LLM 官方 #13
    ("Hy3-Preview", "tencent", "OpenCompass LLM 官方 2026-05-13", 56.5, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "295B, 2026-04-23, 腾讯混元预览版"),
    # HunYuan-Standard-Vision: VLM 开源 #19
    ("HunYuan-Standard-Vision", "tencent", "OpenCompass VLM 开源 2026-05-26", 69.5, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source", "腾讯混元, 多模态"),

    # ========== 蚂蚁 Ring (1 张) ==========
    # Ring-2.5-1T: LLM 官方 #14
    ("Ring-2.5-1T", "ant", "OpenCompass LLM 官方 2026-05-13", 54.5, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "1T 参数, 2026-02-13, 蚂蚁集团"),

    # ========== 商汤 SenseNova (2 张) ==========
    # SenseNova-V6-5-Pro: VLM 开源 #1 国产第一
    ("SenseNova-V6-5-Pro", "sensetime", "OpenCompass VLM 开源 2026-05-26", 82.2, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source", "2025-09-03, VLM 开源榜国产第一"),
    # SenseNova-6-7-Flash-Alpha: VLM 官方 #6
    ("SenseNova-6-7-Flash-Alpha", "sensetime", "OpenCompass VLM 官方 2026-06-01", 68.99, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=official", "2026-05-28, Flash 极速版"),

    # ========== 阶跃 StepFun (3 张, 跨榜) ==========
    # Step-3.5-Flash: LLM 官方 52.9 + LLM 开源 76.9（2 行）
    ("Step-3.5-Flash", "stepfun", "OpenCompass LLM 官方 2026-05-13", 52.9, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=official", "196B, 2026-02-02, 阶跃主力"),
    ("Step-3.5-Flash", "stepfun", "OpenCompass LLM 开源 2026-03-09", 76.9, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "开源榜 #7, 阶跃"),
    # Step-3.7-Flash (High): VLM 官方 #8
    ("Step-3.7-Flash", "stepfun", "OpenCompass VLM 官方 2026-06-01", 67.67, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=official", "2026-04-01, 多模态升级版"),
    # Step-1o: VLM 开源 #14
    ("Step-1o", "stepfun", "OpenCompass VLM 开源 2026-05-26", 72.5, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source", "Step 多模态 1o 代"),

    # ========== 优先级低 (7 张) — 用户说"小众"放最后 ==========
    # 移动 JT (2 张)
    ("JT-Think", "jt", "OpenCompass LLM 开源 2026-03-09", 67.2, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "中国移动, 思考链版本"),
    ("JT-VL-Chat-Mini", "jt", "OpenCompass VLM 开源 2026-05-26", 66.0, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source", "中国移动, 轻量多模态"),
    # 电信 TeleMM (2 张)
    ("TeleChat-Thinking", "chinatelecom", "OpenCompass LLM 开源 2026-03-09", 71.8, "general", "https://rank.opencompass.org.cn/leaderboard/llm?class=open_source", "中国电信, 思考链"),
    ("TeleMM-2.0-Thinking", "chinatelecom", "OpenCompass VLM 开源 2026-05-26", 81.1, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source", "中国电信, 多模态 VLM 开源 #2"),
    # 云从 CongRong (1 张)
    ("CongRong-V2.0", "cloudwalk", "OpenCompass VLM 开源 2026-05-26", 80.7, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source", "云从, VLM 开源 #3"),
    # Ovis (1 张, top 1)
    ("Ovis2.6-80B-A3B", "ovis", "OpenCompass VLM 官方 2026-06-01", 67.0, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=official", "80B MoE, 2026-05, Ovis 多模态"),
    # 上海 AI Lab / InternVL (1 张, top 1)
    ("InternVL3-78B", "internvl", "OpenCompass VLM 开源 2026-05-26", 73.0, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source", "78B, VLM 开源 #7, 上海 AI Lab"),
    # 面壁 MiniCPM (1 张, top 1)
    ("MiniCPM-O-4.5", "minicpm", "OpenCompass VLM 开源 2026-05-26", 72.0, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source", "面壁智能, 多模态端侧"),
    # 百应 Bailing (1 张)
    ("BailingMM-Pro-0120", "bailing", "OpenCompass VLM 开源 2026-05-26", 68.0, "multimodal", "https://rank.opencompass.org.cn/leaderboard/image-vlm?class=open_source", "百应, 80B, VLM 开源 #25"),
]


async def seed():
    await init_db()

    async with async_session_factory() as session:
        # 清旧（演示用，生产保留历史）
        from sqlalchemy import delete, select
        await session.execute(delete(ModelCard))
        await session.commit()

        count = 0
        for model, provider, benchmark, score, tier, source_url, notes in MODEL_CARDS:
            card = ModelCard(
                model=model, provider=provider, benchmark=benchmark,
                score=score, tier=tier, source_url=source_url, notes=notes,
            )
            session.add(card)
            count += 1
        await session.commit()

        # 显示
        cards = list((await session.execute(
            select(ModelCard).order_by(ModelCard.provider, ModelCard.model, ModelCard.benchmark)
        )).scalars().all())
        print(f"\n=== Seeded {count} model cards ===")
        prev_provider = None
        for c in cards:
            if c.provider != prev_provider:
                print(f"\n  [{c.provider}]")
                prev_provider = c.provider
            score_str = f"{c.score:5.1f}" if c.score > 0 else "  0.0"
            print(f"    {c.model:30s}  {score_str}  {c.tier:10s}  {c.benchmark}")


if __name__ == "__main__":
    asyncio.run(seed())
