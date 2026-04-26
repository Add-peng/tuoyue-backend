from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Header
from pydantic import BaseModel
from typing import Optional
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
import json
import logging
import redis
import os
import re
import time
import traceback
import uuid

# 加载 .env 环境变量（项目根目录）
from dotenv import load_dotenv
load_dotenv()

# 业务模块
import sms_service
import user_store
from app.middleware import SensitiveWordMiddleware, sensitive_filter
from app.admin import router as admin_router
import billing_service
import payment_service

try:
    from pythonjsonlogger import jsonlogger
except ImportError:
    jsonlogger = None

# 从 agents_engine 中导入多智能体协作函数（crewai 缺失时禁用生成接口）
try:
    from agents_engine import run_copywriter_crew
    _CREWAI_AVAILABLE = True
except Exception:
    run_copywriter_crew = None
    _CREWAI_AVAILABLE = False
    import logging
    logging.getLogger("tuoyue").warning("crewai not available, /api/generate disabled")

LOG_FILE_PATH = "/var/log/tuoyue.log"


class TaskJsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "task_id": getattr(record, "task_id", None),
            "message": record.getMessage(),
            "extra": self._collect_extra(record),
        }
        if record.exc_info:
            payload["extra"]["error_stack"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

    def _collect_extra(self, record):
        excluded = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
            "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
            "relativeCreated", "thread", "threadName", "processName", "process", "task_id",
        }
        return {
            key: value
            for key, value in record.__dict__.items()
            if not key.startswith("_") and key not in excluded and key not in {"timestamp", "level", "message", "extra"}
        }



def setup_logging():
    root_logger = logging.getLogger()
    if root_logger.handlers:
        return logging.getLogger("tuoyue")
    root_logger.setLevel(logging.INFO)
    formatter = TaskJsonFormatter()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    try:
        os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
        file_handler = logging.FileHandler(LOG_FILE_PATH, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
    except Exception:
        root_logger.warning("failed to initialize file logging", extra={"task_id": None, "log_file": LOG_FILE_PATH})

    return logging.getLogger("tuoyue")


logger = setup_logging()

# 初始化 FastAPI 应用（环境变量控制 API 文档是否可用）
environment = os.getenv("ENVIRONMENT", "development")
docs_enabled = environment != "production"
app = FastAPI(
    title="拓岳 SaaS 引擎接口",
    docs_url="/docs" if docs_enabled else None,
    redoc_url="/redoc" if docs_enabled else None,
)
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# ================= 强制 UTF-8 编码中间件 =================
class ForceUTF8Middleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if response.headers.get("content-type") == "application/json":
            response.headers["content-type"] = "application/json; charset=utf-8"
        return response

app.add_middleware(ForceUTF8Middleware)
# ========================================================

# 配置跨域 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 敏感词过滤中间件（DFA 算法，启动时加载词库）
app.add_middleware(SensitiveWordMiddleware)

# 提前初始化全局敏感词过滤器
_ = sensitive_filter

# Redis 客户端（连接本地 Docker 运行的 Redis）
redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)
# 注入共享实例给 user_store，避免重复连接
user_store.set_redis_client(redis_client)
# 注入共享实例给 admin，避免重复连接
from app import admin
admin.set_redis_client(redis_client)
# 注入共享实例给 billing_service，避免重复连接
billing_service.set_redis_client(redis_client)
# 注入共享实例给 payment_service，避免重复连接
payment_service.set_redis_client(redis_client)

# 注册管理后台路由
app.include_router(admin_router)

# ================== FastAPI 生命周期事件 ==================

@app.on_event("startup")
async def startup_event():
    """应用启动时初始化各组件"""
    logger.info("应用启动完成")


def _check_redis():
    """检查 Redis 连通性，不通则抛 HTTPException 503"""
    try:
        redis_client.ping()
    except redis.exceptions.ConnectionError:
        raise HTTPException(
            status_code=503,
            detail="Redis 服务不可用，请确认 Docker Redis 容器已启动（docker run -d -p 6379:6379 redis）",
        )

# ================== 请求 / 响应模型 ==================

class CopyRequest(BaseModel):
    topic: str


class SendCodeRequest(BaseModel):
    phone: str


class VerifyCodeRequest(BaseModel):
    phone: str
    code: str


class AuthByCodeRequest(BaseModel):
    phone: str
    code: str


class SendCodeResponse(BaseModel):
    success: bool
    message: str = ""


class VerifyCodeResponse(BaseModel):
    valid: bool
    message: str = ""


class AuthResponse(BaseModel):
    token: str
    is_new_user: bool = False


