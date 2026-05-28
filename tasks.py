"""
异步任务基础设施：线程池、任务状态机、解析+打标流水线。

其他模块（路由层）通过 import 共享此模块的全局状态：
  executor        - 全局线程池，整个进程唯一
  _task_store     - 内存任务字典，服务重启后丢失
  _task_lock      - 保护 _task_store 的并发读写
  _parse_semaphore- asyncio 层限流，防止线程池队列积压
  _cfg_lock       - 保护 tag_config_runtime.json 的并发读写
"""
import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import MAX_WORKERS, TASK_TTL_MINUTES, logger, wrap_for_executor, set_log_context
from parser import _llm_call, _parse_with_logging, _save_result
from postprocess import _conform_to_template
from tag.tagger import run_tagging

# ── 全局线程池 ────────────────────────────────────────────────────
# thread_name_prefix 让线程名出现在日志/traceback，便于定位阻塞问题
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="resume")

# ── 任务状态 ──────────────────────────────────────────────────────
_STATUS_DESC = {0: "待处理", 1: "简历解析中", 2: "标签打标中", 3: "任务完成", 4: "任务失败"}

# 纯内存存储；服务重启后丢失，TTL 到期自动清理
_task_store     : dict[str, "TaskRecord"] = {}
_task_lock       = asyncio.Lock()
_parse_semaphore = asyncio.Semaphore(MAX_WORKERS)
_cfg_lock        = asyncio.Lock()   # 保护 tag_config_runtime.json 的并发读写


@dataclass
class TaskRecord:
    task_id:           str
    status:            int   = 0
    created_at:        float = field(default_factory=time.time)
    completed_at:      Optional[float] = None
    result:            Optional[dict]  = None
    error_msg:         Optional[str]   = None
    estimated_seconds: int  = 90
    extra:             Optional[str]   = None  # 透传调用方业务标识（如投递 ID），结果查询时原样返回


# ── 任务状态工具 ──────────────────────────────────────────────────

async def _set_task_field(task_id: str, **kwargs):
    async with _task_lock:
        rec = _task_store.get(task_id)
        if rec:
            for k, v in kwargs.items():
                setattr(rec, k, v)


def _cleanup_task_store():
    cutoff  = time.time() - TASK_TTL_MINUTES * 60
    expired = [tid for tid, t in list(_task_store.items()) if t.created_at < cutoff]
    for tid in expired:
        _task_store.pop(tid, None)
    if expired:
        logger.info(f"task_store 清理 {len(expired)} 条过期任务")


async def _periodic_cleanup():
    while True:
        await asyncio.sleep(60)
        _cleanup_task_store()


# ── 打标结果日志 ──────────────────────────────────────────────────

def _log_tag_info(task_id: str, filename: str, tag_info: dict,
                  resume_parse_time: str, tag_elapsed: float, total_elapsed: float):
    SEP           = "─" * 60
    positive_tags = tag_info.get("positiveTags", [])
    hit_tags      = tag_info.get("hitTags", [])
    severity_icon = {"low": "[低]", "medium": "[中]", "high": "[高]"}

    logger.info(f"┌{SEP}")
    logger.info(f"│ 打标结果  文件: {filename}  taskId: {task_id}")
    logger.info(f"│ 简历解析: {resume_parse_time}  打标耗时: {tag_elapsed:.1f}s  全程: {total_elapsed:.1f}s")
    logger.info(f"├{'─'*20} 正向标签({len(positive_tags)}) {'─'*16}")
    for tag in positive_tags:
        dim     = tag.get("dimension", "")
        arr_key = f"{dim}ValueArray"
        val     = ", ".join(tag[arr_key]) if arr_key in tag and tag[arr_key] else tag.get("value", "")
        if not isinstance(val, str):
            val = ", ".join(val)
        conf    = f"  置信度={tag['confidence']:.2f}" if tag.get("confidence") is not None else ""
        logger.info(f"│  [{dim:12s}] {val}{conf}")
    logger.info(f"│  整体置信度: {tag_info.get('confidence', 0):.2f}")

    total_hits = sum(len(g.get("hits", [])) for g in hit_tags)
    logger.info(f"├{'─'*20} 风险标签({total_hits}) {'─'*17}")
    if hit_tags:
        for group in hit_tags:
            for hit in group.get("hits", []):
                icon = severity_icon.get(hit["severity"], "[?]")
                logger.info(f"│  {icon} [{group['category']}] {hit['tag']}")
                logger.info(f"│     证据: {hit['matchedContent'][:100]}")
        logger.info(f"│  合计扣分: {tag_info.get('deductionScore', 0)}")
    else:
        logger.info("│  无风险标签命中")
    logger.info(f"│  标签打标时间: {tag_info.get('tagParseTime', '')}")
    logger.info(f"└{SEP}")


