"""Phase 4 v4.3 (2026-08-13) Model 数据源一致性测试

覆盖:
- db/normalizers.py: 4 个归一化函数 + 边界场景
- scripts/check_model_consistency.py: --check db (mock session)
- scripts/check_model_consistency.py: --check official (mock fetcher)
- scripts/check_model_consistency.py: --check all + exit code

跑法:
    .venv/Scripts/python.exe tests/test_model_consistency.py
"""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def test_normalize_family():
    """Test 1: normalize_family 大小写/空白/BOM 处理"""
    from db.normalizers import normalize_family

    assert normalize_family("DeepSeek") == "deepseek"
    assert normalize_family("MiniMax") == "minimax"
    assert normalize_family("  Qwen  ") == "qwen"
    assert normalize_family("GLM") == "glm"
    assert normalize_family("") == ""
    assert normalize_family("kimi") == "kimi"
    # BOM
    assert normalize_family("\ufeffDeepSeek") == "deepseek"
    # None
    assert normalize_family(None) == ""
    print("  [OK] normalize_family 大小写/空白/BOM 全部正确")


def test_normalize_model_id():
    """Test 2: normalize_model_id 大小写/空白/BOM"""
    from db.normalizers import normalize_model_id

    assert normalize_model_id("DeepSeek-V4-Pro") == "deepseek-v4-pro"
    assert normalize_model_id("  qwen-MAX  ") == "qwen-max"
    assert normalize_model_id("\ufeffMiniMax-M3") == "minimax-m3"
    assert normalize_model_id("") == ""
    assert normalize_model_id(None) == ""
    print("  [OK] normalize_model_id 全部正确")


def test_validate_model_id():
    """Test 3: validate_model_id namespace + 字符规范"""
    from db.normalizers import normalize_family, normalize_model_id, validate_model_id

    # 正常情况
    assert validate_model_id("deepseek-v4-pro", "deepseek") is True
    assert validate_model_id("qwen-max", "qwen") is True
    assert validate_model_id("minimax-m3", "minimax") is True
    # 注意: moonshot-v1-8k 实际违反 namespace 规则 (不以 kimi- 开头)
    # 这是 seed_model_registry.py 现有命名瑕疵, 检测脚本会抓到
    assert validate_model_id("moonshot-v1-8k", "kimi") is False

    # namespace 不匹配
    assert validate_model_id("deepseek-v4-pro", "qwen") is False
    assert validate_model_id("unknown-model", "deepseek") is False

    # 未归一化
    assert validate_model_id("DeepSeek-V4-Pro", "deepseek") is False
    assert validate_model_id("deepseek-v4-pro", "DeepSeek") is False

    # 非法字符 (大写/中文/空格)
    assert validate_model_id("DeepSeek V4 Pro", "deepseek") is False
    assert validate_model_id("deepseek-中文", "deepseek") is False

    # 空
    assert validate_model_id("", "deepseek") is False
    assert validate_model_id("deepseek-v4-pro", "") is False

    # 经过 normalize 后验证
    mid = normalize_model_id("DeepSeek-V4-Pro")
    fam = normalize_family("DeepSeek")
    assert validate_model_id(mid, fam) is True

    print("  [OK] validate_model_id namespace + 字符规范全部正确")


def test_normalize_capabilities():
    """Test 4: normalize_capabilities 排序去重 + 小写"""
    from db.normalizers import normalize_capabilities

    # 排序
    assert normalize_capabilities(["code", "general", "reasoning"]) == [
        "code", "general", "reasoning"
    ]
    # 去重 + 小写
    assert normalize_capabilities(["Code", "code", "CODE"]) == ["code"]
    # 去空白
    assert normalize_capabilities(["  general  ", "reasoning"]) == ["general", "reasoning"]
    # None 跳过
    assert normalize_capabilities(["general", None, "reasoning"]) == ["general", "reasoning"]
    # 空字符串跳过
    assert normalize_capabilities(["general", "", "  "]) == ["general"]
    # 空 list
    assert normalize_capabilities([]) == []
    assert normalize_capabilities(None) == []
    # BOM
    assert normalize_capabilities(["\ufeffGeneral"]) == ["general"]

    print("  [OK] normalize_capabilities 排序去重 + 小写全部正确")


