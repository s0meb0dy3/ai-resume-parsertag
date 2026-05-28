"""
OpenAI Chat Completions 兼容接口。

model 字段决定行为：
  DeepSeek-OCR      → 仅解析，返回简历 JSON 字符串
  DeepSeek-OCR-JSON → 解析 + 打标；async_mode=true 立即返回 taskId，否则同步等待

批量模式：content 数组含多个文件 item 时自动并发，返回 JSON 数组（每项含 success/result）。
"""
import asyncio
import json
import time
import uuid
from typing import List, Optional, Union

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from config import MAX_WORKERS, logger, set_log_context, wrap_for_executor
from file_utils import get_pdf_source_from_content
from parser import _batch_process, _parse_with_logging
from tasks import TaskRecord, _run_parse_and_tag, _task_lock, _task_store, executor

router = APIRouter()


# ── 请求体模型（驱动 Swagger 文档）────────────────────────────────

class _FileInfo(BaseModel):
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
    role: str = Field(default="user", description="消息角色，固定传 `user`")
    content: Union[str, List[_ContentItem]] = Field(
        description="消息内容：文件列表（List[ContentItem]）或文本字符串。简历解析时固定传 List[ContentItem]"
    )


class ChatCompletionRequest(BaseModel):
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


# ── 路由 ──────────────────────────────────────────────────────────

@router.post("/v1/chat/completions")
async def create_chat_completion(request: ChatCompletionRequest):
    import json_repair as _jr
    set_log_context(request_id=uuid.uuid4().hex[:8], business_type="简历解析")
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
        # 兼容 OpenAI SDK 的连通性探测请求（空 content 或 "### Task" 前缀的测试消息）
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
            return JSONResponse(status_code=200, media_type="application/json; charset=utf-8",
                content=json.dumps({"type": "text", "text": contents}, ensure_ascii=False, indent=2))
        elif "text" != text_json.get("type", "text"):
            contents = [text_json]
        else:
            contents = [{"type": "text", "text": str(contents)}]

    if model == "DeepSeek-OCR":
        source, filename = get_pdf_source_from_content(contents)
        if source is None:
            return JSONResponse(status_code=400, content={"error": "无法从请求中获取 PDF，请检查 content.type 字段"})
        t2 = time.time()
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                executor, wrap_for_executor(_parse_with_logging), source, filename or "unknown.pdf"
            )
        except Exception as e:
            logger.error(f"PDF 解析失败: {e}")
            return JSONResponse(status_code=400, content={"error": f"PDF 解析失败: {str(e)}"})
        generated_text = json.dumps(result, ensure_ascii=False, indent=2)
        logger.info(f"{model} | {len(generated_text)} chars | {time.time() - t2:.2f}s")

    elif model == "DeepSeek-OCR-JSON":
        source, filename = get_pdf_source_from_content(contents)
        if source is None:
            return JSONResponse(status_code=400, content={"error": "无法从请求中获取 PDF，请检查 content.type 字段"})
        task_id  = str(uuid.uuid4())
        job_info = {
            "jobTitle":          request.jobTitle          or "",
            "requiredSkills":    request.requiredSkills    or "",
            "jobDescription":    request.jobDescription    or "",
            "jobSensitiveWords": request.jobSensitiveWords or "",
        }
        rec = TaskRecord(task_id=task_id)
        async with _task_lock:
            _task_store[task_id] = rec

        if request.async_mode:
            asyncio.create_task(_run_parse_and_tag(task_id, source, filename or "resume.pdf", job_info))
            generated_text = json.dumps(
                {"taskId": task_id, "status": 0, "statusDesc": "待处理", "estimatedSeconds": 90},
                ensure_ascii=False,
            )
            logger.info(f"{model} async_mode=true taskId={task_id}")
        else:
            await _run_parse_and_tag(task_id, source, filename or "resume.pdf", job_info)
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
            yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        return StreamingResponse(_stream(content_body), media_type="text/event-stream")
    return JSONResponse(status_code=200, media_type="application/json; charset=utf-8", content=content_body)


@router.post("/v1/batch/resumes")
async def batch_parse_resumes(request: dict):
    """
    同步批量解析（纯解析，无打标），调用方同步等待全部完成后返回。
    适合离线批处理场景；在线场景建议改用 /api/resume/async/batch 异步接口。
    单次上限 50 份；超时受 API_TIMEOUT 控制。
    """
    t0           = time.time()
    resume_items = request.get("resumes", [])
    max_conc     = min(request.get("max_concurrency", MAX_WORKERS), 30)
    if not resume_items:
        return JSONResponse(status_code=400, content={"error": "resumes 数组不能为空"})
    if len(resume_items) > 50:
        return JSONResponse(status_code=400, content={"error": "单次最多处理 50 份简历"})
    results = await asyncio.get_running_loop().run_in_executor(executor, _batch_process, resume_items, max_conc)
    success = sum(1 for r in results if r and r["success"])
    return JSONResponse(content={"total": len(results), "success": success,
                                  "failed": len(results) - success,
                                  "elapsed_s": round(time.time() - t0, 2), "results": results})
