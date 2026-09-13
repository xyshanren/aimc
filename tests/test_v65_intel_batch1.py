"""V6.5 批次一: B1 探活 ErrorInfo NameError + G 周建议部署路径解耦.

B1 背景 (module-integration-audit §B1): channel_health 只 import 了
ErrorCategory/classify, 探活失败分支内联构造 ErrorInfo 抛 NameError →
失败探活永不回写 cooldown_manager (成功分支正常)。
修复: 补 import + 抽模块级 _probe_error_info() 可单测。

G 背景 (module-integration-audit §G): _weekly_suggest_changes_task 硬编码
cwd="/home/lxc/aimc" (恰配生产 VM 路径, 换部署路径即子进程起不来 → 周建议
静默断更)。修复: 模块级 _PROJECT_ROOT 相对定位 + 删死导入 suggest_main。
"""

from pathlib import Path

from gateway.error_classifier import ErrorCategory

from intelligence.channel_health import _probe_error_info


def test_probe_error_info_maps_category():
    r = {"error_type": "upstream_5xx", "error_code": "502", "error_msg": "bad gateway"}
    info = _probe_error_info(r)
    assert info.category == ErrorCategory.UPSTREAM_5XX
    assert info.suggested_cooldown_seconds == 60
    assert info.suggested_probe_after_seconds == 300
    assert info.needs_human is False


def test_probe_error_info_needs_human_categories():
    for et in ("quota_exhausted", "auth_failed", "model_unsupported"):
        info = _probe_error_info({"error_type": et, "error_msg": "x"})
        assert info.needs_human is True, f"{et} 应置 needs_human"
        assert info.suggested_cooldown_seconds == 15


def test_probe_error_info_unknown_fallback():
    """未知/缺失 error_type → UNKNOWN, 不抛 ValueError。"""
    info = _probe_error_info({"error_type": "not_a_category", "error_msg": ""})
    assert info.category == ErrorCategory.UNKNOWN
    info2 = _probe_error_info({})
    assert info2.category == ErrorCategory.UNKNOWN
    assert info2.error_msg == ""


def test_project_root_relative_positioning():
    """G: _PROJECT_ROOT 必须指向真实项目根 (含 main.py), 不再依赖部署绝对路径。"""
    from intelligence import scheduler

    root = Path(scheduler._PROJECT_ROOT)
    assert (root / "main.py").exists(), f"_PROJECT_ROOT 解析错误: {root}"
    assert (root / "intelligence" / "scheduler.py").exists()


def test_scheduler_no_hardcoded_deploy_path():
    """源码断言: 周建议 job 不得再硬编码部署路径 (防回归)。"""
    src = Path("intelligence/scheduler.py").read_text(encoding="utf-8")
    assert 'cwd="/home/lxc/aimc"' not in src, "scheduler 仍硬编码部署路径"
    assert "_PROJECT_ROOT" in src
