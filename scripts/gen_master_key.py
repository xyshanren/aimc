"""生成 AIMC master key (API-Sec-1)

用法:
    python scripts/gen_master_key.py

输出:
    AIMC_MASTER_KEY=<44 字符 base64>
    # 复制到 .env 的 AIMC_MASTER_KEY= 后面
    # 同时存到密码管理器 (master key 丢了 = 全部 ciphertext 不可解)

API-Sec-1 设计: Fernet master key 32 bytes (url-safe base64 编码后 44 字符)
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
    print("AIMC master key 生成成功 (API-Sec-1)")
    print("=" * 60)
    print()
    print(f"AIMC_MASTER_KEY={key}")
    print()
    print("下一步:")
    print("  1. 复制上面 AIMC_MASTER_KEY=... 这一行到 .env")
    print("  2. 同时存到密码管理器 (1Password / Bitwarden / etc.)")
    print()
    print("⚠️  警告:")
    print("  - master key 丢了 = 全部 ciphertext 不可解 = 永久性数据丢失")
    print("  - master key 跟 DB 文件 (./data/aimc.db) 分开保存")
    print("  - 不要 commit master key 到 git (.gitignore 已排除 .env)")
    print("=" * 60)


if __name__ == "__main__":
    main()
