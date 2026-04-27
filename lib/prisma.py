"""
lib/prisma.py - Prisma Client 全局单例

用法示例：
    from lib.prisma import get_db

    async def some_endpoint():
        db = await get_db()
        user = await db.user.find_first(where={"phone": phone})

生命周期：
    - FastAPI startup 事件中调用 connect()
    - FastAPI shutdown 事件中调用 disconnect()
    - 其余代码通过 get_db() 获取已连接的客户端

注意：
    prisma-client-py 的 Prisma 实例是线程安全的，FastAPI 中可全局复用。
"""

from __future__ import annotations

import logging
from typing import Optional

from prisma import Prisma  # type: ignore

logger = logging.getLogger("tuoyue")

# 全局单例
_client: Optional[Prisma] = None


async def connect() -> Prisma:
    """
    建立数据库连接（在 FastAPI startup 中调用一次）。
    重复调用是安全的（已连接时直接返回）。
    """
    global _client
    if _client is None:
        _client = Prisma()
    if not _client.is_connected():
        await _client.connect()
        logger.info("Prisma connected to MySQL")
    return _client


async def disconnect() -> None:
    """断开数据库连接（在 FastAPI shutdown 中调用）。"""
    global _client
    if _client and _client.is_connected():
        await _client.disconnect()
        logger.info("Prisma disconnected")


async def get_db() -> Prisma:
    """
    获取全局 Prisma 客户端（懒连接）。
    可在路由函数、服务函数中直接调用。
    """
    global _client
    if _client is None or not _client.is_connected():
        return await connect()
    return _client
