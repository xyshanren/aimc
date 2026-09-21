"""添加一个 user + api_key 到 AIMC 数据库 (V4.8 多用户)。

用法:
    # 新建 user 并发一把 key
    python scripts/add_user_key.py --username zhangsan --display-name 张三

    # 给已有 user 再加一把 key
    python scripts/add_user_key.py --username zhangsan --label 笔记本

    # admin key
    python scripts/add_user_key.py --username admin --admin

    # 设限额 (daily 100k, monthly 1M)
    python scripts/add_user_key.py --username zhangsan \
        --daily-limit 100000 --monthly-limit 1000000
"""

import argparse
import asyncio
import hashlib
import json
import secrets
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.engine import async_session_factory
from db.init_db import init_db
from db.models import User, ApiKey
from sqlalchemy import select


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="添加 AIMC 用户 + API Key (V4.8 多用户)")
    parser.add_argument("--username", required=True, help="用户名 (唯一)")
    parser.add_argument("--display-name", default=None, help="展示名 (e.g. 张三)")
    parser.add_argument("--label", default=None, help="key 标签 (e.g. 工作机 / 笔记本)")
    parser.add_argument("--admin", action="store_true", help="生成 admin key (可访问 dashboard 全局数据)")
    parser.add_argument("--daily-limit", type=int, default=None,
                        help="每日 token 限额 (NULL=不限, 0=禁用)")
    parser.add_argument("--monthly-limit", type=int, default=None,
                        help="每月 token 限额 (NULL=不限, 0=禁用)")
    parser.add_argument("--notes", default=None, help="用户备注 (e.g. 产品组)")
    parser.add_argument("--allowed-groups", default=None, dest="allowed_groups",
                        help="V6.11 组白名单, 逗号分隔 (e.g. tier:light,tier:balanced); "
                             "不传=不限。组名须存在于 model_groups, 无组成员裸模型默认拒绝")
    parser.add_argument("--role", default=None, choices=["admin", "user"],
                        help="用户角色 (仅新建 user 时生效, 默认 admin key → admin, 否则 user)")
    return parser


def _generate_api_key() -> str:
    """生成 aimc-<token_urlsafe(32)> 格式的 API key 明文"""
    return f"aimc-{secrets.token_urlsafe(32)}"


async def add_user_key(args: argparse.Namespace) -> tuple[int, int, str]:
    """创建/复用 user + 生成 api_key, 返回 (user_id, key_id, key_plain)

    key_plain 只在此返回一次, 调用方负责展示给用户保存。
    """
    await init_db()
    async with async_session_factory() as session:
        # V6.11: 组白名单校验 (组名必须存在于 model_groups, 防 tier:balance 式 typo 入库)
        allowed_groups = None
        if args.allowed_groups:
            allowed_groups = [g.strip() for g in args.allowed_groups.split(",") if g.strip()]
        if allowed_groups:
            from db.models import ModelGroup
            existing_groups = set((await session.execute(
                select(ModelGroup.group_id)
            )).scalars().all())
            unknown = [g for g in allowed_groups if g not in existing_groups]
            if unknown:
                print(f"错误: 未知组 {unknown} (合法组: {sorted(existing_groups)})", file=sys.stderr)
                sys.exit(2)

        # 查或建 user
        user = (await session.execute(
            select(User).where(User.username == args.username)
        )).scalars().first()

        if user is None:
            # 新建 user: role 默认 admin key → admin, 否则 user
            role = args.role or ("admin" if args.admin else "user")
            user = User(
                username=args.username,
                display_name=args.display_name or args.username,
                role=role,
                status="active",
                daily_token_limit=args.daily_limit,
                monthly_token_limit=args.monthly_limit,
                notes=args.notes,
            )
            session.add(user)
            await session.flush()
            print(f"用户创建成功: id={user.id}, username={user.username}, role={user.role}")
        else:
            # 已有 user: 只更新限额/备注 (若传了参数)
            updated_fields = []
            if args.daily_limit is not None and args.daily_limit != user.daily_token_limit:
                user.daily_token_limit = args.daily_limit
                updated_fields.append(f"daily_limit={args.daily_limit}")
            if args.monthly_limit is not None and args.monthly_limit != user.monthly_token_limit:
                user.monthly_token_limit = args.monthly_limit
                updated_fields.append(f"monthly_limit={args.monthly_limit}")
            if args.notes is not None and args.notes != user.notes:
                user.notes = args.notes
                updated_fields.append("notes")
            if updated_fields:
                print(f"用户已存在 (id={user.id}), 更新: {', '.join(updated_fields)}")
            else:
                print(f"用户已存在 (id={user.id}), 复用")

        # 生成新 key
        key_plain = _generate_api_key()
        key_hash = hashlib.sha256(key_plain.encode()).hexdigest()
        key_prefix = key_plain[:8]

        api_key = ApiKey(
            user_id=user.id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            is_admin=1 if args.admin else 0,
            status="active",
            label=args.label,
            allowed_groups=json.dumps(allowed_groups) if allowed_groups else None,
        )
        session.add(api_key)
        await session.commit()

        print()
        print("=" * 60)
        print("API Key 创建成功 (仅显示一次, 请立即保存)")
        print("=" * 60)
        print(f"  用户:   {user.username} (id={user.id})")
        print(f"  Key ID: {api_key.id}")
        print(f"  前缀:   {key_prefix}...")
        print(f"  Admin:  {'是' if api_key.is_admin else '否'}")
        print(f"  标签:   {args.label or '-'}")
        print(f"  组白名单: {', '.join(allowed_groups) if allowed_groups else '不限'}")
        print()
        print(f"  明文:   {key_plain}")
        print()
        print("注意:")
        print("  - 明文 key 不会再次显示, 丢失需重新生成")
        print("  - 客户端使用: Authorization: Bearer <明文 key>")
        print("  - 中间件缓存 30s, 新 key 最迟 30s 后生效")
        print("=" * 60)

        return user.id, api_key.id, key_plain


if __name__ == "__main__":
    user_id, key_id, key_plain = asyncio.run(add_user_key(build_parser().parse_args()))
