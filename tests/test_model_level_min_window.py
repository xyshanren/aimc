"""模型级体量准入测试 (2026-11-04)

背景: taotoken_coding 渠道内两个模型的供给生态不同 —
- glm-5.3-flash: tier:strong 用它, 但该组有 minimax-m3.1-flash 顶包 → 渠道级
  30000 门槛合适 (小请求烧按次计费的次数不划算);
- deepseek-flash: tier:flagship 位次第一的主力位, 也是 vision 次选, 而免费面
  sensenova 93% 失败 → 渠道级门槛把它锁在门外, 流量落付费 deepseek_official
  (7d ¥27.17)。故加模型级覆盖 {渠道: {模型: min}}, 优先于渠道级。

测试隔离注意 (家规): settings 用收集期绑定的同一对象 (本文件顶层 from config
import settings), fixture 内 monkeypatch 变更/自动还原 — 与 test_v66x 同款。
隔离 rate_limiter/cooldown_manager 两个全局单例 (账号级状态会被其他测试文件
污染, 假账号 id 撞限流会让 sensenova 候选消失 → "全部候选窗外保留原候选"
兜底让门槛看起来失效)。

跑法: python -m pytest tests/test_model_level_min_window.py -v
"""
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from config import settings
from gateway.router import RouteEngine

# 每次运行随机分配的账号 id 段 — 避免撞上全局 limiter/cooldown 单例里
# 其他测试文件留下的账号状态 (split-singleton 场景下 monkeypatch 钉不住)
_RUN_ACCOUNT_BASE = random.randint(800000, 900000) * 100


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(settings, "channel_min_input_tokens",
                        {"taotoken_coding": 30000}, raising=False)
    monkeypatch.setattr(settings, "channel_max_input_tokens", {}, raising=False)
    monkeypatch.setattr(settings, "channel_model_min_input_tokens",
                        {"taotoken_coding": {"deepseek-flash": 7000}}, raising=False)
    from gateway.limiter import rate_limiter
    from gateway.cooldown import cooldown_manager
    monkeypatch.setattr(rate_limiter, "is_limited",
                        lambda *a, **k: False, raising=False)
    monkeypatch.setattr(cooldown_manager, "is_in_cooldown",
                        lambda *a, **k: False, raising=False)
    return RouteEngine()


def _ch(name, priority, models):
    return {
        "id": abs(hash(name)) % 10000, "name": name,
        "account_id": _RUN_ACCOUNT_BASE + (abs(hash(name)) % 50),
        "account_name": name + "_01", "priority": priority,
        "status": "active", "quotas": [],
        "quota_remaining": None, "quota_total": None,
        "billing_mode": "subscription", "provider_type": "official",
        "base_url": "https://mock.test/v1",
        "prices": [{"model": m, "price_type": "normal", "time_range": "全天",
                    "input_price": 0.0, "output_price": 0.0,
                    "is_promotion": 0} for m in models],
    }


def _messages(est_tokens):
    """est = chars // 3 → 造出目标 est"""
    return [{"role": "user", "content": "x" * (est_tokens * 3)}]


@pytest.fixture
def wired(engine):
    engine._channels_cache = [
        _ch("taotoken_coding", 9, ["deepseek-flash", "glm-5.3-flash"]),
        _ch("sensenova_official", 8, ["deepseek-flash"]),
        _ch("glm_bigmodel", 8, ["glm-5.3-flash"]),
    ]
    return engine


class TestModelLevelMinWindow:
    def test_deepseek_flash_mid_size_goes_taotoken(self, wired):
        """deepseek-flash est 20k: 模型级 7000 → taotoken 在窗内 → 0 元走 taotoken"""
        picked = wired.select_best_channel("deepseek-flash",
                                           messages=_messages(20000))
        assert picked is not None
        assert picked["name"] == "taotoken_coding"

    def test_deepseek_flash_small_size_skips_taotoken(self, wired):
        """deepseek-flash est 5k: < 模型级 7000 → taotoken 出窗 → 落 sensenova"""
        picked = wired.select_best_channel("deepseek-flash",
                                           messages=_messages(5000))
        assert picked is not None
        assert picked["name"] != "taotoken_coding"

    def test_glm53_flash_mid_size_still_excluded(self, wired):
        """glm-5.3-flash est 20k: 无模型级覆盖 → 回落渠道级 30000 → taotoken 出窗"""
        picked = wired.select_best_channel("glm-5.3-flash",
                                           messages=_messages(20000))
        assert picked is not None
        assert picked["name"] != "taotoken_coding"

    def test_glm53_flash_big_size_goes_taotoken(self, wired):
        """glm-5.3-flash est 40k ≥ 渠道级 30000 → taotoken 在窗内"""
        picked = wired.select_best_channel("glm-5.3-flash",
                                           messages=_messages(40000))
        assert picked is not None
        assert picked["name"] == "taotoken_coding"

    def test_model_level_falls_back_to_channel_level(self, engine, monkeypatch):
        """模型级未列的模型回落渠道级 (未列 glm-5.3-flash → 30000)"""
        monkeypatch.setattr(settings, "channel_model_min_input_tokens",
                            {}, raising=False)
        engine._channels_cache = [
            _ch("taotoken_coding", 9, ["glm-5.3-flash"]),
            _ch("glm_bigmodel", 8, ["glm-5.3-flash"]),
        ]
        picked = engine.select_best_channel("glm-5.3-flash",
                                            messages=_messages(20000))
        assert picked["name"] != "taotoken_coding"

    def test_channel_level_only_legacy_config(self, engine, monkeypatch, caplog):
        """无模型级配置时行为与旧版一致 (渠道级 30000 生效)"""
        import logging
        monkeypatch.setattr(settings, "channel_model_min_input_tokens",
                            {}, raising=False)
        engine._channels_cache = [
            _ch("taotoken_coding", 9, ["deepseek-flash"]),
            _ch("sensenova_official", 8, ["deepseek-flash"]),
        ]
        with caplog.at_level(logging.DEBUG, logger="gateway.router"):
            picked = engine.select_best_channel("deepseek-flash",
                                                messages=_messages(20000))
        for r in caplog.records:
            if r.name == "gateway.router":
                print("\nLOG:", r.levelname, r.getMessage()[:110])
        print("DIAG picked:", picked["name"] if picked else None)
        print("DIAG cache:", [(c["name"], len(c.get("prices", [])))
                              for c in engine._channels_cache])
        import sys as _sys
        import gateway.router as rm_runtime
        cur_mod = _sys.modules.get("gateway.router")
        print("DIAG runtime is sys.modules entry:", rm_runtime is cur_mod)
        print("DIAG runtime module settings min:",
              getattr(rm_runtime.settings, "channel_min_input_tokens", "MISSING"))
        cls_mod_name = type(engine).__module__
        cls_mod = _sys.modules.get(cls_mod_name)
        print("DIAG engine cls module:", cls_mod_name,
              "| cls module settings min:",
              (getattr(cls_mod.settings, "channel_min_input_tokens", "MISSING")
               if cls_mod else "N/A"))
        print("DIAG cls module is runtime module:", cls_mod is rm_runtime)
        assert picked["name"] != "taotoken_coding"
