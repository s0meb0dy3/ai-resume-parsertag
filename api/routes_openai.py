"""
OpenAI Chat Completions 兼容接口。

model 字段决定行为：
  DeepSeek-OCR      → 仅解析，返回简历 JSON 字符串
  DeepSeek-OCR-JSON → 解析 + 打标；async_mode=true 立即返回 taskId，否则同步等待

批量模式：content 数组含多个文件 item 时自动并发，返回 JSON 数组（每项含 success/result）。

同步批量接口（阻塞等待全部完成）：
  /api/resume/sync/batch  → 解析 + 打标，返回完整结果列表
  /api/resume/sync/parse  → 仅解析，结果 resume 字段可直接作为 /api/resume/sync/tag 输入
  /api/resume/sync/tag    → 仅打标，输入已解析的简历 JSON，无需重新解析原文件

注：同步接口均使用 SYNC_MODEL_NAME（可配置为较小/较快模型），异步接口使用 ASYNC_MODEL_NAME。
"""
import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.model_config import get_runtime_model
from config import (
    MAX_WORKERS, logger, reset_current_model_name, set_current_model_name,
    log_context, set_log_context, wrap_for_executor,
)
from api.response_schemas import (
    ChatCompletionResponse,
    HttpErrorResponse,
    SyncBatchResponse,
    SyncParseBatchResponse,
    TextEchoResponse,
)
from api.schemas import SyncBatchRequest, SyncTagRequest
from utils.file_utils import get_document_source_from_content, normalize_filename
from core.parser import (
    _call_llm_with_fallback,
    parse_sync_resume_for_tagging_with_logging,
    parse_sync_resume_with_logging,
)
from core.document_text import detect_document_type
from core.postprocess import _conform_to_template
from tag.tagger import run_tagging
from core.tasks import (
    TaskRecord,
    _STATUS_DESC,
    _log_resume_tag_result,
    _parse_semaphore,
    _task_lock,
    _task_store,
    executor,
    run_parse_and_tag_task,
)
from logger.operation import log_async_operation, log_operation, record_stage_timing
from logger.langfuse_client import observe, update_current_observation

router = APIRouter(tags=["同步与兼容接口"])


# 同步接口使用独立的模型上下文，避免与异步任务共享 ContextVar 导致模型名互相覆盖
@observe()
def parse_sync_document_with_model(source, filename: str):
    """在同步接口的模型上下文中解析一份 PDF。

    Args:
        source (Any): PDF 本地路径或字节。
        filename (str): 日志和结果关联使用的文件名。

    Returns:
        dict: 经后处理和 Schema 归一的简历对象。

    Raises:
        Exception: PDF 打开、模型调用或结果解析失败时向上传递。
    """
    token = set_current_model_name(get_runtime_model("sync"))
    try:
        resume = parse_sync_resume_with_logging(source, filename)
        # 同步链路根 span 内部（span 活跃期）写 trace 级 input/output
        update_current_observation(
            input={"filename": filename},
            output={"status": "completed", "resume": resume},
        )
        return resume
    finally:
        reset_current_model_name(token)


@observe()
def tag_sync_resume_with_model(resume: dict, job_info: dict):
    """在同步接口的模型上下文中对结构化简历打标。

    Args:
        resume (dict): 已解析的简历 JSON。
        job_info (dict): 岗位名称、技能、描述和敏感词上下文。

    Returns:
        dict: 正向标签、风险命中、扣分和打标时间。
    """
    token = set_current_model_name(get_runtime_model("sync"))
    try:
        tag_info = run_tagging(resume, job_info, llm_call_fn=_call_llm_with_fallback)
        # 同步链路根 span 内部（span 活跃期）写 trace 级 input/output
        update_current_observation(
            input={
                "resume": {
                    "works":      len(resume.get("works") or []),
                    "educations": len(resume.get("educations") or []),
                    "projects":   len(resume.get("projects") or []),
                }
            },
            output={"status": "completed", "tagInfo": tag_info},
        )
        return tag_info
    finally:
        reset_current_model_name(token)


# 驱动 Swagger 文档的请求体模型

