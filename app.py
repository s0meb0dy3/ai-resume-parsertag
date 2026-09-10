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
import inspect
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from config import get_logger_manager, MODEL_NAME, ASYNC_MODEL_NAME, SYNC_MODEL_NAME, MODEL_SERVICE_URL, MAX_TOKENS, FALLBACK_MODELS
from logger.logger_manager import log_context, logger
from logger.langfuse_client import propagate_attributes, LANGFUSE_TAGS
from logger.operation import (
    LOG_FILENAME_CONTEXT_HEADER,
    log_completion_summary,
    log_json_response,
)
from core.parser import _cleanup_uploads
from core.tasks import _periodic_cleanup, executor

from api.routes_async     import router as async_router
from api.routes_openai    import router as openai_router
from api.routes_tags      import router as tags_router
from api.routes_algorithm import router as algorithm_router
from api.routes_model_config import router as model_config_router
from core.model_config import ModelSnapshotMiddleware, get_model_store
from api.response_schemas import HealthResponse, ModelListResponse


OPENAPI_TAGS = [
    {
        "name": "服务状态",
        "description": "服务健康检查和 OpenAI 兼容模型能力探测。",
    },
    {
        "name": "异步简历",
        "description": "异步提交、批量处理、结果轮询和任务状态查询。",
    },
    {
        "name": "同步与兼容接口",
        "description": "同步 PDF/DOCX 解析、打标及 OpenAI Chat Completions 兼容入口。",
    },
    {
        "name": "标签配置",
        "description": "正向维度、负向规则和风险扣分配置管理。",
    },
    {
        "name": "算法调试",
        "description": "跳过文档解析，独立验证正向标签和风险检测算法。",
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理日志热更新、任务清理器和全局线程池的生命周期。

    Args:
        app (FastAPI): 当前 FastAPI 应用实例。参数由框架注入，函数不依赖
            其内部状态。

    Yields:
        None: 启动资源就绪后将控制权交给 FastAPI。

    Note:
        关闭时不等待可能长时间阻塞的 LLM 任务，由部署环境的
        终止宽限承担最终回收。
    """
    lm = get_logger_manager()
    # Uvicorn 在导入应用后会重写 access logger；在 lifespan 内再次收口，避免
    # 每次轮询产生一条缺失业务 ContextVar 的重复访问日志。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    lm.start_config_watcher()
    active_models = get_model_store().state().current
    logger.info(f"[运行时模型配置] {active_models.model_dump()}")
    fallback_str = " → ".join(active_models.fallbackModels) or "无"
    logger.info(
        f"[模型配置] 默认={MODEL_NAME} | 异步={active_models.asyncModel} | 同步={active_models.syncModel} | "
        f"备用={fallback_str} | MAX_TOKENS={MAX_TOKENS} | LLM={MODEL_SERVICE_URL}"
    )
    _cleanup_uploads()
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    yield
    cleanup_task.cancel()
    # wait=False：LLM 调用可能耗时数分钟，等待会阻塞 lifespan 关闭；K8s 有 terminationGracePeriodSeconds 兜底
    executor.shutdown(wait=False)
    lm.stop_config_watcher()
    lm.stop_queue_listener()


app = FastAPI(
    title="AI-Resume-ParserTag",
    summary="简历解析、结构化和标签打标服务",
    description=(
        "接口定义由 FastAPI 路由和 Pydantic Schema 自动生成。"
        "Swagger UI 是当前代码版本的接口契约，静态说明仅作调用示例。"
    ),
    version="3.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=OPENAPI_TAGS,
    lifespan=lifespan,
)

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
app.include_router(model_config_router)
app.add_middleware(ModelSnapshotMiddleware)


_JSON_RESPONSE_OPERATION_NAMES = {
    ("GET", "/v1/models"): "查询模型列表",
    ("POST", "/v1/chat/completions"): "OpenAI兼容简历处理",
    ("POST", "/api/resume/sync/parse"): "同步仅解析",
    ("POST", "/api/resume/sync/tag"): "同步仅打标签",
    ("POST", "/api/resume/sync/batch"): "同步解析与打标签",
    ("POST", "/api/resume/async/parse"): "异步解析与打标签提交",
    ("POST", "/api/resume/async/batch"): "异步批量解析与打标签提交",
    ("POST", "/api/resume/async/batch/parse"): "异步批量仅解析提交",
    ("POST", "/api/resume/async/batch/tag"): "异步批量仅打标签提交",
    ("POST", "/api/resume/async/batch/multiform"): "异步混合解析与打标签提交",
    ("GET", "/api/resume/async/result"): "异步结果轮询",
    ("GET", "/api/tasks"): "查询异步任务列表",
    ("GET", "/api/tags/config"): "查询标签配置",
    ("GET", "/api/tags/mapping"): "查询标签ID映射",
    ("PUT", "/api/tags/config/severity"): "更新风险等级扣分配置",
    ("POST", "/api/tags/config/reset"): "重置标签配置",
    ("POST", "/algorithm/positive-tag/extract"): "提取正向标签",
    ("POST", "/algorithm/sensitive-tag/detect"): "检测风险标签",
}


def _describe_json_response_operation(method: str, path: str) -> str:
    """把 JSON 接口的 HTTP 路由转换为稳定、可读的中文业务名称。"""
    normalized_method = method.upper()
    exact = _JSON_RESPONSE_OPERATION_NAMES.get((normalized_method, path))
    if exact:
        return exact

    if path.startswith("/api/tags/config/negative/") and "/rules" in path:
        return {
            "POST": "新增负向标签规则",
            "PUT": "更新负向标签规则",
            "DELETE": "删除负向标签规则",
        }.get(normalized_method, "处理负向标签规则")
    if path.startswith("/api/tags/config/positive"):
        return {
            "POST": "新增正向标签维度",
            "PUT": "更新正向标签维度",
            "DELETE": "删除正向标签维度",
        }.get(normalized_method, "处理正向标签配置")
    if path.startswith("/api/tags/config/negative"):
        return {
            "POST": "新增负向标签维度",
            "PUT": "更新负向标签维度",
            "DELETE": "删除负向标签维度",
        }.get(normalized_method, "处理负向标签配置")
    return "接口响应"


def _format_request_timestamp(timestamp: float) -> str:
    """把请求时间格式化为精确到秒的本地时间。"""
    return datetime.fromtimestamp(timestamp).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _endpoint_description(request: Request) -> tuple[str, str, bool]:
    """返回路由方法名、源码位置以及是否已自行输出完成摘要。"""
    endpoint = request.scope.get("endpoint")
    if endpoint is None:
        return "unknown", "unknown:0", False

    interface_method = getattr(endpoint, "__name__", type(endpoint).__name__)
    source_path = inspect.getsourcefile(endpoint) or "unknown"
    try:
        source_line = inspect.getsourcelines(endpoint)[1]
    except (OSError, TypeError):
        source_line = 0
    return (
        interface_method,
        f"{Path(source_path).name}:{source_line}",
        bool(getattr(endpoint, "_emits_completion_log", False)),
    )


def _summarize_json_payload(payload: object) -> dict[str, object]:
    """从实际响应 JSON 中提取稳定的业务数量和状态字段。"""
    if not isinstance(payload, dict):
        return {}

    summary: dict[str, object] = {}
    for key in ("code", "total", "success", "failed", "submitted"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            summary[key] = value
    data = payload.get("data")
    if isinstance(data, dict):
        status = data.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            summary["businessStatus"] = status
    return summary


def _response_filename_context(payload: object) -> str:
    """从完整 JSON 响应中提取适合日志表头展示的文件名。"""
    filenames: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, dict):
            filename = value.get("filename")
            if isinstance(filename, str) and filename.strip():
                filenames.append(filename.strip())
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(payload)
    unique = list(dict.fromkeys(filenames))
    if not unique:
        return "-"
    if len(unique) == 1:
        return unique[0]
    return f"{unique[0]} 等{len(unique)}个文件"


@app.middleware("http")
async def _reset_log_context(request: Request, call_next):
    """为每个 HTTP 请求建立独立且可恢复的日志上下文。

    Args:
        request (Request): 当前 HTTP 请求。
        call_next (Callable): FastAPI 注入的后续中间件或路由调用器。

    Returns:
        Response: 后续请求链生成的 HTTP 响应。
    """
    request_started_at = time.time()
    with log_context(
        request_id=None,
        task_id="-",
        business_type="-",
        filename="-",
    ),propagate_attributes(trace_name=request.url.path, tags=LANGFUSE_TAGS):
        response = await call_next(request)
        request_ended_at = time.time()
        # 健康探针调用频繁且不属于业务访问，不写入 API 访问日志。
        if request.url.path != "/health":
            # 经 Kubernetes 网关或反向代理调用时，客户端地址通常位于
            # X-Forwarded-For 的第一项；直连服务时回退到 ASGI 连接地址。
            forwarded_for = request.headers.get("x-forwarded-for", "")
            client_ip = (
                forwarded_for.split(",", 1)[0].strip()
                if forwarded_for
                else (request.client.host if request.client else "-")
            )
            # 在全局中间件统一记录，新增路由也能直接按接口或 IP 在 Loki 检索。
            logger.info(
                f"[API访问] method={request.method} path={request.url.path} "
                f"clientIp={client_ip} statusCode={response.status_code} "
                f"durationMs={round((request_ended_at - request_started_at) * 1000)}"
            )
        encoded_route_filename = response.headers.get(LOG_FILENAME_CONTEXT_HEADER)
        route_filename_context = (
            unquote(encoded_route_filename) if encoded_route_filename else "-"
        )
        if encoded_route_filename is not None:
            del response.headers[LOG_FILENAME_CONTEXT_HEADER]

        # 统一记录所有 JSON 接口响应，新增接口无需再逐个补日志。探针和
        # OpenAPI Schema 体积大且调用频繁，明确排除；SSE/文件流不消费。
        content_type = response.headers.get("content-type", "").lower()
        excluded_paths = {"/health", "/openapi.json"}
        if "json" in content_type and request.url.path not in excluded_paths:
            body_iterator = getattr(response, "body_iterator", None)
            if body_iterator is not None:
                chunks: list[bytes] = []
                async for chunk in body_iterator:
                    chunks.append(
                        chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                    )
                response_body = b"".join(chunks)

                async def replay_body():
                    """把已读取的响应体原样交还 ASGI 发送链。"""
                    yield response_body

                response.body_iterator = replay_body()
            else:
                response_body = getattr(response, "body", b"")

            if response_body:
                try:
                    payload = json.loads(response_body)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = None
                if payload is not None:
                    operation_name = _describe_json_response_operation(
                        request.method,
                        request.url.path,
                    )
                    if response.status_code >= 500:
                        response_level = "ERROR"
                    elif response.status_code >= 400:
                        response_level = "WARNING"
                    elif (
                        isinstance(payload, dict)
                        and isinstance(payload.get("code"), int)
                        and payload["code"] != 0
                    ):
                        response_level = "WARNING"
                    elif request.url.path == "/api/resume/async/result":
                        data = payload.get("data") if isinstance(payload, dict) else None
                        task_status = data.get("status") if isinstance(data, dict) else None
                        response_level = (
                            "INFO" if task_status in (3, 4) else "DEBUG"
                        )
                    else:
                        response_level = "INFO"

                    response_filename_context = _response_filename_context(payload)
                    if response_filename_context == "-":
                        response_filename_context = route_filename_context
                    with log_context(
                        business_type=operation_name,
                        filename=response_filename_context,
                    ):
                        interface_method, code_location, emits_completion = (
                            _endpoint_description(request)
                        )
                        # 使用 log_async_operation 的业务接口已在函数返回前输出
                        # 解析/打标/模型/Token 等完整指标；这里只为未装饰接口
                        # 构造通用完成摘要，确保每份响应 JSON 前恰好有一份摘要。
                        if not emits_completion:
                            completion_payload = {
                                "interfaceMethod": interface_method,
                                "codeLocation": code_location,
                                "requestStartTime": _format_request_timestamp(
                                    request_started_at
                                ),
                                "requestEndTime": _format_request_timestamp(
                                    request_ended_at
                                ),
                                "totalDurationMs": round(
                                    (request_ended_at - request_started_at) * 1000
                                ),
                                "responseType": "JSONResponse",
                                "statusCode": response.status_code,
                                **_summarize_json_payload(payload),
                            }
                            log_completion_summary(
                                operation_name,
                                completion_payload,
                                level=response_level,
                            )
                        log_json_response(
                            operation_name,
                            payload,
                            level=response_level,
                        )
        return response


@app.get("/health", tags=["服务状态"], response_model=HealthResponse)
async def health_check():
    """返回不访问外部依赖的进程存活状态。

    Returns:
        dict: 包含固定 `healthy` 状态和当前本地时间的探针响应。
    """
    # 负载均衡/K8s 存活探针，轻量无依赖
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.get("/v1/models", tags=["服务状态"], response_model=ModelListResponse)
async def list_models():
    """返回 OpenAI SDK 兼容的逻辑模型列表。

    Returns:
        dict: 包含解析、解析加打标和兼容别名的 OpenAI `list`
            响应。

    Note:
        该接口用于客户端能力探测，不代表后端模型服务已就绪。
    """
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
