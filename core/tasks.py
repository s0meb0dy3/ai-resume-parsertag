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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import (
    MAX_WORKERS, TASK_TTL_MINUTES, logger, wrap_for_executor, log_context,
    set_current_model_name, reset_current_model_name,
)
from core.parser import (
    ParsedResumeDocument,
    _call_llm_with_fallback,
    _save_resume_result,
    parse_async_visual_resume_with_logging,
)
from core.postprocess import _conform_to_template
from tag.tagger import run_tagging
from tag.tag_config import (
    POSITIVE_DIMENSION_DISPLAY_NAMES,
    sort_positive_tag_entries,
)
from logger.langfuse_client import observe, propagate_attributes, update_current_observation, LANGFUSE_TAGS
from logger.operation import log_operation, record_stage_timing
from utils.file_utils import normalize_filename

# 全局线程池
# thread_name_prefix 让线程名出现在日志/traceback，便于定位阻塞问题
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="resume")

# 任务状态
_STATUS_DESC = {0: "待处理", 1: "简历解析中", 2: "标签打标中", 3: "任务完成", 4: "任务失败"}

# 纯内存存储；服务重启后丢失，TTL 到期自动清理
_task_store     : dict[str, "TaskRecord"] = {}
_task_lock       = asyncio.Lock()
# asyncio 层限流：防止 event loop 分发的任务数超过线程池容量，避免大量协程在 run_in_executor 前阻塞等待
_parse_semaphore = asyncio.Semaphore(MAX_WORKERS)
_cfg_lock        = asyncio.Lock()   # 保护 tag_config_runtime.json 的并发读写


@dataclass
class TaskRecord:
    """保存单个异步任务在当前进程内的状态。

    Attributes:
        task_id: 对外返回的任务唯一标识。
        status: 状态码，取值含义见模块级 ``_STATUS_DESC``。
        created_at: 任务创建时的 Unix 时间戳，也是 TTL 清理的判断基准。
        completed_at: 任务进入成功或失败终态的 Unix 时间戳。
        result: 成功任务的解析或打标结果。
        error_msg: 失败任务向查询接口返回的错误信息。
        estimated_seconds: 向调用方展示的预计处理时长。
        extra: 调用方传入并在查询结果中原样返回的业务标识。

    Note:
        记录仅保存在进程内存中，服务重启或任务超过 TTL 后不可恢复。
    """
    task_id:           str
    status:            int   = 0
    created_at:        float = field(default_factory=time.time)
    completed_at:      Optional[float] = None
    result:            Optional[dict]  = None
    error_msg:         Optional[str]   = None
    estimated_seconds: int  = 90
    extra:             Optional[str]   = None  # 透传调用方业务标识（如投递 ID），结果查询时原样返回


# 任务状态工具

async def _set_task_field(task_id: str, **kwargs):
    """在异步锁保护下更新已存在任务的字段。

    Args:
        task_id: 要更新的任务标识。
        **kwargs: ``TaskRecord`` 字段名及其新值。

    Note:
        任务不存在时静默跳过，避免后台任务结束时与 TTL 清理产生竞态异常。
    """
    async with _task_lock:
        rec = _task_store.get(task_id)
        if rec:
            for k, v in kwargs.items():
                setattr(rec, k, v)


def _cleanup_task_store():
    """删除创建时间早于 TTL 截止点的进程内任务。

    Note:
        过期时间按 ``created_at`` 计算，而非 ``completed_at``。运行时间超过 TTL
        的任务同样可能被清理，调用方需结合该约束设置 ``TASK_TTL_MINUTES``。
    """
    cutoff  = time.time() - TASK_TTL_MINUTES * 60
    expired = [tid for tid, t in list(_task_store.items()) if t.created_at < cutoff]
    for tid in expired:
        _task_store.pop(tid, None)
    if expired:
        logger.info(f"task_store 清理 {len(expired)} 条过期任务")


async def _periodic_cleanup():
    """每 60 秒执行一次任务存储清理。

    Note:
        此协程设计为服务生命周期内持续运行，通常由应用启动逻辑创建并在
        应用关闭时统一取消。
    """
    while True:
        await asyncio.sleep(60)
        _cleanup_task_store()


# 打标结果日志