class _FileInfo(BaseModel):
    """OpenAI content 条目中的文件名、来源和可选内容。"""
    name: str = Field(
        default="resume.pdf",
        description="文件名，用于日志标识和返回结果中的 filename 字段，建议填写真实文件名",
    )
    path: str = Field(
        default="",
        description=(
            "文件地址。\n"
            "- `type=url` 时填写 HTTP/HTTPS 下载链接，服务端需能访问该地址\n"
            "- `type=file` 时填写容器内绝对路径（默认目录 `/app/uploads/`）"
        ),
    )
    content: str = Field(
        default="",
        description="Base64 编码的文件内容，仅在 `type=base64` 时填写",
    )


class _ContentItem(BaseModel):
    """表示 OpenAI 消息中的文件或文本 content 条目。"""
    type: str = Field(
        description=(
            "文件来源类型：\n"
            "- `url`：从 HTTP 链接下载文件\n"
            "- `file`：读取服务器容器内本地文件（路径须为容器内绝对路径）\n"
            "- `base64`：直接上传 Base64 编码的文件内容\n"
            "- `text`：纯文本输入（调试用）"
        ),
    )
    file: _FileInfo = Field(default_factory=_FileInfo, description="文件信息，`type` 为 url / file / base64 时使用")
    text: str = Field(default="", description="纯文本内容，仅 `type=text` 时填写")


class _ChatMessage(BaseModel):
    """表示 OpenAI 兼容的消息角色和 content 载荷。"""
    role: str = Field(default="user", description="消息角色，固定传 `user`")
    content: Union[str, List[_ContentItem]] = Field(
        description="消息内容：文件列表（List[ContentItem]）或文本字符串。简历解析时固定传 List[ContentItem]"
    )


class ChatCompletionRequest(BaseModel):
    """定义简历解析场景使用的 Chat Completions 兼容请求。

    `model` 决定仅解析或解析加打标，`async_mode` 仅对单文件生效。
    批量 content 使用 `max_concurrency` 限制本次请求内的并发数。
    """
    model: str = Field(
        default="DeepSeek-OCR",
        description="模型名称：`DeepSeek-OCR`（仅解析）或 `DeepSeek-OCR-JSON`（解析+打标，兼容别名）",
    )
    messages: List[_ChatMessage] = Field(
        description=(
            "消息列表，取最后一条 role=user 的消息的 content 数组作为文件输入。\n\n"
            "**单份模式**：content 中放 1 个文件 item，返回单份简历 JSON 对象字符串。\n\n"
            "**批量模式**：content 中放多个文件 item，服务端自动并发解析全部文件，"
            "返回 JSON 数组字符串（每项含 index / filename / success / result / elapsed_s / error）。"
        )
    )
    stream: bool = Field(
        default=False,
        description="是否启用 SSE 流式返回。`true` 时以 `text/event-stream` 格式推送；`false` 时一次性返回",
    )
    max_concurrency: int = Field(
        default=MAX_WORKERS,
        ge=1,
        le=30,
        description="批量模式并发上限，取值 1~30，默认 24。单份模式忽略此参数",
    )
    async_mode: bool = Field(
        default=False,
        description=(
            "异步模式（仅单份模式生效）。\n\n"
            "`false`（默认）：阻塞等待解析完成，`content` 直接返回简历 JSON 字符串。\n\n"
            "`true`：立即返回，`content` 为 `{taskId, status, estimatedSeconds}` JSON 字符串，"
            "通过 `GET /api/resume/async/result?taskId=xxx` 轮询结果。"
        ),
    )
    jobTitle:          Optional[str] = Field(default="", description="岗位名称，用于打标时的岗位匹配（仅 DeepSeek-OCR-JSON 生效）")
    requiredSkills:    Optional[str] = Field(default="", description="岗位要求技能，逗号分隔")
    jobDescription:    Optional[str] = Field(default="", description="岗位详细描述")
    jobSensitiveWords: Optional[str] = Field(default="", description="岗位敏感词，用于风险打标")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "单份解析 —— URL 方式",
                    "value": {
                        "model": "DeepSeek-OCR",
                        "stream": False,
                        "messages": [{"role": "user", "content": [
                            {"type": "url", "file": {"name": "张三.pdf", "path": "http://10.2.5.121:9000/bucket/张三.pdf"}}
                        ]}],
                    },
                },
                {
                    "summary": "单份解析 —— 服务器本地文件",
                    "value": {
                        "model": "DeepSeek-OCR",
                        "stream": False,
                        "messages": [{"role": "user", "content": [
                            {"type": "file", "file": {"name": "张三.pdf", "path": "/app/uploads/张三.pdf"}}
                        ]}],
                    },
                },
                {
                    "summary": "单份解析+打标 —— 异步模式",
                    "value": {
                        "model": "DeepSeek-OCR-JSON",
                        "async_mode": True,
                        "messages": [{"role": "user", "content": [
                            {"type": "url", "file": {"name": "张三.pdf", "path": "http://10.2.5.121:9000/bucket/张三.pdf"}}
                        ]}],
                    },
                },
            ]
        }
    }


