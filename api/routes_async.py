"""
异步简历解析+打标接口（主要对外接口）。

  /api/resume/async/parse          - 单文件上传，立即返回 taskId
  /api/resume/async/result         - 轮询任务状态与结果
  /api/resume/async/batch          - 批量 URL 提交，立即返回 taskId 列表
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

from config import ALLOWED_EXTENSIONS, MAX_FILE_SIZE_MB, MAX_WORKERS, logger, set_log_context
from file_utils import get_pdf_source_from_content
from tasks import (
    TaskRecord,
    _STATUS_DESC,
    _run_parse_and_tag,
    _task_lock,
    _task_store,
    executor,
)

router = APIRouter()


@router.post("/api/resume/async/parse")
async def async_parse_resume(
    file:              UploadFile    = File(...),
    requestId:         Optional[str] = Form(None),
    jobTitle:          Optional[str] = Form(None),
    requiredSkills:    Optional[str] = Form(None),
    jobDescription:    Optional[str] = Form(None),
    jobSensitiveWords: Optional[str] = Form(None),
):
    """
    单文件异步解析+打标（multipart/form-data 上传）。

    requestId 幂等保护：同一 requestId 重复提交时直接返回已有任务状态，不重复处理。
    任务提交后立即返回 taskId，通过 GET /api/resume/async/result 轮询结果。
    """
    set_log_context(request_id=requestId or uuid.uuid4().hex[:8], business_type="异步简历解析",
                    filename=file.filename or "resume.pdf")

    if requestId:
        # 幂等检查：requestId 已存在则复用，避免重试时重复消耗 LLM 资源
        async with _task_lock:
            for rec in _task_store.values():
                if getattr(rec, "request_id", None) == requestId:
                    return JSONResponse(content={"code": 0, "msg": "success",
                        "data": {"taskId": rec.task_id, "status": rec.status,
                                 "estimatedSeconds": rec.estimated_seconds}})

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return JSONResponse(content={"code": 1001, "msg": f"文件格式不支持: {suffix}"})
    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
        return JSONResponse(content={"code": 1002, "msg": f"文件大小超限（最大 {MAX_FILE_SIZE_MB}MB）"})
    if suffix != ".pdf":
        return JSONResponse(content={"code": 1001, "msg": "当前仅支持 PDF 格式"})

    task_id = str(uuid.uuid4())
    rec     = TaskRecord(task_id=task_id)
    if requestId:
        rec.request_id = requestId  # type: ignore[attr-defined]
    async with _task_lock:
        _task_store[task_id] = rec

    job_info = {
        "jobTitle":          jobTitle or "",
        "requiredSkills":    requiredSkills or "",
        "jobDescription":    jobDescription or "",
        "jobSensitiveWords": jobSensitiveWords or "",
    }
    asyncio.create_task(_run_parse_and_tag(task_id, file_bytes, file.filename or "resume.pdf", job_info))
    logger.info(f"[{task_id}] 任务入队 file={file.filename} jobTitle={jobTitle}")
    return JSONResponse(content={"code": 0, "msg": "success",
                                  "data": {"taskId": task_id, "status": 0, "estimatedSeconds": 90}})


@router.get("/api/resume/async/result")
async def get_async_result(taskId: str):
    """
    轮询异步任务结果。

    status 含义：0=待处理 1=简历解析中 2=标签打标中 3=任务完成 4=任务失败
    status=3 时响应体包含 result 字段（完整简历+打标 JSON）。
    status=4 时包含 errorMsg 字段。
    taskId 不存在或 TTL（默认 10min）到期后返回 code=2001，调用方需处理该情况。
    建议轮询间隔：3~5s，estimatedSeconds 字段提供参考等待时长。
    """
    async with _task_lock:
        rec = _task_store.get(taskId)
    if rec is None:
        return JSONResponse(content={"code": 2001, "msg": "taskId 不存在或已过期（TTL 10min）", "data": None})
    elapsed = round(time.time() - rec.created_at, 1)
    data: dict = {
        "taskId": taskId, "status": rec.status,
        "statusDesc": _STATUS_DESC.get(rec.status, "未知"),
        "elapsedSeconds": elapsed, "estimatedSeconds": rec.estimated_seconds, "extra": rec.extra,
    }
    if rec.status == 3:
        data["result"] = rec.result
    elif rec.status == 4:
        data["errorMsg"] = rec.error_msg
    return JSONResponse(content={"code": 0, "msg": "success", "data": data})


@router.post("/api/resume/async/batch")
async def async_batch_parse(request: dict):
    """
    【主要对外接口】批量异步解析+打标（JSON Body，仅支持 URL 方式）。

    请求体格式：
    {
        "resumes": [
            {
                "type": "url",
                "file": {
                    "name": "张三.pdf",          # 文件名，用于日志和结果标识
                    "path": "http://...",        # 可访问的 HTTP/HTTPS 下载链接
                    "extra": "投递ID_12345"       # 可选，调用方业务标识，结果查询时原样返回
                }
            },
            ...
        ],
        "jobTitle": "算法工程师",                 # 可选，岗位名称，影响打标结果
        "requiredSkills": "Python,机器学习",      # 可选，技能要求，逗号分隔
        "jobDescription": "...",                 # 可选，岗位 JD
        "jobSensitiveWords": "竞业,保密协议"      # 可选，风险打标关键词
    }

    响应体包含每份简历的 taskId（下载失败时 code≠0 且 taskId=null），
    通过 GET /api/resume/async/result?taskId=xxx 逐个轮询结果。

    设计约束：
      - 仅支持 type=url，不支持 base64/file（避免大文件占用请求内存）
      - 单次上限 20 份；超量返回 code=1003
      - 文件下载在提交阶段同步完成（入队前校验可达性），失败的文件不入队
      - extra 字段透传：调用方可用投递 ID 等业务 key 与结果关联，无需自行维护映射表
    """
    resumes_raw = request.get("resumes", [])
    if not resumes_raw:
        return JSONResponse(content={"code": 1001, "msg": "resumes 数组不能为空"})
    if not isinstance(resumes_raw, list):
        return JSONResponse(content={"code": 1001, "msg": "resumes 必须是数组"})
    for item in resumes_raw:
        if not isinstance(item, dict) or item.get("type") != "url":
            t = item.get("type") if isinstance(item, dict) else type(item).__name__
            return JSONResponse(content={"code": 1001, "msg": f"resumes 仅支持 type=url，不支持: {t}"})
    if len(resumes_raw) > 20:
        return JSONResponse(content={"code": 1003, "msg": f"共 {len(resumes_raw)} 份，单次最多提交 20 份"})

    job_info = {k: request.get(k, "") or "" for k in ("jobTitle", "requiredSkills", "jobDescription", "jobSensitiveWords")}
    loop     = asyncio.get_running_loop()
    tasks    = []
    for item in resumes_raw:
        file_meta = item.get("file", {})
        filename  = file_meta.get("name", "resume.pdf")
        # extra 优先取 file.extra，其次取顶层 extra；用于调用方业务关联（如投递 ID）
        extra     = file_meta.get("extra") or item.get("extra") or None
        try:
            source, fn = await loop.run_in_executor(executor, get_pdf_source_from_content, [item])
        except Exception as e:
            tasks.append({"filename": filename, "extra": extra, "code": 1001,
                          "msg": f"文件下载失败: {str(e)}", "taskId": None})
            continue
        if source is None:
            tasks.append({"filename": filename, "extra": extra, "code": 1001,
                          "msg": "无法下载 PDF，请检查 URL 是否可访问", "taskId": None})
            continue
        task_id = str(uuid.uuid4())
        async with _task_lock:
            _task_store[task_id] = TaskRecord(task_id=task_id, extra=extra)
        asyncio.create_task(_run_parse_and_tag(task_id, source, fn or filename, job_info))
        logger.info(f"[batch] 任务入队 taskId={task_id} file={fn or filename} extra={extra}")
        tasks.append({"filename": fn or filename, "extra": extra, "code": 0,
                      "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    submitted = sum(1 for t in tasks if t["code"] == 0)
    return JSONResponse(content={"code": 0, "msg": "success",
                                  "total": len(resumes_raw), "submitted": submitted,
                                  "failed": len(resumes_raw) - submitted,
                                  "estimatedSeconds": 90, "tasks": tasks})


@router.post("/api/resume/async/batch/multiform")
async def async_batch_parse_multiform(
    files:             Optional[List[UploadFile]] = File(None),
    resumes:           Optional[str]  = Form(None),
    jobTitle:          Optional[str]  = Form(None),
    requiredSkills:    Optional[str]  = Form(None),
    jobDescription:    Optional[str]  = Form(None),
    jobSensitiveWords: Optional[str]  = Form(None),
):
    """
    混合批量异步解析+打标（multipart/form-data，支持文件上传与 URL 混用）。

    请求字段：
      files    - 直接上传的 PDF 文件列表（multipart file 字段，可多个）
      resumes  - JSON 字符串，格式同 /api/resume/async/batch 的 resumes 数组（type=url）
                 两者可同时传，合并后总数不超过 20 份

    适用场景：调用方部分简历在本地（直传）、部分在对象存储（URL），需一次提交混合处理。
    纯 URL 场景推荐使用 /api/resume/async/batch（JSON Body 更简洁）。

    files 与 resumes 至少提供一个；合并后超过 20 份返回 code=1003。
    结果查询同 /api/resume/async/result，taskId 逐个轮询。
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
        filename = upload.filename or "resume.pdf"
        suffix   = Path(filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS or suffix != ".pdf":
            tasks.append({"filename": filename, "code": 1001, "msg": f"格式不支持: {suffix}", "taskId": None})
            continue
        file_bytes = await upload.read()
        if len(file_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
            tasks.append({"filename": filename, "code": 1002,
                          "msg": f"文件超过 {MAX_FILE_SIZE_MB}MB", "taskId": None})
            continue
        task_id = str(uuid.uuid4())
        async with _task_lock:
            _task_store[task_id] = TaskRecord(task_id=task_id)
        asyncio.create_task(_run_parse_and_tag(task_id, file_bytes, filename, job_info))
        logger.info(f"[batch/multiform] 任务入队 taskId={task_id} file={filename} type=upload")
        tasks.append({"filename": filename, "code": 0, "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    # 再处理 URL 条目（下载同步完成后入队，失败的不入队）
    for item in resume_items:
        file_meta = item.get("file", {})
        filename  = file_meta.get("name", "resume.pdf")
        extra     = file_meta.get("extra") or item.get("extra") or None
        try:
            source, fn = await loop.run_in_executor(executor, get_pdf_source_from_content, [item])
        except Exception as e:
            tasks.append({"filename": filename, "extra": extra, "code": 1001,
                          "msg": f"文件下载失败: {str(e)}", "taskId": None})
            continue
        if source is None:
            tasks.append({"filename": filename, "extra": extra, "code": 1001,
                          "msg": "无法下载 PDF，请检查 URL 是否可访问", "taskId": None})
            continue
        task_id = str(uuid.uuid4())
        async with _task_lock:
            _task_store[task_id] = TaskRecord(task_id=task_id, extra=extra)
        asyncio.create_task(_run_parse_and_tag(task_id, source, fn or filename, job_info))
        logger.info(f"[batch/multiform] 任务入队 taskId={task_id} file={fn or filename} type=url extra={extra}")
        tasks.append({"filename": fn or filename, "extra": extra, "code": 0,
                      "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    submitted = sum(1 for t in tasks if t["code"] == 0)
    return JSONResponse(content={"code": 0, "msg": "success",
                                  "total": total_count, "submitted": submitted,
                                  "failed": total_count - submitted,
                                  "estimatedSeconds": 90, "tasks": tasks})


@router.get("/api/tasks")
async def list_tasks(status: Optional[int] = None, include_result: bool = False):
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
            "elapsedSeconds": round(now - rec.created_at, 1),
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
