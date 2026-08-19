"""验证方法边界日志、URL 文件名归一和异步轮询耗时。"""

import base64
import json
import logging
import re
from unittest.mock import patch

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

import app as app_module
import core.parser as parser
import core.tasks as task_runtime
from api.routes_async import get_async_document_source
from api.routes_async import get_async_result
from core.tasks import (
    TaskRecord,
    _build_parse_and_tag_summary,
    _task_store,
    run_parse_and_tag_task,
)
from logger.operation import (
    LOG_FILENAME_CONTEXT_HEADER,
    collect_execution_metrics,
    log_async_operation,
    log_json_response,
    log_operation,
    record_llm_attempt,
    record_llm_usage,
    record_stage_timing,
)
from logger.logger_manager import _build_colored_console_formatter
from test_document_text import _docx_bytes
from test_document_text import _image_bytes
from utils.file_utils import normalize_filename


def test_percent_encoded_filename_is_decoded_for_display_only():
    """验证百分号编码文件名只在展示路径解码一次。"""
    assert normalize_filename(
        "PKU%E5%88%98%E6%97%97%E7%AE%80%E5%8E%86%20%281%29.pdf"
    ) == "PKU刘旗简历 (1).pdf"


def test_percent_encoded_url_path_uses_decoded_basename():
    """验证 URL 路径只保留解码后的可读文件名。"""
    encoded_path = (
        "/records/%E5%BC%A0%E4%B8%89%EF%BC%88%E7%AE%97%E6%B3%95"
        "%E5%B7%A5%E7%A8%8B%E5%B8%88%EF%BC%89.pdf"
    )

    assert normalize_filename(encoded_path) == "张三（算法工程师）.pdf"


def test_normalized_filename_is_not_url_decoded_twice():
    """验证已归一文件名不会因跨层调用发生二次 URL 解码。"""
    normalized = normalize_filename("resume%2520v2.pdf")

    assert normalized == "resume%20v2.pdf"
    assert normalize_filename(normalized) == "resume%20v2.pdf"


def test_operation_boundary_omits_redundant_fields_and_uses_duration_ms():
    """验证边界消息不重复表头字段，并统一使用毫秒耗时。"""

    def parse_one_resume():
        """模拟进入同步解析边界的业务函数。"""
        with log_operation("同步解析", file="resume.pdf"):
            pass

    with patch("logger.operation.logger.log") as log:
        parse_one_resume()

    assert log.call_count == 2
    messages = [call.args[1] for call in log.call_args_list]
    assert messages[0] == "[同步解析开始]"
    assert re.fullmatch(r"\[同步解析完成\] durationMs=\d+", messages[1])
    assert all(call.kwargs["stacklevel"] == 3 for call in log.call_args_list)
    assert all("file=" not in message for message in messages)
    assert all("function=" not in message for message in messages)
    assert all("endTime=" not in message for message in messages)
    assert all("status=" not in message for message in messages)


def test_failed_operation_boundary_has_duration_and_exception_stack():
    """验证失败边界统一使用毫秒耗时并要求输出异常堆栈。"""
    with patch("logger.operation.logger.log") as log:
        with pytest.raises(RuntimeError, match="boom"):
            with log_operation("同步解析"):
                raise RuntimeError("boom")

    assert log.call_count == 2
    failure = log.call_args_list[1]
    assert failure.args[0] == 40
    assert re.search(r"\[同步解析失败\] durationMs=\d+", failure.args[1])
    assert "errorType=RuntimeError error=boom" in failure.args[1]
    assert failure.kwargs["exc_info"] is True