# OpenAI 兼容路由

@router.post(
    "/v1/chat/completions",
    response_model=ChatCompletionResponse | TextEchoResponse,
    responses={
        400: {"model": HttpErrorResponse, "description": "文档来源或解析请求无效"},
        200: {
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "example": "data: {\"object\":\"chat.completion\"}\n\n",
                }
            },
            "description": "JSON completion；stream=true 时为 SSE 数据流",
        },
    },
)
async def create_chat_completion(request: ChatCompletionRequest):
    """处理 OpenAI Chat Completions 兼容的简历解析请求。

    Args:
        request (ChatCompletionRequest): 模型别名、消息、流式/异步选项和
            可选岗位上下文。

    Returns:
        JSONResponse | StreamingResponse: OpenAI 兼容 completion，或在异步
            单文件模式下包含任务标识的 completion。
    """
    import json_repair as _jr
    operation_name = (
        "同步解析"
        if request.model == "DeepSeek-OCR"
        else ("异步解析与打标" if request.async_mode else "同步解析与打标")
    )
    set_log_context(request_id=uuid.uuid4().hex[:8], business_type=operation_name)
    model    = request.model
    stream   = request.stream
    messages = request.messages
    contents = messages[-1].content if messages else []
    # 兼容 Pydantic 模型与 dict 两种 content 结构
    if hasattr(contents, "__iter__") and not isinstance(contents, (str, list)):
        contents = list(contents)
    if isinstance(contents, list):
        contents = [c.model_dump() if hasattr(c, "model_dump") else c for c in contents]

    def _is_probe(c) -> bool:
        """识别客户端发送的连通性或模型探测文本。

        Args:
            c (Any): 最后一条用户消息的 content。

        Returns:
            bool: content 符合已知探测文本特征时返回 `True`。
        """
        # OpenAI SDK 初始化时会发送 "### Task" 前缀的连通性探测消息，无需走解析流程
        # json_repair.loads 返回 str（而非 dict）说明 content 是纯文本而非 JSON 结构，同样视为探测
        if not c:
            return True
        if isinstance(c, list) and len(c) == 0:
            return True
        if isinstance(c, str):
            if "### Task" in c:
                return True
            if isinstance(_jr.loads(c), str):
                return True
        return False

    if _is_probe(contents):
        # 探测请求直接返回 ok，不消耗 LLM 资源
        return JSONResponse(content={
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}", "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1},
        })

    logger.info(f"请求 model={model}, type={contents[0].get('type') if isinstance(contents, list) else type(contents).__name__}")

    if isinstance(contents, str):
        text_json = _jr.loads(contents)
        if isinstance(text_json, str):
            return JSONResponse(
                status_code=200,
                media_type="application/json; charset=utf-8",
                content={"type": "text", "text": contents},
            )
        elif "text" != text_json.get("type", "text"):
            contents = [text_json]
        else:
            contents = [{"type": "text", "text": str(contents)}]

    if model == "DeepSeek-OCR":
        set_log_context(task_id=str(uuid.uuid4()))
        source, filename = get_document_source_from_content(contents)
        if source is None:
            return JSONResponse(status_code=400, content={"error": "无法从请求中获取 PDF/DOCX/JPEG/PNG 简历，请检查 content.type 字段"})
        set_log_context(business_type="同步解析", filename=filename or "resume.pdf")
        t2 = time.time()
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                executor, wrap_for_executor(parse_sync_document_with_model), source, filename or "unknown.pdf"
            )
        except Exception as e:
            logger.error(f"文档解析失败: {e}")
            return JSONResponse(status_code=400, content={"error": f"文档解析失败: {str(e)}"})
        generated_text = json.dumps(result, ensure_ascii=False, indent=2)
        logger.info(f"{model} | {len(generated_text)} chars | {time.time() - t2:.2f}s")

    elif model == "DeepSeek-OCR-JSON":
        task_id = str(uuid.uuid4())
        set_log_context(task_id=task_id)
        source, filename = get_document_source_from_content(contents)
        if source is None:
            return JSONResponse(status_code=400, content={"error": "无法从请求中获取 PDF/DOCX/JPEG/PNG 简历，请检查 content.type 字段"})
        set_log_context(business_type=operation_name, filename=filename or "resume.pdf")
        job_info = {
            "jobTitle":          request.jobTitle          or "",
            "requiredSkills":    request.requiredSkills    or "",
            "jobDescription":    request.jobDescription    or "",
            "jobSensitiveWords": request.jobSensitiveWords or "",
        }
        if request.async_mode:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    executor,
                    wrap_for_executor(detect_document_type),
                    source,
                    filename or "resume.pdf",
                )
            except Exception as exc:
                return JSONResponse(status_code=400, content={"error": f"文档内容校验失败: {exc}"})

        rec = TaskRecord(task_id=task_id)
        async with _task_lock:
            _task_store[task_id] = rec

        if request.async_mode:
            asyncio.create_task(run_parse_and_tag_task(task_id, source, filename or "resume.pdf", job_info, get_runtime_model("sync"), trace_name="/v1/chat/completions"))
            generated_text = json.dumps(
                {"taskId": task_id, "status": 0, "statusDesc": "待处理", "estimatedSeconds": 90},
                ensure_ascii=False,
            )
            logger.info(f"{model} async_mode=true taskId={task_id}")
        else:
            await run_parse_and_tag_task(
                task_id, source, filename or "resume.pdf", job_info, get_runtime_model("sync"),
                parse_fn=parse_sync_resume_for_tagging_with_logging,
                operation_name="同步解析与打标",
                trace_name="/v1/chat/completions",
            )
            async with _task_lock:
                rec = _task_store.get(task_id)
            if rec and rec.status == 3:
                generated_text = json.dumps(rec.result, ensure_ascii=False, indent=2)
                logger.info(f"{model} sync taskId={task_id} | {len(generated_text)} chars")
            elif rec and rec.status == 4:
                return JSONResponse(status_code=400, content={"error": rec.error_msg or "处理失败"})
            else:
                generated_text = "{}"
    else:
        generated_text = ""

    # 用字符数代替真实 token 数：此接口不经过真正的 tokenizer，调用方通常不依赖 usage 字段
    prompt_tokens = completion_tokens = len(generated_text)
    content_body  = {
        "id": f"ocr-{uuid.uuid4().hex}", "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": generated_text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }

    if stream:
        async def _stream(data):
            """将完整 completion 封装为单个 SSE data 事件。

            Args:
                data (dict): 已组装的 OpenAI completion 响应。

            Yields:
                str: 包含 JSON 的 SSE `data:` 行。
            """
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        return StreamingResponse(_stream(content_body), media_type="text/event-stream")
    return JSONResponse(status_code=200, media_type="application/json; charset=utf-8", content=content_body)


