"""
AIHR 简历解析服务入口。

链路：PDF → 渲染为图像 → Qwen3.5 多模态 → JSON → 打标

接口一览：
  /v1/chat/completions            - OpenAI 兼容，支持单份/批量，同步/异步
  /v1/batch/resumes               - 同步批量解析（纯解析，无打标）
  /api/resume/async/parse         - 单文件异步解析+打标
  /api/resume/async/result        - 轮询任务状态与结果
  /api/resume/async/batch         - 批量 URL 异步解析+打标（主要对外接口）
  /api/resume/async/batch/multiform - 混合批量（文件上传 + URL）
  /api/tasks                      - 运维/调试：列出内存中全部任务
  /api/tags/config/*              - 打标维度配置 CRUD
  /algorithm/*                    - 算法内部调试接口

设计要点：
  - _parse_semaphore 在 asyncio 层限流，防止线程池队列积压
  - set_log_context 在每个请求入口注入 request_id，全链路日志自动携带
  - wrap_for_executor 确保线程池任务继承 ContextVar 上下文
  - LoggerManager.start_config_watcher 实现 log_config.yml 热更新
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from config import get_logger_manager
from logger_manager import _thread_local
from parser import _cleanup_uploads
from tasks import _periodic_cleanup, executor

from api.routes_async     import router as async_router
from api.routes_openai    import router as openai_router
from api.routes_tags      import router as tags_router
from api.routes_algorithm import router as algorithm_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    lm = get_logger_manager()
    lm.start_config_watcher()
    _cleanup_uploads()
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    yield
    cleanup_task.cancel()
    executor.shutdown(wait=False)
    lm.stop_config_watcher()
    lm.stop_queue_listener()


app = FastAPI(title="AIHR Resume Parser", version="3.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(async_router)
app.include_router(openai_router)
app.include_router(tags_router)
app.include_router(algorithm_router)


@app.middleware("http")
async def _reset_log_context(request: Request, call_next):
    # 每个请求入口重置 thread-local，防止前一请求的上下文泄漏到探针/系统接口日志
    _thread_local.request_id    = "-"
    _thread_local.business_type = "-"
    _thread_local.filename      = "-"
    return await call_next(request)


@app.get("/health")
async def health_check():
    # 负载均衡/K8s 存活探针，轻量无依赖
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.get("/v1/models")
async def list_models():
    # OpenAI SDK 兼容探测，客户端调用 client.models.list() 时触发
    return {"object": "list", "data": [
        {"id": "DeepSeek-OCR",      "object": "model", "created": 1677610602, "owned_by": "user"},
        {"id": "DeepSeek-OCR-JSON", "object": "model", "created": 1698957206, "owned_by": "user"},
        {"id": "AIHR-Resume",       "object": "model", "created": 1748000000, "owned_by": "user"},
    ]}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("SERVICE_PORT", "21804"))
    uvicorn.run(app, host="0.0.0.0", port=port)
