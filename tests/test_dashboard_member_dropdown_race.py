"""看板"加成员"下拉候选为空的竞态回归 (V6.17c)

背景:
  渠道页的 once 加载器并发拉 /dashboard/api/registry/models 与 /dashboard/api/registry/groups,
  实测 groups 端点恒定更快 (58ms vs 110ms) → 组卡先渲染, populateMemberDropdowns 读到的
  window._aimcRegistryModels 还是 undefined → 7 个组的"加成员"下拉全部只剩占位符;
  models 后到后没有任何代码回头重填 → 下拉永久为空 (minimax-m3.1-flash T1/multimodal
  这类完全够格的模型也选不到)。

修复: 两个渲染函数互相缓存 + 交叉重填 (谁后到谁补填):
  - renderRegistryGroups: window._aimcRegistryGroups = data.groups
  - renderRegistryModels: 设置 _aimcRegistryModels 后调 populateMemberDropdowns(_aimcRegistryGroups)

本测试按静态源码断言 (与 test_v610_asset_cache_busting 同风格: 断言前端资源的关键接线)。
"""
from pathlib import Path

APP_JS = Path(__file__).resolve().parent.parent / "dashboard" / "static" / "app.js"


def _src() -> str:
    return APP_JS.read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """截取某个顶层函数定义到下一个顶层 'function '/'const ' 之前, 避免跨函数误判."""
    start = src.index("function %s(" % name)
    rest = src[start:]
    nxt = rest.find("\nfunction ", 1)
    return rest if nxt == -1 else rest[:nxt]


def test_registry_groups_cached_for_late_models():
    body = _function_body(_src(), "renderRegistryGroups")
    assert "window._aimcRegistryGroups" in body, \
        "renderRegistryGroups 应缓存 window._aimcRegistryGroups, 供 models 后到时重填下拉"


def test_models_renderer_repopulates_dropdowns():
    body = _function_body(_src(), "renderRegistryModels")
    assert "window._aimcRegistryModels" in body, "renderRegistryModels 应写入 window._aimcRegistryModels"
    assert "populateMemberDropdowns(window._aimcRegistryGroups)" in body, \
        "models 后到时必须用缓存的 groups 数据重填加成员下拉 (竞态修复核心)"


def test_dropdown_source_is_registry_models():
    # V6.17d: 下拉候选改由服务端按组定义算好 (g.candidates, 与路由层同源),
    # 不再直接消费 window._aimcRegistryModels —— 规则单一事实源在 gateway/group_rules
    body = _function_body(_src(), "populateMemberDropdowns")
    assert "g.candidates" in body, "下拉候选应使用服务端算好的候选 (g.candidates)"
    src = _src()
    assert "window._aimcRegistryModels" in src, "registry 模型卡仍应缓存模型列表供表格渲染"
