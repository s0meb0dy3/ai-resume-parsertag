"""提供业务方法开始、完成和失败边界日志。

每次调用首行记录开始时间，末行记录结束时间、耗时、状态和业务摘要。时间精确到
秒，耗时使用单调时钟计算，避免系统时间调整影响持续时间。
"""
from __future__ import annotations

import inspect
import json
import logging
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .logger_manager import (
    _UNSET,
    log_context,
    logger,
    request_id_var,
)


LOG_FILENAME_CONTEXT_HEADER = "x-aihr-internal-log-filename"


def _format_fields(fields: dict[str, Any]) -> str:
    """把非空日志字段按输入顺序格式化为单行键值对。

    Args:
        fields (dict[str, Any]): 需要追加到方法边界日志的业务字段。

    Returns:
        str: 使用空格分隔的 `key=value` 文本。
    """
    return " ".join(
        f"{key}={value}"
        for key, value in fields.items()
        if value is not None and key not in {"file", "function", "taskId"}
    )


def _log_level(value: int | str) -> int:
    """把日志级别名称或整数归一为 logging 级别值。"""
    if isinstance(value, int):
        return value
    return logging._nameToLevel.get(str(value).upper(), logging.INFO)


def _format_timestamp(timestamp: float) -> str:
    """把 Unix 时间戳格式化为精确到秒的本地时间。"""
    return datetime.fromtimestamp(timestamp).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _summarize_filenames(filenames: list[str]) -> str:
    """把请求内文件名压缩为适合日志表头展示的文本。"""
    unique = list(dict.fromkeys(name.strip() for name in filenames if name.strip()))
    if not unique:
        return "-"
    if len(unique) == 1:
        return unique[0]
    return f"{unique[0]} 等{len(unique)}个文件"


def _request_filename_context(request: Any, kwargs: dict[str, Any]) -> str:
    """从同步批次模型或上传文件参数中提取请求级文件上下文。"""
    filenames: list[str] = []
    resumes = getattr(request, "resumes", None)
    if isinstance(resumes, list):
        for item in resumes:
            if isinstance(item, dict):
                file_meta = item.get("file")
                if isinstance(file_meta, dict) and file_meta.get("name"):
                    filenames.append(str(file_meta["name"]))
                elif item.get("filename"):
                    filenames.append(str(item["filename"]))
            else:
                filename = getattr(item, "filename", None)
                if filename:
                    filenames.append(str(filename))

    upload = kwargs.get("file")
    upload_filename = getattr(upload, "filename", None)
    if upload_filename:
        filenames.append(str(upload_filename))
    return _summarize_filenames(filenames)