class UserProfileResponse(BaseModel):
    user_id: str
    phone: str
    tier: str
    daily_quota: int


# ================== 验证码 + Redis Key ==================

def _sms_code_key(phone: str) -> str:
    return f"sms:code:{phone}"


def _sms_ip_key(ip: str) -> str:
    return f"sms:ip:{ip}"


def _sms_phone_day_key(phone: str) -> str:
    today = time.strftime("%Y%m%d")
    return f"sms:phone:{phone}:{today}"


def _mask_phone(phone: str) -> str:
    if len(phone) == 11:
        return f"{phone[:3]}****{phone[-4:]}"
    return phone[:3] + "****"


def _validate_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"1[3-9]\d{9}", phone))


# 后台执行 CrewAI 的任务函数
def run_crew_task(task_id: str, topic: str, user_id: str = None):
    import random as _random
    started_at = time.time()
    logger.info(
        "task started",
        extra={"task_id": task_id, "topic": topic, "started_at": started_at},
    )
    if not _CREWAI_AVAILABLE:
        redis_client.hset(task_id, mapping={
            "status": "failed",
            "error": "crewai not available in this environment",
        })
        redis_client.expire(task_id, 3600)
        return
    try:
        result_text, trace_data = run_copywriter_crew(topic)

        # ── Token 用量采集（模拟，生产环境替换为 CrewAI 回调实际值） ──
        prompt_tokens = _random.randint(200, 800)
        completion_tokens = _random.randint(100, 500)
        logger.info(
            "token usage recorded",
            extra={
                "task_id": task_id,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        )

        # ── 积分扣减（任务成功后执行） ──
        if user_id:
            try:
                credits_used = billing_service.calculate_credits(prompt_tokens, completion_tokens)
                success, remaining = billing_service.deduct_credits(user_id, credits_used)
                logger.info(
                    "credits deducted for task",
                    extra={
                        "task_id": task_id,
                        "user_id": user_id,
                        "credits_used": credits_used,
                        "remaining": remaining,
                    },
                )
            except Exception as billing_err:
                # 扣费失败不影响任务结果返回，仅记录错误
                logger.error(
                    "billing deduction failed",
                    extra={"task_id": task_id, "user_id": user_id, "error": str(billing_err)},
                )

        redis_client.hset(task_id, mapping={
            "status": "completed",
            "data": result_text,
            "trace": json.dumps(trace_data, ensure_ascii=False),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        })
        redis_client.expire(task_id, 3600)
        logger.info(
            "task completed",
            extra={
                "task_id": task_id,
                "duration_ms": int((time.time() - started_at) * 1000),
                "result_length": len(result_text),
            },
        )
    except Exception as e:
        error_stack = traceback.format_exc()
        logger.error(
            "task failed",
            extra={"task_id": task_id, "error_stack": error_stack},
        )
        redis_client.hset(task_id, mapping={
            "status": "failed",
            "error": str(e),
        })
        redis_client.expire(task_id, 3600)

# 异步生成接口：立即返回 task_id，后台执行
@app.post("/api/generate")
@limiter.limit("10/minute")
async def generate_api(request: Request, payload: CopyRequest, background_tasks: BackgroundTasks, authorization: str = Header(None)):
    if not _CREWAI_AVAILABLE:
        raise HTTPException(status_code=503, detail="AI 引擎暂不可用（crewai 未安装）")

    # ── 从 JWT 解析 user_id（可选，未登录用户跳过积分检查） ──
    user_id = None
    if authorization:
        token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
        user_id = user_store.extract_user_id_from_token(token)

    # ── 前置积分检查（仅已登录用户） ──
    if user_id:
        try:
            if not billing_service.has_sufficient_credits(user_id):
                raise HTTPException(
                    status_code=402,
                    detail="积分不足，请充值。",
                )
        except HTTPException:
            raise
        except Exception as billing_err:
            logger.warning(
                "billing check failed, proceeding without check",
                extra={"user_id": user_id, "error": str(billing_err)},
            )

    task_id = str(uuid.uuid4())
    redis_client.hset(task_id, mapping={"status": "processing"})
    redis_client.expire(task_id, 3600)
    # 将 user_id 传入后台任务，用于任务完成后扣减积分
    background_tasks.add_task(run_crew_task, task_id, payload.topic, user_id)
    logger.info(
        "request received",
        extra={"task_id": task_id, "topic": payload.topic, "user_id": user_id},
    )
    return {
        "status": "processing",
        "task_id": task_id
    }

# 查询任务结果接口
@app.get("/api/generate/{task_id}")
async def get_task_result(task_id: str):
    task = redis_client.hgetall(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务ID不存在")
    if task.get("status") == "completed" and "trace" in task:
        task["trace"] = json.loads(task["trace"])
    return task


# ================= 认证接口 =================

@app.post("/api/auth/send-code", response_model=SendCodeResponse)
@limiter.limit("3/minute")
async def send_code(request: Request, payload: SendCodeRequest):
    """发送短信验证码"""
    _check_redis()   # 确保 Redis 可用
    phone = payload.phone.strip()
    ip = get_remote_address(request)

    # ① 基础校验：手机号格式
    if not _validate_phone(phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")

    # ② 频率限制：同一 IP 每分钟 ≤3 次
    ip_key = _sms_ip_key(ip)
    ip_count = redis_client.get(ip_key)
    if ip_count and int(ip_count) >= 3:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    # ③ 频率限制：同一手机号 60 秒内不可重发
    code_key = _sms_code_key(phone)
    if redis_client.exists(code_key):
        ttl = redis_client.ttl(code_key)
        if ttl > 0:
            raise HTTPException(
                status_code=429,
                detail=f"验证码已发送，请在 {ttl} 秒后重试",
            )

    # ④ 频率限制：同一手机号每天上限 5 次
    day_key = _sms_phone_day_key(phone)
    day_count = redis_client.get(day_key)
    if day_count and int(day_count) >= 5:
        raise HTTPException(status_code=429, detail="今日验证码次数已用完，请明天再试")

    # ⑤ 生成 6 位验证码
    code = sms_service._generate_code(6)
    redis_client.setex(code_key, 300, code)          # 有效期 5 分钟
    redis_client.setex(ip_key, 60, int(ip_count or 0) + 1)   # IP 计数器 60s
    redis_client.incr(day_key)
    redis_client.expire(day_key, 86400)              # 当日 24:00 前有效

    # ⑥ 发送短信（失败自动降级打印）
    sms_service.send_sms_code(phone, code)

    logger.info(
        "SMS code sent",
        extra={"phone": _mask_phone(phone), "ip": ip},
    )
    return SendCodeResponse(success=True, message="验证码已发送")


@app.post("/api/auth/verify-code", response_model=VerifyCodeResponse)
async def verify_code(request: Request, payload: VerifyCodeRequest):
    """校验短信验证码"""
    _check_redis()
    phone = payload.phone.strip()
    code = payload.code.strip()

    if not _validate_phone(phone):
        raise HTTPException(status_code=400, detail="手机号格式不正确")

    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=400, detail="验证码为 6 位数字")

    code_key = _sms_code_key(phone)
    stored_code = redis_client.get(code_key)

    if not stored_code:
        raise HTTPException(status_code=400, detail="验证码已过期，请重新发送")

    if stored_code != code:
        logger.warning("Invalid SMS code", extra={"phone": _mask_phone(phone)})
        return VerifyCodeResponse(valid=False, message="验证码错误")

    # 比对成功后立即删除（防复用）
    redis_client.delete(code_key)
    logger.info("SMS code verified", extra={"phone": _mask_phone(phone)})
    return VerifyCodeResponse(valid=True)


@app.post("/api/auth/register", response_model=AuthResponse)
async def register(request: Request, payload: AuthByCodeRequest):
    """注册（手机号 + 验证码）"""
    _check_redis()
    phone = payload.phone.strip()
    code = payload.code.strip()

    # 先校验验证码（复用 verify_code 逻辑）
    code_key = _sms_code_key(phone)
    stored_code = redis_client.get(code_key)
    if not stored_code or stored_code != code:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    redis_client.delete(code_key)

    # 获取或创建用户（注册即登录）
    user, is_new = user_store.get_or_create_user(phone)
    token = user_store.create_token(user["user_id"], phone)

    logger.info(
        "User registered",
        extra={"user_id": user["user_id"], "is_new_user": is_new},
    )
    return AuthResponse(token=token, is_new_user=is_new)


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(request: Request, payload: AuthByCodeRequest):
    """登录（手机号 + 验证码，同注册逻辑）"""
    _check_redis()
    phone = payload.phone.strip()
    code = payload.code.strip()

    # 校验验证码
    code_key = _sms_code_key(phone)
    stored_code = redis_client.get(code_key)
    if not stored_code or stored_code != code:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    redis_client.delete(code_key)

    # 获取或创建用户
    user, is_new = user_store.get_or_create_user(phone)
    token = user_store.create_token(user["user_id"], phone)

    logger.info(
        "User logged in",
        extra={"user_id": user["user_id"], "is_new_user": is_new},
    )
    return AuthResponse(token=token, is_new_user=is_new)


@app.get("/api/user/profile", response_model=UserProfileResponse)
async def get_profile(authorization: str = Header(None)):
    """获取用户信息（从 JWT Token 解析）"""
    _check_redis()
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证 Token")

    # 支持 "Bearer <token>" 或直接传 token
    token = authorization
    if authorization.lower().startswith("bearer "):
        token = authorization[7:]

    user_id = user_store.extract_user_id_from_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")

    user = user_store.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    return UserProfileResponse(
        user_id=user["user_id"],
        phone=user.get("phone_masked", "****"),
        tier=user.get("tier", "Free"),
        daily_quota=user.get("daily_quota", 10),
    )




# ================= 积分接口 =================

class CreditsResponse(BaseModel):
    credits: int


class GrantCreditsRequest(BaseModel):
    amount: int


class GrantCreditsResponse(BaseModel):
    success: bool
    user_id: str
    granted: int
    balance: int


@app.get("/api/user/credits", response_model=CreditsResponse)
async def get_user_credits(authorization: str = Header(None)):
    """查询当前用户积分余额（需要 JWT Token）"""
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    user_id = user_store.extract_user_id_from_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")
    credits = billing_service.get_credits(user_id)
    return CreditsResponse(credits=credits)


# ================= 支付接口 =================

class CreateOrderRequest(BaseModel):
    package_id: str  # pkg_10 | pkg_50 | pkg_100


class CreateOrderResponse(BaseModel):
    order_id: str
    pay_url: str
    amount: str
    credits: int
    package_name: str


class OrderStatusResponse(BaseModel):
    order_id: str
    status: str        # pending | paid | failed
    amount: str
    credits: int
    paid_at: Optional[int] = None
    trade_no: Optional[str] = None


@app.post("/api/order/create", response_model=CreateOrderResponse)
async def create_order(
    payload: CreateOrderRequest,
    authorization: str = Header(None),
):
    """
    创建支付宝网页支付订单，返回支付跳转 URL。

    - 需要 JWT 认证
    - package_id: pkg_10 / pkg_50 / pkg_100
    """
    _check_redis()
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    user_id = user_store.extract_user_id_from_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")

    try:
        result = payment_service.create_order(user_id, payload.package_id)
        return CreateOrderResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.error("Payment SDK error: %s", e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("create_order unexpected error: %s", e)
        raise HTTPException(status_code=500, detail="订单创建失败，请稍后重试")


@app.post("/api/order/webhook")
async def alipay_webhook(request: Request):
    """
    接收支付宝异步通知（POST form-data）。

    流程：
    1. 解析 form 参数
    2. 验签（RSA2）
    3. 更新订单状态并发放积分
    4. 返回 "success" 字符串（支付宝规范）
    """
    form = await request.form()
    params = dict(form)

    # 验签
    if not payment_service.verify_notify(params.copy()):
        logger.warning("Alipay notify: signature verification failed")
        raise HTTPException(status_code=400, detail="签名验证失败")

    # 处理支付结果（幂等，发放积分）
    payment_service.handle_paid_notify(params)

    # 支付宝规范：必须返回纯文本 "success"
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("success")


@app.get("/api/order/status/{order_id}", response_model=OrderStatusResponse)
async def get_order_status(order_id: str, authorization: str = Header(None)):
    """
    查询订单状态。

    - 需要 JWT 认证
    - 只能查询自己的订单
    """
    _check_redis()
    if not authorization:
        raise HTTPException(status_code=401, detail="未提供认证 Token")
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    user_id = user_store.extract_user_id_from_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Token 无效或已过期")

    order = payment_service.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 权限检查：只能查自己的订单
    if order["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="无权查询该订单")

    return OrderStatusResponse(
        order_id=order["order_id"],
        status=order["status"],
        amount=order["amount"],
        credits=order["credits"],
        paid_at=order.get("paid_at"),
        trade_no=order.get("trade_no"),
    )


# 健康检查接口
@app.get("/")
def read_root():
    logger.info("health check", extra={"task_id": None})
    return {"message": "拓岳 AI 引擎正在运行中...", "storage": "redis"}

# 独立健康检查端点
@app.get("/health")
async def health_check():
    return {"status": "ok"}

# Redis 连通性检查
@app.get("/api/health/redis")
def check_redis():
    try:
        redis_client.ping()
        return {"status": "ok", "backend": "redis"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis 连接失败: {str(e)}")