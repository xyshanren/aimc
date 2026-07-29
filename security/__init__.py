"""AIMC 安全模块

Phase API-Sec-1 (2026-07-29):
- DB api_key 加密存储 (Fernet + master key in .env)
- 不加密 .env 本身 (避免 "开机输入 master key 解密 .env 才能解 DB" 的鸡生蛋问题)
- master key 丢了 = 永久性数据丢失 (迁移时强制让 user 存到密码管理器)

详细设计: docs/api-key-security.md
"""
