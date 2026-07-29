"""AIMC 安全模块

Phase API-Sec-1 (2026-07-29, 轻量化版):
- DB api_key 加密存储 (XOR+Base64 自实现, 零外部依赖)
- 不加密 .env 本身 (避免 "开机输入 master key 解密 .env 才能解 DB" 的鸡生蛋问题)
- 每次部署重新生成 master key (无跨环境同步需求)

详细设计: docs/api-key-security.md
"""

import logging

logger = logging.getLogger(__name__)
