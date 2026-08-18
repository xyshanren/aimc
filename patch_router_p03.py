"""P0-3: patch router.py."""
import sys

path = "/home/lxc/aimc/gateway/router.py"
content = open(path).read()

# 1. select_channel 加 messages 参数
old1 = '''    def select_channel(self, model: str, prefs: Optional[dict] = None) -> Optional[dict]:
        """Phase 2.4 入口: 按 model 字符串分发

        - "tier:xxx" / "scene:xxx" → _select_by_group (组路由 + fallback chain)
        - 其他 → select_best_channel (精确模型路由, 现有行为不变)

        Args:
            model: 请求的 model 字符串
            prefs: Phase 2.6 Agent 偏好, 3 个字段:
                - prefer_family: 同价时优先的 model family (e.g. "deepseek")
                - exclude_providers: 排除的 channel provider 列表 (e.g. ["local"])
                - max_input_price: 价格上限 (元/百万 tokens, 超过的排除)
              只对组路由生效, 精确模型路由 Phase 2 不实现偏好

        返回的 channel 字典会带额外字段 (组路由时注入):
        - _actual_model: 实际命中的精确 model_id
        - _group_id: 命中的 model group (e.g. "tier:balanced")
        - _registry_tier: 命中 model 的能力档

        P0-4 (2026-08-14): stale check 由 caller (gateway/routes.py) 显式 await
        _auto_refresh_if_stale, 避免 sync 入口处 fire-and-forget 引入 race condition。
        """
        if model.startswith("tier:") or model.startswith("scene:"):
            return self._select_by_group(model, prefs=prefs)
        # 精确模型路由 Phase 2.6 不实现偏好 (跟 design §4.5 一致)
        return self.select_best_channel(model)'''

new1 = '''    def select_channel(self, model: str, prefs: Optional[dict] = None,
                      messages: Optional[list] = None) -> Optional[dict]:
        """Phase 2.4 入口: 按 model 字符串分发

        - "tier:xxx" / "scene:xxx" → _select_by_group (组路由 + fallback chain)
        - 其他 → select_best_channel (精确模型路由, 现有行为不变)

        Args:
            model: 请求的 model 字符串
            prefs: Phase 2.6 Agent 偏好
            messages: P0-3 (2026-08-14) — 用于判断是否含 multimodal content

        P0-4 (2026-08-14): stale check 由 caller 显式 await
        """
        if model.startswith("tier:") or model.startswith("scene:"):
            return self._select_by_group(model, prefs=prefs, messages=messages)
        return self.select_best_channel(model, messages=messages)'''

if old1 not in content:
    print("ERROR: old1 not found")
    sys.exit(1)
content = content.replace(old1, new1, 1)

# 2. multimodal 跳过逻辑
old2 = '''                # 组过滤: require_vision
                if group.require_vision and registry.modality != "vision":
                    logger.debug(
                        f"  [{current}] {model_id} modality={registry.modality} "
                        f"!= vision, 跳过"
                    )
                    skipped += 1
                    continue

                # 跑精确模型路由 (复用 select_best_channel)
                best = self.select_best_channel(model_id)'''

new2 = '''                # 组过滤: require_vision
                if group.require_vision and registry.modality != "vision":
                    logger.debug(
                        f"  [{current}] {model_id} modality={registry.modality} "
                        f"!= vision, 跳过"
                    )
                    skipped += 1
                    continue

                # P0-3 (2026-08-14): 纯文本请求跳过 multimodal candidate
                # 背景: multimodal model (modality=multimodal) 拒纯文本 → 上游 400
                # 修复: messages 不含 image/audio/file 时, 跳过 multimodal member
                # 例外: tier:vision 组用户显式选 multimodal (但仍可走 text model fallback)
                has_mm = _has_multimodal_content(messages)
                if not has_mm and registry.modality == "multimodal":
                    logger.debug(
                        f"  [{current}] {model_id} modality=multimodal 但请求纯文本, 跳过"
                    )
                    skipped += 1
                    continue

                # 跑精确模型路由 (复用 select_best_channel)
                best = self.select_best_channel(model_id, messages=messages)'''

if old2 not in content:
    print("ERROR: old2 not found")
    sys.exit(1)
content = content.replace(old2, new2, 1)

open(path, "w").write(content)
print("OK")
