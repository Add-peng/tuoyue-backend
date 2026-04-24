"""
阿里云短信服务模块 (sms_service.py)
- 使用阿里云 dysmsv20170525 OpenAPI 直接调用（无需额外 SDK）
- 环境变量配置：ALIBABA_CLOUD_ACCESS_KEY_ID / ACCESS_KEY_SECRET / SMS_SIGN_NAME / SMS_TEMPLATE_CODE
- 发送失败时降级为 console.log 打印验证码
"""

import os
import json
import time
import random
import hashlib
import hmac
import base64
import urllib.parse
import logging
from datetime import datetime

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

logger = logging.getLogger("tuoyue")


# ================== 配置 ==================

ALIYUN_ACCESS_KEY_ID = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
ALIYUN_ACCESS_KEY_SECRET = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")
ALIYUN_SMS_SIGN_NAME = os.getenv("SMS_SIGN_NAME", "拓岳科技")
ALIYUN_SMS_TEMPLATE_CODE = os.getenv("SMS_TEMPLATE_CODE", "SMS_XXXXX")
ALIYUN_SMS_ENDPOINT = "https://dysmsapi.aliyuncs.com"
ALIYUN_SMS_VERSION = "2017-05-25"
ALIYUN_SMS_REGION = "cn-hangzhou"


# ================== 核心工具函数 ==================

def _generate_code(length: int = 6) -> str:
    """生成指定位数的数字验证码"""
    return "".join(random.choices("0123456789", k=length))


def _percent_encode(value: str) -> str:
    """URL 编码（RFC 3986 标准）"""
    return urllib.parse.quote(str(value), safe="-_.~")


def _compose_string_to_sign(method: str, params: dict) -> str:
    """构造待签名字符串（按字母顺序排列参数）"""
    sorted_keys = sorted(params.keys())
    parts = []
    for key in sorted_keys:
        parts.append(f"{_percent_encode(key)}={_percent_encode(params[key])}")
    query_string = "&".join(parts)
    return f"{method}&{_percent_encode('/')}&{_percent_encode(query_string)}"


def _sign_string(string_to_sign: str, secret: str) -> str:
    """HMAC-SHA1 签名，Base64 编码"""
    secret = f"{secret}&"
    h = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    )
    return base64.b64encode(h.digest()).decode("utf-8")


def _build_request_params(action: str, extra_params: dict | None = None) -> dict:
    """构造带签名信息的 API 请求参数"""
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "Format": "JSON",
        "Version": ALIYUN_SMS_VERSION,
        "AccessKeyId": ALIYUN_ACCESS_KEY_ID,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": f"{int(time.time() * 1000)}{random.randint(1000, 9999)}",
        "RegionId": ALIYUN_SMS_REGION,
        "Timestamp": timestamp,
        "Action": action,
    }
    if extra_params:
        params.update(extra_params)

    string_to_sign = _compose_string_to_sign("POST", params)
    signature = _sign_string(string_to_sign, ALIYUN_ACCESS_KEY_SECRET)
    params["Signature"] = signature
    return params


# ================== 短信发送接口 ==================

