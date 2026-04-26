"""
billing_service.py - 积分计费服务

Redis Key 规范：
  user:{user_id}:credits  → 整数，用户当前积分余额

计费规则：
  输入/输出 Token 统一按 $0.001 / 1K tokens 换算积分
  不足 1 积分按 1 积分计（向上取整）

新用户奖励：
  注册时自动发放 INITIAL_CREDITS = 100 积分
"""

import math
import logging
from typing import Optional

logger = logging.getLogger("tuoyue")

# ===================== 配置常量 =====================
INITIAL_CREDITS: int = 100          # 新用户注册奖励积分
MIN_CREDITS_REQUIRED: int = 10      # 生成接口最低消费门槛（积分）
CREDITS_PER_1K_TOKENS: float = 1.0  # 每 1K tokens 消耗 1 积分（$0.001/1K 换算）

# ===================== Redis 客户端 =====================
_redis_client = None


def set_redis_client(rc) -> None:
    """由 main.py 注入共享 Redis 实例，避免重复建连"""
    global _redis_client
    _redis_client = rc


def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis as _redis_lib
        _redis_client = _redis_lib.Redis(host="localhost", port=6379, decode_responses=True)
    return _redis_client


# ===================== Redis Key =====================

def _credits_key(user_id: str) -> str:
    return f"user:{user_id}:credits"


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


def get_credits(user_id: str) -> int:
    """
    查询用户当前积分余额（key 不存在时返回 0）。

    TODO: 接入真实 DB 时替换为：
      wallet = db.credit_wallets.find_first(where={"user_id": user_id})
      return wallet.balance if wallet else 0
    """
    try:
        r = _get_redis()
        val = r.get(_credits_key(user_id))
        return int(val) if val is not None else 0
    except Exception as e:
        logger.error("get_credits: Redis error for user %s: %s", user_id, e)
        return 0


def grant_credits(user_id: str, amount: int, reason: str = "manual") -> int:
    """
    发放积分（原子 INCRBY）。

    :param user_id: 目标用户
    :param amount:  发放数量（必须 > 0）
    :param reason:  日志用途标注（如 "new_user_bonus"、"admin_grant"）
    :return: 发放后的积分余额

    TODO: 接入真实 DB 时替换为：
      db.credit_transactions.create(
          data={"user_id": user_id, "amount": amount, "reason": reason}
      )
      db.credit_wallets.upsert(
          where={"user_id": user_id},
          update={"balance": {"increment": amount}},
          create={"user_id": user_id, "balance": amount},
      )
    """
    if amount <= 0:
        raise ValueError(f"grant_credits: amount must be > 0, got {amount}")
    try:
        r = _get_redis()
        new_balance = r.incrby(_credits_key(user_id), amount)
        logger.info(
            "Credits granted",
            extra={"user_id": user_id, "amount": amount, "balance": new_balance, "reason": reason},
        )
        return new_balance
    except Exception as e:
        logger.error("grant_credits: Redis error for user %s: %s", user_id, e)
        raise


def deduct_credits(user_id: str, credits: int) -> tuple[bool, int]:
    """
    从 Redis 原子扣减积分（Lua 脚本保证原子性）。

    :return: (success, remaining_balance)
      success=True  → 扣减成功
      success=False → 余额不足或 Redis 异常（不扣减）

    TODO: 接入真实 DB 时替换为带锁的事务：
      wallet = db.credit_wallets.find_first(where={"user_id": user_id})
      if wallet.balance < credits:
          return False, wallet.balance
      db.credit_wallets.update(
          where={"user_id": user_id},
          data={"balance": {"decrement": credits}},
      )
    """
    lua_script = """
local key = KEYS[1]
local amount = tonumber(ARGV[1])
local current = tonumber(redis.call('GET', key) or 0)
if current < amount then
    return {0, current}
end
local new_val = redis.call('DECRBY', key, amount)
return {1, new_val}
"""
    try:
        r = _get_redis()
        result = r.eval(lua_script, 1, _credits_key(user_id), credits)
        success = bool(result[0])
        remaining = int(result[1])
        if success:
            logger.info(
                "Credits deducted",
                extra={"user_id": user_id, "deducted": credits, "remaining": remaining},
            )
        else:
            logger.warning(
                "Credits insufficient",
                extra={"user_id": user_id, "required": credits, "current": remaining},
            )
        return success, remaining
    except Exception as e:
        logger.error("deduct_credits: Redis error for user %s: %s", user_id, e)
        return False, 0


def has_sufficient_credits(user_id: str, min_credits: int = MIN_CREDITS_REQUIRED) -> bool:
    """
    检查用户积分是否达到最低门槛（用于接口前置校验）。
    """
    return get_credits(user_id) >= min_credits
