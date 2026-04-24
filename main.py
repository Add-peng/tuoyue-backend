from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Header
from pydantic import BaseModel
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
def run_crew_task(task_id: str, topic: str):
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
        redis_client.hset(task_id, mapping={
            "status": "completed",
            "data": result_text,
            "trace": json.dumps(trace_data, ensure_ascii=False),
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
async def generate_api(request: Request, payload: CopyRequest, background_tasks: BackgroundTasks):
    if not _CREWAI_AVAILABLE:
        raise HTTPException(status_code=503, detail="AI 引擎暂不可用（crewai 未安装）")
    task_id = str(uuid.uuid4())
    redis_client.hset(task_id, mapping={"status": "processing"})
    redis_client.expire(task_id, 3600)
    background_tasks.add_task(run_crew_task, task_id, payload.topic)
    logger.info(
        "request received",
        extra={"task_id": task_id, "topic": payload.topic},
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