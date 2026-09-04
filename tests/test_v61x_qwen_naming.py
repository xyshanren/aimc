"""V6.x: qwen 命名规范化测试

覆盖 (docs/next-major-v6-plan.md §4 V6.x):
1. validate_model_id 放宽 — 接受 "{family}<版本数字/点>", 仍拒无关 family
2. convert_sf_model_id 去冗余前缀 — Qwen/Qwen3-8B → qwen3-8b (不再 qwen-qwen3-8b)
3. 迁移 2026_09_04_v61x_qwen_naming — 双前缀/qwen-vl 改名 + 重复条目合并 +
   members/usage 级联 + 幂等

跑法:
    python -m pytest tests/test_v61x_qwen_naming.py -v
"""

import asyncio
import importlib.util
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent.resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_mock_env():
    tmp_dir = tempfile.mkdtemp(prefix="aimc_v61x_qwen_")
    db_path = os.path.join(tmp_dir, "test.db")
    db_url = "sqlite+aiosqlite:///" + db_path

    sys.modules.setdefault("config", types.ModuleType("config"))
    sys.modules["config"].settings = types.SimpleNamespace(
        openai_api_key="sk-test",
        openai_base_url="https://mock.local/v1",
        parse_model="gpt-4o-mini",
        parse_confidence_threshold=0.6,
        crawl_enabled=True,
        aimc_master_key="a" * 64,
        route_refresh_interval=600,
        db_url=db_url,
        db_path=db_path,
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )


_install_mock_env()


