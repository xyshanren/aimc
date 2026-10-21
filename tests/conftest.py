"""P2-16 (2026-08-15): 测试 fixture

设计:
- 用 in-memory SQLite (sqlite:///:memory:) 隔离测试, 不影响 prod db
- 每个 test function 创建新 engine + 新 session (避免状态污染)
- pytest-asyncio mode=auto (所有 async test 自动 async)

P1 (2026-08-19): 全局状态隔离 fixture
- 背景: 全量跑 177 failed 但单文件跑全过 — 跨文件全局状态污染
- 污染机制 (两层):
  1. config.settings 覆盖: 所有测试文件顶层 _install_mock_env() 在
     collection 期依次执行, 每个覆盖 sys.modules['config'].settings —
     最后一个文件的 mock 胜出, 其他文件 test 运行时拿到错的 settings。
     例: test_v45_reports 的 master_key='test-key' (非 hex) 覆盖了
     test_v44_cli 的 master_key='a'*64 -> add_account 报错。
  2. 第三方模块 mock 持久化: test_hermes_agent_cn_e2e / test_phase3e_headers
     在顶层 sys.modules['httpx'/'fastapi'/'starlette.*'] = Mock, 这些 mock
     没有 __file__ (或假 __file__), 不在项目根下, 跨文件持久污染。
     例: test_v46_dashboard_accounts 的 httpx.ASGITransport 报 AttributeError。

- 修法:
  1. pytest_collectreport: 每个模块 collection 后快照 config.settings 引用,
     autouse fixture 在每个 test 前 restore 该 test 所在文件的快照。
  2. conftest import 时快照 sys.modules 全量 keys + 真实模块对象引用,
     autouse fixture 在每个 test 后:
       - 移除所有项目模块 (含 config) — 强制重新 import 绑定正确 db_url
       - 还原被 mock 的第三方模块 (httpx/fastapi/starlette 等) 到真实对象
"""
import asyncio
import os
import sys
import types
from pathlib import Path

import pytest

# 项目根目录: tests/conftest.py -> 父级父级 = project root
ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

# P1: 预 import 第三方库 — 确保它们在 _PRISTINE_SYS_MODULES 里 (真实对象)。
# 部分测试文件 (test_hermes_agent_cn_e2e / test_phase3e_headers) 在顶层
# sys.modules['httpx'/'fastapi'/'starlette.*'] = Mock, 会污染后续文件
# 顶层 'import httpx' (如 test_usage_log_parse)。预 import 后:
# 1. 这些库进入 _PRISTINE_SYS_MODULES, fixture 能还原成真实对象
# 2. 后续文件 collection 时 'import httpx' 直接拿到已缓存的真 httpx
import httpx  # noqa: E402
import fastapi  # noqa: E402
import starlette.datastructures  # noqa: E402
import fastapi.responses  # noqa: E402

# ============================================================
# conftest import 时快照 (此时无任何测试文件 collection 过)
# ============================================================

# 每个 test 模块 collection 后的 config 快照: {module_nodeid: settings_obj}
_MODULE_SETTINGS_SNAPSHOTS: dict[str, object] = {}

# conftest 自身 import 时的真实 config settings (给没 mock 的文件兜底)
_PRISTINE_SETTINGS = None
try:
    import config as _config_mod
    _PRISTINE_SETTINGS = _config_mod.settings
except Exception:
    pass

# 快照 conftest import 时的 sys.modules — 用于还原被测试文件 mock 的第三方模块
# 存 {name: module_obj}; 还原时把被改的 module 换回真实对象, 删掉新增的假 module
_PRISTINE_SYS_MODULES: dict[str, object] = {}
for _name, _mod in list(sys.modules.items()):
    if _mod is not None:
        _PRISTINE_SYS_MODULES[_name] = _mod


def _is_project_module(mod) -> bool:
    """判断是否本项目模块 (按 __file__ 路径), 只对项目模块做隔离"""
    f = getattr(mod, "__file__", None)
    if not f:
        return False
    try:
        p = Path(f).resolve()
        # VM 的 .venv 建在项目根内 — site-packages 里的第三方库 (_pytest/
        # pytest_asyncio 等) 会被误判为项目模块而在 test 后被删, 导致第二个
        # test 起 Session scope fixture 解析失败。site-packages 一律不算。
        if "site-packages" in p.parts:
            return False
        return p.is_relative_to(ROOT)
    except (OSError, ValueError):
        return False