@pytest.mark.asyncio
async def test_async_operation_completion_summarizes_real_pipeline_metrics():
    """验证接口完成摘要以多行 JSON 展示真实阶段、模型和 token 指标。"""

    class Request:
        resumes = [{}, {}, {}]

    @log_async_operation("同步解析与打标")
    async def demo_sync_pipeline(request):
        record_stage_timing("parse", 100.0, 102.0)
        record_stage_timing("tag", 102.0, 105.0)
        record_llm_attempt("primary-model")
        record_llm_attempt("fallback-model")
        record_llm_usage(
            "fallback-model",
            input_tokens=120,
            output_tokens=30,
            total_tokens=150,
            cached_input_tokens=20,
        )
        return JSONResponse(content={
            "total": 3,
            "success": 2,
            "failed": 1,
            "results": [],
        })

    with patch("logger.operation.logger.log") as log:
        await demo_sync_pipeline(Request())

    messages = [call.args[1] for call in log.call_args_list]
    assert messages[0].startswith("[同步解析与打标开始]")
    assert messages[1] == "[同步解析与打标完成]"
    completion = json.loads("\n".join(message[3:] for message in messages[2:]))
    assert completion["interfaceMethod"] == "demo_sync_pipeline"
    assert completion["codeLocation"].startswith("test_logging_and_polling.py:")
    timestamp_pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
    assert re.fullmatch(timestamp_pattern, completion["requestStartTime"])
    assert re.fullmatch(timestamp_pattern, completion["requestEndTime"])
    assert re.fullmatch(timestamp_pattern, completion["parseStartTime"])
    assert re.fullmatch(timestamp_pattern, completion["parseEndTime"])
    assert completion["parseDurationMs"] == 2000
    assert re.fullmatch(timestamp_pattern, completion["tagStartTime"])
    assert re.fullmatch(timestamp_pattern, completion["tagEndTime"])
    assert completion["tagDurationMs"] == 3000
    assert completion["parseAndTagDurationMs"] == 5000
    assert completion["modelsAttempted"] == "primary-model->fallback-model"
    assert completion["modelsSucceeded"] == "fallback-model"
    assert completion["llmAttempts"] == 2
    assert completion["llmSuccessfulCalls"] == 1
    assert completion["inputTokens"] == 120
    assert completion["outputTokens"] == 30
    assert completion["totalTokens"] == 150
    assert completion["cachedInputTokens"] == 20
    assert completion["total"] == 3
    assert completion["success"] == 2
    assert completion["failed"] == 1
    assert completion["responseType"] == "JSONResponse"
    assert completion["statusCode"] == 200
    assert all("\n" not in message for message in messages)


@pytest.mark.asyncio
async def test_async_operation_uses_single_request_filename_in_log_context():
    """验证同步接口完成摘要表头展示唯一文件名。"""

    class Request:
        resumes = [{"file": {"name": "java%E5%BC%80%E5%8F%91%E5%B7%A5%E7%A8%8B%E5%B8%88_%E6%B7%B1%E5%9C%B3.pdf"}}]

    @log_async_operation("同步解析")
    async def demo_sync_parse(request):
        return JSONResponse(content={"total": 1, "results": []})

    from logger.logger_manager import filename_var

    filename_contexts = []
    with patch("logger.operation.logger.log") as log:
        log.side_effect = lambda *args, **kwargs: filename_contexts.append(
            filename_var.get()
        )
        await demo_sync_parse(Request())

    assert filename_contexts
    assert set(filename_contexts) == {"java开发工程师_深圳.pdf"}


def test_execution_metrics_aggregate_concurrent_stage_wall_time():
    """验证批量并发阶段使用最早开始到最晚结束，而不是耗时求和。"""
    with collect_execution_metrics() as metrics:
        record_stage_timing("parse", 100.0, 104.0)
        record_stage_timing("parse", 101.0, 103.0)
        record_stage_timing("tag", 103.0, 106.0)
        record_stage_timing("tag", 104.0, 105.0)

    snapshot = metrics.snapshot()
    assert snapshot["parseDurationMs"] == 4000
    assert snapshot["tagDurationMs"] == 3000
    assert snapshot["parseAndTagDurationMs"] == 6000


def test_complete_json_response_contains_only_pretty_json_and_is_not_truncated():
    """验证完整响应只逐行输出缩进 JSON，并且可以无损还原。"""
    payload = {
        "total": 1,
        "results": [{"data": {"resume": {
            "candidate": {"name": "张三", "phone": "13800000000"},
            "educations": [{"school": "测试大学", "major": "计算机"}],
            "works": [{
                "company": "测试公司",
                "position": "工程师",
                "description": "第一行\n第二行",
            }],
            "projects": [],
        }}}],
    }

    with patch("logger.operation.logger.log") as log:
        log_json_response("同步仅解析", payload)

    messages = [call.args[1] for call in log.call_args_list]
    assert messages[0] == "│  {"
    assert messages[-1] == "│  }"
    assert not any("基本信息" in message for message in messages)
    assert not any("教育经历(" in message for message in messages)
    assert not any("完整 JSON" in message for message in messages)
    restored = json.loads("\n".join(message[3:] for message in messages))
    assert restored == payload
    assert all(call.args[0] == 20 for call in log.call_args_list)
    assert all(call.kwargs["stacklevel"] == 2 for call in log.call_args_list)
    assert all("\n" not in message for message in messages)