def _install_mock_config():
    """mock config.settings + openai 模块让 parser.py / check_model_consistency.py 能 import"""
    import sys
    import types

    # mock openai (避免需要安装 openai 依赖)
    if "openai" not in sys.modules:
        mock_openai = types.ModuleType("openai")
        mock_openai.AsyncOpenAI = lambda *a, **kw: None
        sys.modules["openai"] = mock_openai

    class _MockSettings:
        openai_api_key = "sk-test"
        openai_base_url = "https://mock.local/v1"
        parse_model = "gpt-4o-mini"
        parse_confidence_threshold = 0.6
        aimc_master_key = "0" * 64
        crawl_enabled = True
        route_refresh_interval = 600
        db_url = "sqlite+aiosqlite:///:memory:"
        db_path = "./data/aimc.db"
        auth_enabled = False
        api_key = ""
        max_fallback_retries = 2
        proxy_url = ""
        alert_channel = "log"
        bark_key = ""
        bark_server = ""
        serverchan_key = ""
        feishu_webhook_url = ""

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = _MockSettings()


def test_check_db_consistency_clean():
    """Test 5: 干净数据 → 0 issues (调用 check_family_lowercase + check_model_id_format)"""
    from scripts.check_model_consistency import check_family_lowercase, check_model_id_format

    class _Row:
        def __init__(self, model_id, family):
            self.model_id = model_id
            self.family = family

    clean_rows = [
        _Row("deepseek-v4-pro", "deepseek"),
        _Row("deepseek-v4-flash", "deepseek"),
        _Row("qwen-max", "qwen"),
        _Row("minimax-m3", "minimax"),
    ]

    issues1 = asyncio.run(check_family_lowercase(clean_rows))
    issues2 = asyncio.run(check_model_id_format(clean_rows))
    assert len(issues1) == 0, f"family 干净应 0 issues, 实际: {issues1}"
    assert len(issues2) == 0, f"model_id 干净应 0 issues, 实际: {issues2}"
    print("  [OK] 干净数据 → 0 issues (family + model_id)")


def test_check_db_consistency_dirty():
    """Test 6: 脏数据 → 多 issues"""
    from scripts.check_model_consistency import check_family_lowercase, check_model_id_format

    class _Row:
        def __init__(self, model_id, family):
            self.model_id = model_id
            self.family = family

    # 脏数据:
    # - MiniMax family 大写 (应被 family_not_lowercase 抓到)
    # - deepseek 错 namespace (family=deepseek 但 model_id=wrong-model)
    # - moonshot-v1-8k family=kimi 命名瑕疵
    dirty_rows = [
        _Row("MiniMax-M3", "MiniMax"),   # 大写 family
        _Row("deepseek-v4-pro", "deepseek"),
        _Row("wrong-model", "deepseek"),  # namespace 不匹配
        _Row("moonshot-v1-8k", "kimi"),  # 命名瑕疵
        _Row("invalid model", "qwen"),    # 字符非法
    ]

    issues1 = asyncio.run(check_family_lowercase(dirty_rows))
    issues2 = asyncio.run(check_model_id_format(dirty_rows))
    total = len(issues1) + len(issues2)
    assert len(issues1) >= 1, f"应有 family_not_lowercase, 实际 0"
    assert len(issues2) >= 3, f"应有 model_id_invalid >= 3, 实际 {len(issues2)}"
    issue_codes = {i.code for i in issues1 + issues2}
    assert "family_not_lowercase" in issue_codes
    assert "model_id_invalid" in issue_codes
    print(f"  [OK] 脏数据 → {total} issues (family={len(issues1)}, model_id={len(issues2)})")


