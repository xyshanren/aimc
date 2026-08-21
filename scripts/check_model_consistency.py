"""Phase 4 v4.3 (2026-08-13): Model 数据源一致性检测脚本

3 个检测模式 (argparse 切换):
- --check db: db 内一致性 (family 小写 / model_id 命名规范 / registry↔price_snapshots 漂移)
- --check official: 第三方权威源 cross-check (爬 5 家厂商官方 API 文档页)
- --check all: 顺序执行 1 + 2

Exit code:
- 0: PASS (db 一致, official 完全一致 或官方爬取失败但 warning)
- 1: WARN (db 一致但有官方源漂移)
- 2: ERROR (db 不一致)

用法:
    python scripts/check_model_consistency.py --check db
    python scripts/check_model_consistency.py --check official
    python scripts/check_model_consistency.py --check all
    python scripts/check_model_consistency.py --check db --json   # CI 用
    python scripts/check_model_consistency.py --check official --provider minimax

历史背景 (commit 1005111):
- MiniMax family 缩到 5 个 + 注释错指 sensenova_official
- fix commit 4a2cfc5 修了这一处, 但根本问题没解决
- 本脚本是系统性 fix: 应用层归一化 + 检测兜底
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================
# Data classes
# ============================================================

@dataclass
class Issue:
    """db 检查发现的一个问题"""
    code: str  # family_not_lowercase / model_id_invalid / model_id_not_in_registry / extra_model_in_registry
    provider: str
    model_id: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "provider": self.provider,
            "model_id": self.model_id,
            "detail": self.detail,
        }


@dataclass
class OfficialDiff:
    """第三方权威源 vs model_registry 的 diff"""
    provider: str
    official_models: list[str] = field(default_factory=list)
    registry_models: list[str] = field(default_factory=list)
    official_only: list[str] = field(default_factory=list)  # 官方有, registry 无
    registry_only: list[str] = field(default_factory=list)  # registry 有, 官方无
    fetch_error: str = ""  # 爬取失败时的错误信息

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "official_models": self.official_models,
            "registry_models": self.registry_models,
            "official_only": self.official_only,
            "registry_only": self.registry_only,
            "fetch_error": self.fetch_error,
        }


# ============================================================
# --check db 实现
# ============================================================

async def check_family_lowercase(rows: list) -> list[Issue]:
    """子检查 1: family 字段全小写

    Args:
        rows: ModelRegistry 行列表 (mock 或真)
    Returns:
        Issue 列表
    """
    from db.normalizers import normalize_family

    issues = []
    for r in rows:
        if r.family != r.family.lower():
            issues.append(Issue(
                code="family_not_lowercase",
                provider=r.family,
                model_id=r.model_id,
                detail=f"family='{r.family}' 应为 '{normalize_family(r.family)}'",
            ))
    return issues


async def check_model_id_format(rows: list) -> list[Issue]:
    """子检查 2: model_id 命名规范 (namespace + 字符)

    Args:
        rows: ModelRegistry 行列表
    Returns:
        Issue 列表
    """
    from db.normalizers import validate_model_id

    issues = []
    for r in rows:
        if not validate_model_id(r.model_id, r.family):
            issues.append(Issue(
                code="model_id_invalid",
                provider=r.family,
                model_id=r.model_id,
                detail=f"model_id 应以 '{r.family}-' 开头且符合字符规范",
            ))
    return issues


async def check_registry_vs_pricesnapshots(
    registry_rows: list,
    channels: list,
    price_models_per_channel: dict[int, set[str]],
) -> list[Issue]:
    """子检查 3: model_registry ↔ price_snapshots 漂移

    Args:
        registry_rows: ModelRegistry 行列表
        channels: Channel 行列表 (有 .id / .name / .provider 属性)
        price_models_per_channel: {channel_id: {model, model, ...}}

    Returns:
        Issue 列表 (model_id_not_in_registry)
    """
    reg_per_provider: dict[str, set[str]] = {}
    for r in registry_rows:
        reg_per_provider.setdefault(r.family, set()).add(r.model_id)

    issues = []
    for ch in channels:
        provider = ch.provider
        if provider not in reg_per_provider:
            continue
        reg_models = reg_per_provider[provider]
        ps_models = price_models_per_channel.get(ch.id, set())
        missing_in_registry = ps_models - reg_models
        for m in missing_in_registry:
            issues.append(Issue(
                code="model_id_not_in_registry",
                provider=provider,
                model_id=m,
                detail=f"channel={ch.name} 有 price_snapshot 但 model_registry 无",
            ))
    return issues


async def check_db_consistency(session) -> list[Issue]:
    """db 内一致性检查 (3 个子检查)

    子检查:
    1. family 字段全小写 (check_family_lowercase)
    2. model_id 命名规范 (check_model_id_format)
    3. model_registry ↔ price_snapshots 漂移 (check_registry_vs_pricesnapshots)

    Args:
        session: AsyncSession (sqlalchemy)

    Returns:
        Issue 列表 (空表示通过)
    """
    from sqlalchemy import select
    from db.models import Channel, ModelRegistry, PriceSnapshot

    # 取全部 ModelRegistry 行
    rows = list((await session.execute(select(ModelRegistry))).scalars().all())

    # 取全部 active channels + 他们的 price_snapshots
    channels = list((await session.execute(
        select(Channel).where(Channel.status == "active")
    )).scalars().all())

    price_models_per_channel: dict[int, set[str]] = {}
    if channels:
        ps_rows = (await session.execute(
            select(PriceSnapshot.channel_id, PriceSnapshot.model)
        )).all()
        for ch_id, model in ps_rows:
            price_models_per_channel.setdefault(ch_id, set()).add(model)

    # 调用 3 个子检查
    issues = []
    issues += await check_family_lowercase(rows)
    issues += await check_model_id_format(rows)
    issues += await check_registry_vs_pricesnapshots(rows, channels, price_models_per_channel)
    return issues


# ============================================================
# --check official 实现
# ============================================================

# 第三方权威源 URL 配置 (官方 API 文档页 / model 列表页)
OFFICIAL_URLS: dict[str, dict] = {
    "minimax": {
        "url": "https://platform.minimaxi.com/docs/api-reference/api-overview",
        "wait_until": "domcontentloaded",
    },
    "deepseek": {
        "url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "wait_until": "domcontentloaded",
    },
    "kimi": {
        "url": "https://platform.kimi.com/docs/intro/model-list",
        "wait_until": "networkidle",
    },
    "qwen": {
        "url": "https://help.aliyun.com/zh/model-studio/developer-reference/model-list",
        "wait_until": "domcontentloaded",
    },
    "glm": {
        "url": "https://bigmodel.cn/dev/api/normal-model/glm-4",
        "wait_until": "domcontentloaded",
    },
}


async def check_official_consistency(
    session,
    providers: list[str] | None = None,
) -> list[OfficialDiff]:
    """第三方权威源 cross-check

    复用 fetcher + parser, 爬 5 家厂商官方 API 文档, AI 解析出 model 列表,
    跟 model_registry 同 family 的 model_id diff.

    Args:
        session: AsyncSession
        providers: 指定厂商 (None = 全部 5 家)

    Returns:
        OfficialDiff 列表 (一个厂商一个)
    """
    from sqlalchemy import select
    from db.models import ModelRegistry
    from db.normalizers import normalize_model_id

    # lazy import (fetcher + parser 较重, 避免脚本无谓 import)
    try:
        from intelligence.fetcher import fetch_text
        from intelligence.parser import parse_content
    except Exception as e:
        # 第三方依赖 (openai, playwright) 缺失时, 全部厂商标记 fetch_error
        diffs: list[OfficialDiff] = []
        for p in (providers or OFFICIAL_URLS.keys()):
            diffs.append(OfficialDiff(provider=p, fetch_error=f"依赖缺失: {e}"))
        return diffs

    target_providers = providers or list(OFFICIAL_URLS.keys())

    # 查 registry 全部 model
    rows = (await session.execute(select(ModelRegistry))).scalars().all()
    reg_per_provider: dict[str, set[str]] = {}
    for r in rows:
        reg_per_provider.setdefault(r.family, set()).add(normalize_model_id(r.model_id))

    diffs = []
    for provider in target_providers:
        diff = OfficialDiff(provider=provider)
        config = OFFICIAL_URLS.get(provider)
        if not config:
            diff.fetch_error = f"未配置 URL"
            diffs.append(diff)
            continue

        # 1. 抓页面
        try:
            content = await fetch_text(
                config["url"],
                wait_until=config["wait_until"],
                timeout=20000,
            )
        except Exception as e:
            diff.fetch_error = f"fetch_text 失败: {e}"
            diffs.append(diff)
            continue

        if not content or not content.strip():
            diff.fetch_error = "fetch_text 返回空"
            diffs.append(diff)
            continue

        # 2. AI 解析出 model 列表
        try:
            parsed = await parse_content(
                content=content,
                provider=provider,
                url=config["url"],
                source_type="model_metadata",
            )
        except Exception as e:
            diff.fetch_error = f"parse_content 失败: {e}"
            diffs.append(diff)
            continue

        if not parsed.models:
            diff.fetch_error = "解析无 models (页面结构变化或 AI 解析失败)"
            diffs.append(diff)
            continue

        # 3. diff
        official_models = {normalize_model_id(m.model_id) for m in parsed.models if m.model_id}
        registry_models = reg_per_provider.get(provider, set())

        diff.official_models = sorted(official_models)
        diff.registry_models = sorted(registry_models)
        diff.official_only = sorted(official_models - registry_models)
        diff.registry_only = sorted(registry_models - official_models)

        diffs.append(diff)

    return diffs


# ============================================================
# 报告输出
# ============================================================

def _print_text_report(db_issues: list[Issue], official_diffs: list[OfficialDiff]) -> None:
    """人类可读的报告 (stdout)"""
    print("=" * 60)
    print(f"Model Registry 一致性检测 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("=" * 60)

    if db_issues is not None:
        print(f"\n[DB 检查]")
        if not db_issues:
            print("  [OK] db 内一致性通过 (family 全小写 / model_id 命名规范 / 无漂移)")
        else:
            # 按 code 分组
            by_code: dict[str, list[Issue]] = {}
            for i in db_issues:
                by_code.setdefault(i.code, []).append(i)
            for code, items in by_code.items():
                print(f"  [{code.upper()}] {len(items)} 个:")
                for it in items[:10]:  # 最多展示 10 个
                    print(f"    - provider={it.provider}, model_id={it.model_id}: {it.detail}")
                if len(items) > 10:
                    print(f"    ... 还有 {len(items) - 10} 个")

    if official_diffs:
        print(f"\n[OFFICIAL 检查] {len(official_diffs)} 厂商")
        for d in official_diffs:
            if d.fetch_error:
                print(f"  [{d.provider}] [ERROR] {d.fetch_error}")
                continue
            print(f"  [{d.provider}] 官方 {len(d.official_models)} model, registry {len(d.registry_models)} model")
            if d.official_only or d.registry_only:
                if d.official_only:
                    print(f"    - 官方有, registry 无 ({len(d.official_only)}): {', '.join(d.official_only[:5])}{' ...' if len(d.official_only) > 5 else ''}")
                if d.registry_only:
                    print(f"    - registry 有, 官方无 ({len(d.registry_only)}): {', '.join(d.registry_only[:5])}{' ...' if len(d.registry_only) > 5 else ''}")
            else:
                print(f"    [OK] 完全一致")


def _print_json_report(db_issues: list[Issue], official_diffs: list[OfficialDiff]) -> None:
    """JSON 报告 (CI 集成)"""
    report = {
        "timestamp": datetime.now().isoformat(),
        "db_issues": [i.to_dict() for i in (db_issues or [])],
        "official_diffs": [d.to_dict() for d in (official_diffs or [])],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _summarize_exit_code(db_issues: list[Issue], official_diffs: list[OfficialDiff]) -> int:
    """根据检查结果返回 exit code

    - 0: PASS (db 一致, official 也一致或爬取失败)
    - 1: WARN (db 一致但有官方源漂移)
    - 2: ERROR (db 不一致)
    """
    if db_issues:
        return 2  # db ERROR
    # 检查 official diffs
    has_diff = any(d.official_only or d.registry_only for d in official_diffs if not d.fetch_error)
    return 1 if has_diff else 0


# ============================================================
# 主入口
# ============================================================

async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Model Registry 一致性检测脚本",
    )
    parser.add_argument(
        "--check",
        choices=["db", "official", "all"],
        default="db",
        help="检查模式: db (db 内) / official (第三方源) / all (全部)",
    )
    parser.add_argument(
        "--provider",
        action="append",
        help="指定厂商 (--check official 时用, 可多次指定)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON 输出 (CI 集成)",
    )

    args = parser.parse_args()

    # lazy db import (避免脚本无 db 时也能跑 --help)
    from db.engine import async_session_factory

    db_issues: list[Issue] = []
    official_diffs: list[OfficialDiff] = []

    async with async_session_factory() as session:
        if args.check in ("db", "all"):
            db_issues = await check_db_consistency(session)

        if args.check in ("official", "all"):
            official_diffs = await check_official_consistency(session, args.provider)

    # 输出
    if args.json:
        _print_json_report(db_issues, official_diffs)
    else:
        _print_text_report(db_issues, official_diffs)
        # 总结
        print()
        print("=" * 60)
        n_db_warn = len(db_issues)
        n_off_warn = sum(
            len(d.official_only) + len(d.registry_only)
            for d in official_diffs if not d.fetch_error
        )
        n_off_err = sum(1 for d in official_diffs if d.fetch_error)
        print(f"总结: db {n_db_warn} ERROR, official {n_off_warn} 漂移, {n_off_err} 爬取失败")
        ec = _summarize_exit_code(db_issues, official_diffs)
        ec_name = {0: "PASS", 1: "WARN", 2: "ERROR"}.get(ec, "?")
        print(f"exit code: {ec} ({ec_name})")
        print("=" * 60)

    return _summarize_exit_code(db_issues, official_diffs)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))