class ExecutionMetrics:
    """线程安全地聚合一次接口调用中的阶段时间和 LLM token 用量。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, list[tuple[float, float]]] = {}
        self._models_attempted: list[str] = []
        self._models_succeeded: list[str] = []
        self._llm_attempts = 0
        self._llm_successful_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._total_tokens = 0
        self._cached_input_tokens = 0

    def record_stage(self, stage: str, started_at: float, ended_at: float) -> None:
        """记录单项解析或打标签阶段的真实起止时间。"""
        with self._lock:
            self._stages.setdefault(stage, []).append((started_at, ended_at))

    def record_model_attempt(self, model_name: str) -> None:
        """记录一次实际发往模型服务的 HTTP 调用。"""
        with self._lock:
            self._llm_attempts += 1
            self._models_attempted.append(model_name)

    def record_llm_usage(
        self,
        model_name: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int = 0,
        cached_input_tokens: int = 0,
    ) -> None:
        """累计一次成功模型响应返回的 usage 数据。"""
        with self._lock:
            self._llm_successful_calls += 1
            self._models_succeeded.append(model_name)
            self._input_tokens += input_tokens
            self._output_tokens += output_tokens
            self._total_tokens += total_tokens
            self._cached_input_tokens += cached_input_tokens

    def snapshot(self) -> dict[str, Any]:
        """返回可直接追加到完成日志的稳定字段快照。"""
        with self._lock:
            stages = {
                name: list(intervals)
                for name, intervals in self._stages.items()
            }
            fields: dict[str, Any] = {}
            for stage, prefix in (("parse", "parse"), ("tag", "tag")):
                intervals = stages.get(stage, [])
                if not intervals:
                    continue
                stage_start = min(start for start, _ in intervals)
                stage_end = max(end for _, end in intervals)
                fields.update({
                    f"{prefix}StartTime": _format_timestamp(stage_start),
                    f"{prefix}EndTime": _format_timestamp(stage_end),
                    f"{prefix}DurationMs": round((stage_end - stage_start) * 1000),
                })
            if stages.get("parse") and stages.get("tag"):
                pipeline_start = min(
                    start for start, _ in stages["parse"] + stages["tag"]
                )
                pipeline_end = max(
                    end for _, end in stages["parse"] + stages["tag"]
                )
                fields["parseAndTagDurationMs"] = round(
                    (pipeline_end - pipeline_start) * 1000
                )
            if self._llm_attempts:
                fields.update({
                    "modelsAttempted": "->".join(self._models_attempted),
                    "modelsSucceeded": "->".join(self._models_succeeded) or "-",
                    "llmAttempts": self._llm_attempts,
                    "llmSuccessfulCalls": self._llm_successful_calls,
                    "inputTokens": self._input_tokens,
                    "outputTokens": self._output_tokens,
                    "totalTokens": self._total_tokens,
                    "cachedInputTokens": self._cached_input_tokens,
                })
            return fields


_execution_metrics_var: ContextVar[ExecutionMetrics | None] = ContextVar(
    "execution_metrics",
    default=None,
)


@contextmanager
def collect_execution_metrics() -> Iterator[ExecutionMetrics]:
    """为当前接口创建可跨协程和显式复制线程上下文共享的指标收集器。"""
    collector = ExecutionMetrics()
    token = _execution_metrics_var.set(collector)
    try:
        yield collector
    finally:
        _execution_metrics_var.reset(token)


def record_stage_timing(stage: str, started_at: float, ended_at: float) -> None:
    """向当前接口指标收集器写入阶段时间；无收集器时为空操作。"""
    collector = _execution_metrics_var.get()
    if collector is not None:
        collector.record_stage(stage, started_at, ended_at)


def record_llm_attempt(model_name: str) -> None:
    """记录一次模型 HTTP 尝试；无接口收集器时为空操作。"""
    collector = _execution_metrics_var.get()
    if collector is not None:
        collector.record_model_attempt(model_name)


def record_llm_usage(
    model_name: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    cached_input_tokens: int = 0,
) -> None:
    """记录成功模型响应的 token 用量；无接口收集器时为空操作。"""
    collector = _execution_metrics_var.get()
    if collector is not None:
        collector.record_llm_usage(
            model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=cached_input_tokens,
        )


def log_json_response(
    operation: str,
    payload: Any,
    *,
    level: int | str = logging.INFO,
) -> None:
    """把接口完整响应记录为只包含缩进 JSON 的多行日志块。

    Args:
        operation: 接口操作名称；接口身份由日志业务类型表头展示。
        payload: 与接口 ``JSONResponse.content`` 相同的对象。
        level: 日志级别名称或整数值。

    Note:
        每一行都独立调用 logger，使时间、级别、业务类型和调用位置完整保留。
        不输出人工摘要、标题或边框，也不做字段删减。
    """
    active_level = _log_level(level)
    for line in json.dumps(payload, ensure_ascii=False, indent=2).splitlines():
        logger.log(active_level, f"│  {line}", stacklevel=2)


def log_completion_summary(
    operation: str,
    payload: dict[str, Any],
    *,
    level: int | str = logging.INFO,
    stacklevel: int = 2,
) -> None:
    """先输出完成标识，再把完成信息作为多行缩进 JSON 展示。"""
    active_level = _log_level(level)
    logger.log(
        active_level,
        f"[{operation}完成]",
        stacklevel=stacklevel,
    )
    for line in json.dumps(payload, ensure_ascii=False, indent=2).splitlines():
        logger.log(
            active_level,
            f"│  {line}",
            stacklevel=stacklevel,
        )


def _calling_function_name() -> str:
    """查找进入 `log_operation` 的实际业务函数名。

    Returns:
        str: 首个非日志模块和非 `contextlib` 栈帧的函数名；未找到时为
        `unknown`。
    """
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame else None
        while frame:
            module_name = frame.f_globals.get("__name__", "")
            if module_name not in {__name__, "contextlib"}:
                return frame.f_code.co_name
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


class OperationLog:
    """收集单次方法调用在结束日志中使用的状态和汇总字段。

    Attributes:
        operation (str): 面向日志阅读者的中文操作名称。
        start_fields (dict[str, Any]): 方法开始时已知的业务字段。
        end_fields (dict[str, Any]): 执行过程中补充的结果摘要。
        failed (bool): 是否由业务代码显式标记失败。
        error (Exception | None): 显式失败对应的异常。
        start_time (str): 精确到秒的方法开始时间。
    """

    def __init__(self, operation: str, start_fields: dict[str, Any]) -> None:
        """初始化一次方法调用的日志状态。

        Args:
            operation (str): 面向日志阅读者的中文操作名称。
            start_fields (dict[str, Any]): 调用开始时需要记录的业务字段。
        """
        self.operation = operation
        self.start_fields = dict(start_fields)
        self.end_fields: dict[str, Any] = {}
        self.failed = False
        self.error: Exception | None = None
        self.error_exc_info = None
        self._started = time.monotonic()
        # ``None`` 表示沿用操作本身级别。不能用 INFO 作为哨兵，否则一个
        # 默认 DEBUG 的操作无法显式把终态完成行提升到 INFO。
        self.completion_level: int | None = None

    def add(self, **fields: Any) -> None:
        """追加只在完成或失败末行展示的业务汇总字段。

        Args:
            **fields (Any): 需要合并到结束日志的命名字段。
        """
        self.end_fields.update(fields)

    def elapsed_seconds(self) -> float:
        """计算不受系统时间调整影响的调用耗时。

        Returns:
            float: 保留三位小数的耗时秒数。
        """
        return round(time.monotonic() - self._started, 3)

    def set_completion_level(self, level: int | str) -> None:
        """设置成功完成行使用的日志级别。"""
        self.completion_level = _log_level(level)

    def mark_failed(self, error: Exception) -> None:
        """标记没有抛出但业务结果已失败的调用。

        Args:
            error (Exception): 需要在结束日志展示的失败原因。
        """
        self.failed = True
        self.error = error
        exc_info = sys.exc_info()
        if exc_info[1] is error:
            self.error_exc_info = exc_info


@contextmanager
def log_operation(
    operation: str,
    *,
    level: int | str = logging.INFO,
    failure_level: int | str = logging.ERROR,
    emit_start: bool = True,
    completion_as_json: bool = False,
    **start_fields: Any,
) -> Iterator[OperationLog]:
    """记录一次同步方法调用的开始行和结束行。

    Args:
        operation (str): 面向日志阅读者的中文操作名称。
        **start_fields (Any): 文件名、任务 ID 等调用开始字段。

    Yields:
        OperationLog: 供业务代码补充完成摘要或显式失败状态的上下文。

    Raises:
        Exception: 业务代码抛出的异常会在记录失败末行后原样继续抛出。
    """
    start_fields.setdefault("function", _calling_function_name())
    state = OperationLog(operation, start_fields)
    active_level = _log_level(level)
    active_failure_level = _log_level(failure_level)
    context_fields = {}
    if start_fields.get("file"):
        context_fields["filename"] = start_fields["file"]
    if start_fields.get("taskId"):
        context_fields["task_id"] = str(start_fields["taskId"])

    with log_context(**context_fields):
        if emit_start:
            logger.log(
                active_level,
                f"[{operation}开始] {_format_fields(start_fields)}".rstrip(),
                stacklevel=3,
            )
        try:
            yield state
        except Exception as exc:
            fields = {
                **start_fields,
                **state.end_fields,
                "durationMs": round(state.elapsed_seconds() * 1000),
                "errorType": type(exc).__name__,
                "error": str(exc),
            }
            logger.log(
                active_failure_level,
                f"[{operation}失败] {_format_fields(fields)}",
                stacklevel=3,
                exc_info=active_failure_level >= logging.ERROR,
            )
            raise
        else:
            if state.failed:
                error = state.error or RuntimeError("unknown error")
                fields = {
                    **start_fields,
                    **state.end_fields,
                    "durationMs": round(state.elapsed_seconds() * 1000),
                    "errorType": type(error).__name__,
                    "error": str(error),
                }
                logger.log(
                    active_failure_level,
                    f"[{operation}失败] {_format_fields(fields)}",
                    stacklevel=3,
                    exc_info=(
                        state.error_exc_info
                        if active_failure_level >= logging.ERROR
                        else None
                    ),
                )
            else:
                fields = {
                    **start_fields,
                    **state.end_fields,
                }
                if "totalDurationMs" not in state.end_fields:
                    fields["durationMs"] = round(state.elapsed_seconds() * 1000)
                completion_level = (
                    state.completion_level
                    if state.completion_level is not None
                    else active_level
                )
                if completion_as_json:
                    completion_payload = {
                        key: value
                        for key, value in fields.items()
                        if value is not None
                        and key not in {"file", "function", "taskId"}
                    }
                    log_completion_summary(
                        operation,
                        completion_payload,
                        level=completion_level,
                        stacklevel=4,
                    )
                else:
                    logger.log(
                        completion_level,
                        f"[{operation}完成] {_format_fields(fields)}",
                        stacklevel=3,
                    )


def _response_summary(result: Any) -> dict[str, Any]:
    """从实际 JSONResponse 中提取批次和业务状态摘要。"""
    fields: dict[str, Any] = {
        "responseType": type(result).__name__,
        "statusCode": getattr(result, "status_code", None),
    }
    raw_body = getattr(result, "body", None)
    if not raw_body:
        return fields
    try:
        payload = json.loads(raw_body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return fields
    if not isinstance(payload, dict):
        return fields
    for key in ("code", "total", "success", "failed", "submitted"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            fields[key] = value
    data = payload.get("data")
    if isinstance(data, dict):
        status = data.get("status")
        if isinstance(status, int) and not isinstance(status, bool):
            fields["businessStatus"] = status
    return fields


def log_async_operation(operation: str):
    """为异步 API 处理函数添加完整方法边界日志。

    Args:
        operation (str): 面向日志阅读者的中文操作名称。

    Returns:
        Callable: 保留原函数元数据的异步装饰器。
    """

    def decorator(func):
        """包装一个异步处理函数并保留其名称和签名元数据。

        Args:
            func: 需要记录方法边界的异步处理函数。

        Returns:
            Callable: 已添加日志上下文和结果摘要的异步包装函数。
        """

        @wraps(func)
        async def wrapper(*args, **kwargs):
            """执行原异步函数并记录请求数量、响应类型和状态码。

            Args:
                *args: 原处理函数的位置参数。
                **kwargs: 原处理函数的命名参数。

            Returns:
                Any: 原异步处理函数返回的响应对象。

            Raises:
                Exception: 原处理函数异常会在记录失败日志后继续抛出。
            """
            request_id = None if request_id_var.get() is _UNSET else request_id_var.get()
            source_path = inspect.getsourcefile(func) or "unknown"
            try:
                source_line = inspect.getsourcelines(func)[1]
            except (OSError, TypeError):
                source_line = 0
            code_location = f"{Path(source_path).name}:{source_line}"
            request_started_at = time.time()
            request = args[0] if args else kwargs.get("request")
            filename_context = _request_filename_context(request, kwargs)
            with log_context(
                request_id=request_id,
                task_id="-",
                business_type=operation,
                filename=filename_context,
            ):
                start_fields: dict[str, Any] = {
                    "function": func.__name__,
                    "interfaceMethod": func.__name__,
                    "codeLocation": code_location,
                    "requestStartTime": _format_timestamp(request_started_at),
                }
                resumes = getattr(request, "resumes", None)
                if isinstance(resumes, list):
                    start_fields["items"] = len(resumes)
                with collect_execution_metrics() as metrics:
                    with log_operation(
                        operation,
                        completion_as_json=True,
                        **start_fields,
                    ) as state:
                        result = await func(*args, **kwargs)
                        response_headers = getattr(result, "headers", None)
                        if filename_context != "-" and response_headers is not None:
                            # 中间件与路由运行在不同 asyncio 上下文，ContextVar
                            # 不会反向传播。使用临时 ASCII 响应头传递文件上下文，
                            # 中间件记录日志后会立即删除，不暴露给调用方。
                            response_headers[LOG_FILENAME_CONTEXT_HEADER] = quote(
                                filename_context,
                                safe="",
                            )
                        request_ended_at = time.time()
                        metric_fields = metrics.snapshot()
                        if operation in {
                            "同步解析",
                            "同步打标",
                            "同步解析与打标",
                        }:
                            if "parseStartTime" not in metric_fields:
                                metric_fields["parseStage"] = "skipped"
                            if "tagStartTime" not in metric_fields:
                                metric_fields["tagStage"] = "skipped"
                        state.add(
                            requestEndTime=_format_timestamp(request_ended_at),
                            totalDurationMs=round(
                                (request_ended_at - request_started_at) * 1000
                            ),
                            **metric_fields,
                            **_response_summary(result),
                        )
                        return result

        wrapper._emits_completion_log = True

        return wrapper

    return decorator
