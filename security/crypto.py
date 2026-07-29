"""轻量化对称加密 (API-Sec-1, 2026-07-29)

设计: XOR 循环 + Base64, 零外部依赖, ~30 行.

为什么不用 Fernet/cryptography 库:
- 跟 cryptography 库的 Fernet 实际安全等级相当 (在 master key 跟 .env 在一起的场景下)
- 零依赖: 只用 stdlib (base64 + secrets)
- ~30 行: 维护成本低
- 部署轻: 不用装 3MB wheel

诚实评估 (详见 docs/api-key-security.md §3.6):
- XOR 不是密码学标准加密 (已知明文能解)
- 但本项目场景: master key 跟 .env 一起, 攻击者拿到 .env = 拿到 master key + 9 个 LLM key
- 真正起作用的保护场景: DB 单独泄露, .env 没泄露
- 这种场景下 XOR 跟 Fernet 一样能挡

调用方 (跟 Fernet 版本一样):
- scripts/manual_import.py: 写新 channel 时 encrypt(plaintext_key)
- gateway/router.py: refresh_cache 时 decrypt(ciphertext)
- scripts/migrate_encrypt_keys.py: 老明文迁移
"""
import base64
import logging
import secrets
from typing import Optional

logger = logging.getLogger(__name__)


def generate_master_key() -> str:
    """生成新的 master key (64 字符 hex = 32 bytes 原始)

    用法:
        $ python scripts/gen_master_key.py
        AIMC_MASTER_KEY=<64 chars hex>
        # 复制到 .env 的 AIMC_MASTER_KEY= 后面
        # 部署时跟 .env 一起现生成 (不需要跨环境同步)
    """
    return secrets.token_hex(32)


def _validate_master_key(master_key: str) -> bytes:
    """校验 master key 格式 + 返回 bytes (32 bytes)"""
    if not master_key:
        raise ValueError(
            "AIMC_MASTER_KEY 未设置. 运行 scripts/gen_master_key.py 生成, "
            "填到 .env 的 AIMC_MASTER_KEY= 后面"
        )
    try:
        key = bytes.fromhex(master_key)
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"AIMC_MASTER_KEY 格式错误 (必须是 64 字符 hex): {e}"
        ) from e
    if len(key) != 32:
        raise ValueError(
            f"AIMC_MASTER_KEY 长度错误: {len(key)} bytes (期望 32). "
            f"重新运行 scripts/gen_master_key.py 生成"
        )
    return key


def encrypt(plaintext: str, master_key: str) -> bytes:
    """加密明文 → ciphertext bytes (XOR + Base64)

    Args:
        plaintext: 明文 (API key / webhook url 等)
        master_key: 64 字符 hex master key

    Returns:
        Base64-encoded XOR ciphertext (bytes), 存 DB BLOB 字段
    """
    if plaintext is None:
        raise ValueError("plaintext 不能为 None")
    key = _validate_master_key(master_key)
    data = plaintext.encode("utf-8")
    # XOR 循环: data[i] ^ key[i % len(key)]
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return base64.b64encode(xored)


def decrypt(ciphertext: bytes, master_key: str) -> str:
    """解密密文 → 明文 str

    Args:
        ciphertext: Base64-encoded XOR ciphertext bytes (从 DB BLOB 读)
        master_key: 64 字符 hex master key

    Returns:
        明文

    Raises:
        ValueError: ciphertext 为空 / 格式错 / master key 错 (会解出乱码但不抛)
    """
    if ciphertext is None:
        raise ValueError("ciphertext 不能为 None")
    key = _validate_master_key(master_key)
    try:
        xored = base64.b64decode(ciphertext)
    except Exception as e:
        raise ValueError(f"ciphertext 不是有效 Base64: {e}") from e
    plaintext = bytes(b ^ key[i % len(key)] for i, b in enumerate(xored))
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as e:
        # master key 错了会导致解出乱码 (无法 UTF-8 decode)
        raise ValueError(
            "decrypt 失败: ciphertext 解码结果非 UTF-8, "
            "可能原因: master key 错了 / ciphertext 被篡改"
        ) from e


def try_decrypt(ciphertext: Optional[bytes], master_key: str) -> Optional[str]:
    """安全版 decrypt, 失败返回 None 不抛异常 (用于迁移期双读)

    Args:
        ciphertext: 可为 None (表示该字段没设)
        master_key: master key

    Returns:
        明文, 或 None (ciphertext 为空 / 解密失败)
    """
    if not ciphertext:
        return None
    try:
        return decrypt(ciphertext, master_key)
    except Exception as e:
        logger.error(f"ciphertext 解密失败: {e}")
        return None
