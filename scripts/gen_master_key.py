"""生成 AIMC master key (API-Sec-1, 轻量化版)

用法:
    python scripts/gen_master_key.py

输出:
    AIMC_MASTER_KEY=<64 字符 hex = 32 bytes>
    # 复制到 .env 的 AIMC_MASTER_KEY= 后面
    # 部署时跟 .env 一起现生成 (每次部署 = 全新, 不需要跨环境同步)

API-Sec-1 设计: XOR+Base64 自实现加密, master key 32 bytes (hex 编码后 64 字符)
"""
import sys
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from security.crypto import generate_master_key


def main():
    key = generate_master_key()
    print("=" * 60)
    print("AIMC master key 生成成功 (API-Sec-1, 轻量化版)")
    print("=" * 60)
    print()
    print(f"AIMC_MASTER_KEY={key}")
    print()
    print("下一步:")
    print("  1. 复制上面 AIMC_MASTER_KEY=... 这一行到 .env")
    print("  2. 部署时跟 .env 一起现生成 (每次部署 = 全新 master key)")
    print()
    print("注意:")
    print("  - 本项目每次部署都是新开始, master key 不需要跨环境同步")
    print("  - 部署脚本现生成 .env + master key, 丢了 = 重新部署")
    print("  - 不要 commit master key 到 git (.gitignore 已排除 .env)")
    print("=" * 60)


if __name__ == "__main__":
    main()