# ── 解析+打标流水线 ───────────────────────────────────────────────

async def _run_parse_and_tag(task_id: str, source, filename: str, job_info: dict):
    """
    两阶段流水线：① PDF → 简历 JSON（受 _parse_semaphore 限流）② 简历 JSON → 打标结果。
    打标阶段不占 semaphore，允许解析与其他任务的打标并发执行，提高吞吐量。
    wrap_for_executor 确保 ContextVar（request_id/filename 等）传播到线程池子线程。
    """
    loop           = asyncio.get_running_loop()
    pipeline_start = time.time()
    try:
        # 阶段一：PDF 渲染 + LLM 解析（CPU/IO 密集，限流防止线程池积压）
        # set_log_context 必须在 wrap_for_executor 之前调用，使 copy_context() 能捕获正确的 ContextVar 快照
        set_log_context(request_id=task_id[:8], business_type="简历解析", filename=filename)
        async with _parse_semaphore:
            await _set_task_field(task_id, status=1)
            resume_start = time.time()
            resume       = await loop.run_in_executor(
                executor, wrap_for_executor(_parse_with_logging), source, filename
            )
        resume_parse_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"[{task_id}] 阶段一完成，耗时 {time.time() - resume_start:.1f}s")

        # 阶段二：正向标签 + 风险标签打标（不占 semaphore）
        set_log_context(request_id=task_id[:8], business_type="标签打标", filename=filename)
        await _set_task_field(task_id, status=2)
        tag_start = time.time()
        tag_info  = await loop.run_in_executor(
            executor, wrap_for_executor(lambda: run_tagging(resume, job_info, llm_call_fn=_llm_call))
        )
        tag_elapsed   = time.time() - tag_start
        total_elapsed = time.time() - pipeline_start

        _log_tag_info(task_id, filename, tag_info, resume_parse_time, tag_elapsed, total_elapsed)

        result = _conform_to_template(task_id, resume, tag_info, resume_parse_time)

        async with _task_lock:
            _rec = _task_store.get(task_id)
        _extra = _rec.extra if _rec else None

        completed_at      = time.time()
        full_api_response = {
            "code": 0, "msg": "success",
            "data": {
                "taskId":           task_id,
                "status":           3,
                "statusDesc":       "任务完成",
                "elapsedSeconds":   round(completed_at - pipeline_start, 1),
                "estimatedSeconds": 90,
                "extra":            _extra,
                "result":           result,
            },
        }
        SEP = "─" * 60
        logger.debug(f"┌{SEP}")
        logger.debug(f"│ 完整响应  文件: {filename}  taskId: {task_id}")
        for line in json.dumps(full_api_response, ensure_ascii=False, indent=2).splitlines():
            logger.debug(f"│  {line}")
        logger.debug(f"└{SEP}")

        _save_result(task_id, filename, result)
        await _set_task_field(task_id, status=3, result=result, completed_at=completed_at)

    except Exception as e:
        logger.error(f"[{task_id}] 任务失败: {e}")
        await _set_task_field(task_id, status=4, error_msg=str(e), completed_at=time.time())