def _log_resume_tag_result(task_id: str, filename: str, tag_info: dict,
                           resume_parse_time: str, tag_elapsed: float, total_elapsed: float):
    """按可读分组格式记录一次打标结果摘要。

    Args:
        task_id: 当前任务标识。
        filename: 原始简历文件名。
        tag_info: ``run_tagging`` 返回的标签结果。
        resume_parse_time: 简历解析完成时间文本。
        tag_elapsed: 标签阶段耗时，单位为秒。
        total_elapsed: 整条处理链路耗时，单位为秒。

    Note:
        该函数只写日志，不修改 ``tag_info`` 或任务状态。
    """
    display_filename = normalize_filename(filename)
    with log_operation(
        "打标结果日志",
        taskId=task_id,
        file=display_filename,
        level="DEBUG",
        emit_start=False,
    ) as operation:
        separator = "─" * 60
        positive_tags = sort_positive_tag_entries(
            tag_info.get("positiveTags", [])
        )
        hit_tags = tag_info.get("hitTags", [])
        severity_icon = {"low": "[低]", "medium": "[中]", "high": "[高]"}

        logger.debug(f"┌{separator}")
        logger.debug(f"│ 打标结果  文件: {display_filename}  taskId: {task_id}")
        logger.debug(f"│ 简历解析: {resume_parse_time}  打标耗时: {tag_elapsed:.1f}s  全程: {total_elapsed:.1f}s")
        logger.debug(f"├{'─'*20} 正向标签({len(positive_tags)}) {'─'*16}")
        for tag in positive_tags:
            dim = tag.get("dimension", "")
            display_name = POSITIVE_DIMENSION_DISPLAY_NAMES.get(dim, dim)
            arr_key = f"{dim}ValueArray"
            val = ", ".join(tag[arr_key]) if arr_key in tag and tag[arr_key] else tag.get("value", "")
            if not isinstance(val, str):
                val = ", ".join(val)
            confidence = f"  置信度={tag['confidence']:.2f}" if tag.get("confidence") is not None else ""
            logger.debug(f"│  [{display_name}] {val}{confidence}")
        logger.debug(f"│  整体置信度: {tag_info.get('confidence', 0):.2f}")

        total_hits = sum(len(group.get("hits", [])) for group in hit_tags)
        logger.debug(f"├{'─'*20} 风险标签({total_hits}) {'─'*17}")
        if hit_tags:
            for group in hit_tags:
                for hit in group.get("hits", []):
                    icon = severity_icon.get(hit["severity"], "[?]")
                    logger.debug(f"│  {icon} [{group['category']}] {hit['tag']}")
                logger.debug(
                    f"│     evidenceCharacters={len(hit.get('matchedContent', ''))}"
                )
            logger.debug(f"│  合计扣分: {tag_info.get('deductionScore', 0)}")
        else:
            logger.debug("│  无风险标签命中")
        logger.debug(f"│  标签打标时间: {tag_info.get('tagParseTime', '')}")
        logger.debug(f"└{separator}")
        operation.add(
            tagDurationMs=round(tag_elapsed * 1000),
            totalDurationMs=round(total_elapsed * 1000),
            positiveDimensions=len(positive_tags),
            negativeHits=total_hits,
            confidence=tag_info.get("confidence"),
        )


# 保留旧私有名称，避免现有日志调用方中断。
_log_tag_info = _log_resume_tag_result


# 解析与打标流水线

def _build_parse_and_tag_summary(
    result: dict, tag_info: dict, total_seconds: float
) -> dict:
    """构造完整响应和方法结束日志共用的流水线摘要。

    Args:
        result (dict): 已归一的结构化简历。
        tag_info (dict): 正向标签和负向命中结果。
        total_seconds (float): 解析与打标全程耗时秒数。

    Returns:
        dict: 全程耗时、候选人姓名、经历数量和标签数量摘要。
    """
    return {
        "totalDurationMs": round(total_seconds * 1000),
        "educations": len(result.get("educations") or []),
        "works": len(result.get("works") or []),
        "projects": len(result.get("projects") or []),
        "positiveDimensions": len(tag_info.get("positiveTags") or []),
        "negativeHits": tag_info.get("hitCount", 0),
    }