def send_sms_code(phone: str, code: str) -> bool:
    """
    发送短信验证码
    - phone: 目标手机号
    - code: 6位验证码
    返回: True=成功, False=失败
    """
    params = _build_request_params(
        action="SendSms",
        extra_params={
            "PhoneNumbers": phone,
            "SignName": ALIYUN_SMS_SIGN_NAME,
            "TemplateCode": ALIYUN_SMS_TEMPLATE_CODE,
            "TemplateParam": json.dumps({"code": code}, ensure_ascii=False),
        },
    )

    if not ALIYUN_ACCESS_KEY_ID or not ALIYUN_ACCESS_KEY_SECRET:
        logger.warning(
            "SMS: Aliyun credentials not configured, using console fallback",
            extra={"phone": _mask_phone(phone), "code": code},
        )
        _console_fallback(phone, code)
        return True  # 降级场景视为成功

    if not _REQUESTS_AVAILABLE:
        logger.warning(
            "SMS: requests library not available, using console fallback",
            extra={"phone": _mask_phone(phone), "code": code},
        )
        _console_fallback(phone, code)
        return True

    try:
        response = requests.post(
            ALIYUN_SMS_ENDPOINT,
            data=params,
            timeout=10,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        result = response.json()
        code_api = result.get("Code", "")

        if code_api in ("OK", "isv.SMS_SUCCESS"):
            logger.info(
                "SMS sent successfully",
                extra={"phone": _mask_phone(phone), "biz_id": result.get("BizId", "")},
            )
            return True
        else:
            logger.error(
                "SMS API error",
                extra={
                    "phone": _mask_phone(phone),
                    "code": code,
                    "api_code": code_api,
                    "message": result.get("Message", ""),
                },
            )
            # 降级打印
            _console_fallback(phone, code)
            return False

    except Exception as e:
        logger.error(
            "SMS request failed, using console fallback",
            extra={"phone": _mask_phone(phone), "code": code, "error": str(e)},
        )
        _console_fallback(phone, code)
        return False


def _console_fallback(phone: str, code: str) -> None:
    """降级方案：打印验证码到控制台"""
    print(f"\n{'='*50}")
    print(f"  [SMS FALLBACK] 短信发送失败，验证码降级打印")
    print(f"  手机号: {_mask_phone(phone)}")
    print(f"  验证码: {code}")
    print(f"  有效期: 5 分钟")
    print(f"{'='*50}\n")


def _mask_phone(phone: str) -> str:
    """手机号脱敏：138****5678"""
    if len(phone) == 11:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone[:3] + "****"


def send_password_sms(phone: str, new_password: str) -> bool:
    """
    发送密码通知短信（供管理员重置密码接口调用）。

    - phone: 目标手机号
    - new_password: 新密码原文（8位随机字符串）

    返回: True=成功, False=失败（网络异常等）
    降级: credentials 未配置或 requests 不可用时在控制台打印密码
    """
    # 优先使用与验证码相同的模板（${code} 占位符接受任意文本）
    params = _build_request_params(
        action="SendSms",
        extra_params={
            "PhoneNumbers": phone,
            "SignName": ALIYUN_SMS_SIGN_NAME,
            "TemplateCode": ALIYUN_SMS_TEMPLATE_CODE,
            "TemplateParam": json.dumps({"code": new_password}, ensure_ascii=False),
        },
    )

    if not ALIYUN_ACCESS_KEY_ID or not ALIYUN_ACCESS_KEY_SECRET:
        logger.warning(
            "Password SMS: credentials not configured, using console fallback",
            extra={"phone": _mask_phone(phone), "new_password": new_password},
        )
        _console_password_fallback(phone, new_password)
        return True  # 降级场景视为成功

    if not _REQUESTS_AVAILABLE:
        logger.warning(
            "Password SMS: requests library not available, using console fallback",
            extra={"phone": _mask_phone(phone), "new_password": new_password},
        )
        _console_password_fallback(phone, new_password)
        return True

    try:
        response = requests.post(
            ALIYUN_SMS_ENDPOINT,
            data=params,
            timeout=10,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        result = response.json()
        code_api = result.get("Code", "")

        if code_api in ("OK", "isv.SMS_SUCCESS"):
            logger.info(
                "Password SMS sent successfully",
                extra={"phone": _mask_phone(phone), "biz_id": result.get("BizId", "")},
            )
            return True
        else:
            logger.error(
                "Password SMS send failed",
                extra={"phone": _mask_phone(phone), "code": code_api, "msg": result.get("Message", "")},
            )
            return False

    except Exception as e:
        logger.error(
            "Password SMS send exception",
            extra={"phone": _mask_phone(phone), "error": str(e)},
        )
        return False


def _console_password_fallback(phone: str, password: str) -> None:
    """降级打印密码（控制台，不走短信）"""
    print(f"\n{'='*50}")
    print(f"  [密码通知-降级打印] （短信服务不可用）")
    print(f"  手机号: {_mask_phone(phone)}")
    print(f"  新密码: {password}")
    print(f"  请人工联系用户告知密码")
    print(f"{'='*50}\n")
