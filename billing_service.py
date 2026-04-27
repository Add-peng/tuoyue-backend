"""
billing_service.py - 积分计费服务

数据层：MySQL（User.credits 字段），Prisma ORM
计费规则：
  输入/输出 Token 统一按 $0.001 / 1K tokens 换算积分
  不足 1 积分按 1 积分计（向上取整）
"""

import asyncio
import math
import logging
from typing import Optional

logger = logging.getLogger("tuoyue")

# ===================== 配置常量 =====================
INITIAL_CREDITS: int = 100          # 新用户注册奖励积分（由 schema.prisma User.credits 默认值承载）
MIN_CREDITS_REQUIRED: int = 10      # 生成接口最低消费门槛（积分）
CREDITS_PER_1K_TOKENS: float = 1.0  # 每 1K tokens 消耗 1 积分（$0.001/1K 换算）

# ===================== Redis 客户端（保留用于其他用途） =====================
_redis_client = None


def set_redis_client(rc) -> None:
    """由 main.py 注入共享 Redis 实例（用于短信验证码/频率限制/任务状态等）"""
    global _redis_client
    _redis_client = rc


# ===================== 核心函数 =====================

def calculate_credits(prompt_tokens: int, completion_tokens: int) -> int:
    """
    根据 Token 用量计算消耗积分（向上取整，最低 1 积分）。

    公式：ceil((prompt_tokens + completion_tokens) / 1000 * CREDITS_PER_1K_TOKENS)

    示例：
      prompt=500, completion=300 → total=800 → 0.8 积分 → 1 积分
      prompt=800, completion=500 → total=1300 → 1.3 积分 → 2 积分
    """
    total_tokens = prompt_tokens + completion_tokens
    raw = total_tokens / 1000.0 * CREDITS_PER_1K_TOKENS
    return max(1, math.ceil(raw))


# ── 同步入口（供 sync 上下文调用，内部自动处理 async）───────────────

def _run_async(coro):
    """在同步上下文中执行 async 函数（用于 background_tasks 等 sync 场景）"""
    try:
        loop = asyncio.get_running_loop()
        # 已在事件循环中，嵌套调用不可行，降级返回
        logger.warning("_run_async: already in async context, cannot nest")
        return None
    except RuntimeError:
        return asyncio.run(coro)


def get_credits(user_id: str) -> int:
    """
    查询用户当前积分余额（MySQL User.credits）。
    同步入口，内部调用 async 版本。
    """
    try:
        loop = asyncio.get_running_loop()
        # 在 async 上下文中，调度任务
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, _get_credits_async(user_id))
            return future.result()
    except RuntimeError:
        return asyncio.run(_get_credits_async(user_id))


async def _get_credits_async(user_id: str) -> int:
    """异步查询积分（供 async 上下文直接调用）"""
    from user_db import mysql_get_credits
    try:
        return await mysql_get_credits(user_id)
    except Exception as e:
        logger.error("get_credits: MySQL error for user %s: %s", user_id, e)
        return 0


def grant_credits(user_id: str, amount: int, reason: str = "manual") -> int:
    """
    发放积分（MySQL 原子 increment）。
    同步入口。

    :param user_id: 目标用户
    :param amount:  发放数量（必须 > 0）
    :param reason:  日志用途标注（如 "new_user_bonus"、"admin_grant"）
    :return: 发放后的积分余额
    """
    if amount <= 0:
        raise ValueError(f"grant_credits: amount must be > 0, got {amount}")
    try:
        return asyncio.run(_grant_credits_async(user_id, amount, reason))
    except Exception as e:
        logger.error("grant_credits: MySQL error for user %s: %s", user_id, e)
        raise


async def _grant_credits_async(user_id: str, amount: int, reason: str) -> int:
    """异步发放积分"""
    from user_db import mysql_grant_credits
    balance = await mysql_grant_credits(user_id, amount)
    logger.info(
        "Credits granted",
        extra={"user_id": user_id, "amount": amount, "balance": balance, "reason": reason},
    )
    return balance


def deduct_credits(user_id: str, amount: int) -> tuple[bool, int]:
    """
    从 MySQL 原子扣减积分。
    同步入口。

    :return: (success, remaining_balance)
      success=True  → 扣减成功
      success=False → 余额不足或异常（不扣减）
    """
    try:
        return asyncio.run(_deduct_credits_async(user_id, amount))
    except Exception as e:
        logger.error("deduct_credits: MySQL error for user %s: %s", user_id, e)
        return False, 0


async def _deduct_credits_async(user_id: str, amount: int) -> tuple[bool, int]:
    """异步扣减积分"""
    from user_db import mysql_deduct_credits
    return await mysql_deduct_credits(user_id, amount)


def has_sufficient_credits(user_id: str, min_credits: int = MIN_CREDITS_REQUIRED) -> bool:
    """
    检查用户积分是否达到最低门槛（MySQL）。
    """
    return get_credits(user_id) >= min_credits