@observe()
async def run_parse_and_tag_task(
    task_id: str,
    source,
    filename: str,
    job_info: dict,
    model_name: str | None = None,
    parse_fn=None,
    operation_name: str = "异步解析与打标",
):
    """依次执行简历解析和标签打标，并写入任务终态。

    Args:
        task_id: 当前任务标识。
        source: 异步入口的 PDF、DOCX、JPEG 或 PNG 数据源。
        filename: 原始简历文件名。
        job_info: 打标规则所需的岗位及业务配置。
        model_name: 本次任务指定的模型名称；为空时使用全局默认模型。
        parse_fn: 可选解析函数；为空时使用异步多格式视觉解析。
        operation_name: 日志中展示的流水线业务名称。

    Note:
        文档解析受 ``_parse_semaphore`` 限流，打标阶段不占用该信号量。
        ``wrap_for_executor`` 负责把日志上下文传播到线程池。所有异常均转换为
        失败任务状态，不再向创建该后台任务的调用方抛出。
    """
    filename = normalize_filename(filename)
    loop           = asyncio.get_running_loop()
    pipeline_start = time.time()
    with log_context(
        task_id=task_id,
        business_type=operation_name,
        filename=filename,
    ), log_operation(operation_name, taskId=task_id, file=filename) as operation:
      with propagate_attributes(tags=LANGFUSE_TAGS, session_id=task_id):
        token = set_current_model_name(model_name) if model_name else None
        try:
            # 异步主链保持纯视觉解析；同步组合接口显式传入文本优先解析函数。
            # set_log_context 必须在 wrap_for_executor 之前调用，使 copy_context() 能捕获正确的 ContextVar 快照
            async with _parse_semaphore:
                await _set_task_field(task_id, status=1)
                resume_start = time.time()
                active_parse_fn = (
                    parse_fn or parse_async_visual_resume_with_logging
                )
                try:
                    parse_result = await loop.run_in_executor(
                        executor, wrap_for_executor(active_parse_fn), source, filename
                    )
                finally:
                    resume_end = time.time()
                    record_stage_timing("parse", resume_start, resume_end)
                if isinstance(parse_result, ParsedResumeDocument):
                    resume = parse_result.resume
                    skill_evidence_text = parse_result.skill_evidence_text
                else:
                    resume = parse_result
                    skill_evidence_text = ""
            resume_elapsed = resume_end - resume_start
            resume_parse_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.info(
                f"[解析阶段完成] durationMs={round(resume_elapsed * 1000)}"
            )

            # 阶段二：正向标签 + 风险标签打标（不占 semaphore）
            await _set_task_field(task_id, status=2)
            tag_start = time.time()
            try:
                tag_info = await loop.run_in_executor(
                    executor,
                    wrap_for_executor(
                        lambda: run_tagging(
                            resume,
                            job_info,
                            llm_call_fn=_call_llm_with_fallback,
                            skill_evidence_text=skill_evidence_text,
                        )
                    ),
                )
            finally:
                tag_end = time.time()
                record_stage_timing("tag", tag_start, tag_end)
            tag_elapsed   = tag_end - tag_start
            total_elapsed = time.time() - pipeline_start

            _log_resume_tag_result(
                task_id, filename, tag_info, resume_parse_time, tag_elapsed, total_elapsed
            )

            result = _conform_to_template(task_id, resume, tag_info, resume_parse_time)

            async with _task_lock:
                _rec = _task_store.get(task_id)
            _extra = _rec.extra if _rec else None

            completed_at      = time.time()
            total_seconds = completed_at - pipeline_start
            response_summary = _build_parse_and_tag_summary(
                result, tag_info, total_seconds
            )
            summary_text = " ".join(
                f"{key}={value}" for key, value in response_summary.items()
            )
            logger.info(
                f"[任务结果就绪] {summary_text}"
            )

            result_file = _save_resume_result(task_id, filename, result)
            update_current_observation(output={
                "taskId":   task_id,
                "filename": filename,
                "status":   "completed",
                "elapsed":  round(completed_at - pipeline_start, 1),
                "confidence": result.get("tagInfo", {}).get("confidence"),
            })
            await _set_task_field(task_id, status=3, result=result, completed_at=completed_at)
            operation.add(
                parseDurationMs=round(resume_elapsed * 1000),
                tagDurationMs=round(tag_elapsed * 1000),
                skillEvidenceCharacters=len(skill_evidence_text),
                **response_summary,
                resultFile=result_file,
            )

        except Exception as e:
            update_current_observation(output={"taskId": task_id, "status": "failed", "error": str(e)})
            await _set_task_field(task_id, status=4, error_msg=str(e), completed_at=time.time())
            operation.mark_failed(e)
        finally:
            if token is not None:
                reset_current_model_name(token)


