"""Seed ModelRegistry 表 (Phase 2.2, 2026-07-29)

存模型的 **路由元信息**（tier / capabilities / context_length / modality），
跟 model_cards（benchmark 引用分）双轨，model_id 字符串 soft join。

设计：
- tier/capabilities 是路由"硬"字段（决策依据）
- benchmark_scores 仅 Dashboard 展示用（Phase 2.9 才用），不参与路由
- capabilities 是 JSON 字符串，应用层 json.loads
- model_id 规范: {family}-{variant} 或 {family}<版本数字>{-variant}，
  如 deepseek-chat / qwen-plus / qwen3-vl-32b-instruct / kimi-k3 (V6.x 放宽)

来源（2026-07-29）：
- 已配 channel 实际支持的 model（直连 + 硅基流动）
- architecture.md §九 预置模型组配置
- 实际能力定位：tier 反映"在该 family 内的能力档"

用法：
    python scripts/seed_model_registry.py
    重复运行会先清后插 (idempotent, 跟 seed_model_cards.py 风格一致)

规模：30 个 model (P2-A 补充 5 个 vision: glm-4.5v + 4 qwen3-vl-*)
覆盖厂商：deepseek / qwen / glm / kimi / doubao / hunyuan / stepfun / MiniMax / llama
覆盖 tier：T0 / T1 / T2 / T3
覆盖 capabilities：reasoning / code / math / general / long_context / agent / vision
覆盖 modality：text / vision / multimodal
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import delete, select

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import ModelRegistry
from db.normalizers import (
    normalize_capabilities,
    normalize_family,
    normalize_model_id,
    validate_model_id,
)


# 字段：model_id, family, display_name, tier, capabilities (list), context_length,
#       modality, is_open_source, release_date, notes
MODEL_REGISTRY = [
    # ========== DeepSeek (3 个, V4 系列) ==========
    # Phase 2a+ fix (2026-08-13): 官方 API 当前只支持 V4 系列
    # (deepseek-v4-flash 升级到 V4-Flash-0731, deepseek-v4-pro 升级到 V4-Pro-0813)
    # 历史 model 名 (deepseek-chat / deepseek-reasoner / deepseek-coder / deepseek-v2.5)
    # 已不在官方 API 中, 调用会 404
    # V6.14 (2026-09-27): 补 deepseek-flash — 生产 V4.1 迁移时入库 (T0/multimodal/1M,
    #   官方 V4.1-Flash) 但 seed 漏记; 9/27 起为 tier:flagship 首位成员, 补行对齐
    (
        "deepseek-flash", "deepseek", "DeepSeek V4.1 Flash", "T0",
        ["reasoning", "code", "general", "long_context"], 1000000, "multimodal", 0, "2026-09",
        "官方 V4.1-Flash, 多模态, 1M ctx; 官方价 1/4 (谷) 2/8 (峰) cache 0.02/0.04; "
        "modelscope/sensenova 0 元 + ch1 分时段付费; tier:flagship 首位",
    ),
    (
        "deepseek-v4-pro", "deepseek", "DeepSeek V4 Pro", "T0",
        ["reasoning", "math", "code", "long_context"], 128000, "text", 0, "2026-08",
        "V4 旗舰, 升级到 V4-Pro-0813, 支持 thinking",
    ),
    (
        "deepseek-v4-flash", "deepseek", "DeepSeek V4 Flash", "T1",
        ["reasoning", "code", "general"], 128000, "text", 0, "2026-07",
        "V4 Flash 升级到 V4-Flash-0731, 性价比",
    ),
    # V6.0 回填: 这两个一直在 tier:code/balanced 组里 (V4.9.5 时代经生产迁移
    # 入组), 但 seed 文件漏了 — 全新环境 reseed 会因 registry 缺失跳过它们
    (
        "deepseek-v3", "deepseek", "DeepSeek V3", "T2",
        ["general"], 128000, "text", 0, "2026-06",
        "V3 硅基流动常驻, 低价付费, tier:balanced 主力",
    ),
    (
        "deepseek-v3.2", "deepseek", "DeepSeek V3.2", "T2",
        ["reasoning", "code", "general"], 128000, "text", 0, "2026-07",
        "V3.2 代码向, tier:code 成员",
    ),
    # V4.9.5 (2026-08-25): DeepSeek 视觉实验版, 官方已上线
    # 价格同 v4-flash, 图片转 token 后与文本合并计费
    # modality=vision, tier=T1, 加入 tier:vision 组
    (
        "deepseek-v4-flash-vision-exp", "deepseek", "DeepSeek V4 Flash Vision Exp", "T1",
        ["vision", "reasoning", "code", "general"], 128000, "vision", 0, "2026-08",
        "V4 Flash 视觉实验版, 图片转 token 计费, 价格同 v4-flash",
    ),

    # ========== Qwen (6 个) ==========
    (
        "qwen-max", "qwen", "Qwen Max", "T1",
        ["reasoning", "code", "general"], 128000, "text", 0, "2025-04",
        "通义主力旗舰"
    ),
    (
        "qwen-plus", "qwen", "Qwen Plus", "T2",
        ["general"], 128000, "text", 0, "2025-04",
        "通义主力, 性价比"
    ),
    (
        "qwen-turbo", "qwen", "Qwen Turbo", "T3",
        ["general"], 128000, "text", 0, "2025-04",
        "通义极速版, 便宜"
    ),
    (
        "qwen-2.5-72b", "qwen", "Qwen 2.5 72B", "T1",
        ["reasoning", "code", "general"], 128000, "text", 1, "2024-09",
        "Qwen 2.5 72B 开源旗舰, 硅基流动常驻"
    ),
    (
        "qwen-2.5-32b", "qwen", "Qwen 2.5 32B", "T2",
        ["general"], 128000, "text", 1, "2024-09",
        "Qwen 2.5 32B 开源, 性价比"
    ),
    (
        "qwen-2.5-7b", "qwen", "Qwen 2.5 7B", "T3",
        ["general"], 32000, "text", 1, "2024-09",
        "Qwen 2.5 7B 轻量"
    ),
    # V6.0 回填: tier:light 成员 (V4.6.4 起在组), seed 漏了 — 同 deepseek-v3 注释
    (
        "qwen3-8b", "qwen", "Qwen3 8B", "T3",
        ["general"], 128000, "text", 1, "2026-07",
        "Qwen3 8B 硅基流动常驻, tier:light; 命名带 family 前缀 (validate_model_id 约束)",
    ),
    (
        "qwen3.5-4b", "qwen", "Qwen3.5 4B", "T3",
        ["general"], 128000, "text", 1, "2026-08",
        "Qwen3.5 4B 硅基流动常驻, tier:light; 命名带 family 前缀 (validate_model_id 约束)",
    ),

    # ========== Qwen VL 系列 (4 个, P2-A 补充) ==========
    # 硅基流动常驻, tier:vision-light 组成员
    # V6.x: model_id 用规范名 "qwen3-vl-" — validate_model_id 已放宽为接受
    # "{family}<版本数字>", 不再需要丢版本号的 qwen-vl- workaround
    (
        "qwen3-vl-32b-instruct", "qwen", "Qwen3 VL 32B Instruct", "T1",
        ["vision", "general"], 32768, "vision", 1, "2026-08",
        "Qwen3 VL 32B 指令版, 硅基流动常驻",
    ),
    (
        "qwen3-vl-32b-thinking", "qwen", "Qwen3 VL 32B Thinking", "T1",
        ["reasoning", "vision"], 32768, "vision", 1, "2026-08",
        "Qwen3 VL 32B 推理版",
    ),
    (
        "qwen3-vl-30b-a3b-thinking", "qwen", "Qwen3 VL 30B A3B Thinking", "T1",
        ["reasoning", "vision"], 32768, "vision", 1, "2026-08",
        "Qwen3 VL 30B MoE 推理版",
    ),
    (
        "qwen3-vl-8b-instruct", "qwen", "Qwen3 VL 8B Instruct", "T3",
        ["vision", "general"], 32768, "vision", 1, "2026-08",
        "Qwen3 VL 8B 轻量视觉",
    ),

    # ========== GLM (5 个) ==========
    (
        "glm-4-plus", "glm", "GLM-4 Plus", "T1",
        ["reasoning", "code", "general"], 128000, "text", 0, "2025-01",
        "智谱旗舰闭源, 主力"
    ),
    (
        "glm-4-air", "glm", "GLM-4 Air", "T2",
        ["general"], 128000, "text", 0, "2024-12",
        "智谱性价比"
    ),
    (
        "glm-4-flash", "glm", "GLM-4 Flash", "T3",
        ["general", "agent"], 128000, "text", 0, "2024-12",
        "智谱极速, Agent 调优"
    ),
    (
        "glm-4-9b", "glm", "GLM-4 9B", "T3",
        ["general"], 32000, "text", 1, "2024-06",
        "智谱开源轻量, 硅基流动常驻"
    ),
    # P2-A: 补充 GLM 视觉模型 (硅基流动 zai-org/GLM-4.5V)
    (
        "glm-4.5v", "glm", "GLM-4.5V", "T1",
        ["reasoning", "vision", "general"], 128000, "vision", 0, "2026-08",
        "智谱视觉多模态, 硅基流动常驻",
    ),
    # V4.9.7: bigmodel coding plan Lite 官方渠道 (仅支持 GLM-5.3/GLM-5.3-Flash)
    (
        "glm-5.3", "glm", "GLM-5.3", "T1",
        ["reasoning", "code", "general"], 128000, "text", 0, "2026-08",
        "智谱新一代旗舰, coding plan Lite; 调用 GLM-5.2/5.1 自动切换至本模型",
    ),
    (
        # V6.0 (2026-09-03): modality text → multimodal — 定价页输入模态为
        # 图片/视频/文件/文本; 进 tier:vision 补位旗舰 (require_vision 只认
        # vision/multimodal)。P0-3 已收窄为仅 require_vision 组内跳过,
        # strong/code 的纯文本角色不受影响 (docs/next-major-v6-plan.md §1.3)
        # 2026-10-15: T2 → T1 (用户拍板: 5.3 系新一代不应低于 glm-5.2 的 T1)
        "glm-5.3-flash", "glm", "GLM-5.3 Flash", "T1",
        ["general", "code"], 128000, "multimodal", 0, "2026-08",
        "GLM-5.3 极速版, coding plan Lite; 多模态; 调用 GLM-5-Turbo/4.7 自动切换至本模型",
    ),
    # V6.0 (2026-09-03): bigmodel 免费模型 (定价页确认 + VM 生产 key 实测)
    # glm-4.7-flash: 文本免费, 200K ctx → tier:light 免费默认
    # 实测: coding plan 端点识别该 ID 但持续 1305 过载 (无静默切换;
    # 自动切换规则只影响不带 Flash 的 glm-4.7 → glm-5.3-flash)
    (
        "glm-4.7-flash", "glm", "GLM-4.7 Flash", "T3",
        ["general", "code"], 200000, "text", 0, "2026-09",
        "GLM-4.7 极速版, bigmodel 免费 (输入/输出/缓存全免费); 常态 1305 过载, 靠 failover 兜底",
    ),
    # glm-4.6v-flash: 视觉免费, 128K ctx → tier:vision-light 免费视觉兜底
    # 实测: coding/paas/v4 200 OK, model 回显自身 (无静默切换)
    (
        "glm-4.6v-flash", "glm", "GLM-4.6V Flash", "T3",
        ["vision", "general"], 131072, "vision", 0, "2026-09",
        "GLM-4.6V 极速视觉, bigmodel 免费; 接受图片/视频/文件/文本; "
        "注意与付费 GLM-4.6V (1/3元) 是不同模型",
    ),
    # V6.0 回填: tier:strong 成员 (V4.9.5 经生产迁移入组), seed 漏了
    (
        "glm-5.2", "glm", "GLM-5.2", "T1",
        ["reasoning", "code", "general"], 128000, "text", 0, "2026-07",
        "上一代旗舰, sensenova 免费第二渠道主车型 (官方标称 1M ctx)",
    ),

    # ========== Kimi (2 个) ==========
    (
        "kimi-k2", "kimi", "Kimi K2", "T1",
        ["reasoning", "long_context", "general"], 200000, "text", 0, "2025-07",
        "月之暗面 K2 闭源, 200k 长上下文"
    ),
    (
        "kimi-k3", "kimi", "Kimi K3", "T0",
        ["reasoning", "long_context", "code"], 1000000, "text", 0, "2026-07",
        "K3 最新旗舰, 1M ctx, 2026-07-27 开源权重"
    ),

    # ========== Moonshot (1 个) ==========
    # Phase 4 v4.3 fix (2026-08-13): family 从 kimi 改为 moonshot
    # 让 model_id namespace 与 family 一致 (validate_model_id 规则)
    (
        "moonshot-v1-8k", "moonshot", "Moonshot V1 8K", "T3",
        ["general"], 8000, "text", 0, "2024-08",
        "Moonshot 短上下文, 跟 Kimi K2/K3 同公司但 API 系列不同"
    ),

    # ========== Doubao (2 个) ==========
    (
        "doubao-pro-32k", "doubao", "Doubao Pro 32K", "T1",
        ["reasoning", "general"], 32000, "text", 0, "2025-02",
        "字节豆包旗舰"
    ),
    (
        "doubao-lite-32k", "doubao", "Doubao Lite 32K", "T3",
        ["general"], 32000, "text", 0, "2025-02",
        "字节豆包轻量"
    ),

    # ========== Hunyuan (2 个) ==========
    (
        "hunyuan-pro", "hunyuan", "Hunyuan Pro", "T1",
        ["reasoning", "code", "general"], 32000, "text", 0, "2025-04",
        "腾讯混元旗舰"
    ),
    (
        "hunyuan-standard", "hunyuan", "Hunyuan Standard", "T2",
        ["general"], 32000, "text", 0, "2025-04",
        "腾讯混元标准"
    ),

    # ========== Step (1 个, 阶跃星辰 step-* 系列) ==========
    # Phase 4 v4.3 fix (2026-08-13): family 从 stepfun 改为 step
    # 让 model_id namespace 与 family 一致 (validate_model_id 规则)
    # channel.name 仍叫 stepfun_official (跟 family 解耦)
    (
        "step-1-8k", "step", "Step-1 8K", "T2",
        ["general"], 8000, "text", 0, "2024-10",
        "阶跃星辰基础"
    ),
    (
        # V6.11 (2026-09-20): stepfun 新旗舰, 取代 step-1o (下线)
        # 规格: https://platform.stepfun.com/docs/zh/guides/models/step-5-preview
        "step-5-preview", "step", "Step-5 Preview", "T1",
        ["reasoning", "code", "long_context", "general"], 1000000, "multimodal", 0, "2026-09",
        "阶跃旗舰 preview: 1M ctx, 文本+图像+视频输入, tools/JSON mode/思考档位 low-medium-high; "
        "标价 7/20 元每百万tokens (缓存命中 0.35)"
    ),

    # ========== MiniMax (9 个; minimax_compat 渠道侧 2026-10-19 起收敛为 2 规范行 = m3/m3.1-flash) ==========
    # Phase 4 fix (2026-08-13): 修复 1005111 commit 的家族错乱
    # - 修复前错误状态: 5 个 model + family="MiniMax" 大写 + 注释指向 sensenova_official (错误)
    # - sensenova_official 渠道实际一个 MiniMax model 都没有, 跟 family 字段无关
    # - 对齐来源 (1): MiniMax 官方 API 文档 https://platform.minimaxi.com/docs/api-reference/api-overview (2026-08 现行)
    # - 对齐来源 (2): db 中 minimax_compat 渠道 price_snapshots
    # - V6.14 (2026-09-27): + m3.1-flash-preview; m2/m2.1/m2.1-highspeed/m2.5/m2.5-highspeed
    #   渠道价行下线 (registry 条目保留: m2.5 仍有 siliconflow 规范名兜底行, 其余 4 个防历史 usage_logs 关联)
    # - 字段约定: family 统一小写 "minimax" (跟 provider / channel.name 对齐, 与其他 family 一致)
    (
        # 2026-10-19 GA (用户拍板): 去 preview 后缀正式发布 — 早期以 Space Bunny
        # 匿名身份灰度 (200T token), 官方公布推理提速近 3 倍 (日常 200+ TPS, P50
        # 170+), Agentic 综合成本降 70%; 官方标价 1/4 cache 0.1 元每百万tokens
        # (list_* 双轨回填由 2026_10_19 迁移执行); tier T1 不变, vision(90) →
        # strong(100) 首位, 待用一段再评估升组
        # ⚠️ status 必须保持 active: 路由缓存只拉 active (2026-09-27 e2e 实证,
        # 非 active 会被组路由当"不在注册表"跳过)
        "minimax-m3.1-flash", "minimax", "MiniMax M3.1 Flash", "T1",
        ["reasoning", "code", "general"], 1000000, "multimodal", 0, "2026-09",
        "M3.1 Flash 正式版 (2026-10-19 GA, 原 m3.1-flash-preview), 多模态, 1M ctx; "
        "官方标价 1/4 cache 0.1 元每百万tokens; tier:strong 成员 (priority=100 首位)",
    ),
    (
        # V5.1 (2026-09-02): T1 → T2 人工降档 — 能力低于 glm-5.3-flash (T2) 和
        # deepseek-v4-flash (T1); TokenPlanMax 年度额度充足, 定位为 balanced 容量兜底
        # V6.14 (2026-09-27): modality text → multimodal 对齐生产 (2026-09-08 入
        # tier:vision 时已改, 2026-09-18 拍板规则不变; 纯文本请求 P0-3 跳过属预期)
        "minimax-m3", "minimax", "MiniMax M3", "T2",
        ["reasoning", "code", "long_context", "general"], 1000000, "multimodal", 0, "2026-08",
        "1M ctx, 原生多模态; tier:vision + tier:balanced 成员; "
        "纯文本请求 P0-3 跳过属预期 (multimodal)",
    ),
    (
        "minimax-m2.7", "minimax", "MiniMax M2.7", "T1",
        ["reasoning", "code", "general"], 204800, "text", 0, "2026-08",
        "M2.7 自我迭代, 60tps",
    ),
    (
        "minimax-m2.7-highspeed", "minimax", "MiniMax M2.7 Highspeed", "T2",
        ["reasoning", "general"], 204800, "text", 0, "2026-08",
        "M2.7 极速版, 100tps",
    ),
    (
        "minimax-m2.5", "minimax", "MiniMax M2.5", "T2",
        ["general"], 204800, "text", 0, "2026-08",
        "M2.5 极致性价比, 60tps",
    ),
    (
        "minimax-m2.5-highspeed", "minimax", "MiniMax M2.5 Highspeed", "T3",
        ["general"], 204800, "text", 0, "2026-08",
        "M2.5 极速版, 100tps",
    ),
    (
        "minimax-m2.1", "minimax", "MiniMax M2.1", "T2",
        ["code", "general"], 204800, "text", 0, "2026-08",
        "M2.1 多语言编程, 60tps",
    ),
    (
        "minimax-m2.1-highspeed", "minimax", "MiniMax M2.1 Highspeed", "T3",
        ["general"], 204800, "text", 0, "2026-08",
        "M2.1 极速版, 100tps",
    ),
    (
        "minimax-m2", "minimax", "MiniMax M2", "T2",
        ["code", "general"], 204800, "text", 0, "2026-08",
        "M2 高效编码/Agent 工作流",
    ),

    # ========== SenseNova (1 个, 商汤日日新公测) ==========
    # sensenova-6.8-flash-lite: 256K ctx, 支持 tools/json_mode/vision input
    # API 接受纯文本, modality=text 避免 P0-3 纯文本跳过
    # price=0.0 (公测免费), sensenova_official 渠道常驻
    (
        "sensenova-6.8-flash-lite", "sensenova", "SenseNova 6.8 Flash Lite", "T2",
        ["reasoning", "general"], 262144, "text", 0, "2026-08",
        "商汤日日新公测, 256K ctx, 支持 tools/vision, price=0.0 (公测免费)",
    ),

    # ========== Llama (2 个) ==========
    (
        "llama-3.1-70b", "llama", "Llama 3.1 70B", "T2",
        ["general"], 128000, "text", 1, "2024-07",
        "Meta 开源旗舰, 硅基流动常驻"
    ),
    (
        "llama-3.1-8b", "llama", "Llama 3.1 8B", "T3",
        ["general"], 128000, "text", 1, "2024-07",
        "Meta 轻量"
    ),
]


async def seed():
    await init_db()

    async with async_session_factory() as session:
        # 幂等: 先清再插 (跟 seed_model_cards.py 风格一致)
        await session.execute(delete(ModelRegistry))
        await session.commit()

        count = 0
        for (
            model_id, family, display_name, tier,
            capabilities, context_length, modality,
            is_open_source, release_date, notes,
        ) in MODEL_REGISTRY:
            # Phase 4 v4.3 (2026-08-13): 应用层归一化 + assert
            # 防止 commit 1005111 历史问题: family 大写 / model_id 不规范
            family_norm = normalize_family(family)
            model_id_norm = normalize_model_id(model_id)
            assert family_norm == family, (
                f"MODEL_REGISTRY 元组里 family='{family}' 应已归一化为 '{family_norm}'"
            )
            assert model_id_norm == model_id, (
                f"MODEL_REGISTRY 元组里 model_id='{model_id}' 应已归一化为 '{model_id_norm}'"
            )
            assert validate_model_id(model_id_norm, family_norm), (
                f"model_id='{model_id}' 不符合 '{family_norm}-' namespace 规范"
            )
            capabilities_norm = normalize_capabilities(capabilities)

            entry = ModelRegistry(
                model_id=model_id_norm,
                family=family_norm,
                display_name=display_name,
                tier=tier,
                capabilities=json.dumps(capabilities_norm, ensure_ascii=False),
                context_length=context_length,
                modality=modality,
                is_open_source=is_open_source,
                release_date=release_date,
                status="active",
                notes=notes,
            )
            session.add(entry)
            count += 1
        await session.commit()

        # 按 family + tier 分组展示
        rows = list((await session.execute(
            select(ModelRegistry).order_by(
                ModelRegistry.family, ModelRegistry.tier, ModelRegistry.model_id
            )
        )).scalars().all())
        print(f"\n=== Seeded {count} model registry entries ===")
        prev_family = None
        for r in rows:
            if r.family != prev_family:
                print(f"\n  [{r.family}]")
                prev_family = r.family
            caps = json.loads(r.capabilities) if r.capabilities else []
            ctx = f"{r.context_length // 1000}k" if r.context_length else "?"
            print(
                f"    {r.model_id:25s}  {r.tier}  ctx={ctx:5s}  "
                f"open={r.is_open_source}  caps={caps}"
            )

        # 统计
        print("\n=== 统计 ===")
        from collections import Counter
        tier_count = Counter(r.tier for r in rows)
        family_count = Counter(r.family for r in rows)
        print(f"  按 tier: {dict(tier_count)}")
        print(f"  按 family: {dict(family_count)}")


if __name__ == "__main__":
    asyncio.run(seed())