# ============================================================
# Collection hooks: 每个模块 collection 后快照 config.settings
# ============================================================
def pytest_collectreport(report):
    """每个模块 collection 完成后 — 快照 config.settings + 还原第三方模块

    report.nodeid 形如 'tests/test_v44_cli.py' (Module 级)
    此时该模块顶层 _install_mock_env() 已执行, config.settings 是它的 mock。
    存下引用, 后续 test 运行时按文件 restore。

    同时还原被该模块顶层 mock 的第三方模块 (httpx/fastapi/starlette 等)
    到真实对象 — 防止污染后续模块的 collection-time import。

    P1 fix (2026-08-19): 不 mock config 的文件 (e.g. test_p0_usage_fixes)
    会继承上一个文件的 _MockSettings 污染。修法: collection 后先把 config.settings
    restore 到 _PRISTINE_SETTINGS, 再判断当前 settings 是否跟 pristine 不同 —
    只有不同 (该文件自己 mock 了) 才存快照; 相同则不存 (test 时走 pristine 兜底)。
    """
    # 只处理 Module 级 (文件), 跳过 Package/Dir
    if report.nodeid and report.nodeid.endswith(".py") and not report.failed:
        try:
            import config as cfg
            cur_settings = cfg.settings
            # 只存"该文件自己 mock 的" settings — 跟 pristine 不同才算
            if cur_settings is not _PRISTINE_SETTINGS:
                _MODULE_SETTINGS_SNAPSHOTS[report.nodeid] = cur_settings
            else:
                # 跟 pristine 相同 → 该文件没 mock, 不存 (test 时走 pristine 兜底)
                _MODULE_SETTINGS_SNAPSHOTS.pop(report.nodeid, None)
            # restore 到 pristine, 防止污染下一个文件的 collection-time import
            if _PRISTINE_SETTINGS is not None:
                cfg.settings = _PRISTINE_SETTINGS
        except Exception:
            pass

    # 还原第三方模块到真实对象 (在模块 collection 之间)
    # 防止 test_hermes/test_phase3e 的 sys.modules['httpx']=Mock 污染
    # 后续模块 collection 时的 'import httpx'
    for name in list(sys.modules):
        pristine_mod = _PRISTINE_SYS_MODULES.get(name)
        if pristine_mod is not None:
            cur = sys.modules.get(name)
            if cur is not pristine_mod:
                sys.modules[name] = pristine_mod
        else:
            # 不在 pristine 里的 — 若是假 module (无 __file__ 或不存在) 删掉
            cur = sys.modules.get(name)
            if cur is None:
                continue
            f = getattr(cur, "__file__", None)
            if f is None:
                del sys.modules[name]
            elif not _is_project_module(cur):
                try:
                    if not Path(f).exists():
                        del sys.modules[name]
                except (OSError, ValueError):
                    del sys.modules[name]


# ============================================================
# autouse fixture: 每个 test 前 restore config, test 后清模块 + 还原第三方
# ============================================================
@pytest.fixture(autouse=True)
def _isolate_project_state(request):
    """P1: 每个 test 前 restore 所属文件的 config.settings,
    test 后移除项目模块 + 还原被 mock 的第三方模块

    - restore config: 把 config.settings 设回该 test 文件 collection 时的快照,
      撤销其他文件 collection 期的覆盖。
    - 清项目模块: test 后从 sys.modules 移除所有项目模块 (含 config),
      下次 import 重新绑定 engine 到当前 (已 restore 的) config.settings。
    - 还原第三方: 把 httpx/fastapi/starlette 等被测试文件 mock 的模块还原
      到 conftest import 时的真实对象, 删掉测试新增的假 module。
    """
    # 找到 test 所属文件的 nodeid (取到 .py 为止)
    nodeid = request.node.nodeid
    file_nodeid = nodeid.split("::", 1)[0]

    # restore 该文件的 config.settings 快照
    snapshot = _MODULE_SETTINGS_SNAPSHOTS.get(file_nodeid)
    if snapshot is not None:
        try:
            import config as cfg
            cfg.settings = snapshot
        except Exception:
            pass
    elif _PRISTINE_SETTINGS is not None:
        # 没 mock 的文件 — 用真实 config
        try:
            import config as cfg
            cfg.settings = _PRISTINE_SETTINGS
        except Exception:
            pass

    yield

    # test 后: 还原第三方模块 + 移除项目模块
    # 1) 还原被测试文件 mock 的第三方模块 (httpx/fastapi/starlette 等)
    #    遍历当前 sys.modules, 把不在 pristine 里的删掉 (测试新增的假 module),
    #    把在 pristine 里的还原回真实对象 (测试覆盖的)
    for name in list(sys.modules):
        cur = sys.modules.get(name)
        pristine_mod = _PRISTINE_SYS_MODULES.get(name)
        if pristine_mod is not None:
            # pristine 里有这个模块 — 还原成真实对象 (防被 mock 覆盖)
            if cur is not pristine_mod:
                sys.modules[name] = pristine_mod
        else:
            # pristine 里没有 — 测试新增的, 若是假 module (非项目) 删掉
            if cur is not None and not _is_project_module(cur):
                # 保留真实第三方模块 (可能是 lazy import 产生的真模块)
                # 只删明显是 mock 的 (无 __file__ 或 __file__ 不存在)
                f = getattr(cur, "__file__", None)
                if f is None:
                    del sys.modules[name]
                else:
                    try:
                        if not Path(f).exists():
                            del sys.modules[name]
                    except (OSError, ValueError):
                        del sys.modules[name]

    # 2) 移除所有项目模块 (含 config) — 下次 import 重新绑定 engine
    for name in list(sys.modules):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        if isinstance(mod, types.ModuleType) and _is_project_module(mod):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _event_loop_policy():
    """强制 event loop scope = function (避免 async fixture 跨 test 复用 loop)"""
    import asyncio
    loop_policy = asyncio.DefaultEventLoopPolicy()
    asyncio.set_event_loop_policy(loop_policy)


