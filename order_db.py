"""
order_db.py - MySQL（Prisma）订单数据访问层

职责：
  - 订单创建：写入 Order 表（status=pending）
  - 订单更新：支付成功后更新 status=paid + trade_no + paidAt
  - 订单查询：按 order_id 查询
  - 幂等检查：判断订单是否已处理

Redis 保留项（不走这里）：
  - 验证码
  - 频率限制
  - 任务状态
  - 临时缓存（webhook 回调前快速写入 Redis）

关键：webhook 回调时先查 MySQL（幂等），再更新（避免重复发放积分）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from lib.prisma import get_db

logger = logging.getLogger("tuoyue")


async def create_order(
    order_id: str,
    user_id: str,
    amount: str,
    credits: int,
) -> dict:
    """
    创建待支付订单（写入 MySQL Order 表）。

    :param order_id: 我方生成的全局唯一订单号（UUID，无连字符）
    :param user_id:  购买用户 ID（Prisma cuid）
    :param amount:   订单金额（字符串，如 "10.00"）
    :param credits:  本次购买积分（整数）
    :return: 订单 dict（含 createdAt）
    """
    db = await get_db()
    order = await db.order.create(
        data={
            "id": order_id,
            "userId": user_id,
            "amount": amount,
            "credits": credits,
            "status": "pending",
        }
    )
    logger.info(
        "Order created (MySQL)",
        extra={
            "order_id": order.id,
            "user_id": order.userId,
            "amount": order.amount,
            "credits": order.credits,
        },
    )
    return {
        "id": order.id,
        "userId": order.userId,
        "amount": order.amount,
        "credits": order.credits,
        "status": order.status,
        "tradeNo": order.tradeNo,
        "paidAt": order.paidAt,
        "createdAt": order.createdAt,
    }


async def get_order(order_id: str) -> Optional[dict]:
    """
    按 order_id 查询订单。

    :return: 订单 dict 或 None
    """
    db = await get_db()
    order = await db.order.find_first(
        where={"id": order_id},
    )
    if not order:
        return None
    return {
        "id": order.id,
        "userId": order.userId,
        "amount": order.amount,
        "credits": order.credits,
        "status": order.status,
        "tradeNo": order.tradeNo,
        "paidAt": order.paidAt,
        "createdAt": order.createdAt,
    }


async def mark_order_paid(order_id: str, trade_no: str) -> Optional[dict]:
    """
    将订单标记为已支付（幂等）。

    :param order_id: 我方订单号
    :param trade_no: 支付宝交易号
    :return: 更新后的订单 dict，或 None（订单不存在）
    """
    db = await get_db()
    order = await db.order.find_first(where={"id": order_id})
    if not order:
        logger.warning("mark_order_paid: order not found, order_id=%s", order_id)
        return None

    # 幂等：已支付订单不重复处理
    if order.status == "paid":
        logger.info("mark_order_paid: already paid, skipping. order_id=%s", order_id)
        return {
            "id": order.id,
            "userId": order.userId,
            "amount": order.amount,
            "credits": order.credits,
            "status": order.status,
            "tradeNo": order.tradeNo,
            "paidAt": order.paidAt,
            "createdAt": order.createdAt,
        }

    updated = await db.order.update(
        where={"id": order_id},
        data={
            "status": "paid",
            "tradeNo": trade_no,
            "paidAt": datetime.now(timezone.utc),
        },
    )
    logger.info(
        "Order marked paid (MySQL)",
        extra={
            "order_id": updated.id,
            "trade_no": trade_no,
            "user_id": updated.userId,
            "credits": updated.credits,
        },
    )
    return {
        "id": updated.id,
        "userId": updated.userId,
        "amount": updated.amount,
        "credits": updated.credits,
        "status": updated.status,
        "tradeNo": updated.tradeNo,
        "paidAt": updated.paidAt,
        "createdAt": updated.createdAt,
    }


async def is_order_paid(order_id: str) -> bool:
    """快速幂等检查：订单是否已支付。"""
    db = await get_db()
    order = await db.order.find_first(where={"id": order_id})
    return order is not None and order.status == "paid"
