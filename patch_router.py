"""Apply P0-4 changes to router.py via surgical edits."""
import sys

path = sys.argv[1]
content = open(path).read()

# 1. 加 imports (os, Path)
old_import = "import json\nimport logging\nimport time\nfrom datetime import datetime, date\nfrom typing import Optional"
new_import = "import json\nimport logging\nimport os\nimport time\nfrom datetime import datetime, date\nfrom pathlib import Path\nfrom typing import Optional"
assert old_import in content, "imports not found"
content = content.replace(old_import, new_import, 1)

# 2. 加 ROUTER_CACHE_TTL_SECONDS 常量
old_logger = 'logger = logging.getLogger(__name__)\n\n\nclass RouteEngine:'
new_logger = '''logger = logging.getLogger(__name__)


# P0-4 (2026-08-14): router cache TTL + db file mtime 监听
ROUTER_CACHE_TTL_SECONDS = int(os.getenv("AIMC_ROUTER_CACHE_TTL", "60"))


class RouteEngine:'''
assert old_logger in content, "logger anchor not found"
content = content.replace(old_logger, new_logger, 1)

# 3. __init__ 加 _last_db_mtime
old_init = "        self._last_refresh: datetime | None = None"
new_init = "        self._last_refresh: datetime | None = None\n        # P0-4: db file mtime\n        self._last_db_mtime: float | None = None"
assert old_init in content, "init anchor not found"
content = content.replace(old_init, new_init, 1)

# 4. refresh_cache 加 mtime 记录
old_refresh = "        self._last_refresh = datetime.now()\n        logger.info(\n            f\"路由缓存已全量刷新: {len(self._channels_cache)} 个可用渠道账号\"\n        )"
new_refresh = """        self._last_refresh = datetime.now()
        # P0-4: 记录 db 文件 mtime
        try:
            db_path = Path(settings.db_path)
            if db_path.exists():
                self._last_db_mtime = db_path.stat().st_mtime
        except Exception as e:
            logger.debug(f"[P0-4] 记录 db mtime 失败: {e}")
        logger.info(
            f"路由缓存已全量刷新: {len(self._channels_cache)} 个可用渠道账号"
        )"""
assert old_refresh in content, "refresh anchor not found"
content = content.replace(old_refresh, new_refresh, 1)

# 5. _auto_refresh_if_stale 方法
old_after = """        logger.info(
            f"路由缓存已全量刷新: {len(self._channels_cache)} 个可用渠道账号"
        )

    async def invalidate_channel"""
new_after = """        logger.info(
            f"路由缓存已全量刷新: {len(self._channels_cache)} 个可用渠道账号"
        )

    async def _auto_refresh_if_stale(self) -> bool:
        # P0-4 2026-08-14: cache stale 时自动 refresh

        # 触发条件 (满足任一):
        # 1. TTL 超时 > AIMC_ROUTER_CACHE_TTL (默认 60s)
        # 2. db file mtime 变化 (sqlite3 CLI, 修复脚本, dashboard)
        if self._last_refresh is None:
            await self.refresh_cache()
            return True
        ttl_age = (datetime.now() - self._last_refresh).total_seconds()
        if ttl_age > ROUTER_CACHE_TTL_SECONDS:
            logger.debug(f"[P0-4] cache TTL 超时 ({ttl_age:.1f}s), 自动 refresh")
            await self.refresh_cache()
            return True
        try:
            db_path = Path(settings.db_path)
            if not db_path.exists():
                return False
            current_mtime = db_path.stat().st_mtime
            if self._last_db_mtime is None or current_mtime > self._last_db_mtime:
                logger.debug(f"[P0-4] db mtime 变化, 自动 refresh")
                await self.refresh_cache()
                return True
        except Exception as e:
            logger.debug(f"[P0-4] mtime 检查失败: {e}")
        return False

    async def invalidate_channel"""
assert old_after in content, "invalidate_channel anchor not found"
content = content.replace(old_after, new_after, 1)

open(path, "w").write(content)
print("P0-4 patch applied successfully")
