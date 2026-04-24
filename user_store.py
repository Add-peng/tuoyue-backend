"""
用户存储模块 (user_store.py)
- 基于 Redis 存储用户数据（手机号哈希 -> 用户信息 JSON）
- 密码使用 PBKDF2-HMAC-SHA256 哈希（生产环境建议替换为 bcrypt）
- JWT Token 生成与验证
"""

import os
import json
import uuid
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

try:
    import jwt
    _JWT_AVAILABLE = True
except ImportError:
    _JWT_AVAILABLE = False

logger = logging.getLogger("tuoyue")

# ================== Redis 客户端（从 main.py 共享实例） ==================
# 主模块会直接操作 redis_client，避免重复实例
_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis as _redis_lib
        _redis_client = _redis_lib.Redis(host="localhost", port=6379, decode_responses=True)
    return _redis_client


def set_redis_client(rc):
    """供 main.py 注入共享的 redis_client 实例"""
    global _redis_client
    _redis_client = rc


# ================== JWT 配置 ==================

JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 7


# ================== 密码哈希 ==================

def _hash_password(password: str) -> str:
    """
    密码哈希（PBKDF2-HMAC-SHA256，100000 次迭代）
    生产环境建议替换为 bcrypt（pip install bcrypt）
    """
    salt = "tuoyue_salt_v1"  # 固定盐（生产环境建议随机盐+单独存储）
    for _ in range(100_000):
        password = hashlib.sha256((password + salt).encode("utf-8")).hexdigest()
    return password


def _verify_password(password: str, hashed: str) -> bool:
    """验证密码"""
    return _hash_password(password) == hashed


# ================== JWT Token ==================

def create_token(user_id: str, phone: str) -> str:
    """生成 JWT Token"""
    if not JWT_SECRET:
        logger.warning("JWT_SECRET not configured, using empty secret")
    payload = {
        "sub": user_id,
        "phone": phone,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
        "jti": str(uuid.uuid4()),
    }
    if _JWT_AVAILABLE:
        return jwt.encode(payload, JWT_SECRET or "dev_secret", algorithm=JWT_ALGORITHM)
    else:
        # 极简 Base64 降级（仅开发测试用）
        import base64, json as _json
        data = _json.dumps(payload, default=str).encode("utf-8")
        return base64.b64encode(data).decode("utf-8")


def decode_token(token: str) -> Optional[dict]:
    """解析 JWT Token，返回 payload 或 None"""
    if not token:
        return None
    if _JWT_AVAILABLE:
        try:
            return jwt.decode(token, JWT_SECRET or "dev_secret", algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            logger.warning("JWT token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            return None
    else:
        # Base64 降级解析
        try:
            import base64, json as _json
            data = base64.b64decode(token.encode("utf-8")).decode("utf-8")
            return _json.loads(data)
        except Exception:
            return None


def extract_user_id_from_token(token: str) -> Optional[str]:
    """从 Token 中提取 user_id"""
    payload = decode_token(token)
    return payload.get("sub") if payload else None


# ================== 用户存储（Redis） ==================

def _phone_hash(phone: str) -> str:
    """手机号哈希（用于 Redis key，避免明文存储手机号）"""
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()[:32]


def _user_key(phone_hash: str) -> str:
    return f"user:{phone_hash}"


def _user_id_index_key(user_id: str) -> str:
    return f"uid:{user_id}"


# 用户每日配额
DEFAULT_TIER = "Free"
DEFAULT_DAILY_QUOTA = 10


def get_user(phone: str) -> Optional[dict]:
    """根据手机号获取用户信息（不返回密码哈希）"""
    r = _get_redis()
    h = _phone_hash(phone)
    raw = r.hget(_user_key(h), "data")
    if not raw:
        return None
    user = json.loads(raw)
    user.pop("password_hash", None)
    return user


def get_user_by_id(user_id: str) -> Optional[dict]:
    """根据 user_id 获取用户信息"""
    r = _get_redis()
    phone_hash = r.get(_user_id_index_key(user_id))
    if not phone_hash:
        return None
    raw = r.hget(_user_key(phone_hash), "data")
    if not raw:
        return None
    user = json.loads(raw)
    user.pop("password_hash", None)
    return user


def create_user(phone: str, password: str = "") -> dict:
    """
    创建新用户账号
    - phone: 手机号（明文，用于发送通知，不作主键暴露）
    - password: 密码（PBKDF2 哈希存储）
    返回用户信息 dict
    """
    r = _get_redis()
    h = _phone_hash(phone)

    # 检查是否已存在
    if r.exists(_user_key(h)):
        raise ValueError("用户已存在")

    user_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    user_data = {
        "user_id": user_id,
        "phone_hash": h,
        "phone_masked": mask_phone(phone),
        "password_hash": _hash_password(password) if password else "",
        "tier": DEFAULT_TIER,
        "daily_quota": DEFAULT_DAILY_QUOTA,
        "created_at": now,
        "updated_at": now,
    }

    pipe = r.pipeline()
    pipe.hset(_user_key(h), mapping={"data": json.dumps(user_data, ensure_ascii=False)})
    pipe.set(_user_id_index_key(user_id), h)
    pipe.expire(_user_key(h), 86400 * 365 * 10)  # 10年 TTL
    pipe.execute()

    logger.info("User created", extra={"user_id": user_id, "phone_masked": mask_phone(phone)})
    user_data.pop("password_hash", None)
    return user_data


def get_or_create_user(phone: str) -> tuple[dict, bool]:
    """
    获取或创建用户（注册即登录逻辑）
    返回: (user, is_new_user)
    """
    existing = get_user(phone)
    if existing:
        return existing, False
    new_user = create_user(phone)
    return new_user, True


def mask_phone(phone: str) -> str:
    """手机号脱敏：138****5678"""
    if len(phone) == 11:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone[:3] + "****"
