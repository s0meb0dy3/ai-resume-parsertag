"""验证同步文本优先解析和多格式视觉解析边界。"""

import json
from contextlib import contextmanager

import pytest
from PIL import Image

import core.parser as parser
from core.document_text import DocumentText, DocumentTextUnavailableError


def test_json_conversion_reports_decode_and_post_process_time(monkeypatch):
    """验证本地 JSON 解码日志区分文本解码和结构归一耗时。"""
    captured: dict = {}

    class FakeOperation:
        """收集 JSON 转换完成日志需要输出的汇总字段。"""

        def add(self, **fields) -> None:
            """保存转换函数追加的结束日志字段。

            Args:
                **fields: JSON 解码、后处理耗时和结果条数。
            """
            captured["fields"] = fields

    @contextmanager
    def fake_log_operation(operation: str, **start_fields):
        """替代日志上下文并保存操作名称及开始字段。

        Args:
            operation (str): 当前业务操作名称。
            **start_fields: JSON 转换开始时已知的来源和字符数。

        Yields:
            FakeOperation: 用于收集结束日志字段的测试对象。
        """
        captured["operation"] = operation
        captured["start_fields"] = start_fields
        yield FakeOperation()

    monkeypatch.setattr(parser, "log_operation", fake_log_operation)
    raw = json.dumps({
        "candidate": {"name": "候选人甲"},
        "educations": [],
        "works": [],
        "projects": [],
    }, ensure_ascii=False)

    result = parser._convert_llm_json_to_resume(raw, "resume.pdf")

    assert result["candidate"]["name"] == "候选人甲"
    assert captured["operation"] == "本地JSON解码与契约校验"
    assert captured["start_fields"]["source"] == "resume.pdf"
    assert captured["start_fields"]["responseCharacters"] == len(raw)
    assert captured["start_fields"]["level"] == "DEBUG"
    assert captured["start_fields"]["failure_level"] == "DEBUG"
    assert captured["start_fields"]["emit_start"] is False
    fields = captured["fields"]
    assert fields["jsonDecodeDurationMs"] >= 0
    assert fields["postProcessDurationMs"] >= 0
    assert fields["conversionDurationMs"] >= (
        fields["jsonDecodeDurationMs"]
        + fields["postProcessDurationMs"]
        - 0.002
    )
    assert fields["educations"] == 0
    assert fields["works"] == 0
    assert fields["projects"] == 0


