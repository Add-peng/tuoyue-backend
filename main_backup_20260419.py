from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
# 从刚才明轩写好的 agents_engine.py 中导入干活的函数
from agents_engine import run_copywriter

# 1. 初始化 FastAPI 应用
app = FastAPI(title="拓岳 SaaS 引擎接口")

# 2. 核心配置：解决跨域问题 (CORS)
# 这一步如果不做，Vercel 上的前端就无法访问阿里云的服务器
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 开发阶段允许所有来源，正式上线可限制为你的前端域名
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有请求方式（GET, POST 等）
    allow_headers=["*"],  # 允许所有请求头
)

# 3. 定义前端传过来的数据格式（只要一个主题 topic）
class CopyRequest(BaseModel):
    topic: str

# 4. 路由：前端 POST 请求 http://IP:8000/api/generate
@app.post("/api/generate")
async def generate_api(request: CopyRequest):
    try:
        print(f">>> 收到前端请求，正在处理主题: {request.topic}")
        
        # 调用 CrewAI 引擎干活
        result_text = run_copywriter(request.topic)
        
        # 将结果以标准的 JSON 格式返回给前端
        return {
            "status": "success",
            "data": result_text
        }
    except Exception as e:
        # 如果出错了，返回错误信息给前端
        print(f"!!! 接口报错: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# 5. 健康检查接口（用来测试接口通不通）
@app.get("/")
def read_root():
    return {"message": "拓岳 AI 引擎正在运行中..."}