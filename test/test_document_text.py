"""验证 PDF、DOCX 和图片输入的抽取、质量选择与安全边界。"""

import io
import sys
import types
import zipfile

import fitz
import pytest
from PIL import Image

import core.document_text as document_text
from core.document_text import (
    DocumentExtractionError,
    DocumentTextUnavailableError,
    detect_document_type,
    extract_docx_images,
    extract_document_text,
    extract_standalone_image,
    render_docx_pages,
)


def _pdf_bytes(text: str = "张三 Java工程师") -> bytes:
    """构造包含指定可检索文本的单页 PDF。

    Args:
        text (str): 写入 PDF 的测试文本。

    Returns:
        bytes: 内存中生成的 PDF 字节。
    """
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontname="china-s")
    data = document.tobytes()
    document.close()
    return data


def _docx_bytes(text: str = "张三 Java工程师") -> bytes:
    """构造 Mammoth 可读取的最小 DOCX 容器。

    Args:
        text (str): 写入 Word 主文档的测试文本。

    Returns:
        bytes: 内存中生成的 DOCX 字节。
    """
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _image_bytes(
    image_format: str = "PNG",
    size: tuple[int, int] = (800, 1200),
) -> bytes:
    """构造可作为独立简历输入的内存图片。

    Args:
        image_format (str): Pillow 写出格式，支持 PNG 或 JPEG。
        size (tuple[int, int]): 图片宽度和高度。

    Returns:
        bytes: 指定格式的 RGB 图片字节。
    """
    image = Image.new("RGB", size, "white")
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _image_docx_bytes() -> bytes:
    """构造包含两张正文图片且没有文本内容的最小 DOCX。

    Returns:
        bytes: 图片引用顺序与媒体文件名顺序相反的 DOCX 字节。
    """
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    package_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document_relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rIdRed" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/z-red.png"/>
  <Relationship Id="rIdBlue" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/a-blue.png"/>
</Relationships>"""
    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:p><w:r><w:drawing><a:blip r:embed="rIdRed"/></w:drawing></w:r></w:p>
    <w:p><w:r><w:drawing><a:blip r:embed="rIdBlue"/></w:drawing></w:r></w:p>
  </w:body>
</w:document>"""

    def png_bytes(color: str) -> bytes:
        """生成超过最小边长限制的纯色 PNG。

        Args:
            color (str): Pillow 支持的颜色名称。

        Returns:
            bytes: 64×64 RGB PNG 数据。
        """
        image = Image.new("RGB", (64, 64), color)
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_relationships)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", document_relationships)
        archive.writestr("word/media/a-blue.png", png_bytes("blue"))
        archive.writestr("word/media/z-red.png", png_bytes("red"))
    return buffer.getvalue()


def test_render_docx_pages_converts_text_docx_to_visual_page():
    """验证普通文本 DOCX 会排版并渲染为视觉模型可用的页面图片。"""
    pages = render_docx_pages(_docx_bytes("张三 Java工程师"), "resume.docx")

    assert len(pages) == 1
    assert pages[0].mode == "RGB"
    assert pages[0].width > 1000
    assert pages[0].height > pages[0].width
    assert pages[0].getbbox() is not None


def test_render_docx_pages_falls_back_to_embedded_images():
    """验证无可排版正文的图片型 DOCX 会保留正文图片阅读顺序。"""
    pages = render_docx_pages(_image_docx_bytes(), "scan.docx")

    assert len(pages) == 2
    assert pages[0].getpixel((0, 0)) == (255, 0, 0)
    assert pages[1].getpixel((0, 0)) == (0, 0, 255)


def _long_pdf_bytes() -> bytes:
    """构造足以触发 Markdown 严重截断判断的多页 PDF。

    Returns:
        bytes: 包含重复项目描述的四页 PDF 字节。
    """
    document = fitz.open()
    for page_number in range(4):
        page = document.new_page()
        for line_number in range(24):
            page.insert_text(
                (36, 36 + line_number * 30),
                f"project {page_number}-{line_number} Python Java FPGA experience detail",
            )
    data = document.tobytes()
    document.close()
    return data


