"""从 db.models ORM 生成 db/schema_align.py 的 _ALIGN_DDL / _LEGACY_INSERTS 常量。

V6.5+ 迁移收敛轻方案 A 的维护工具。ORM 列变更后重跑本脚本，
将打印出的常量块整段替换 schema_align.py 对应部分（diff 逐条可评审，
纯字面量，与 Mimosa 过审形态一致）。

用法:
    python scripts/gen_orm_column_ddl.py > /tmp/align_block.txt

规则:
- 跳过主键列
- 跳过 NOT NULL 且无 server_default 的列 (SQLite ALTER 硬限制不可 ADD;
  这些列只在全新库出现, create_all 已带约束)
- DDL 形态: 类型 + DEFAULT '<server_default>' + NOT NULL (SQLite 接受
  DEFAULT 在 NOT NULL 之前)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.models import Base  # noqa: E402


def main():
    print('_ALIGN_DDL = {')
    for table in sorted(Base.metadata.tables):
        for col in Base.metadata.tables[table].columns:
            if col.primary_key:
                continue
            if col.nullable is False and col.server_default is None:
                continue
            t = col.type.compile()
            if col.server_default is not None:
                t += " DEFAULT '%s'" % col.server_default.arg
            if col.nullable is False:
                t += " NOT NULL"
            stmt = "ALTER TABLE %s ADD COLUMN %s %s" % (table, col.name, t)
            mig = "align:%s.%s" % (table, col.name)
            ins = (
                "INSERT OR IGNORE INTO schema_migrations (migration, note) "
                "VALUES ('%s', 'orm-align')" % mig
            )
            print('    "%s": ("%s", "%s",' % (stmt, table, col.name))
            print('        "%s"),' % ins)
    print('}')


if __name__ == "__main__":
    main()
