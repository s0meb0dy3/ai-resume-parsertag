"""
异步多格式视觉解析和打标接口（主要对外接口）。

  /api/resume/async/parse          - 单文件上传，立即返回 taskId
  /api/resume/async/result         - 轮询任务状态与结果
  /api/resume/async/batch          - 批量 URL 提交，立即返回 taskId 列表
  /api/resume/async/batch/parse    - 批量 URL 提交，仅异步解析，立即返回 taskId 列表
  /api/resume/async/batch/tag      - 批量提交解析结果，仅异步打标，立即返回 taskId 列表
  /api/resume/async/batch/multiform- 混合批量（文件上传 + URL），立即返回 taskId 列表
  /api/tasks                       - 运维/调试：列出内存中全部任务
"""
import asyncio
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

from config import (
    ASYNC_MODEL_NAME,
    MAX_FILE_SIZE_MB,
    MAX_WORKERS,
    TASK_TTL_MINUTES,
    log_context,
    logger,
    set_log_context,
    wrap_for_executor,
)
from utils.file_utils import get_document_source_from_content, normalize_filename
from api.response_schemas import (
    AsyncBatchSubmissionResponse,
    BusinessErrorResponse,
    TaskListResponse,
    TaskStatusResponse,
    TaskSubmissionResponse,
)
from api.schemas import AsyncBatchRequest, AsyncTagBatchRequest
from core.document_text import detect_document_type
from core.tasks import (
    TaskRecord,
    _STATUS_DESC,
    run_async_parse_task,
    run_async_tag_task,
    run_parse_and_tag_task,
    _task_lock,
    _task_store,
    executor,
)
from logger.operation import log_async_operation, log_operation

router = APIRouter(tags=["异步简历"])
_ASYNC_ALLOWED_EXTENSIONS = {".pdf", ".docx", ".jpg", ".jpeg", ".png"}
_ASYNC_SUPPORTED_FORMATS = "PDF、DOCX、JPG、JPEG、PNG"


def validate_async_document(source, filename: str):
    """校验异步视觉入口的扩展名、真实内容和二者一致性。

    Args:
        source: 文档路径、字节数据或文件对象。
        filename (str): 调用方声明的原始文件名。

    Returns:
        DocumentType: `pdf`、`docx` 或 `image`。

    Raises:
        ValueError: 扩展名不受支持或文件真实内容无效时抛出。
    """
    display_filename = normalize_filename(filename or "resume.pdf")
    suffix = Path(display_filename).suffix.lower()
    if suffix not in _ASYNC_ALLOWED_EXTENSIONS:
        raise ValueError(f"异步解析仅支持 {_ASYNC_SUPPORTED_FORMATS}")
    return detect_document_type(source, display_filename)


def get_async_document_source(content_items):
    """获取并校验异步视觉解析使用的多格式文档来源。

    Args:
        content_items: OpenAI 兼容消息中的单个内容条目或条目列表。

    Returns:
        tuple[Any | None, str | None]: 文档来源和用于日志展示的文件名。

    Raises:
        ValueError: 文件名、真实内容或二者对应关系无效时抛出。
    """
    items = content_items if isinstance(content_items, list) else [content_items]
    for content in items:
        if isinstance(content, dict) and content.get("type", "text") != "text":
            filename = normalize_filename(
                (content.get("file") or {}).get("name", "resume.pdf")
            )
            if Path(filename).suffix.lower() not in _ASYNC_ALLOWED_EXTENSIONS:
                raise ValueError(f"异步解析仅支持 {_ASYNC_SUPPORTED_FORMATS}")
            break
    source, filename = get_document_source_from_content(content_items)
    if source is not None:
        validate_async_document(source, filename or "resume.pdf")
    return source, filename


def get_async_pdf_source(content_items):
    """通过旧函数名调用通用异步文档来源校验。

    Args:
        content_items: OpenAI 兼容消息中的单个内容条目或条目列表。

    Returns:
        tuple[Any | None, str | None]: 受支持文档来源及展示文件名。

    Note:
        该名称仅用于兼容旧内部调用；新代码应使用
        ``get_async_document_source``。
    """
    return get_async_document_source(content_items)


