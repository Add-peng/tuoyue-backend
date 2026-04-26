"""
payment_service.py - 支付宝支付集成服务

功能：
  - 创建支付宝网页支付订单（alipay.trade.page.pay）
  - 验证支付宝异步通知签名
  - 查询订单状态

积分套餐：
  PACKAGES = {
    "pkg_10":  {"price": "10.00",  "credits": 100,  "name": "入门套餐"},
    "pkg_50":  {"price": "50.00",  "credits": 600,  "name": "标准套餐"},
    "pkg_100": {"price": "100.00", "credits": 1500, "name": "旗舰套餐"},
  }

Redis Key 规范：
  order:{order_id}  → Hash，字段：user_id, package_id, amount, credits, status, created_at, paid_at

依赖：
  - alipay-sdk-python >= 3.3.398（生产环境）
  - 本地无 SDK 时自动降级，接口照常注册，调用时抛 503
"""

import os
import json
import uuid
import time
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("tuoyue")

# ===================== 积分套餐定义 =====================

PACKAGES: Dict[str, Dict[str, Any]] = {
    "pkg_10": {
        "price": "10.00",
        "credits": 100,
        "name": "入门套餐 · 100积分",
    },
    "pkg_50": {
        "price": "50.00",
        "credits": 600,
        "name": "标准套餐 · 600积分",
    },
    "pkg_100": {
        "price": "100.00",
        "credits": 1500,
        "name": "旗舰套餐 · 1500积分",
    },
}

# ===================== 支付宝 SDK 懒加载 =====================

_alipay_client = None
_ALIPAY_SDK_AVAILABLE = False


def _init_alipay() -> bool:
    """
    懒加载支付宝客户端。
    成功返回 True，SDK 不可用返回 False（记录警告，不抛异常）。
    """
    global _alipay_client, _ALIPAY_SDK_AVAILABLE

    if _alipay_client is not None:
        return _ALIPAY_SDK_AVAILABLE

    app_id = os.getenv("ALIPAY_APP_ID", "")
    private_key = os.getenv("ALIPAY_PRIVATE_KEY", "")
    public_key = os.getenv("ALIPAY_PUBLIC_KEY", "")

    if not all([app_id, private_key, public_key]):
        logger.warning("Alipay env vars missing: ALIPAY_APP_ID / ALIPAY_PRIVATE_KEY / ALIPAY_PUBLIC_KEY")
        return False

    try:
        from alipay import AliPay, AliPayConfig  # type: ignore

        # SDK 要求密钥带 PEM 头尾（如果用户只填了 base64 裸密钥则自动补全）
        def _wrap_pem(key: str, key_type: str) -> str:
            key = key.strip()
            if key.startswith("-----"):
                return key
            if key_type == "private":
                return f"-----BEGIN PRIVATE KEY-----\n{key}\n-----END PRIVATE KEY-----"
            return f"-----BEGIN PUBLIC KEY-----\n{key}\n-----END PUBLIC KEY-----"

        _alipay_client = AliPay(
            appid=app_id,
            app_notify_url=os.getenv("ALIPAY_NOTIFY_URL", ""),
            app_private_key_string=_wrap_pem(private_key, "private"),
            alipay_public_key_string=_wrap_pem(public_key, "public"),
            sign_type="RSA2",
            debug=False,
            config=AliPayConfig(timeout=15),
        )
        _ALIPAY_SDK_AVAILABLE = True
        logger.info("Alipay SDK initialized successfully")
        return True
    except ImportError:
        logger.warning(
            "alipay-sdk-python not installed. "
            "Run: pip install alipay-sdk-python  "
            "Payment endpoints are registered but will return 503 until SDK is available."
        )
        _ALIPAY_SDK_AVAILABLE = False
        return False
    except Exception as e:
        logger.error("Alipay SDK init error: %s", e)
        _ALIPAY_SDK_AVAILABLE = False
        return False


def _require_alipay():
    """确保 SDK 可用，否则抛 RuntimeError（被上层转为 HTTP 503）"""
    if not _init_alipay():
        raise RuntimeError(
            "支付宝 SDK 不可用。请在生产环境中安装：pip install alipay-sdk-python"
        )
    return _alipay_client


# ===================== Redis 客户端 =====================

_redis_client = None


def set_redis_client(rc) -> None:
    """由 main.py 注入共享 Redis 实例"""
    global _redis_client
    _redis_client = rc


def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis as _redis_lib
        _redis_client = _redis_lib.Redis(host="localhost", port=6379, decode_responses=True)
    return _redis_client


# ===================== Redis Key =====================

def _order_key(order_id: str) -> str:
    return f"order:{order_id}"


# ===================== 核心函数 =====================

def get_package(package_id: str) -> Optional[Dict[str, Any]]:
    """根据 package_id 获取套餐信息，不存在返回 None"""
    return PACKAGES.get(package_id)