@router.post("/api/resume/sync/batch", response_model=SyncBatchResponse)
@log_async_operation("同步解析与打标")
async def sync_parse_and_tag_resumes(request: SyncBatchRequest):
    """同步批量执行 PDF 解析和打标并返回按项结果。

    每条 result 格式与 GET /api/resume/async/result (status=3) 的 data 字段完全一致。
    单次上限 50 份；max_concurrency 控制并发上限（1~30，默认同 MAX_WORKERS）。
    支持与 /api/resume/async/batch 相同的请求体字段（jobTitle / requiredSkills / jobDescription / jobSensitiveWords）。

    Args:
        request (SyncBatchRequest): 1–50 份简历、并发上限和岗位上下文。

    Returns:
        JSONResponse: 批次统计以及每项成功结果或错误。
    """
    t0           = time.time()
    resume_items = request.resumes
    max_conc     = min(request.max_concurrency or MAX_WORKERS, 30)
    job_info     = request.to_job_info()

    loop = asyncio.get_running_loop()

    # 下载/读取每份简历文件（同步完成，失败直接标记 error）
    prepared = []
    for item in resume_items:
        file_meta = item.get("file", {})
        filename  = normalize_filename(file_meta.get("name", "resume.pdf"))
        extra     = file_meta.get("extra") or item.get("extra") or None
        task_id = str(uuid.uuid4())
        with log_context(task_id=task_id, filename=filename):
            try:
                source, fn = await loop.run_in_executor(
                    executor,
                    wrap_for_executor(get_document_source_from_content),
                    [item],
                )
            except Exception as e:
                prepared.append({"task_id": None, "filename": filename, "extra": extra, "error": str(e), "source": None})
                continue
            if source is None:
                prepared.append({"task_id": None, "filename": filename, "extra": extra,
                                  "error": "无法获取 PDF/DOCX/JPEG/PNG 简历，请检查 type/path/content 字段", "source": None})
                continue
            async with _task_lock:
                _task_store[task_id] = TaskRecord(task_id=task_id, extra=extra)
            prepared.append({"task_id": task_id, "filename": fn or filename,
                             "extra": extra, "source": source, "error": None})

    # 双层限流：sem 控制本次批量请求内的并发数，_parse_semaphore 在全局层面防止线程池积压
    sem = asyncio.Semaphore(max_conc)

    async def _run_sync_parse_and_tag_item(p: dict):
        """在批次并发限制内执行单份解析打标任务。

        Args:
            p (dict): 包含任务标识、简历来源和预处理错误的批次项。
        """
        if p["error"]:
            return
        async with sem:
            await run_parse_and_tag_task(
                p["task_id"], p["source"], p["filename"], job_info, get_runtime_model("sync"),
                parse_fn=parse_sync_resume_for_tagging_with_logging,
                operation_name="同步解析与打标",
                trace_name="/api/resume/sync/batch",
            )

    await asyncio.gather(*[_run_sync_parse_and_tag_item(p) for p in prepared])

    # 每条结果完整包装为与 GET /api/resume/async/result 相同的响应结构
    results = []
    for p in prepared:
        if p["error"]:
            data: dict = {
                "taskId":           None,
                "status":           4,
                "statusDesc":       "任务失败",
                "elapsedSeconds":   0,
                "estimatedSeconds": 90,
                "extra":            p["extra"],
                "errorMsg":         p["error"],
            }
            results.append({"code": 1, "msg": p["error"], "data": data})
            continue
        async with _task_lock:
            rec = _task_store.get(p["task_id"])
        status  = rec.status if rec else 4
        elapsed = round((rec.completed_at or time.time()) - rec.created_at, 1) if rec else 0
        data = {
            "taskId":           p["task_id"],
            "status":           status,
            "statusDesc":       _STATUS_DESC.get(status, "未知"),
            "elapsedSeconds":   elapsed,
            "estimatedSeconds": 90,
            "extra":            p["extra"],
        }
        if rec and rec.status == 3:
            data["result"] = rec.result
        elif rec and rec.status == 4:
            data["errorMsg"] = rec.error_msg
        results.append({"code": 0, "msg": "success", "data": data})

    success = sum(1 for r in results if r.get("code") == 0 and r.get("data", {}).get("status") == 3)
    response_body = {
        "total":     len(results),
        "success":   success,
        "failed":    len(results) - success,
        "elapsed_s": round(time.time() - t0, 2),
        "results":   results,
    }
    return JSONResponse(content=response_body)