@router.post(
    "/api/resume/async/parse",
    response_model=TaskSubmissionResponse | BusinessErrorResponse,
)
@log_async_operation("异步解析与打标")
async def async_parse_resume(
    file:              UploadFile    = File(...),
    requestId:         Optional[str] = Form(None),
    jobTitle:          Optional[str] = Form(None),
    requiredSkills:    Optional[str] = Form(None),
    jobDescription:    Optional[str] = Form(None),
    jobSensitiveWords: Optional[str] = Form(None),
):
    """提交单个多格式文档的异步视觉解析和打标任务。

    requestId 幂等保护：同一 requestId 重复提交时直接返回已有任务状态，不重复处理。
    任务提交后立即返回 taskId，通过 GET /api/resume/async/result 轮询结果。

    Args:
        file (UploadFile): 上传的 PDF、DOCX、JPG、JPEG 或 PNG 文件。
        requestId (str | None): 调用方幂等标识；重复值复用内存任务。
        jobTitle (str | None): 目标岗位名称。
        requiredSkills (str | None): 逗号分隔的岗位必要技能。
        jobDescription (str | None): 岗位描述。
        jobSensitiveWords (str | None): 风险检测使用的敏感词。

    Returns:
        JSONResponse: 任务标识和初始状态，或文档格式/大小错误。
    """
    filename = normalize_filename(file.filename or "resume.pdf")
    set_log_context(request_id=requestId or uuid.uuid4().hex[:8], business_type="异步解析与打标",
                    filename=filename)

    if requestId:
        # 幂等检查：requestId 已存在则复用，避免调用方重试时重复消耗 LLM 资源
        async with _task_lock:
            for rec in _task_store.values():
                if getattr(rec, "request_id", None) == requestId:
                    return JSONResponse(content={"code": 0, "msg": "success",
                        "data": {"taskId": rec.task_id, "status": rec.status,
                                 "estimatedSeconds": rec.estimated_seconds}})

    suffix = Path(filename).suffix.lower()
    if suffix not in _ASYNC_ALLOWED_EXTENSIONS:
        return JSONResponse(content={"code": 1001, "msg": f"文件格式不支持: {suffix}"})
    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
        return JSONResponse(content={"code": 1002, "msg": f"文件大小超限（最大 {MAX_FILE_SIZE_MB}MB）"})
    try:
        validate_async_document(file_bytes, filename)
    except Exception as exc:
        return JSONResponse(content={"code": 1001, "msg": f"文档内容校验失败: {exc}"})

    task_id = str(uuid.uuid4())
    set_log_context(task_id=task_id)
    rec     = TaskRecord(task_id=task_id)
    if requestId:
        # TaskRecord 是 dataclass，不含 request_id 字段；动态挂载避免为低频幂等场景污染数据类定义
        rec.request_id = requestId  # type: ignore[attr-defined]
    async with _task_lock:
        _task_store[task_id] = rec

    job_info = {
        "jobTitle":          jobTitle or "",
        "requiredSkills":    requiredSkills or "",
        "jobDescription":    jobDescription or "",
        "jobSensitiveWords": jobSensitiveWords or "",
    }
    asyncio.create_task(run_parse_and_tag_task(task_id, file_bytes, filename, job_info, ASYNC_MODEL_NAME))
    logger.info("[任务入队] source=upload")
    return JSONResponse(content={"code": 0, "msg": "success",
                                  "data": {"taskId": task_id, "status": 0, "estimatedSeconds": 90}})