def test_visual_parser_keeps_existing_multi_image_single_call(monkeypatch):
    """验证阈值内多页图片仍在同一次模型调用中解析工作年限。"""
    calls = []
    monkeypatch.setattr(parser, "pil_to_base64", lambda image: "data:image/png;base64,test")
    monkeypatch.setattr(
        parser,
        "get_resume_prompt_bundle",
        lambda: ("dynamic prompt", "dynamic batch prompt", "dynamic-cache-key"),
    )

    def fake_llm(messages, prompt_cache_key=None):
        calls.append(messages)
        return json.dumps({
            "candidate": {"name": "张三", "workYears": 7},
            "educations": [],
            "works": [],
            "projects": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(parser, "_call_llm_with_fallback", fake_llm)
    result = parser._parse_visual_images_with_llm([object(), object()], "PDF")

    assert len(calls) == 1
    assert calls[0][0]["content"] == "dynamic prompt"
    assert result["candidate"]["workYears"] == 7
    assert sum(
        item.get("type") == "image_url"
        for item in calls[0][1]["content"]
    ) == 2


def test_sync_parser_extracts_text_then_calls_llm_once(monkeypatch):
    """验证文本抽取后仅调用一次 LLM 生成完整标准简历 JSON。"""
    extracted = DocumentText(
        text="张三，Java工程师，2020-01至2023-01在A公司工作",
        document_type="docx",
        extractor="mammoth",
        character_count=31,
    )
    calls = []

    monkeypatch.setattr(parser, "extract_document_text", lambda source, filename: extracted)

    def fake_llm(messages, prompt_cache_key=None):
        """记录模型输入并返回包含可空集合的结构化结果。

        Args:
            messages: 同步解析器构造的模型消息列表。
            prompt_cache_key: 固定解析 Prompt 的缓存键。

        Returns:
            str: 用于验证统一归一逻辑的 JSON 文本。
        """
        calls.append((messages, prompt_cache_key))
        return json.dumps({
            "candidate": {"name": "张三"},
            "educations": None,
            "works": [{
                "company": "A公司", "position": "Java工程师",
                "startDate": "2020-01", "endDate": "2023-01",
            }],
            "projects": None,
        }, ensure_ascii=False)

    monkeypatch.setattr(parser, "_call_llm_with_fallback", fake_llm)
    result = parser.parse_sync_resume_document(b"ignored", "张三.docx")

    assert len(calls) == 1
    messages, prompt_cache_key = calls[0]
    assert extracted.text in messages[1]["content"]
    assert '"dateCandidates":["2020-01","2023-01"]' in messages[1]["content"]
    assert prompt_cache_key == parser.RESUME_PROMPT_CACHE_KEY
    assert result["candidate"]["name"] == "张三"
    assert result["educations"] == []
    assert len(result["works"]) == 1
    assert result["projects"] == []


def test_sync_parser_keeps_skill_evidence_internal(monkeypatch):
    """验证同步解析可向内部流水线传递原文且不污染公开简历 JSON。"""
    extracted = DocumentText(
        text="杨强华熟悉 Vivado、Linux、DSP 和 FPGA 开发。",
        document_type="pdf",
        extractor="PyMuPDF",
        character_count=35,
    )
    monkeypatch.setattr(
        parser,
        "extract_document_text",
        lambda source, filename: extracted,
    )
    monkeypatch.setattr(
        parser,
        "_call_llm_with_fallback",
        lambda messages, prompt_cache_key=None: json.dumps({
            "candidate": {"name": "杨强华"},
            "educations": [],
            "works": [],
            "projects": [],
        }, ensure_ascii=False),
    )

    parsed_document = parser.parse_sync_resume_document(
        b"ignored",
        "杨强华.pdf",
        include_skill_evidence=True,
    )

    assert isinstance(parsed_document, parser.ParsedResumeDocument)
    assert parsed_document.skill_evidence_text == extracted.text
    assert set(parsed_document.resume) == {
        "candidate", "educations", "works", "projects",
    }
    public_json = json.dumps(parsed_document.resume, ensure_ascii=False)
    assert "skill_evidence_text" not in public_json
    assert extracted.text not in public_json


def test_async_document_wrapper_uses_visual_parser_without_evidence(monkeypatch):
    """验证异步兼容包装器只走视觉链路且不生成 Python 文本证据。"""
    seen = {}

    def fail_python_text_extraction(*args, **kwargs):
        """异步链一旦调用 Python 文本抽取就直接令测试失败。"""
        pytest.fail("异步视觉解析不应调用 extract_document_text")

    def fake_visual(source, filename):
        """记录异步包装器传入的视觉解析参数。

        Args:
            source: 测试文档来源。
            filename (str): 待解析文件名。
        Returns:
            dict: 最小视觉解析结果。
        """
        seen.update({
            "source": source,
            "filename": filename,
        })
        return {
            "candidate": {"name": "张三"},
            "educations": [],
            "works": [],
            "projects": [],
        }

    monkeypatch.setattr(parser, "extract_document_text", fail_python_text_extraction)
    monkeypatch.setattr(parser, "parse_async_visual_resume_with_logging", fake_visual)

    result = parser.parse_async_resume_for_tagging_with_logging(
        b"pdf-bytes",
        "张三.pdf",
    )

    assert result.skill_evidence_text == ""
    assert seen == {
        "source": b"pdf-bytes",
        "filename": "张三.pdf",
    }


@pytest.mark.parametrize(
    ("document_type", "filename", "parser_name"),
    [
        ("pdf", "resume.pdf", "parse_pdf_with_visual_llm"),
        ("docx", "resume.docx", "parse_docx_images_with_visual_llm"),
        ("image", "resume.png", "parse_image_with_visual_llm"),
    ],
)
def test_async_visual_parser_dispatches_by_real_document_type(
    monkeypatch,
    document_type,
    filename,
    parser_name,
):
    """验证异步入口按真实类型转为图片，且不会调用 Python 文本抽取。"""
    calls = []
    result = {
        "candidate": {"name": "视觉候选人"},
        "educations": [],
        "works": [],
        "projects": [],
    }

    monkeypatch.setattr(
        parser,
        "extract_document_text",
        lambda *args, **kwargs: pytest.fail("异步纯视觉链路不得抽取文本"),
    )
    monkeypatch.setattr(
        parser,
        "detect_document_type",
        lambda source, display_filename: document_type,
    )
    for candidate in (
        "parse_pdf_with_visual_llm",
        "parse_docx_images_with_visual_llm",
        "parse_image_with_visual_llm",
    ):
        def fake_visual(*args, _candidate=candidate, **kwargs):
            calls.append((_candidate, args, kwargs))
            return result

        monkeypatch.setattr(parser, candidate, fake_visual)
    monkeypatch.setattr(parser, "_log_resume_parse_result", lambda *args: None)

    parsed = parser.parse_async_visual_resume_with_logging(b"document", filename)

    assert parsed == result
    assert [call[0] for call in calls] == [parser_name]


def test_sync_parser_rejects_non_object_model_output(monkeypatch):
    """验证模型返回数组时不会被当作有效简历对象。"""
    monkeypatch.setattr(parser, "extract_document_text", lambda source, filename: DocumentText(
        text="resume", document_type="pdf", extractor="PyMuPDF", character_count=6,
    ))
    monkeypatch.setattr(
        parser,
        "_call_llm_with_fallback",
        lambda messages, prompt_cache_key=None: "[]",
    )
    with pytest.raises(ValueError, match="Resume JSON 无效"):
        parser.parse_sync_resume_document(b"ignored", "resume.pdf")


def test_sync_parser_uses_visual_fallback_for_textless_pdf(monkeypatch):
    """验证无文本层 PDF 使用与异步入口一致的视觉解析函数。"""
    monkeypatch.setattr(
        parser,
        "extract_document_text",
        lambda source, filename: (_ for _ in ()).throw(
            DocumentTextUnavailableError("PDF 未抽取到可读文本")
        ),
    )
    visual_calls = []

    def fake_visual(source, verbose=False):
        """记录扫描件视觉降级调用并返回最小标准简历。

        Args:
            source: 视觉解析接收的 PDF 来源。
            verbose (bool): 是否记录视觉解析详细结果。

        Returns:
            dict: 包含候选人和教育背景的测试简历。
        """
        visual_calls.append((source, verbose))
        return {
            "candidate": {"name": "扫描候选人"},
            "educations": [{"school": "测试大学"}],
            "works": [],
            "projects": [],
        }

    monkeypatch.setattr(parser, "parse_pdf_with_visual_llm", fake_visual)
    monkeypatch.setattr(
        parser,
        "_call_llm_with_fallback",
        lambda messages: pytest.fail("text LLM must not run after visual fallback"),
    )

    result = parser.parse_sync_resume_document(b"scan", "scan.pdf")

    assert visual_calls == [(b"scan", True)]
    assert result["candidate"]["name"] == "扫描候选人"


def test_sync_parser_uses_visual_fallback_for_image_only_docx(monkeypatch):
    """验证无文本 DOCX 使用独立的正文图片视觉解析器。"""
    monkeypatch.setattr(
        parser,
        "extract_document_text",
        lambda source, filename: (_ for _ in ()).throw(
            DocumentTextUnavailableError("DOCX 未抽取到可读文本")
        ),
    )
    monkeypatch.setattr(
        parser,
        "parse_pdf_with_visual_llm",
        lambda *args, **kwargs: pytest.fail("DOCX must not use PDF renderer"),
    )
    visual_calls = []

    def fake_docx_visual(source, filename, verbose=False):
        """记录 DOCX 图片视觉降级并返回最小标准简历。

        Args:
            source: 图片型 DOCX 来源。
            filename (str): 传给图片抽取器的文件名。
            verbose (bool): 是否记录详细视觉解析信息。

        Returns:
            dict: 用于验证降级成功的标准简历。
        """
        visual_calls.append((source, filename, verbose))
        return {
            "candidate": {"name": "图片简历候选人"},
            "educations": [],
            "works": [],
            "projects": [],
        }

    monkeypatch.setattr(
        parser,
        "parse_docx_images_with_visual_llm",
        fake_docx_visual,
    )

    result = parser.parse_sync_resume_document(b"scan", "scan.docx")

    assert visual_calls == [(b"scan", "scan.docx", True)]
    assert result["candidate"]["name"] == "图片简历候选人"


def test_parser_routes_standalone_image_to_visual_llm(monkeypatch):
    """验证独立简历图片经过安全抽取后进入专用视觉解析函数。"""
    monkeypatch.setattr(
        parser,
        "extract_document_text",
        lambda source, filename: DocumentText(
            text="",
            document_type="image",
            extractor="none",
            character_count=0,
            page_count=1,
        ),
    )
    visual_calls = []

    def fake_image_visual(source, filename, verbose=False):
        """记录独立图片视觉解析参数并返回最小简历。

        Args:
            source: 独立图片来源。
            filename (str): 图片文件名。
            verbose (bool): 是否启用详细视觉日志。

        Returns:
            dict: 包含候选人姓名的最小简历对象。
        """
        visual_calls.append((source, filename, verbose))
        return {
            "candidate": {"name": "图片候选人"},
            "educations": [],
            "works": [],
            "projects": [],
        }

    monkeypatch.setattr(parser, "parse_image_with_visual_llm", fake_image_visual)

    result = parser.parse_sync_resume_document(b"image", "resume.png")

    assert visual_calls == [(b"image", "resume.png", True)]
    assert result["candidate"]["name"] == "图片候选人"


def test_docx_visual_parser_sends_extracted_images_to_llm(monkeypatch):
    """验证 DOCX 页面图片经过统一视觉消息和 Resume 归一流程。"""
    monkeypatch.setattr(
        parser,
        "render_docx_pages",
        lambda source, filename: [Image.new("RGB", (64, 64), "white")],
    )
    messages_seen = []

    def fake_llm(messages, prompt_cache_key=None):
        """记录 DOCX 视觉消息并返回最小简历 JSON。

        Args:
            messages: 视觉解析器生成的 OpenAI 兼容消息。
            prompt_cache_key: 固定解析 Prompt 的缓存键。

        Returns:
            str: 包含候选人姓名的最小简历 JSON。
        """
        messages_seen.append((messages, prompt_cache_key))
        return json.dumps({
            "candidate": {"name": "视觉候选人"},
            "educations": [],
            "works": [],
            "projects": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(parser, "_call_llm_with_fallback", fake_llm)

    result = parser.parse_docx_images_with_visual_llm(
        b"image-docx",
        "scan.docx",
    )

    messages, prompt_cache_key = messages_seen[0]
    user_content = messages[1]["content"]
    assert any(item.get("type") == "image_url" for item in user_content)
    assert prompt_cache_key == parser.RESUME_PROMPT_CACHE_KEY
    assert result["candidate"]["name"] == "视觉候选人"