@router.post("/api/resume/sync/parse", response_model=SyncParseBatchResponse)
@log_async_operation("同步解析")
async def sync_parse_resumes(request: SyncBatchRequest):
    """同步批量解析 PDF/DOCX/JPEG/PNG，不执行标签阶段。

    results[].data.resume 为解析后的原始简历 JSON，可直接作为 /api/resume/sync/tag 的输入。
    单次上限 50 份；max_concurrency 控制并发上限（1~30，默认同 MAX_WORKERS）。
    请求体字段与 /api/resume/sync/batch 完全一致（resumes 数组，每项含 type / file / text）。

    Args:
        request (SyncBatchRequest): 1–50 份简历和本次请求的并发上限。

    Returns:
        JSONResponse: 批次统计和每项已归一的 `resume`，或获取/解析错误。
    """
    t0           = time.time()
    resume_items = request.resumes
    max_conc     = min(request.max_concurrency or MAX_WORKERS, 30)

    loop = asyncio.get_running_loop()

    # 下载/读取每份简历文件（同步完成，失败直接标记 error）
    prepared = []
    for item in resume_items:
        file_meta = item.get("file", {})
        filename  = normalize_filename(file_meta.get("name", "resume.pdf"))
        extra     = file_meta.get("extra") or item.get("extra") or None
        item_id = str(uuid.uuid4())
        with log_context(task_id=item_id, filename=filename):
            try:
                source, fn = await loop.run_in_executor(
                    executor,
                    wrap_for_executor(get_document_source_from_content),
                    [item],
                )
            except Exception as e:
                prepared.append({"item_id": item_id, "filename": filename, "extra": extra, "error": str(e), "source": None, "sourceType": item.get("type")})
                continue
            if source is None:
                prepared.append({"item_id": item_id, "filename": filename, "extra": extra,
                                  "error": "无法获取 PDF/DOCX/JPEG/PNG 简历，请检查 type/path/content 字段", "source": None, "sourceType": item.get("type")})
                continue
            prepared.append({"item_id": item_id, "filename": fn or filename, "extra": extra, "source": source, "error": None, "sourceType": item.get("type")})

    sem = asyncio.Semaphore(max_conc)

    async def _run_sync_parse_item(p: dict):
        """在请求级和全局限流器下解析一份 PDF。

        Args:
            p (dict): 包含文件名、简历来源和预处理错误的批次项。
        """
        if p["error"]:
            return
        async with sem:
            async with _parse_semaphore:
                with log_context(
                    task_id=p["item_id"],
                    business_type="同步解析",
                    filename=p["filename"],
                ):
                  parse_started_at = time.time()
                  try:
                    with log_operation("同步解析单项", file=p["filename"]) as operation:
                        p["resume"] = await loop.run_in_executor(
                            executor,
                            wrap_for_executor(parse_sync_document_with_model),
                            p["source"], p["filename"],
                        )
                        p["parse_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        operation.add(
                            educations=len(p["resume"]["educations"]),
                            works=len(p["resume"]["works"]),
                            projects=len(p["resume"]["projects"]),
                        )
                  except Exception as e:
                    p["error"]      = str(e)
                    p["resume"]     = None
                    p["parse_time"] = ""
                  finally:
                    record_stage_timing(
                        "parse",
                        parse_started_at,
                        time.time(),
                    )

    await asyncio.gather(*[_run_sync_parse_item(p) for p in prepared])

    results = []
    for p in prepared:
        if p.get("error") or p.get("resume") is None:
            err_msg = p.get("error", "解析失败")
            results.append({
                "code": 1,
                "msg":  err_msg,
                "data": {
                    "filename": p["filename"],
                    "extra":    p["extra"],
                    "errorMsg": err_msg,
                },
            })
        else:
            results.append({
                "code": 0,
                "msg":  "success",
                "data": {
                    "filename":             p["filename"],
                    "extra":                p["extra"],
                    "resumeParseFinishTime": p["parse_time"],
                    "resume":               p["resume"],
                },
            })

    success = sum(1 for r in results if r.get("code") == 0)
    response_body = {
        "total":     len(results),
        "success":   success,
        "failed":    len(results) - success,
        "elapsed_s": round(time.time() - t0, 2),
        "results":   results,
    }
    return JSONResponse(content=response_body)