def _load_migration():
    path = ROOT / "db" / "migrations" / "2026_09_04_v61x_qwen_naming.py"
    spec = importlib.util.spec_from_file_location("v61x_qwen_naming", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ============================================================
# Part 1: validate_model_id 放宽
# ============================================================

def test_validate_accepts_family_version_form():
    """V6.x 放宽: family 后跟版本数字/点 → 合法"""
    from db.normalizers import validate_model_id

    assert validate_model_id("qwen3-vl-32b-instruct", "qwen") is True
    assert validate_model_id("qwen3.5-4b", "qwen") is True
    assert validate_model_id("qwen2.5-14b-instruct", "qwen") is True
    # 旧形态仍合法
    assert validate_model_id("qwen-max", "qwen") is True
    assert validate_model_id("deepseek-v4-pro", "deepseek") is True


def test_validate_still_rejects_unrelated_and_malformed():
    from db.normalizers import validate_model_id

    # family 后跟字母 = 别的词, 拒
    assert validate_model_id("qwenx-weird", "qwen") is False
    # 无关 family
    assert validate_model_id("unknown-model", "deepseek") is False
    assert validate_model_id("deepseek-v4-pro", "qwen") is False
    # 未归一化
    assert validate_model_id("Qwen3-8B", "qwen") is False
    # 空值
    assert validate_model_id("", "qwen") is False


def test_seed_registry_all_pass_relaxed_validator():
    """seed 全量模型在放宽后的 validator 下全部合法 (含 qwen3-vl-* 规范名)"""
    from db.normalizers import validate_model_id, normalize_model_id
    from scripts.seed_model_registry import MODEL_REGISTRY
    for row in MODEL_REGISTRY:
        assert validate_model_id(normalize_model_id(row[0]), row[1]), \
            f"{row[0]!r} (family={row[1]!r}) 未通过校验"


def test_seed_no_double_prefix():
    """seed 里不应再有 qwen-qwen 双前缀 (V6.x 规范)"""
    from scripts.seed_model_registry import MODEL_REGISTRY
    from scripts.seed_model_groups import GROUP_MEMBERS
    for row in MODEL_REGISTRY:
        assert not row[0].startswith("qwen-qwen"), f"双前缀残留: {row[0]}"
    for m in GROUP_MEMBERS:
        assert not m[1].startswith("qwen-qwen"), f"组内双前缀残留: {m[1]}"


# ============================================================
# Part 2: convert_sf_model_id 去冗余前缀
# ============================================================

def test_convert_strips_redundant_qwen_prefix():
    from scripts.sync_siliconflow_to_registry import convert_sf_model_id

    assert convert_sf_model_id("Qwen/Qwen3-8B") == ("qwen", "qwen3-8b")
    assert convert_sf_model_id("Qwen/Qwen3-VL-32B-Instruct") == (
        "qwen", "qwen3-vl-32b-instruct")
    assert convert_sf_model_id("Qwen/Qwen2.5-14B-Instruct") == (
        "qwen", "qwen2.5-14b-instruct")


def test_convert_keeps_non_redundant_names():
    from scripts.sync_siliconflow_to_registry import convert_sf_model_id

    # 非 qwen 家族不受影响
    assert convert_sf_model_id("THUDM/GLM-4.5V")[1] == "glm-4.5v"
    assert convert_sf_model_id("deepseek-ai/DeepSeek-V3")[1] == "deepseek-v3"
    # vendor 前缀与模型名不同源 (qwen-2.5 系列本就带 family-) 保持不变
    fam, mid = convert_sf_model_id("Qwen/Qwen-2.5-7B")
    assert mid.startswith("qwen-")


# ============================================================
# Part 3: 迁移 (改名 + 级联 + 重复合并 + 幂等)
# ============================================================

def _make_qwen_db(db_path):
    """模拟迁移前的生产数据: 双前缀 + qwen-vl + seed/sync 重复条目"""
    conn = sqlite3.connect(db_path)
    # model_id UNIQUE 对齐生产 schema — 撞名必须在测试里就炸出来
    conn.execute(
        "CREATE TABLE model_registry ("
        "id INTEGER PRIMARY KEY, model_id TEXT UNIQUE, tier TEXT, modality TEXT)")
    conn.execute(
        "CREATE TABLE model_group_members ("
        "id INTEGER PRIMARY KEY, group_id TEXT, model_id TEXT, is_active INTEGER)")
    conn.execute(
        "CREATE TABLE usage_logs (id INTEGER PRIMARY KEY, model TEXT)")
    conn.execute(
        "CREATE TABLE price_snapshots (id INTEGER PRIMARY KEY, model TEXT)")
    rows = [
        ("qwen-qwen3-8b", "T3", "text"),                       # 双前缀, 组内
        ("qwen-qwen3.5-4b", "T3", "text"),                     # 双前缀, 组内
        ("qwen-vl-32b-instruct", "T1", "vision"),              # seed 行, 组内
        ("qwen-qwen3-vl-32b-instruct", "T1", "vision"),        # sync 重复 → 并入上一行
        ("qwen-vl-8b-instruct", "T3", "vision"),               # seed 行, 组内
        ("qwen-qwen3-vl-8b-instruct", "T1", "vision"),         # sync 重复
        ("qwen-max", "T1", "text"),                            # 合法名, 不动
        ("qwen-2.5-7b", "T3", "text"),                         # 合法名, 不动
    ]
    for mid, tier, mod in rows:
        conn.execute(
            "INSERT INTO model_registry (model_id, tier, modality) VALUES (?, ?, ?)",
            (mid, tier, mod))
    for gid, mid in (("tier:light", "qwen-qwen3-8b"),
                     ("tier:light", "qwen-qwen3.5-4b"),
                     ("tier:vision-light", "qwen-vl-32b-instruct"),
                     ("tier:vision-light", "qwen-vl-8b-instruct")):
        conn.execute(
            "INSERT INTO model_group_members (group_id, model_id, is_active) "
            "VALUES (?, ?, 1)", (gid, mid))
    for mid in ("qwen-qwen3-8b", "qwen-qwen3-vl-32b-instruct", "qwen-max"):
        conn.execute("INSERT INTO usage_logs (model) VALUES (?)", (mid,))
    conn.commit()
    conn.close()


def test_migration_renames_and_merges(tmp_path):
    migration = _load_migration()
    db_path = str(tmp_path / "qwen.db")
    _make_qwen_db(db_path)
    sys.modules["config"].settings.db_path = db_path

    assert migration.run(apply=True) == 0
    conn = sqlite3.connect(db_path)

    ids = {r[0] for r in conn.execute("SELECT model_id FROM model_registry")}
    # 改名到位
    assert {"qwen3-8b", "qwen3.5-4b", "qwen3-vl-32b-instruct", "qwen3-vl-8b-instruct"} <= ids
    # 合法名不动
    assert {"qwen-max", "qwen-2.5-7b"} <= ids
    # 双前缀清零; 重复 sync 行被并入 (8b 的 T3 seed 行保留, T1 sync 行删除)
    assert not [i for i in ids if i.startswith("qwen-qwen")]

    # 组成员级联
    members = {r[0] for r in conn.execute(
        "SELECT model_id FROM model_group_members WHERE group_id='tier:vision-light'")}
    assert members == {"qwen3-vl-32b-instruct", "qwen3-vl-8b-instruct"}

    # usage 历史合并 (qwen-qwen3-vl-32b-instruct 的行并入规范名)
    usage = {r[0]: r[1] for r in conn.execute(
        "SELECT model, COUNT(*) FROM usage_logs GROUP BY model")}
    assert usage.get("qwen3-8b") == 1
    assert usage.get("qwen3-vl-32b-instruct") == 1
    assert usage.get("qwen-max") == 1
    conn.close()

    # 再跑一遍: 幂等
    assert migration.run(apply=True) == 0
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM model_registry WHERE model_id LIKE 'qwen-qwen%'"
    ).fetchone()[0]
    assert n == 0
    conn.close()


def test_migration_dry_run_no_changes(tmp_path):
    migration = _load_migration()
    db_path = str(tmp_path / "qwen2.db")
    _make_qwen_db(db_path)
    sys.modules["config"].settings.db_path = db_path

    assert migration.run(apply=False) == 0
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM model_registry WHERE model_id LIKE 'qwen-qwen%'"
    ).fetchone()[0]
    assert n > 0, "dry-run 不应改库"
    conn.close()