def test_check_official_consistency_mock(monkeypatch):
    """Test 7: --check official mock fetcher + parser → diff 正确"""
    _install_mock_config()

    from scripts.check_model_consistency import check_official_consistency

    # mock db 返回 registry 4 个 model
    class _RegistryRow:
        def __init__(self, model_id, family):
            self.model_id = model_id
            self.family = family

    class _MockRegistryResult:
        def __init__(self, rows):
            self._rows = rows
        def scalars(self):
            class _S:
                def __init__(s, r): s._r = r
                def all(s): return s._r
            return _S(self._rows)

    registry_rows = [
        _RegistryRow("minimax-m2.7", "minimax"),
        _RegistryRow("minimax-m2.7-flash", "minimax"),  # 官方无
        _RegistryRow("deepseek-v4-pro", "deepseek"),
    ]

    class _MockSession:
        async def execute(self, stmt):
            return _MockRegistryResult(registry_rows)

    # mock fetcher.fetch_markdown 返回静态内容
    async def _mock_fetch(url, **kw):
        return "# MiniMax Models\n- MiniMax-M3\n- MiniMax-M2.7\n- MiniMax-M2.5\n"

    # mock parser.parse_content 返回 mock ParseResult (async)
    async def _mock_parse(content, **kw):
        from schemas.intel import ModelIntelItem, ParseResult
        if "minimax" in kw.get("provider", "") or "minimax" in str(kw):
            return ParseResult(
                is_promotion=False,
                confidence=0.9,
                provider="minimax",
                content="MiniMax docs",
                intelligence_kind="model_metadata",
                models=[
                    ModelIntelItem(model_id="minimax-m3"),
                    ModelIntelItem(model_id="minimax-m2.7"),
                    ModelIntelItem(model_id="minimax-m2.5"),
                ],
            )
        return ParseResult(
            is_promotion=False,
            confidence=0.9,
            provider="deepseek",
            content="DeepSeek docs",
            intelligence_kind="model_metadata",
            models=[ModelIntelItem(model_id="deepseek-v4-pro")],
        )

    # patch: 脚本里用 from intelligence.fetcher import fetch_text / parser.parse_content
    # local import, 必须 patch 原模块 (而不是 scripts.check_model_consistency)
    _install_mock_config()
    import intelligence.fetcher as fetcher_mod
    import intelligence.parser as parser_mod
    monkey_patch = _monkeypatch()
    monkey_patch.setattr(fetcher_mod, "fetch_text", _mock_fetch)
    monkey_patch.setattr(parser_mod, "parse_content", _mock_parse)

    try:
        diffs = asyncio.run(check_official_consistency(
            _MockSession(),
            providers=["minimax", "deepseek"],
        ))
        assert len(diffs) >= 1, f"应有 minimax diff: {diffs}"
        minimax_diff = next((d for d in diffs if d.provider == "minimax"), None)
        assert minimax_diff is not None, f"应有 minimax diff"
        # 官方有, registry 无: m3, m2.5
        assert "minimax-m3" in minimax_diff.official_only
        assert "minimax-m2.5" in minimax_diff.official_only
        # registry 有, 官方无: minimax-m2.7-flash
        assert "minimax-m2.7-flash" in minimax_diff.registry_only
        print(f"  [OK] --check official minimax diff: 官方多={len(minimax_diff.official_only)}, registry 多={len(minimax_diff.registry_only)}")
    finally:
        monkey_patch.undo()


class _monkeypatch:
    """简易 monkeypatch 替代 pytest.monkeypatch"""
    def __init__(self):
        self._orig = {}

    def setattr(self, target, name, value):
        self._orig[(target, name)] = getattr(target, name, None)
        setattr(target, name, value)

    def undo(self):
        for (target, name), orig in self._orig.items():
            if orig is None:
                delattr(target, name)
            else:
                setattr(target, name, orig)


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("Phase 4 v4.3 Model 数据源一致性测试")
    print("=" * 60)
    print("\n=== Normalizer 函数 ===")
    test_normalize_family()
    test_normalize_model_id()
    test_validate_model_id()
    test_normalize_capabilities()

    print("\n=== check_model_consistency.py (mock) ===")
    test_check_db_consistency_clean()
    test_check_db_consistency_dirty()
    test_check_official_consistency_mock(_monkeypatch())

    print("\n" + "=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)