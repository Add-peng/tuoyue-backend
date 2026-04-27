"""
user_db.py - MySQL（Prisma）用户数据访问层

职责：
  - 用户注册：写入 User 表（phone/tier/credits）
  - 用户查询：按手机号、按 ID
  - 积分读写：读取/更新 User.credits 字段

Redis 保留项（不走这里）：
  - 短信验证码
  - 频率限制
  - 任务状态
"""

from __future__ import annotations

import logging
from typing import Optional

from lib.prisma import get_db

logger = logging.getLogger("tuoyue")

# Prisma 字段默认值（与 schema.prisma 保持一致）
DEFAULT_TIER = "Free"
DEFAULT_CREDITS = 100


async def get_user_by_phone(phone: str) -> Optional[dict]:
    """
    根据手机号查询用户（不含密码敏感字段）。
    返回 dict 或 None。
    """
    db = await get_db()
    user = await db.user.find_first(
        where={"phone": phone},
    )
    if not user:
        return None
    return {
        "id": user.id,
        "phone": user.phone,
        "tier": user.tier,
        "credits": user.credits,
        "createdAt": user.createdAt,
        "updatedAt": user.updatedAt,
    }


async def get_user_by_id(user_id: str) -> Optional[dict]:
    """
    根据用户 ID（Prisma cuid）查询用户。
    """
    db = await get_db()
    user = await db.user.find_first(
        where={"id": user_id},
    )
    if not user:
        return None
    return {
        "id": user.id,
        "phone": user.phone,
        "tier": user.tier,
        "credits": user.credits,
        "createdAt": user.createdAt,
        "updatedAt": user.updatedAt,
    }


async def create_user(phone: str) -> tuple[dict, bool]:
    """
    创建新用户。

    - 先查 phone 是否已存在（唯一索引），存在则抛 ValueError
    - 不存在则创建，credits 使用 schema 默认值 100

    返回: (user_dict, is_new_user)
    异常: ValueError("用户已存在")
    """
    db = await get_db()

    existing = await db.user.find_first(where={"phone": phone})
    if existing:
        raise ValueError("用户已存在")

    user = await db.user.create(
        data={
            "phone": phone,
            "tier": DEFAULT_TIER,
            "credits": DEFAULT_CREDITS,
        }
    )
    logger.info("User created in MySQL", extra={"user_id": user.id, "phone_masked": _mask_phone(phone)})

    return {
        "id": user.id,
        "phone": user.phone,
        "tier": user.tier,
        "credits": user.credits,
        "createdAt": user.createdAt,
        "updatedAt": user.updatedAt,
    }, True


async def get_or_create_user(phone: str) -> tuple[dict, bool]:
    """
    获取或创建用户（注册即登录语义）。
    返回: (user, is_new_user)
    """
    existing = await get_user_by_phone(phone)
    if existing:
        return existing, False
    return await create_user(phone)


# ── 积分操作（由 billing_service 调用）─────────────────────────────

async def mysql_get_credits(user_id: str) -> int:
    """查询用户积分余额（MySQL）。"""
    db = await get_db()
    user = await db.user.find_first(where={"id": user_id})
    return user.credits if user else 0


async def mysql_grant_credits(user_id: str, amount: int) -> int:
    """
    增加用户积分（原子 +N）。
    返回: 更新后的积分余额
    """
    if amount <= 0:
        raise ValueError("amount must be > 0")
    db = await get_db()
    user = await db.user.update(
        where={"id": user_id},
        data={"credits": {"increment": amount}},
    )
    logger.info(
        "Credits granted (MySQL)",
        extra={"user_id": user_id, "amount": amount, "balance": user.credits},
    )
    return user.credits


async def mysql_deduct_credits(user_id: str, amount: int) -> tuple[bool, int]:
    """
    原子扣减用户积分（乐观锁风格：先查后改）。

    返回: (success, remaining_balance)
      success=True  → 扣减成功
      success=False → 余额不足或用户不存在
    """
    if amount <= 0:
        return False, 0
    db = await get_db()
    try:
        user = await db.user.find_first(where={"id": user_id})
        if not user or user.credits < amount:
            current = user.credits if user else 0
            logger.warning(
                "Credits insufficient",
                extra={"user_id": user_id, "required": amount, "current": current},
            )
            return False, current

        updated = await db.user.update(
            where={"id": user_id},
            data={"credits": {"decrement": amount}},
        )
        logger.info(
            "Credits deducted (MySQL)",
            extra={"user_id": user_id, "deducted": amount, "remaining": updated.credits},
        )
        return True, updated.credits
    except Exception as e:
        logger.error("mysql_deduct_credits error: %s", e)
        return False, 0


def _mask_phone(phone: str) -> str:
    """手机号脱敏：138****5678"""
    if len(phone) == 11:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone[:3] + "****"
