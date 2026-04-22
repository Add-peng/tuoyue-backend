from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
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
import time
import traceback
import uuid

try:
    from pythonjsonlogger import jsonlogger
except ImportError:
    jsonlogger = None

# 从 agents_engine 中导入多智能体协作函数
from agents_engine import run_copywriter_crew

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

# 初始化 FastAPI 应用
app = FastAPI(title="拓岳 SaaS 引擎接口")
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

# Redis 客户端（连接本地 Docker 运行的 Redis）
redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)

# 定义前端请求体格式
class CopyRequest(BaseModel):
    topic: str


class SendCodeRequest(BaseModel):
    phone: str


class VerifyCodeRequest(BaseModel):
    phone: str
    code: str


class RegisterRequest(BaseModel):
    phone: str
    password: str
    nickname: str | None = None


class LoginRequest(BaseModel):
    phone: str
    password: str


class SendCodeResponse(BaseModel):
    success: bool


class VerifyCodeResponse(BaseModel):
    valid: bool


class RegisterResponse(BaseModel):
    user_id: str


class LoginResponse(BaseModel):
    token: str


class UserProfileResponse(BaseModel):
    tier: str
    daily_quota: int


# 后台执行 CrewAI 的任务函数
def run_crew_task(task_id: str, topic: str):
    started_at = time.time()
    logger.info(
        "task started",
        extra={"task_id": task_id, "topic": topic, "started_at": started_at},
    )
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


# ================= 认证与用户信息 Mock 接口 =================
@app.post("/api/auth/send-code", response_model=SendCodeResponse)
async def send_code(request: SendCodeRequest):
    return SendCodeResponse(success=True)


@app.post("/api/auth/verify-code", response_model=VerifyCodeResponse)
async def verify_code(request: VerifyCodeRequest):
    return VerifyCodeResponse(valid=True)


@app.post("/api/auth/register", response_model=RegisterResponse)
async def register(request: RegisterRequest):
    return RegisterResponse(user_id=f"mock_{uuid.uuid4().hex[:8]}")


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    return LoginResponse(token=f"mock_token_{uuid.uuid4().hex[:12]}")


@app.get("/api/user/profile", response_model=UserProfileResponse)
async def get_profile():
    return UserProfileResponse(tier="Free", daily_quota=10)


# 健康检查接口
@app.get("/")
def read_root():
    logger.info("health check", extra={"task_id": None})
    return {"message": "拓岳 AI 引擎正在运行中...", "storage": "redis"}

# Redis 连通性检查
@app.get("/api/health/redis")
def check_redis():
    try:
        redis_client.ping()
        return {"status": "ok", "backend": "redis"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis 连接失败: {str(e)}")