@observe()
async def run_async_parse_task(task_id: str, source, filename: str, model_name: str | None = None):
    """单独执行多格式视觉解析，并保存可供打标接口使用的结果。

    Args:
        task_id: 当前任务标识。
        source: 已由路由层校验的 PDF、DOCX、JPEG 或 PNG 数据源。
        filename: 原始简历文件名。
        model_name: 本次任务指定的模型名称；为空时使用全局默认模型。

    Note:
        解析过程受全局信号量限流。所有异常均记录到任务状态，不向后台任务
        调度方重新抛出。
    """
    filename = normalize_filename(filename)
    loop           = asyncio.get_running_loop()
    pipeline_start = time.time()
    with log_context(task_id=task_id, business_type="异步解析", filename=filename), \
            log_operation("异步解析", taskId=task_id, file=filename) as operation, \
            propagate_attributes(tags=LANGFUSE_TAGS, session_id=task_id):
        token = set_current_model_name(model_name) if model_name else None
        try:
            async with _parse_semaphore:
                await _set_task_field(task_id, status=1)
                resume = await loop.run_in_executor(
                    executor,
                    wrap_for_executor(parse_async_visual_resume_with_logging),
                    source,
                    filename,
                )
            resume_parse_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            completed_at      = time.time()

            async with _task_lock:
                rec = _task_store.get(task_id)
            extra = rec.extra if rec else None
            result = {
                "filename": filename,
                "extra": extra,
                "resumeParseFinishTime": resume_parse_time,
                "resume": resume,
            }
            update_current_observation(output={
                "taskId": task_id,
                "filename": filename,
                "status": "parsed",
                "elapsed": round(completed_at - pipeline_start, 1),
            })
            await _set_task_field(task_id, status=3, result=result, completed_at=completed_at)
            operation.add(
                totalDurationMs=round((completed_at - pipeline_start) * 1000),
                educations=len(resume.get("educations", [])),
                works=len(resume.get("works", [])),
                projects=len(resume.get("projects", [])),
            )
        except Exception as e:
            update_current_observation(output={"taskId": task_id, "status": "failed", "error": str(e)})
            await _set_task_field(task_id, status=4, error_msg=str(e), completed_at=time.time())
            operation.mark_failed(e)
        finally:
            if token is not None:
                reset_current_model_name(token)


@observe()
async def run_async_tag_task(
    task_id: str,
    resume: dict,
    filename: str,
    resume_parse_time: str,
    job_info: dict,
    model_name: str | None = None,
):
    """对已解析的简历执行标签打标，并保存统一格式结果。

    Args:
        task_id: 当前任务标识。
        resume: 已解析的简历 JSON 对象。
        filename: 原始简历文件名。
        resume_parse_time: 上游简历解析完成时间文本。
        job_info: 打标规则所需的岗位及业务配置。
        model_name: 本次任务指定的模型名称；为空时使用全局默认模型。

    Note:
        此流程不重新解析 PDF，也不占用解析信号量。所有异常均记录到任务
        状态，不向后台任务调度方重新抛出。
    """
    filename = normalize_filename(filename)
    loop      = asyncio.get_running_loop()
    with log_context(task_id=task_id, business_type="异步打标", filename=filename), \
            log_operation("异步打标", taskId=task_id, file=filename) as operation, \
            propagate_attributes(tags=LANGFUSE_TAGS, session_id=task_id):
        token = set_current_model_name(model_name) if model_name else None
        try:
            await _set_task_field(task_id, status=2)
            tag_start = time.time()
            tag_info = await loop.run_in_executor(
                executor,
                wrap_for_executor(
                    lambda: run_tagging(resume, job_info, llm_call_fn=_call_llm_with_fallback)
                ),
            )
            completed_at = time.time()
            tag_elapsed  = completed_at - tag_start
            _log_resume_tag_result(
                task_id, filename, tag_info, resume_parse_time, tag_elapsed, tag_elapsed
            )
            result = _conform_to_template(task_id, resume, tag_info, resume_parse_time)

            result_file = _save_resume_result(task_id, filename, result)
            update_current_observation(output={
                "taskId": task_id,
                "filename": filename,
                "status": "completed",
                "elapsed": round(tag_elapsed, 1),
                "confidence": result.get("tagInfo", {}).get("confidence"),
            })
            await _set_task_field(task_id, status=3, result=result, completed_at=completed_at)
            operation.add(
                tagDurationMs=round(tag_elapsed * 1000),
                positiveDimensions=len(tag_info.get("positiveTags", [])),
                negativeHits=tag_info.get("hitCount", 0),
                resultFile=result_file,
            )
        except Exception as e:
            update_current_observation(output={"taskId": task_id, "status": "failed", "error": str(e)})
            await _set_task_field(task_id, status=4, error_msg=str(e), completed_at=time.time())
            operation.mark_failed(e)
        finally:
            if token is not None:
                reset_current_model_name(token)


# 保留旧私有名称，避免现有内部导入中断。
_run_parse_and_tag = run_parse_and_tag_task
_run_parse_only = run_async_parse_task
_run_tag_only = run_async_tag_task