@pytest.mark.asyncio
async def test_http_middleware_logs_tag_config_json_response():
    """验证统一中间件能覆盖未单独埋点的标签配置类 JSON 接口。"""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/tags/config",
        "raw_path": b"/api/tags/config",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 21804),
        "root_path": "",
    }
    async def get_tags_config():
        """模拟没有方法边界装饰器的 JSON 路由。"""

    scope["endpoint"] = get_tags_config
    request = Request(scope)
    payload = {"code": 0, "msg": "success", "data": {"dimensions": []}}

    async def call_next(_request):
        return JSONResponse(content=payload)

    calls = []
    with patch("app.log_completion_summary") as completion_log, patch(
        "app.log_json_response"
    ) as response_log:
        completion_log.side_effect = lambda *args, **kwargs: calls.append("completion")
        response_log.side_effect = lambda *args, **kwargs: calls.append("response")
        response = await app_module._reset_log_context(request, call_next)

    assert response.status_code == 200
    completion_log.assert_called_once()
    completion_args, completion_kwargs = completion_log.call_args
    assert completion_args[0] == "查询标签配置"
    completion = completion_args[1]
    assert completion["interfaceMethod"] == "get_tags_config"
    assert completion["codeLocation"].startswith("test_logging_and_polling.py:")
    timestamp_pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
    assert re.fullmatch(timestamp_pattern, completion["requestStartTime"])
    assert re.fullmatch(timestamp_pattern, completion["requestEndTime"])
    assert isinstance(completion["totalDurationMs"], int)
    assert completion["responseType"] == "JSONResponse"
    assert completion["statusCode"] == 200
    assert completion["code"] == 0
    assert completion_kwargs == {"level": "INFO"}
    response_log.assert_called_once_with(
        "查询标签配置",
        payload,
        level="INFO",
    )
    assert calls == ["completion", "response"]


@pytest.mark.asyncio
async def test_http_middleware_uses_response_filename_for_sync_parse_json():
    """验证同步仅解析的完整 JSON 日志表头展示单项文件名。"""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/resume/sync/parse",
        "raw_path": b"/api/resume/sync/parse",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 21804),
        "root_path": "",
    }

    async def sync_parse_resumes():
        """模拟已经输出完成摘要的同步解析路由。"""

    sync_parse_resumes._emits_completion_log = True
    scope["endpoint"] = sync_parse_resumes
    request = Request(scope)
    payload = {
        "total": 1,
        "success": 1,
        "failed": 0,
        "results": [{
            "code": 0,
            "data": {"filename": "java开发工程师_深圳.pdf", "resume": {}},
        }],
    }

    async def call_next(_request):
        return JSONResponse(content=payload)

    from logger.logger_manager import filename_var

    filename_contexts = []
    with patch("app.log_json_response") as response_log:
        response_log.side_effect = lambda *args, **kwargs: filename_contexts.append(
            filename_var.get()
        )
        response = await app_module._reset_log_context(request, call_next)

    assert response.status_code == 200
    assert filename_contexts == ["java开发工程师_深圳.pdf"]


@pytest.mark.asyncio
async def test_http_middleware_uses_and_removes_route_filename_context_header():
    """验证无 filename 字段的批量结果继承路由文件上下文且不泄露内部头。"""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/resume/sync/batch",
        "raw_path": b"/api/resume/sync/batch",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 21804),
        "root_path": "",
    }

    async def sync_parse_and_tag_resumes():
        """模拟已经输出完成摘要的同步解析与打标路由。"""

    sync_parse_and_tag_resumes._emits_completion_log = True
    scope["endpoint"] = sync_parse_and_tag_resumes
    request = Request(scope)
    payload = {"total": 3, "success": 3, "failed": 0, "results": []}
    expected_filename = "java开发工程师.pdf 等3个文件"

    async def call_next(_request):
        response = JSONResponse(content=payload)
        from urllib.parse import quote

        response.headers[LOG_FILENAME_CONTEXT_HEADER] = quote(
            expected_filename,
            safe="",
        )
        return response

    from logger.logger_manager import filename_var

    filename_contexts = []
    with patch("app.log_json_response") as response_log:
        response_log.side_effect = lambda *args, **kwargs: filename_contexts.append(
            filename_var.get()
        )
        response = await app_module._reset_log_context(request, call_next)

    assert filename_contexts == [expected_filename]
    assert LOG_FILENAME_CONTEXT_HEADER not in response.headers


