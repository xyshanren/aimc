"""Fernet 加密封装

API-Sec-1: DB api_key 加密存储的核心模块.

设计要点:
- 唯一加密算法: Fernet (AES-128-CBC + HMAC-SHA256, 32-byte key, url-safe base64)
- 不做 key 派生: master key 直接作为 Fernet key (避免 PBKDF2 慢 + 简化)
- 不缓存 Fernet 实例: 每次 encrypt/decrypt 重新构造 (开销可忽略, 启动时 cache 已足够)

调用方:
- scripts/manual_import.py: 写新 channel 时 encrypt(plaintext_key)
- gateway/router.py: refresh_cache 时 decrypt(ciphertext)
- scripts/migrate_encrypt_keys.py: 老明文迁移
"""
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def generate_master_key() -> str:
    """生成新的 master key (44 字符 url-safe base64, 32 bytes 原始)

    用法:
        $ python scripts/gen_master_key.py
        AIMC_MASTER_KEY=XUfuUsVRaaiEYZp80gaExGsfRMMAi8vAlLQxW8N_5Us=
        # ↑ 复制到 .env 的 AIMC_MASTER_KEY= 后面
        # ↑ 同时存到密码管理器 (master key 丢了 = 全部 ciphertext 不可解)
    """
    return Fernet.generate_key().decode("ascii")


def _make_fernet(master_key: str) -> Fernet:
    """构造 Fernet 实例, master key 校验放一起"""
    if not master_key:
        raise ValueError(
            "AIMC_MASTER_KEY 未设置. 运行 scripts/gen_master_key.py 生成, "
            "填到 .env 的 AIMC_MASTER_KEY= 后面"
        )
    try:
        return Fernet(master_key.encode("ascii"))
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"AIMC_MASTER_KEY 格式错误 (必须是 44 字符 url-safe base64): {e}"
        ) from e


def encrypt(plaintext: str, master_key: str) -> bytes:
    """加密明文 → ciphertext bytes

    Args:
        plaintext: 明文 (API key / webhook url 等)
        master_key: 44 字符 url-safe base64 master key

    Returns:
        Fernet ciphertext (bytes), 存 DB BLOB 字段
    """
    if plaintext is None:
        raise ValueError("plaintext 不能为 None")
    fernet = _make_fernet(master_key)
    return fernet.encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes, master_key: str) -> str:
    """解密密文 → 明文 str

    Args:
        ciphertext: Fernet ciphertext bytes (从 DB BLOB 读)
        master_key: 44 字符 url-safe base64 master key

    Returns:
        明文

    Raises:
        InvalidToken: master key 错 / ciphertext 被篡改 / Fernet 版本不匹配
        ValueError: ciphertext 为 None
    """
    if ciphertext is None:
        raise ValueError("ciphertext 不能为 None")
    fernet = _make_fernet(master_key)
    return fernet.decrypt(ciphertext).decode("utf-8")


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
    except InvalidToken:
        logger.error(
            "ciphertext 解密失败 (InvalidToken). "
            "可能原因: master key 错了 / ciphertext 被篡改 / Fernet 版本不匹配"
        )
        return None
    except Exception as e:
        logger.error(f"ciphertext 解密异常: {e}")
        return None
