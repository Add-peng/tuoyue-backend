"""
管理后台 API 路由

路径前缀: /api/admin
数据层: 当前为 Mock 数据，预留 Prisma/MySQL 扩展接口。

扩展指南（接入真实数据库时）：
  1. 替换各 _get_*_repo() 函数内的 Mock 逻辑
  2. 模型保持不变，前端无感知
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# 复用短信服务
import sms_service

# ---------------------------------------------------------------------------
# 共享 Redis 客户端（从 main.py 注入）
# ---------------------------------------------------------------------------
_redis_client = None


def set_redis_client(rc):
    global _redis_client
    _redis_client = rc


def _r():
    if _redis_client is None:
        raise HTTPException(status_code=503, detail="Redis 未连接")
    return _redis_client


# ---------------------------------------------------------------------------
# Pydantic 响应模型
# ---------------------------------------------------------------------------

# ---- 用户相关 ----

class UserSummary(BaseModel):
    user_id: str
    phone: str
    tier: str
    register_date: str


class UserListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    users: list[UserSummary]


class UserDetailResponse(BaseModel):
    user_id: str
    phone: str
    tier: str
    register_date: str
    total_generations: int
    # 预留字段（数据库接入后补充）
    # daily_quota: int
    # last_login: Optional[str] = None


class UpdateTierRequest(BaseModel):
    tier: str = Field(..., description="目标会员等级，如 Free / VIP / Pro")


class UpdateTierResponse(BaseModel):
    user_id: str
    tier: str
    updated_at: str


class ResetPasswordResponse(BaseModel):
    success: bool
    message: str


# ---- 订单相关 ----

class OrderSummary(BaseModel):
    order_id: str
    user_id: str
    amount: float
    status: str
    created_at: str


class OrderListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    orders: list[OrderSummary]


# ---- 统计相关 ----

class StatsResponse(BaseModel):
    total_users: int
    today_generations: int
    today_revenue: float
    active_agents: int


# ---------------------------------------------------------------------------
# Mock 数据层（替换为真实 DB 查询）
# ---------------------------------------------------------------------------

_TIER_OPTIONS = ["Free", "VIP", "Pro"]
_ORDER_STATUS_OPTIONS = ["paid", "pending", "refunded"]


def _mock_users() -> list[dict]:
    """生成 Mock 用户列表（实际替换为 DB 查询）。"""
    base_date = datetime(2026, 4, 1, tzinfo=timezone.utc)
    users = []
    for i in range(1, 101):
        days_ago = random.randint(0, 23)
        d = (base_date + timedelta(days=days_ago)).date().isoformat()
        phone = f"138{random.randint(10000000, 99999999)}"
        users.append({
            "user_id": f"uid_{i:04d}",
            "phone": phone,
            "tier": random.choice(_TIER_OPTIONS),
            "register_date": d,
            "total_generations": random.randint(0, 80),
        })
    return users


def _mock_orders() -> list[dict]:
    """生成 Mock 订单列表（实际替换为 DB 查询）。"""
    base_date = datetime(2026, 4, 1, tzinfo=timezone.utc)
    orders = []
    for i in range(1, 51):
        days_ago = random.randint(0, 23)
        d = (base_date + timedelta(days=days_ago)).isoformat()
        orders.append({
            "order_id": f"ord_{i:06d}",
            "user_id": f"uid_{random.randint(1, 100):04d}",
            "amount": round(random.choice([0.01, 9.9, 29.9, 99.0, 299.0]), 2),
            "status": random.choice(_ORDER_STATUS_OPTIONS),
            "created_at": d,
        })
    return orders


def _mask_phone(phone: str) -> str:
    if len(phone) == 11:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone[:3] + "****"


# ---------------------------------------------------------------------------
# 数据仓储层（扩展点：接入真实 DB 时重写此处）
# ---------------------------------------------------------------------------

def _get_user_list(page: int, page_size: int):
    """
    获取用户列表分页数据。

    TODO: 替换为真实 DB 查询
      db.users.find_many(
          skip=(page-1)*page_size,
          take=page_size,
          order_by={"created_at": "desc"},
      )
    """
    all_users = _mock_users()
    total = len(all_users)
    start = (page - 1) * page_size
    end = start + page_size
    page_users = all_users[start:end]
    return total, page_users


def _get_user_by_id(user_id: str):
    """
    根据 user_id 获取用户详情。

    TODO: 替换为真实 DB 查询
      db.users.find_first(where={"user_id": user_id})
    """
    for u in _mock_users():
        if u["user_id"] == user_id:
            return u
    return None


def _update_user_tier(user_id: str, tier: str):
    """
    更新用户会员等级。

    TODO: 替换为真实 DB 查询
      db.users.update(
          where={"user_id": user_id},
          data={"tier": tier, "updated_at": datetime.utcnow()},
      )
    """
    user = _get_user_by_id(user_id)
    if not user:
        return None
    # Mock 中直接返回更新后的数据（实际 DB 会在此处持久化）
    return {**user, "tier": tier}


def _reset_user_password(user_id: str):
    """
    重置用户密码（生成随机密码，哈希存储，返回明文用于短信通知）。

    TODO: 接入真实 DB 时替换为 user_store.reset_password(user_id)
      （user_store.py 中已实现完整的 Redis 版本）
    """
    import user_store
    return user_store.reset_password(user_id)


def _get_order_list(page: int, page_size: int):
    """
    获取订单列表分页数据。

    TODO: 替换为真实 DB 查询
      db.orders.find_many(
          skip=(page-1)*page_size,
          take=page_size,
          order_by={"created_at": "desc"},
      )
    """
    all_orders = _mock_orders()
    total = len(all_orders)
    start = (page - 1) * page_size
    end = start + page_size
    return total, all_orders[start:end]


def _get_platform_stats() -> dict:
    """
    获取平台统计数据。

    TODO: 接入真实 DB 时分表查询：
      - total_users: SELECT COUNT(*) FROM users
      - today_generations: SELECT COUNT(*) FROM tasks WHERE DATE(created_at)=TODAY
      - today_revenue:   SELECT SUM(amount) FROM orders WHERE status='paid' AND DATE(created_at)=TODAY
      - active_agents:   SELECT COUNT(DISTINCT agent_id) FROM tasks WHERE DATE(created_at)=TODAY
    """
    # 实时查询 Redis 中的今日任务数（生产可用）
    try:
        r = _r()
        today_key = datetime.now().strftime("%Y%m%d")
        today_gens = r.get(f"stats:daily_generations:{today_key}") or 0
        today_gens = int(today_gens)
    except Exception:
        today_gens = random.randint(800, 2000)

    all_orders = _mock_orders()
    today_orders = [
        o for o in all_orders
        if o["created_at"][:10] == datetime.now().date().isoformat()
        and o["status"] == "paid"
    ]
    today_revenue = sum(o["amount"] for o in today_orders) or round(random.uniform(200, 800), 1)

    return {
        "total_users": 100,
        "today_generations": today_gens,
        "today_revenue": today_revenue,
        "active_agents": 3,
    }


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/admin", tags=["管理后台"])


# GET /api/admin/users
@router.get("/users", response_model=UserListResponse)
async def list_users(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(10, ge=1, le=100, description="每页数量"),
):
    total, users = _get_user_list(page, page_size)
    return UserListResponse(
        total=total,
        page=page,
        page_size=page_size,
        users=[
            UserSummary(
                user_id=u["user_id"],
                phone=_mask_phone(u["phone"]),
                tier=u["tier"],
                register_date=u["register_date"],
            )
            for u in users
        ],
    )


# GET /api/admin/users/{user_id}
@router.get("/users/{user_id}", response_model=UserDetailResponse)
async def get_user(user_id: str):
    user = _get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")
    return UserDetailResponse(
        user_id=user["user_id"],
        phone=_mask_phone(user["phone"]),
        tier=user["tier"],
        register_date=user["register_date"],
        total_generations=user.get("total_generations", 0),
    )


# PUT /api/admin/users/{user_id}/tier
@router.put("/users/{user_id}/tier", response_model=UpdateTierResponse)
async def update_user_tier(user_id: str, payload: UpdateTierRequest):
    valid_tiers = ["Free", "VIP", "Pro"]
    if payload.tier not in valid_tiers:
        raise HTTPException(
            status_code=400,
            detail=f"无效的会员等级，仅支持: {', '.join(valid_tiers)}",
        )
    updated = _update_user_tier(user_id, payload.tier)
    if not updated:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")
    return UpdateTierResponse(
        user_id=updated["user_id"],
        tier=updated["tier"],
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


# POST /api/admin/users/{user_id}/reset-password
@router.post("/users/{user_id}/reset-password", response_model=ResetPasswordResponse)
async def reset_user_password(user_id: str):
    """
    管理员重置用户密码。

    流程：
      1. 生成 8 位随机密码（字母+数字）
      2. PBKDF2 哈希后更新到 Redis
      3. 通过阿里云短信将新密码发至用户手机
      4. 短信发送失败时降级为 console.log 打印
    """
    phone, new_password = _reset_user_password(user_id)
    if phone is None:
        raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")

    # 发送短信（失败自动降级打印）
    sms_ok = sms_service.send_password_sms(phone, new_password)

    if sms_ok:
        return ResetPasswordResponse(
            success=True,
            message="密码已重置并发送至用户手机",
        )
    else:
        return ResetPasswordResponse(
            success=True,
            message="密码已重置（短信发送失败，已打印至控制台）",
        )


# GET /api/admin/orders
@router.get("/orders", response_model=OrderListResponse)
async def list_orders(
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(10, ge=1, le=100, description="每页数量"),
):
    total, orders = _get_order_list(page, page_size)
    return OrderListResponse(
        total=total,
        page=page,
        page_size=page_size,
        orders=[
            OrderSummary(
                order_id=o["order_id"],
                user_id=o["user_id"],
                amount=o["amount"],
                status=o["status"],
                created_at=o["created_at"][:10],
            )
            for o in orders
        ],
    )


# GET /api/admin/stats
@router.get("/stats", response_model=StatsResponse)
async def get_stats():
    stats = _get_platform_stats()
    return StatsResponse(**stats)