def _identity_pdf_bytes() -> bytes:
    """构造包含姓名、电话和教育关键标识的 PDF。

    Returns:
        bytes: 用于验证关键字段覆盖率的 PDF 字节。
    """
    document = fitz.open()
    page = document.new_page()
    lines = [
        "刘江波",
        "18575579612",
        "工作经历",
        "教育经历",
        "江西理工大学",
        "软件工程 本科",
    ]
    for line_number, line in enumerate(lines):
        page.insert_text(
            (72, 72 + line_number * 30), line, fontname="china-s"
        )
    data = document.tobytes()
    document.close()
    return data


def _blank_pdf_bytes() -> bytes:
    """构造没有文本层的单页 PDF。

    Returns:
        bytes: 用于验证扫描件识别路径的空白 PDF 字节。
    """
    document = fitz.open()
    document.new_page()
    data = document.tobytes()
    document.close()
    return data


def test_pdf_and_docx_use_python_text_extractors():
    """验证普通 PDF 和 DOCX 均由 Python 库抽取出可用文本。"""
    pdf = extract_document_text(_pdf_bytes(), "张三.pdf")
    docx = extract_document_text(_docx_bytes(), "张三.docx")

    assert pdf.document_type == "pdf"
    assert "Java" in pdf.text
    assert docx.document_type == "docx"
    assert "Java" in docx.text


def test_standalone_png_is_validated_for_visual_parsing():
    """验证独立 PNG 被识别为图片并返回视觉解析标记。"""
    source = _image_bytes()

    assert detect_document_type(source, "张三.png") == "image"
    result = extract_document_text(source, "张三.png")
    image = extract_standalone_image(source, "张三.png")

    assert result.document_type == "image"
    assert result.text == ""
    assert result.extractor == "none"
    assert image.size == (800, 1200)


def test_standalone_image_extension_must_match_real_format():
    """验证 JPEG/PNG 扩展名与真实编码不一致时在模型调用前失败。"""
    with pytest.raises(DocumentExtractionError, match="真实图片格式 PNG 不一致"):
        detect_document_type(_image_bytes("PNG"), "resume.jpg")


def test_standalone_image_rejects_excessive_pixel_count(monkeypatch):
    """验证独立图片超过像素预算时不会进入视觉模型。"""
    monkeypatch.setattr(document_text, "_MAX_STANDALONE_IMAGE_PIXELS", 100)

    with pytest.raises(DocumentExtractionError, match="像素超过"):
        extract_standalone_image(_image_bytes(size=(800, 1200)), "resume.png")


def test_image_only_docx_extracts_images_in_document_order():
    """验证图片型 DOCX 按正文关系顺序提取图片而非按文件名排序。"""
    source = _image_docx_bytes()
    with pytest.raises(DocumentTextUnavailableError, match="未抽取到可读文本"):
        extract_document_text(source, "scan.docx")
    images = extract_docx_images(source, "scan.docx")

    assert len(images) == 2
    assert images[0].getpixel((0, 0)) == (255, 0, 0)
    assert images[1].getpixel((0, 0)) == (0, 0, 255)


def test_textless_docx_without_images_has_no_visual_fallback_material():
    """验证既无文本又无正文图片的 DOCX 返回明确的不可降级错误。"""
    with pytest.raises(DocumentTextUnavailableError, match="未找到可供视觉解析"):
        extract_docx_images(_docx_bytes(""), "empty.docx")


def test_image_only_docx_rejects_excessive_image_count(monkeypatch):
    """验证 DOCX 图片数量超限时在视觉模型调用前失败。"""
    monkeypatch.setattr(document_text, "_MAX_DOCX_VISUAL_IMAGES", 1)

    with pytest.raises(DocumentExtractionError, match="图片超过 1 张"):
        extract_docx_images(_image_docx_bytes(), "too-many-images.docx")


def test_seekable_file_source_can_be_detected_then_extracted():
    """验证可定位文件对象经过类型检测后仍保持原读取位置。"""
    source = io.BytesIO(_pdf_bytes())
    result = extract_document_text(source, "张三.pdf")
    assert result.document_type == "pdf"
    assert source.tell() == 0


