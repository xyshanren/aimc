"""Phase 4 v4.3.1 (2026-08-13): model_group_members 一致性检测脚本

4 个子检查:
1. member_not_in_registry: member.model_id 不在 model_registry 表里
2. member_priority_out_of_range: priority 异常 (<0 或 >1000)
3. group_no_members: group.enabled=1 但 0 members
4. member_duplicated_in_group: 同一 group 内同一 model_id 重复 (违反 UNIQUE 但 db 层未强制)

设计:
- 子函数单独可测 (不依赖 db session)
- main() 接 AsyncSession + argparse
- exit code: 0=PASS, 1=WARN (优先级异常等), 2=ERROR (member 不在 registry / 重复)
- 复用 db/normalizers.py (validate_model_id 等)

用法:
    python scripts/check_group_members_consistency.py
    python scripts/check_group_members_consistency.py --json

历史:
- Phase 4 v4.3 (450499c): 创建 model_registry 检测脚本 (check_model_consistency.py)
- Phase 4 v4.3.1 (本脚本): 补充 model_group_members 检测, 形成"model 数据 + group 数据"双向一致性
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
class GroupIssue:
    """group_members 检查发现的一个问题"""
    code: str  # member_not_in_registry / member_priority_out_of_range / group_no_members / member_duplicated_in_group
    group_id: str
    model_id: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "group_id": self.group_id,
            "model_id": self.model_id,
            "detail": self.detail,
        }


# ============================================================
# 子检查函数 (可单独测试, 不依赖 db session)
# ============================================================

def check_member_not_in_registry(
    members: list,  # list[ModelGroupMember]
    registered_model_ids: set[str],
) -> list[GroupIssue]:
    """子检查 1: member.model_id 是否在 model_registry 里

    Args:
        members: ModelGroupMember 行列表 (mock 或真)
        registered_model_ids: model_registry.model_id 集合

    Returns:
        GroupIssue 列表
    """
    issues = []
    for m in members:
        if not m.is_active:
            continue
        if m.model_id not in registered_model_ids:
            issues.append(GroupIssue(
                code="member_not_in_registry",
                group_id=m.group_id,
                model_id=m.model_id,
                detail=f"model_id='{m.model_id}' 不在 model_registry 表, 路由时无法获取 tier/capabilities",
            ))
    return issues


def check_member_priority_range(
    members: list,
    min_priority: int = 0,
    max_priority: int = 1000,
) -> list[GroupIssue]:
    """子检查 2: priority 范围合理性

    默认 0-1000, 越低越优先 (跟 SQL ORDER BY priority ASC 一致).
    priority < min 或 > max 视为异常.
    """
    issues = []
    for m in members:
        if not m.is_active:
            continue
        if m.priority is None:
            issues.append(GroupIssue(
                code="member_priority_out_of_range",
                group_id=m.group_id,
                model_id=m.model_id,
                detail="priority 为 NULL",
            ))
            continue
        if m.priority < min_priority or m.priority > max_priority:
            issues.append(GroupIssue(
                code="member_priority_out_of_range",
                group_id=m.group_id,
                model_id=m.model_id,
                detail=f"priority={m.priority} 超出范围 [{min_priority}, {max_priority}]",
            ))
    return issues


def check_group_no_members(
    groups: list,  # list[ModelGroup]
    active_members_per_group: dict[str, int],
) -> list[GroupIssue]:
    """子检查 3: enabled=1 但 0 active members 的 group

    Args:
        groups: ModelGroup 行列表
        active_members_per_group: {group_id: active member 数}
    """
    issues = []
    for g in groups:
        if g.enabled != 1:
            continue
        count = active_members_per_group.get(g.group_id, 0)
        if count == 0:
            issues.append(GroupIssue(
                code="group_no_members",
                group_id=g.group_id,
                detail=f"group enabled 但 0 active members, 路由请求会回 503",
            ))
    return issues


def check_member_duplicated_in_group(members: list) -> list[GroupIssue]:
    """子检查 4: 同一 group 内 model_id 重复

    UNIQUE(group_id, model_id) 是 schema 层的约束, 但 db 层可能未强制
    (SQLite 默认不强制 UNIQUE). 这里应用层兜底.
    """
    issues = []
    seen: dict[tuple[str, str], int] = {}
    for m in members:
        if not m.is_active:
            continue
        key = (m.group_id, m.model_id)
        if key in seen:
            issues.append(GroupIssue(
                code="member_duplicated_in_group",
                group_id=m.group_id,
                model_id=m.model_id,
                detail=f"同 group 内重复 (第 {seen[key]+1} 次出现, 累计 {seen[key]+1})",
            ))
        else:
            seen[key] = 1
    return issues


# ============================================================
# 主检查
# ============================================================

async def check_group_members_consistency(session) -> list[GroupIssue]:
    """db 内 group_members 一致性检查 (4 个子检查)

    Args:
        session: AsyncSession

    Returns:
        GroupIssue 列表 (空表示通过)
    """
    from sqlalchemy import select
    from db.models import ModelGroup, ModelGroupMember, ModelRegistry

    members = list((await session.execute(
        select(ModelGroupMember).where(ModelGroupMember.is_active == 1)
    )).scalars().all())

    # 注册表全集 (用于检查 member 是否在 registry 里)
    reg_rows = (await session.execute(select(ModelRegistry.model_id))).all()
    registered_model_ids = {r[0] for r in reg_rows}

    # 全部 groups (含 enabled=0, 用于 group_no_members 检查)
    groups = list((await session.execute(
        select(ModelGroup)
    )).scalars().all())

    # 统计 active member 数 per group
    active_members_per_group: dict[str, int] = {}
    for m in members:
        active_members_per_group[m.group_id] = active_members_per_group.get(m.group_id, 0) + 1

    # 调用 4 个子检查
    issues = []
    issues += check_member_not_in_registry(members, registered_model_ids)
    issues += check_member_priority_range(members)
    issues += check_group_no_members(groups, active_members_per_group)
    issues += check_member_duplicated_in_group(members)
    return issues


# ============================================================
# 报告输出
# ============================================================

def _print_text_report(issues: list[GroupIssue]) -> None:
    print("=" * 60)
    print(f"Model Group Members 一致性检测 ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print("=" * 60)
    if not issues:
        print("\n  [OK] group_members 一致性通过 (全部 4 项检查)")
        return

    by_code: dict[str, list[GroupIssue]] = {}
    for i in issues:
        by_code.setdefault(i.code, []).append(i)
    for code, items in by_code.items():
        print(f"\n  [{code.upper()}] {len(items)} 个:")
        for it in items[:10]:
            mid = f", model_id={it.model_id}" if it.model_id else ""
            print(f"    - group_id={it.group_id}{mid}: {it.detail}")
        if len(items) > 10:
            print(f"    ... 还有 {len(items) - 10} 个")


def _print_json_report(issues: list[GroupIssue]) -> None:
    report = {
        "timestamp": datetime.now().isoformat(),
        "issues": [i.to_dict() for i in issues],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _summarize_exit_code(issues: list[GroupIssue]) -> int:
    """根据检查结果返回 exit code

    - 0: PASS (0 issues)
    - 1: WARN (仅 priority 异常, 不阻断路由)
    - 2: ERROR (member 缺失 / 重复 / group 空)
    """
    if not issues:
        return 0
    error_codes = {"member_not_in_registry", "member_duplicated_in_group", "group_no_members"}
    has_error = any(i.code in error_codes for i in issues)
    return 2 if has_error else 1


# ============================================================
# 主入口
# ============================================================

async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Model Group Members 一致性检测脚本",
    )
    parser.add_argument("--json", action="store_true", help="JSON 输出 (CI 集成)")
    args = parser.parse_args()

    from db.engine import async_session_factory

    async with async_session_factory() as session:
        issues = await check_group_members_consistency(session)

    if args.json:
        _print_json_report(issues)
    else:
        _print_text_report(issues)
        print()
        print("=" * 60)
        ec = _summarize_exit_code(issues)
        ec_name = {0: "PASS", 1: "WARN", 2: "ERROR"}.get(ec, "?")
        print(f"总结: {len(issues)} issues")
        print(f"exit code: {ec} ({ec_name})")
        print("=" * 60)

    return _summarize_exit_code(issues)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))