@router.get("/api/resume/async/result", response_model=TaskStatusResponse)
async def get_async_result(taskId: str):
    """轮询单个内存异步任务的状态和结果。

    status 含义：0=待处理 1=简历解析中 2=标签打标中 3=任务完成 4=任务失败
    status=3 时响应体包含 result 字段（完整简历+打标 JSON）。
    status=4 时包含 errorMsg 字段。
    taskId 不存在或 TTL 到期后返回 code=2001。TTL 默认 120 分钟，可通过
    ``TASK_TTL_MINUTES`` 调整，并从任务创建时间开始计算。
    建议轮询间隔：3~5s，estimatedSeconds 字段提供参考等待时长。

    Args:
        taskId (str): 提交接口返回的任务 UUID。

    Returns:
        JSONResponse: 当前状态和耗时；终态时包含 `result` 或 `errorMsg`。
    """
    set_log_context(
        request_id=uuid.uuid4().hex[:8],
        business_type="轮询异步解析或打标结果",
        filename="-",
    )
    with log_operation(
        "轮询异步解析或打标结果",
        taskId=taskId,
        level="DEBUG",
        failure_level="WARNING",
    ) as operation:
        async with _task_lock:
            rec = _task_store.get(taskId)
        if rec is None:
            operation.add(found=False)
            body = {
                "code": 2001,
                "msg": (
                    "taskId 不存在或已过期"
                    f"（TTL {TASK_TTL_MINUTES}分钟）"
                ),
                "data": None,
            }
            return JSONResponse(content=body)

        end_at = rec.completed_at if rec.status in (3, 4) and rec.completed_at else time.time()
        elapsed = round(end_at - rec.created_at, 1)
        data: dict = {
            "taskId": taskId, "status": rec.status,
            "statusDesc": _STATUS_DESC.get(rec.status, "未知"),
            "elapsedSeconds": elapsed, "estimatedSeconds": rec.estimated_seconds, "extra": rec.extra,
        }
        if rec.status == 3:
            data["result"] = rec.result
            body = {"code": 0, "msg": "success", "data": data}
        else:
            if rec.status == 4:
                data["errorMsg"] = rec.error_msg
            body = {"code": 0, "msg": "success", "data": data}
        operation.add(
            found=True,
            status=rec.status,
            taskElapsedMs=round(elapsed * 1000),
        )
        return JSONResponse(content=body)