def test_real_content_and_extension_must_match():
    """验证扩展名伪装和损坏 DOCX 容器在模型调用前被拒绝。"""
    with pytest.raises(DocumentExtractionError, match="不一致"):
        detect_document_type(_pdf_bytes(), "resume.docx")
    with pytest.raises(DocumentExtractionError, match="DOCX 容器损坏"):
        detect_document_type(b"PK\x03\x04not-a-docx", "resume.docx")


def test_extracted_text_over_budget_is_rejected_without_splitting(monkeypatch):
    """验证超出同步字符预算的文本不会被拆成多次模型请求。"""
    monkeypatch.setattr(document_text, "SYNC_DOCUMENT_MAX_TEXT_CHARS", 5)
    with pytest.raises(DocumentExtractionError, match="不拆分模型请求"):
        extract_document_text(_pdf_bytes("123456789"), "resume.pdf")


def test_pdf_uses_native_text_when_markdown_is_severely_truncated(monkeypatch):
    """验证 Markdown 明显截断时选择更完整的 PyMuPDF 原生文本。"""
    fake_module = types.SimpleNamespace(to_markdown=lambda document: "short result")
    monkeypatch.setitem(sys.modules, "pymupdf4llm", fake_module)

    result = extract_document_text(_long_pdf_bytes(), "long-resume.pdf")

    assert result.extractor == "PyMuPDF"
    assert result.character_count > 3000
    assert any("明显不完整" in warning for warning in result.warnings)


def test_pdf_uses_native_text_when_markdown_extractor_fails(monkeypatch):
    """验证 `pymupdf4llm` 异常时降级而不终止同步解析。"""

    def fail_markdown(document):
        """模拟 `pymupdf4llm` 在非文本页抛出的已知异常。

        Args:
            document: 测试路径传入的 PyMuPDF 文档。

        Raises:
            ValueError: 每次调用均抛出以触发原生文本降级。
        """
        raise ValueError("not a textpage of this page")

    fake_module = types.SimpleNamespace(to_markdown=fail_markdown)
    monkeypatch.setitem(sys.modules, "pymupdf4llm", fake_module)

    result = extract_document_text(_pdf_bytes(), "resume.pdf")

    assert result.extractor == "PyMuPDF"
    assert "Java" in result.text
    assert any("ValueError" in warning for warning in result.warnings)


def test_pdf_uses_native_text_when_markdown_drops_critical_fields(monkeypatch):
    """验证 Markdown 遗漏姓名和学校时选择原生文本。"""

    def markdown_without_identity(document):
        """生成长度接近原文但故意删除身份和教育关键字段的文本。

        Args:
            document: 测试路径传入的 PyMuPDF 文档。

        Returns:
            str: 已替换姓名和学校的伪 Markdown 文本。
        """
        text = "\n".join(
            page.get_text("text", sort=True) for page in document
        )
        return text.replace("刘江波", "候选人").replace(
            "江西理工大学", "其他内容字段"
        )

    fake_module = types.SimpleNamespace(to_markdown=markdown_without_identity)
    monkeypatch.setitem(sys.modules, "pymupdf4llm", fake_module)

    result = extract_document_text(_identity_pdf_bytes(), "刘江波.pdf")

    assert result.extractor == "PyMuPDF"
    assert "刘江波" in result.text
    assert "江西理工大学" in result.text
    assert any("明显不完整" in warning for warning in result.warnings)


def test_image_only_pdf_ignores_markdown_image_placeholder(monkeypatch):
    """验证图片占位符不会把扫描 PDF 误判为存在可读文本。"""
    fake_module = types.SimpleNamespace(
        to_markdown=lambda document: "![](scan-page-1.png)"
    )
    monkeypatch.setitem(sys.modules, "pymupdf4llm", fake_module)

    result = extract_document_text(_blank_pdf_bytes(), "scan.pdf")

    assert result.extractor == "none"
    assert result.text == ""
    assert result.character_count == 0