# ============================================================
# V6.18 (2026-10-22): 真实本地库防污染守卫
# ============================================================
# 背景: db/engine.py 在**模块导入时**用 settings.db_url 建 engine;
#       db/init_db.py 是 `from db.engine import engine` (导入时绑定)。
#       测试文件顶层 mock config 后, 若 db.engine / db.init_db 已被别的
#       模块间接导入 (模块缓存), engine 仍绑真实 data/aimc.db ——
#       init_db() + 测试写入会污染真实本地库。
#       2026-10-21 实录: 本地库被塞进 4 条 family='x' 的假模型行,
#       触发 pre-commit 的 model_registry 一致性检查报错。
#
# 守卫两层:
# ① 预防 —— conftest 加载时把 config.settings.db_url 指向本文件自己的
#    临时库, 并删掉真实库文件路径的可达性: 任何"漏改"的测试即使
#    沿用了默认 settings, 也只会写到临时库, 碰不到 data/aimc.db。
#    (自建临时库的测试文件会自己重绑 engine, 不受影响)
# ② 事后 —— autouse fixture 检测真实库指纹变化, 污染即失败并给出修法。

_REAL_LOCAL_DB = ROOT / "data" / "aimc.db"

# conftest 专用临时库 (预防层用)
import tempfile as _tempfile
_GUARD_TMP_DIR = _tempfile.mkdtemp(prefix="aimc_conftest_guard_")
_GUARD_TMP_DB = os.path.join(_GUARD_TMP_DIR, "guard.db")
_GUARD_TMP_URL = f"sqlite+aiosqlite:///{_GUARD_TMP_DB}"


def _local_db_fingerprint():
    """真实本地库的 (大小, mtime_ns) — 不存在时返回 None"""
    try:
        st = _REAL_LOCAL_DB.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _apply_guard_db_url():
    """把 config.settings 的库指向守卫临时库 (预防层)。

    只改 db_url / db_path 两个字段, 其余 settings 原样保留 ——
    真实 config 的 db_path 是开发者的本地库, 绝不能让测试碰到。
    """
    try:
        import config as cfg
        cfg.settings.db_url = _GUARD_TMP_URL
        cfg.settings.db_path = _GUARD_TMP_DB
        return True
    except Exception:
        return False


# 预防层: conftest import 期就改掉 settings 的库路径。
# 之后任何测试 import config 拿到的都是临时库。
_apply_guard_db_url()


@pytest.fixture(autouse=True)
def _guard_real_local_db():
    """禁止测试写真实本地库 (data/aimc.db)。

    失败时给出可操作提示: 哪个测试污染了库 / 怎么修 (显式重绑
    db.engine.engine + db.init_db.engine 到临时库)。
    """
    # 每个 test 前再确认一次 (别的测试文件可能 mock 了 config.settings)
    _apply_guard_db_url()
    before = _local_db_fingerprint()
    yield
    after = _local_db_fingerprint()
    if before is not None and after is not None and before != after:
        # 再确认一次: 可能是别的并行进程写的 (少见), 但仍要报出来
        raise AssertionError(
            f"测试污染了真实本地库 {_REAL_LOCAL_DB} (before={before}, after={after})。"
            f" 修法: 显式把 db.engine.engine 与 db.init_db.engine 重绑到临时库 "
            f"(参考 tests/test_v617d_group_admission.py 的 _seed_rows)。"
        )


@pytest.fixture
def event_loop():
    """每个 test 一个新 event loop"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