def create_order(user_id: str, package_id: str) -> Dict[str, Any]:
    """
    创建支付宝网页支付订单。

    :param user_id:    当前登录用户 ID
    :param package_id: 套餐 ID（pkg_10 / pkg_50 / pkg_100）
    :return: {
        "order_id": str,
        "pay_url": str,   # 支付宝收银台 URL（前端跳转）
        "amount": str,    # 订单金额（元）
        "credits": int,   # 本次购买积分
        "package_name": str,
    }
    :raises ValueError: 套餐不存在
    :raises RuntimeError: SDK 不可用
    """
    pkg = get_package(package_id)
    if not pkg:
        raise ValueError(f"套餐 {package_id!r} 不存在，可用套餐：{list(PACKAGES.keys())}")

    client = _require_alipay()

    order_id = str(uuid.uuid4()).replace("-", "")
    subject = pkg["name"]
    amount = pkg["price"]
    credits = pkg["credits"]

    return_url = os.getenv("ALIPAY_RETURN_URL", "")
    notify_url = os.getenv("ALIPAY_NOTIFY_URL", "")

    # 生成支付宝网页支付跳转 URL
    order_string = client.api_alipay_trade_page_pay(
        out_trade_no=order_id,
        total_amount=amount,
        subject=subject,
        return_url=return_url,
        notify_url=notify_url,
    )
    pay_url = f"https://openapi.alipay.com/gateway.do?{order_string}"

    # 订单信息写入 Redis（TTL 2 小时）
    r = _get_redis()
    order_data = {
        "user_id": user_id,
        "package_id": package_id,
        "amount": amount,
        "credits": str(credits),
        "status": "pending",
        "created_at": str(int(time.time())),
        "paid_at": "",
        "trade_no": "",           # 支付宝交易号，webhook 回调后写入
    }
    r.hset(_order_key(order_id), mapping=order_data)
    r.expire(_order_key(order_id), 7200)

    logger.info(
        "Order created",
        extra={
            "order_id": order_id,
            "user_id": user_id,
            "package_id": package_id,
            "amount": amount,
        },
    )
    return {
        "order_id": order_id,
        "pay_url": pay_url,
        "amount": amount,
        "credits": credits,
        "package_name": subject,
    }


def verify_notify(params: Dict[str, str]) -> bool:
    """
    验证支付宝异步通知签名。

    :param params: 支付宝 POST 表单参数（已解码为 dict）
    :return: True 表示验签通过，False 表示失败
    """
    try:
        client = _require_alipay()
        sign = params.pop("sign", None)
        sign_type = params.pop("sign_type", "RSA2")
        if not sign:
            return False
        return client.verify(params, sign)
    except Exception as e:
        logger.error("Alipay notify verify error: %s", e)
        return False


def handle_paid_notify(params: Dict[str, str]) -> bool:
    """
    处理支付成功的异步通知：更新订单状态 + 发放积分。

    :param params: 已验签通过的支付宝通知参数
    :return: True 表示处理成功
    """
    trade_status = params.get("trade_status", "")
    if trade_status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
        logger.info("Alipay notify ignored: trade_status=%s", trade_status)
        return False

    out_trade_no = params.get("out_trade_no", "")   # 我方订单号
    trade_no = params.get("trade_no", "")            # 支付宝交易号

    if not out_trade_no:
        logger.warning("Alipay notify: missing out_trade_no")
        return False

    r = _get_redis()
    order_key = _order_key(out_trade_no)
    order = r.hgetall(order_key)

    if not order:
        logger.warning("Alipay notify: order not found in Redis, order_id=%s", out_trade_no)
        return False

    # 幂等：已完成的订单不重复处理
    if order.get("status") == "paid":
        logger.info("Alipay notify: order already paid, skipping. order_id=%s", out_trade_no)
        return True

    user_id = order.get("user_id", "")
    credits = int(order.get("credits", 0))

    # 更新订单状态
    r.hset(order_key, mapping={
        "status": "paid",
        "paid_at": str(int(time.time())),
        "trade_no": trade_no,
    })
    # 延长 TTL（已支付订单保留 30 天供查询）
    r.expire(order_key, 86400 * 30)

    # 发放积分（延迟导入避免循环依赖）
    try:
        import billing_service
        new_balance = billing_service.grant_credits(user_id, credits, reason=f"order_{out_trade_no}")
        logger.info(
            "Credits granted via payment",
            extra={
                "order_id": out_trade_no,
                "user_id": user_id,
                "credits": credits,
                "new_balance": new_balance,
            },
        )
    except Exception as e:
        logger.error(
            "Failed to grant credits after payment: %s",
            e,
            extra={"order_id": out_trade_no, "user_id": user_id},
        )
        # 即使发放失败也返回 True（已应答支付宝），避免重复通知；
        # 运营可通过 /api/admin/users/{id}/grant-credits 手动补发

    return True


def get_order(order_id: str) -> Optional[Dict[str, Any]]:
    """
    查询订单信息。

    :return: 订单 dict，不存在返回 None
    """
    r = _get_redis()
    order = r.hgetall(_order_key(order_id))
    if not order:
        return None
    # 对外屏蔽内部字段，credits 转 int
    return {
        "order_id": order_id,
        "user_id": order.get("user_id", ""),
        "package_id": order.get("package_id", ""),
        "amount": order.get("amount", ""),
        "credits": int(order.get("credits", 0)),
        "status": order.get("status", "pending"),
        "created_at": int(order.get("created_at") or 0),
        "paid_at": int(order.get("paid_at") or 0) or None,
        "trade_no": order.get("trade_no", "") or None,
    }