@router.post("/api/resume/sync/tag", response_model=SyncBatchResponse)
@log_async_operation("同步打标")
async def sync_tag_resumes(request: SyncTagRequest):
    """同步批量打标已解析简历，不重复处理 PDF。

    输入中 resumes 数组的每项结构与 /api/resume/sync/parse 响应的 results[].data 字段完全一致：
      filename / extra / resumeParseFinishTime / resume（必填，解析后的简历 JSON）。
    输出格式与 /api/resume/sync/batch 完全一致，results[].data.result 包含解析+打标完整 JSON。
    单次上限 50 份；max_concurrency 控制并发上限（1~30，默认同 MAX_WORKERS）。
    支持 jobTitle / requiredSkills / jobDescription / jobSensitiveWords 岗位信息字段。

    Args:
        request (SyncTagRequest): 1–50 份已解析简历、并发上限和岗位上下文。

    Returns:
        JSONResponse: 批次统计和每项完整打标结果或错误。
    """
    t0           = time.time()
    resume_items = request.resumes
    max_conc     = min(request.max_concurrency or MAX_WORKERS, 30)
    job_info     = request.to_job_info()

    loop = asyncio.get_running_loop()
    sem  = asyncio.Semaphore(max_conc)

    async def _run_sync_tag_item(item) -> dict:
        """在本次请求的并发限制内打标一份简历。

        Args:
            item (ParsedResumeItem): 已解析简历和其业务元数据。

        Returns:
            dict: 单项成功结果或带任务标识的错误结果。
        """
        filename          = normalize_filename(item.filename)
        extra             = item.extra
        resume_parse_time = item.resumeParseFinishTime
        resume            = item.resume.model_dump()
        task_id           = str(uuid.uuid4())

        async with sem:
            tag_start = time.time()
            with log_context(
                task_id=task_id,
                business_type="同步打标",
                filename=filename,
            ), log_operation("同步打标单项", taskId=task_id, file=filename) as operation:
              try:
                # 用局部变量捕获当前迭代的 resume/job_info，避免 asyncio.gather 并发时 lambda 闭包共享外层变量
                _resume   = resume
                _job_info = job_info
                tag_info  = await loop.run_in_executor(
                    executor,
                    wrap_for_executor(lambda: tag_sync_resume_with_model(_resume, _job_info)),
                )
                tag_elapsed = time.time() - tag_start
                elapsed = round(tag_elapsed, 1)
                result  = _conform_to_template(task_id, resume, tag_info, resume_parse_time)
                item_resp = {
                    "code": 0,
                    "msg":  "success",
                    "data": {
                        "taskId":           task_id,
                        "status":           3,
                        "statusDesc":       "任务完成",
                        "elapsedSeconds":   elapsed,
                        "estimatedSeconds": 90,
                        "extra":            extra,
                        "result":           result,
                    },
                }
                _log_resume_tag_result(
                    task_id,
                    filename,
                    tag_info,
                    resume_parse_time,
                    tag_elapsed,
                    tag_elapsed,
                )
                operation.add(
                    tagDurationMs=round(tag_elapsed * 1000),
                    positiveDimensions=len(tag_info.get("positiveTags", [])),
                    negativeHits=tag_info.get("hitCount", 0),
                )
                return item_resp
              except Exception as e:
                operation.mark_failed(e)
                return {
                    "code": 1,
                    "msg":  str(e),
                    "data": {
                        "taskId": task_id, "status": 4, "statusDesc": "任务失败",
                        "elapsedSeconds": round(time.time() - tag_start, 1),
                        "estimatedSeconds": 90,
                        "extra": extra, "errorMsg": str(e),
                    },
                }
              finally:
                record_stage_timing("tag", tag_start, time.time())

    results = list(await asyncio.gather(*[_run_sync_tag_item(item) for item in resume_items]))

    success = sum(1 for r in results if r.get("code") == 0 and r.get("data", {}).get("status") == 3)
    response_body = {
        "total":     len(results),
        "success":   success,
        "failed":    len(results) - success,
        "elapsed_s": round(time.time() - t0, 2),
        "results":   results,
    }
    return JSONResponse(content=response_body)