@pytest.mark.asyncio
async def test_http_middleware_does_not_duplicate_decorated_completion():
    """验证已由业务装饰器输出完成摘要的接口只记录一次完成信息。"""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/resume/sync/tag",
        "raw_path": b"/api/resume/sync/tag",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 21804),
        "root_path": "",
    }

    async def sync_tag_resumes():
        """模拟已由 log_async_operation 装饰的路由。"""

    sync_tag_resumes._emits_completion_log = True
    scope["endpoint"] = sync_tag_resumes
    request = Request(scope)
    payload = {"total": 1, "success": 1, "failed": 0, "results": []}

    async def call_next(_request):
        return JSONResponse(content=payload)

    with patch("app.log_completion_summary") as completion_log, patch(
        "app.log_json_response"
    ) as response_log:
        response = await app_module._reset_log_context(request, call_next)

    assert response.status_code == 200
    completion_log.assert_not_called()
    response_log.assert_called_once_with(
        "同步仅打标签",
        payload,
        level="INFO",
    )


def test_json_response_operation_uses_chinese_business_names():
    """验证响应日志不会退回到英文 HTTP 方法和路由。"""
    describe = app_module._describe_json_response_operation
    assert describe("POST", "/api/resume/sync/parse") == "同步仅解析"
    assert describe("POST", "/api/resume/sync/tag") == "同步仅打标签"
    assert describe("POST", "/api/resume/sync/batch") == "同步解析与打标签"
    assert describe("GET", "/api/tags/config") == "查询标签配置"
    assert (
        describe("PUT", "/api/tags/config/negative/location/rules/rule-a")
        == "更新负向标签规则"
    )
    assert describe("GET", "/future/json-api") == "接口响应"


def test_console_colors_timestamp_and_each_severity():
    """验证日期时间和五种标准级别使用约定的 ANSI 配色。"""
    formatter = _build_colored_console_formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )
    expected_message_styles = {
        logging.DEBUG: "\x1b[36m",
        logging.INFO: "\x1b[32m",
        logging.WARNING: "\x1b[1;33m",
        logging.ERROR: "\x1b[1;31m",
        logging.CRITICAL: "\x1b[1;35m",
    }

    for level, style in expected_message_styles.items():
        record = logging.LogRecord(
            "color-test", level, __file__, 1, "sample", (), None
        )
        record.created = 1786075200
        rendered = formatter.format(record)
        assert re.match(
            r"^\x1b\[1;34m\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\x1b\[0m",
            rendered,
        )
        assert f"{style}sample\x1b[0m" in rendered


def test_complete_response_summary_contains_pipeline_counts():
    """验证同步解析与打标完成日志包含全链路数量摘要。"""
    summary = _build_parse_and_tag_summary(
        {
            "candidate": {"name": "张三"},
            "educations": [{}],
            "works": [{}, {}],
            "projects": [{}, {}, {}],
        },
        {
            "positiveTags": [{}, {}, {}, {}],
            "hitCount": 2,
        },
        19.3842,
    )

    assert summary == {
        "totalDurationMs": 19384,
        "educations": 1,
        "works": 2,
        "projects": 3,
        "positiveDimensions": 4,
        "negativeHits": 2,
    }


def test_result_filename_uses_decoded_readable_stem(tmp_path, monkeypatch):
    """验证保存结果使用解码后的可读文件名且不残留百分号编码。"""
    monkeypatch.setattr(parser, "RESULT_DIR", tmp_path)
    result_file = parser._save_resume_result(
        "f1f469f5-0000-0000-0000-000000000000",
        "PKU%E5%88%98%E6%97%97%E7%AE%80%E5%8E%86%20%281%29.pdf",
        {"candidate": {"name": "刘旗"}},
    )

    assert result_file is not None
    assert "PKU刘旗简历_1_f1f469f5.json" in result_file
    assert "%" not in result_file
    assert (tmp_path / result_file).is_file()


def test_async_document_source_accepts_docx():
    """验证异步视觉来源接受真实 DOCX。"""
    document = _docx_bytes()
    item = {
        "type": "base64",
        "file": {
            "name": "resume.docx",
            "content": base64.b64encode(document).decode("ascii"),
        },
    }
    source, filename = get_async_document_source([item])

    assert source == document
    assert filename == "resume.docx"


def test_async_document_source_accepts_standalone_image():
    """验证异步视觉来源接受独立 PNG 简历。"""
    image_bytes = _image_bytes()
    item = {
        "type": "base64",
        "file": {
            "name": "resume.png",
            "content": base64.b64encode(image_bytes).decode("ascii"),
        },
    }

    source, filename = get_async_document_source([item])

    assert source == image_bytes
    assert filename == "resume.png"


def test_async_document_source_rejects_unsupported_extension():
    """验证异步视觉来源对未知扩展名给出明确的格式清单。"""
    item = {
        "type": "base64",
        "file": {
            "name": "resume.txt",
            "content": base64.b64encode(b"resume").decode("ascii"),
        },
    }

    with pytest.raises(ValueError, match="PDF、DOCX、JPG、JPEG、PNG"):
        get_async_document_source([item])


