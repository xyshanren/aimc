"""Add archive notice to historical doc files."""
from pathlib import Path

ARCHIVE_NOTICE = """## 📦 历史归档说明

> **本文档已归档**（2026-08-15 文档重组时迁移到 docs/archive/）。
> 新内容见 [docs/index.md](../index.md) 或相关 [docs/dev/](../dev/) / [docs/user/](../user/) 文档。"""

OLD_FILES = [
    "agent-integration.md",
    "competitive-research.md",
    "phase2-design.md",
    "v44-feedback-and-alert-design.md",
    "review-phase3-4-phase4-e1.md",
    "fix-plan-phase3-4-phase4-e1.md",
    "e2e-results.md",
    "model-data-source.md",
    "crawl-improvements.md",
    "hermes-agent-cn-integration-pr.md",
]

for fname in OLD_FILES:
    p = Path("/root/dsh-workspace/aimc-work/aimc-wt/docs") / fname
    if not p.exists():
        continue
    content = p.read_text(encoding="utf-8")
    lines = content.split("\n")
    insert_idx = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and not line.startswith("## 📦"):
            insert_idx = i
            break
    if insert_idx is None:
        insert_idx = len(lines)
    notice = ["", *ARCHIVE_NOTICE.split("\n"), ""]
    new_lines = lines[:insert_idx] + notice + lines[insert_idx:]
    p.write_text("\n".join(new_lines), encoding="utf-8")
    print(f"updated {fname}")

print("\nDone")