@router.post(
    "/api/resume/async/batch",
    response_model=AsyncBatchSubmissionResponse | BusinessErrorResponse,
)
@log_async_operation("异步解析与打标")
async def async_batch_parse(request: AsyncBatchRequest):
    """批量提交 URL 多格式文档的异步视觉解析和打标任务。

    响应体包含每份简历的 taskId（下载失败时 code≠0 且 taskId=null），
    通过 GET /api/resume/async/result?taskId=xxx 逐个轮询结果。

    设计约束：
      - 仅支持 type=url，不支持 base64/file（避免大文件占用请求内存）
      - 单次上限 20 份；超出时 Pydantic 返回 422
      - 文件下载在提交阶段同步完成（入队前校验可达性），失败的文件不入队
      - extra 字段透传：调用方可用投递 ID 等业务 key 与结果关联，无需自行维护映射表

    Args:
        request (AsyncBatchRequest): 1–20 个 URL 简历和可选岗位上下文。

    Returns:
        JSONResponse: 按输入顺序返回每份简历的任务标识或下载错误。
    """
    job_info = request.to_job_info()
    loop     = asyncio.get_running_loop()
    tasks    = []
    for item in request.resumes:
        filename = normalize_filename(item.filename)
        extra    = item.effective_extra
        task_id = str(uuid.uuid4())
        with log_context(task_id=task_id, filename=filename):
            try:
                source, fn = await loop.run_in_executor(
                    executor,
                    wrap_for_executor(get_async_document_source),
                    [item.model_dump()],
                )
            except Exception as e:
                tasks.append({"filename": filename, "extra": extra, "code": 1001,
                              "msg": f"文件下载失败: {str(e)}", "taskId": None})
                continue
            if source is None:
                tasks.append({"filename": filename, "extra": extra, "code": 1001,
                              "msg": "无法下载简历文件，请检查 URL 是否可访问", "taskId": None})
                continue
            async with _task_lock:
                _task_store[task_id] = TaskRecord(task_id=task_id, extra=extra)
            asyncio.create_task(run_parse_and_tag_task(task_id, source, fn or filename, job_info, ASYNC_MODEL_NAME))
            logger.info("[任务入队] source=batch")
            tasks.append({"filename": fn or filename, "extra": extra, "code": 0,
                          "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    submitted = sum(1 for t in tasks if t["code"] == 0)
    return JSONResponse(content={"code": 0, "msg": "success",
                                  "total": len(request.resumes), "submitted": submitted,
                                  "failed": len(request.resumes) - submitted,
                                  "estimatedSeconds": 90, "tasks": tasks})


@router.post(
    "/api/resume/async/batch/parse",
    response_model=AsyncBatchSubmissionResponse | BusinessErrorResponse,
)
@log_async_operation("异步解析")
async def async_batch_parse_only(request: AsyncBatchRequest):
    """批量提交仅解析 URL 多格式文档的异步视觉任务。

    请求体格式与 /api/resume/async/batch 完全一致（resumes 数组，每项 type=url）。
    结果通过 GET /api/resume/async/result 轮询；status=3 时 result 包含：
      filename / extra / resumeParseFinishTime / resume（解析后的结构化简历 JSON）。
    result 可直接作为 /api/resume/async/batch/tag 的 resumes 数组元素输入。

    Args:
        request (AsyncBatchRequest): 1–20 个 URL 简历；岗位上下文不参与仅解析。

    Returns:
        JSONResponse: 按输入顺序返回仅解析任务标识或下载错误。
    """
    loop  = asyncio.get_running_loop()
    tasks = []
    for item in request.resumes:
        filename = normalize_filename(item.filename)
        extra    = item.effective_extra
        task_id = str(uuid.uuid4())
        with log_context(task_id=task_id, filename=filename):
            try:
                source, fn = await loop.run_in_executor(
                    executor,
                    wrap_for_executor(get_async_document_source),
                    [item.model_dump()],
                )
            except Exception as e:
                tasks.append({"filename": filename, "extra": extra, "code": 1001,
                              "msg": f"文件下载失败: {str(e)}", "taskId": None})
                continue
            if source is None:
                tasks.append({"filename": filename, "extra": extra, "code": 1001,
                              "msg": "无法下载简历文件，请检查 URL 是否可访问", "taskId": None})
                continue
            async with _task_lock:
                _task_store[task_id] = TaskRecord(task_id=task_id, extra=extra)
            asyncio.create_task(run_async_parse_task(task_id, source, fn or filename, ASYNC_MODEL_NAME))
            logger.info("[任务入队] source=batch/parse")
            tasks.append({"filename": fn or filename, "extra": extra, "code": 0,
                          "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    submitted = sum(1 for t in tasks if t["code"] == 0)
    return JSONResponse(content={"code": 0, "msg": "success",
                                  "total": len(request.resumes), "submitted": submitted,
                                  "failed": len(request.resumes) - submitted,
                                  "estimatedSeconds": 60, "tasks": tasks})


@router.post(
    "/api/resume/async/batch/tag",
    response_model=AsyncBatchSubmissionResponse | BusinessErrorResponse,
)
@log_async_operation("异步打标")
async def async_batch_tag_only(request: AsyncTagBatchRequest):
    """批量提交已解析简历的异步仅打标任务。

    resumes 数组的每项结构与 /api/resume/async/batch/parse 结果（status=3 时的 result 字段）一致，
    可直接将解析结果透传为打标输入。
    结果通过 GET /api/resume/async/result 轮询；打标完成后 result 格式与 /api/resume/async/batch 完全一致。
    单次上限 20 份。

    Args:
        request (AsyncTagBatchRequest): 1–20 个已解析简历和可选岗位上下文。

    Returns:
        JSONResponse: 为每个输入分配的打标任务标识和初始状态。
    """
    job_info = request.to_job_info()
    tasks    = []
    for item in request.resumes:
        task_id = str(uuid.uuid4())
        filename = normalize_filename(item.filename)
        with log_context(task_id=task_id, filename=filename):
            async with _task_lock:
                _task_store[task_id] = TaskRecord(task_id=task_id, extra=item.extra)
            asyncio.create_task(run_async_tag_task(
                task_id, item.resume.model_dump(), filename, item.resumeParseFinishTime, job_info, ASYNC_MODEL_NAME
            ))
            logger.info("[任务入队] source=batch/tag")
            tasks.append({"filename": filename, "extra": item.extra, "code": 0,
                          "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    submitted = sum(1 for t in tasks if t["code"] == 0)
    return JSONResponse(content={"code": 0, "msg": "success",
                                  "total": len(request.resumes), "submitted": submitted,
                                  "failed": len(request.resumes) - submitted,
                                  "estimatedSeconds": 30, "tasks": tasks})


@router.post(
    "/api/resume/async/batch/multiform",
    response_model=AsyncBatchSubmissionResponse | BusinessErrorResponse,
)
@log_async_operation("异步解析与打标")
async def async_batch_parse_multiform(
    files:             Optional[List[UploadFile]] = File(None),
    resumes:           Optional[str]  = Form(None),
    jobTitle:          Optional[str]  = Form(None),
    requiredSkills:    Optional[str]  = Form(None),
    jobDescription:    Optional[str]  = Form(None),
    jobSensitiveWords: Optional[str]  = Form(None),
):
    """混合提交上传和 URL 多格式文档的异步视觉解析打标任务。

    请求字段：
      files    - 直接上传的 PDF/DOCX/JPG/JPEG/PNG 文件列表（可多个）
      resumes  - JSON 字符串，格式同 /api/resume/async/batch 的 resumes 数组（type=url）
                 两者可同时传，合并后总数不超过 20 份

    适用场景：调用方部分简历在本地（直传）、部分在对象存储（URL），需一次提交混合处理。
    纯 URL 场景推荐使用 /api/resume/async/batch（JSON Body 更简洁）。

    files 与 resumes 至少提供一个；合并后超过 20 份返回 code=1003。
    结果查询同 /api/resume/async/result，taskId 逐个轮询。

    Args:
        files (list[UploadFile] | None): 直接上传的多格式简历文件列表。
        resumes (str | None): `type=url` 条目数组的 JSON 字符串。
        jobTitle (str | None): 目标岗位名称。
        requiredSkills (str | None): 岗位必要技能。
        jobDescription (str | None): 岗位描述。
        jobSensitiveWords (str | None): 风险检测敏感词。

    Returns:
        JSONResponse: 合并批次的提交统计和每项任务/错误信息。
    """
    if not files and not resumes:
        return JSONResponse(content={"code": 1001, "msg": "files 或 resumes 至少提供一个"})

    job_info = {k: v or "" for k, v in [
        ("jobTitle", jobTitle), ("requiredSkills", requiredSkills),
        ("jobDescription", jobDescription), ("jobSensitiveWords", jobSensitiveWords),
    ]}
    resume_items: list = []
    if resumes:
        try:
            resume_items = json.loads(resumes)
            if not isinstance(resume_items, list):
                return JSONResponse(content={"code": 1001, "msg": "resumes 必须是 JSON 数组"})
        except Exception:
            return JSONResponse(content={"code": 1001, "msg": "resumes 不是合法的 JSON 字符串"})
        for item in resume_items:
            if item.get("type") != "url":
                return JSONResponse(content={"code": 1001,
                                             "msg": f"resumes 仅支持 type=url，不支持: {item.get('type')}"})

    total_count = len(files or []) + len(resume_items)
    if total_count > 20:
        return JSONResponse(content={"code": 1003, "msg": f"合并后共 {total_count} 份，单次最多提交 20 份"})

    loop  = asyncio.get_running_loop()
    tasks = []

    # 先处理直传文件（同步读取内容，校验格式和大小后入队）
    for upload in (files or []):
        filename = normalize_filename(upload.filename or "resume.pdf")
        task_id = str(uuid.uuid4())
        with log_context(task_id=task_id, filename=filename):
            suffix = Path(filename).suffix.lower()
            if suffix not in _ASYNC_ALLOWED_EXTENSIONS:
                tasks.append({"filename": filename, "code": 1001, "msg": f"格式不支持: {suffix}", "taskId": None})
                continue
            file_bytes = await upload.read()
            if len(file_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
                tasks.append({"filename": filename, "code": 1002,
                              "msg": f"文件超过 {MAX_FILE_SIZE_MB}MB", "taskId": None})
                continue
            try:
                validate_async_document(file_bytes, filename)
            except Exception as exc:
                tasks.append({"filename": filename, "code": 1001,
                              "msg": f"文档内容校验失败: {exc}", "taskId": None})
                continue
            async with _task_lock:
                _task_store[task_id] = TaskRecord(task_id=task_id)
            asyncio.create_task(run_parse_and_tag_task(task_id, file_bytes, filename, job_info, ASYNC_MODEL_NAME))
            logger.info("[任务入队] source=batch/multiform/upload")
            tasks.append({"filename": filename, "code": 0, "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    # 再处理 URL 条目（下载同步完成后入队，失败的不入队）
    for item in resume_items:
        file_meta = item.get("file", {})
        filename  = normalize_filename(file_meta.get("name", "resume.pdf"))
        extra     = file_meta.get("extra") or item.get("extra") or None
        task_id = str(uuid.uuid4())
        with log_context(task_id=task_id, filename=filename):
            try:
                source, fn = await loop.run_in_executor(
                    executor,
                    wrap_for_executor(get_async_document_source),
                    [item],
                )
            except Exception as e:
                tasks.append({"filename": filename, "extra": extra, "code": 1001,
                              "msg": f"文件下载失败: {str(e)}", "taskId": None})
                continue
            if source is None:
                tasks.append({"filename": filename, "extra": extra, "code": 1001,
                              "msg": "无法下载简历文件，请检查 URL 是否可访问", "taskId": None})
                continue
            async with _task_lock:
                _task_store[task_id] = TaskRecord(task_id=task_id, extra=extra)
            asyncio.create_task(run_parse_and_tag_task(task_id, source, fn or filename, job_info, ASYNC_MODEL_NAME))
            logger.info("[任务入队] source=batch/multiform/url")
            tasks.append({"filename": fn or filename, "extra": extra, "code": 0,
                          "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    submitted = sum(1 for t in tasks if t["code"] == 0)
    return JSONResponse(content={"code": 0, "msg": "success",
                                  "total": total_count, "submitted": submitted,
                                  "failed": total_count - submitted,
                                  "estimatedSeconds": 90, "tasks": tasks})


@router.get("/api/tasks", response_model=TaskListResponse)
@log_async_operation("查询异步任务列表")
async def list_tasks(status: Optional[int] = None, include_result: bool = False):
    """列出当前进程内存中的异步任务。

    Args:
        status (int | None): 可选任务状态过滤值，范围由 `_STATUS_DESC` 定义。
        include_result (bool): 是否在已完成任务中附带完整简历结果。

    Returns:
        JSONResponse: 按创建时间倒序排列的任务摘要列表。

    Note:
        该接口仅用于运维和调试；`include_result=true` 会返回完整简历。
    """
    # 运维/调试用：列出内存中全部任务，可按 status 过滤；include_result=true 时返回完整结果体（数据量大，慎用）
    async with _task_lock:
        snapshot = list(_task_store.items())
    now   = time.time()
    items = []
    for task_id, rec in sorted(snapshot, key=lambda x: x[1].created_at, reverse=True):
        if status is not None and rec.status != status:
            continue
        item: dict = {
            "taskId": task_id, "status": rec.status,
            "statusDesc": _STATUS_DESC.get(rec.status, "未知"),
            "elapsedSeconds": round(
                ((rec.completed_at if rec.status in (3, 4) and rec.completed_at else now) - rec.created_at),
                1,
            ),
            "createdAt": datetime.fromtimestamp(rec.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            "extra": rec.extra,
        }
        if rec.status == 3 and rec.result:
            c = rec.result.get("candidate", {})
            item["candidateName"]   = c.get("name", "")
            item["resumeParseTime"] = rec.result.get("resumeParseFinishTime", "")
            item["tagParseTime"]    = rec.result.get("tagParseFinishTime", "")
            item["completedAt"]     = (datetime.fromtimestamp(rec.completed_at).strftime("%Y-%m-%d %H:%M:%S")
                                       if rec.completed_at else "")
            if include_result:
                item["result"] = rec.result
        elif rec.status == 4:
            item["errorMsg"] = rec.error_msg
        items.append(item)
    return JSONResponse(content={"code": 0, "msg": "success", "data": {"total": len(items), "tasks": items}})