@pytest.mark.asyncio
async def test_async_pipeline_uses_visual_parser_without_python_evidence(
    monkeypatch,
):
    """验证异步多格式流水线默认走视觉解析且不传 Python 原文证据。"""
    task_id = "async-visual-task"
    seen = {}

    def fake_parse(source, filename):
        """返回异步视觉解析结果。

        Args:
            source: 测试文档来源。
            filename (str): 异步任务传入的文件名。

        Returns:
            dict: 最小公开简历。
        """
        seen["source"] = source
        seen["filename"] = filename
        return {
            "candidate": {"name": "张三"},
            "educations": [],
            "works": [],
            "projects": [],
        }

    def fake_tagging(
        resume,
        job_info,
        llm_call_fn=None,
        skill_evidence_text="",
    ):
        """记录流水线传给打标器的内部证据并返回空标签结果。

        Args:
            resume (dict): 已解析的公开简历。
            job_info (dict): 测试岗位上下文。
            llm_call_fn: 测试流水线传入的模型函数。
            skill_evidence_text (str): 仅供技能校验使用的原文。

        Returns:
            dict: 可由结果模板序列化的最小标签对象。
        """
        seen["skill_evidence_text"] = skill_evidence_text
        return {
            "positiveTags": [],
            "confidence": 0.7,
            "hitTags": [],
            "hitCount": 0,
            "deductionScore": 0,
            "tagParseTime": "2026-08-05 10:00:00",
        }

    monkeypatch.setattr(
        task_runtime,
        "parse_async_visual_resume_with_logging",
        fake_parse,
    )
    monkeypatch.setattr(task_runtime, "run_tagging", fake_tagging)
    monkeypatch.setattr(task_runtime, "_log_resume_tag_result", lambda *args: None)
    monkeypatch.setattr(task_runtime, "_save_resume_result", lambda *args: None)
    monkeypatch.setattr(task_runtime, "update_current_observation", lambda **kwargs: None)
    _task_store[task_id] = TaskRecord(task_id=task_id)
    try:
        await run_parse_and_tag_task(
            task_id,
            b"pdf-bytes",
            "张三.pdf",
            {},
        )

        assert seen["source"] == b"pdf-bytes"
        assert seen["filename"] == "张三.pdf"
        assert seen["skill_evidence_text"] == ""
        assert _task_store[task_id].status == 3
    finally:
        _task_store.pop(task_id, None)


@pytest.mark.asyncio
async def test_terminal_task_elapsed_time_does_not_grow_with_polling():
    """验证完成任务的耗时固定在完成时刻，不随后续轮询增长。"""
    task_id = "terminal-task"
    _task_store[task_id] = TaskRecord(
        task_id=task_id,
        status=3,
        created_at=100.0,
        completed_at=112.5,
        result={"candidate": {"name": "张三"}},
    )
    try:
        response = await get_async_result(task_id)
        body = json.loads(response.body)
        assert body["data"]["elapsedSeconds"] == 12.5
    finally:
        _task_store.pop(task_id, None)


@pytest.mark.asyncio
async def test_running_poll_logs_only_at_debug():
    """验证高频非终态轮询不会进入 INFO 控制台。"""
    task_id = "running-task"
    _task_store[task_id] = TaskRecord(task_id=task_id, status=1)
    try:
        with patch("logger.operation.logger.log") as log:
            await get_async_result(task_id)

        assert [call.args[0] for call in log.call_args_list] == [10, 10]
        messages = [call.args[1] for call in log.call_args_list]
        summary_message = messages[-1]
        assert "taskElapsedMs=" in summary_message
        assert "taskElapsedSeconds=" not in summary_message
    finally:
        _task_store.pop(task_id, None)


@pytest.mark.asyncio
async def test_terminal_poll_promotes_completion_to_info():
    """验证任务进入终态时仍保留一条 INFO 完成摘要。"""
    task_id = "completed-task"
    now = 100.0
    _task_store[task_id] = TaskRecord(
        task_id=task_id,
        status=3,
        created_at=now,
        completed_at=now + 2.5,
        result={},
    )
    try:
        with patch("logger.operation.logger.log") as log:
            await get_async_result(task_id)

        assert [call.args[0] for call in log.call_args_list] == [10, 10]
        messages = [call.args[1] for call in log.call_args_list]
        summary_message = messages[-1]
        assert "status=3" in summary_message
        assert "taskElapsedMs=2500" in summary_message
    finally:
        _task_store.pop(task_id, None)
