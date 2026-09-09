"""为简历解析提供 PDF、DOCX 和独立图片的安全输入处理。

PDF 同时比较 `pymupdf4llm` Markdown 与 PyMuPDF 原生文本，避免抽取器静默遗漏
姓名、学校或整页内容。有效 PDF 没有文本层或 DOCX 没有文本但含正文图片时，
上层可转入视觉解析；独立 JPEG/PNG 通过像素预算后直接进入视觉解析。损坏、
加密、类型伪装和超出预算的文件不会误触发降级。
"""
from __future__ import annotations

import io
import inspect
import math
import posixpath
import re
import zipfile
from collections import Counter
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import fitz
from PIL import Image, UnidentifiedImageError

from config import MAX_FILE_SIZE_MB, PDF_DPI, SYNC_DOCUMENT_MAX_TEXT_CHARS
from logger.operation import log_operation

DocumentType = Literal["pdf", "docx", "image"]
_MAX_DOCX_ENTRIES = 5000
_MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
_MAX_DOCX_ENTRY_BYTES = 30 * 1024 * 1024
_MAX_DOCX_COMPRESSION_RATIO = 200
_MAX_DOCX_VISUAL_IMAGES = 20
_MAX_DOCX_VISUAL_PAGES = 20
_MAX_DOCX_VISUAL_PIXELS = 80_000_000
_MIN_DOCX_VISUAL_EDGE = 32
_MAX_STANDALONE_IMAGE_PIXELS = 40_000_000
_MIN_STANDALONE_IMAGE_EDGE = 64
_PDF_FALLBACK_MIN_MISSING_CHARS = 500
_PDF_FALLBACK_MIN_LENGTH_RATIO = 2
_PDF_CRITICAL_MARKER_MIN_LENGTH_RATIO = 0.9
_PDF_SECTION_MARKERS = (
    "基本信息",
    "个人信息",
    "教育背景",
    "教育经历",
    "学历背景",
    "工作经历",
    "项目经历",
)
_PDF_NAME_LABEL_RE = re.compile(
    r"(?:姓名|求职意向)\s*[:：]?\s*([\u4e00-\u9fff·]{2,4})"
)
_PDF_STANDALONE_NAME_RE = re.compile(r"^[\u4e00-\u9fff·]{2,4}$")
_PDF_SCHOOL_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9·（）()]{2,30}(?:大学|学院|学校)"
)
_PDF_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PDF_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LOW_CJK_ENTROPY_MIN_CHARS = 100
_LOW_CJK_ENTROPY_THRESHOLD = 3.0


def is_low_entropy_cjk_text(text: str) -> bool:
    """判断抽取文本是否因汉字映射塌缩而明显异常。

    少于 100 个汉字时样本不足，不进行熵判断；样本充足且 Shannon 信息熵
    低于 3.0 时，认为大量字形可能被错误映射成少量汉字，应改走视觉解析。
    """
    han_chars = re.findall(r"[\u4e00-\u9fff]", text or "")
    if len(han_chars) < _LOW_CJK_ENTROPY_MIN_CHARS:
        return False
    counts = Counter(han_chars)
    total = len(han_chars)
    entropy = -sum(
        (count / total) * math.log2(count / total)
        for count in counts.values()
    )
    return entropy < _LOW_CJK_ENTROPY_THRESHOLD


class DocumentExtractionError(ValueError):
    """表示文档不受支持、已损坏或违反抽取安全限制。"""


class DocumentTextUnavailableError(DocumentExtractionError):
    """表示有效文档没有可用文本，可由上层尝试对应视觉降级。"""


@dataclass(frozen=True)
class DocumentText:
    """保存 Python 文本抽取结果及质量诊断信息。

    Attributes:
        text (str): 送入单次 LLM 统一 JSON 调用的完整文本。
        document_type (DocumentType): 根据真实内容识别的 `pdf`、`docx` 或
            `image`。
        extractor (str): 最终选用的文本抽取器名称。
        character_count (int): 清理后文本的字符数量。
        page_count (int): PDF 页数，DOCX 固定为 0。
        warnings (tuple[str, ...]): 抽取器降级或质量选择的诊断信息。
    """

    text: str
    document_type: DocumentType
    extractor: str
    character_count: int
    page_count: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _read_source_bytes(source: Any) -> bytes:
    """读取路径、字节或文件对象，并执行统一文件大小限制。

    Args:
        source (Any): 文件路径、字节数据或提供 `read` 方法的文件对象。

    Returns:
        bytes: 文档原始字节；可定位文件对象会恢复读取前位置。

    Raises:
        OSError: 文件路径无法读取时抛出。
        DocumentExtractionError: 来源类型不支持或文件超过大小限制时抛出。
    """
    if isinstance(source, bytes):
        data = source
    elif isinstance(source, bytearray):
        data = bytes(source)
    elif isinstance(source, (str, Path)):
        data = Path(source).read_bytes()
    elif hasattr(source, "read"):
        position = source.tell() if hasattr(source, "tell") else None
        raw_data = source.read()
        if position is not None and hasattr(source, "seek"):
            source.seek(position)
        data = raw_data if isinstance(raw_data, bytes) else bytes(raw_data)
    else:
        raise DocumentExtractionError(f"不支持的文档来源类型: {type(source).__name__}")
    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    if len(data) > max_bytes:
        raise DocumentExtractionError(f"文档大小超过 {MAX_FILE_SIZE_MB}MB 限制")
    return data


def _validate_docx_archive(data: bytes) -> None:
    """在交给 Mammoth 前校验 DOCX 容器和解压安全边界。

    Args:
        data (bytes): 待校验的 DOCX 原始字节。

    Raises:
        DocumentExtractionError: 容器损坏、结构不完整、包含危险路径或超过
            条目数量、解压大小和压缩比限制时抛出。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise DocumentExtractionError("ZIP 文件不是有效 DOCX")
            if len(entries) > _MAX_DOCX_ENTRIES:
                raise DocumentExtractionError("DOCX 条目数量超过安全限制")
            total_size = 0
            for entry in entries:
                if entry.flag_bits & 0x1:
                    raise DocumentExtractionError("DOCX 包含加密条目")
                path = Path(entry.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise DocumentExtractionError("DOCX 包含不安全的文件路径")
                if entry.file_size > _MAX_DOCX_ENTRY_BYTES:
                    raise DocumentExtractionError("DOCX 单个条目超过安全限制")
                total_size += entry.file_size
                if total_size > _MAX_DOCX_UNCOMPRESSED_BYTES:
                    raise DocumentExtractionError("DOCX 解压后大小超过安全限制")
                compressed_size = max(entry.compress_size, 1)
                if entry.file_size / compressed_size > _MAX_DOCX_COMPRESSION_RATIO:
                    raise DocumentExtractionError("DOCX 压缩比超过安全限制")
    except zipfile.BadZipFile as exc:
        raise DocumentExtractionError("DOCX 容器损坏") from exc


def _decode_standalone_image(data: bytes, filename: str) -> Image.Image:
    """解码并校验独立 JPEG/PNG 简历图片。

    Args:
        data (bytes): 待解码的图片原始字节。
        filename (str): 用于扩展名与真实格式一致性校验的文件名。

    Returns:
        Image.Image: 已完整加载并复制到 RGB 内存图像的图片。

    Raises:
        DocumentExtractionError: 图片损坏、格式不支持、扩展名伪装、边长过小
            或像素数量超过安全预算时抛出。

    Note:
        独立图片只允许 JPEG 和 PNG。四千万像素上限在模型编码前限制内存占用，
        Pillow 的解压炸弹告警同样按错误处理。
    """
    suffix = Path(filename).suffix.lower()
    expected_format = {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".png": "PNG",
    }.get(suffix)
    if expected_format is None:
        raise DocumentExtractionError(f"不支持的图片扩展名: {suffix or '无'}")
    try:
        with Image.open(io.BytesIO(data)) as source_image:
            actual_format = str(source_image.format or "").upper()
            if actual_format not in {"JPEG", "PNG"}:
                raise DocumentExtractionError(
                    f"不支持的图片格式: {actual_format or 'unknown'}"
                )
            if actual_format != expected_format:
                raise DocumentExtractionError(
                    f"文件扩展名 {suffix} 与真实图片格式 {actual_format} 不一致"
                )
            width, height = source_image.size
            if min(width, height) < _MIN_STANDALONE_IMAGE_EDGE:
                raise DocumentExtractionError(
                    f"简历图片边长不能小于 {_MIN_STANDALONE_IMAGE_EDGE} 像素"
                )
            if width * height > _MAX_STANDALONE_IMAGE_PIXELS:
                raise DocumentExtractionError("简历图片像素超过视觉解析安全上限")
            source_image.load()
            return source_image.convert("RGB").copy()
    except DocumentExtractionError:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise DocumentExtractionError("图片损坏或超过 Pillow 安全限制") from exc


def extract_standalone_image(source: Any, filename: str) -> Image.Image:
    """读取并校验可直接进入视觉解析的独立简历图片。

    Args:
        source (Any): JPEG/PNG 文件路径、字节数据或文件对象。
        filename (str): 用于类型校验和日志展示的文件名。

    Returns:
        Image.Image: 已完整加载的 RGB 简历图片。

    Raises:
        DocumentExtractionError: 来源超限、图片无效或扩展名与内容不一致时抛出。
    """
    with log_operation(
        "简历图片校验",
        file=filename or "unknown",
        level="DEBUG",
        failure_level="DEBUG",
        emit_start=False,
    ) as operation:
        data = _read_source_bytes(source)
        image = _decode_standalone_image(data, filename)
        operation.add(
            imageFormat=Path(filename).suffix.lower().lstrip("."),
            width=image.width,
            height=image.height,
            pixels=image.width * image.height,
        )
        return image


def _docx_image_parts_in_order(archive: zipfile.ZipFile) -> list[str]:
    """按 Word 主文档引用顺序解析内嵌图片部件路径。

    Args:
        archive (zipfile.ZipFile): 已通过安全校验的 DOCX 容器。

    Returns:
        list[str]: 去重后按正文出现顺序排列的 `word/media/*` 部件路径。

    Raises:
        DocumentExtractionError: 主文档或关系 XML 无法解析时抛出。
    """
    relationship_path = "word/_rels/document.xml.rels"
    try:
        document_bytes = archive.read("word/document.xml")
        relationship_bytes = archive.read(relationship_path)
    except KeyError:
        return []
    try:
        document_root = ET.fromstring(document_bytes)
        relationship_root = ET.fromstring(relationship_bytes)
    except ET.ParseError as exc:
        raise DocumentExtractionError("DOCX 主文档图片关系 XML 损坏") from exc

    relationship_targets: dict[str, str] = {}
    for relationship in relationship_root:
        relationship_type = relationship.attrib.get("Type", "")
        if not relationship_type.endswith("/image"):
            continue
        if relationship.attrib.get("TargetMode") == "External":
            continue
        relationship_id = relationship.attrib.get("Id", "")
        target = relationship.attrib.get("Target", "")
        if not relationship_id or not target:
            continue
        part_name = (
            posixpath.normpath(target.lstrip("/"))
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("word", target))
        )
        if not part_name.startswith("word/media/"):
            raise DocumentExtractionError("DOCX 图片关系指向了不安全的部件路径")
        relationship_targets[relationship_id] = part_name

    ordered_parts: list[str] = []
    seen: set[str] = set()
    relationship_namespace = (
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    )
    for element in document_root.iter():
        relationship_id = (
            element.attrib.get(f"{relationship_namespace}embed")
            or element.attrib.get(f"{relationship_namespace}id")
        )
        part_name = relationship_targets.get(relationship_id or "")
        if part_name and part_name not in seen:
            seen.add(part_name)
            ordered_parts.append(part_name)
    return ordered_parts


def extract_docx_images(source: Any, filename: str = "") -> list[Image.Image]:
    """提取无文本 DOCX 中可用于视觉解析的正文图片。

    Args:
        source (Any): DOCX 文件路径、字节数据或文件对象。
        filename (str): 用于日志展示的文件名。

    Returns:
        list[Image.Image]: 按正文引用顺序排列、已加载到内存的 RGB 图片。

    Raises:
        DocumentExtractionError: DOCX 容器、图片关系或像素预算无效时抛出。
        DocumentTextUnavailableError: 正文没有可识别的有效图片时抛出。

    Note:
        最多接受 20 张图片和 8000 万总像素，避免图片型 DOCX 在视觉降级时
        形成无界内存及模型请求。
    """
    with log_operation(
        "DOCX图片抽取",
        file=filename or "unknown",
        emit_start=False,
        failure_level="DEBUG",
    ) as operation:
        data = _read_source_bytes(source)
        _validate_docx_archive(data)
        images: list[Image.Image] = []
        skipped = 0
        total_pixels = 0
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            parts = _docx_image_parts_in_order(archive)
            if len(parts) > _MAX_DOCX_VISUAL_IMAGES:
                raise DocumentExtractionError(
                    f"DOCX 正文图片超过 {_MAX_DOCX_VISUAL_IMAGES} 张视觉解析上限"
                )
            for part_name in parts:
                try:
                    with Image.open(io.BytesIO(archive.read(part_name))) as source_image:
                        source_image.load()
                        width, height = source_image.size
                        if min(width, height) < _MIN_DOCX_VISUAL_EDGE:
                            skipped += 1
                            continue
                        total_pixels += width * height
                        if total_pixels > _MAX_DOCX_VISUAL_PIXELS:
                            raise DocumentExtractionError(
                                "DOCX 正文图片总像素超过视觉解析安全上限"
                            )
                        images.append(source_image.convert("RGB").copy())
                except (KeyError, UnidentifiedImageError, OSError):
                    skipped += 1

        if not images:
            raise DocumentTextUnavailableError(
                "DOCX 未找到可供视觉解析的正文图片"
            )
        operation.add(
            images=len(images),
            skippedImages=skipped,
            totalPixels=total_pixels,
        )
        return images


def render_docx_pages(source: Any, filename: str = "") -> list[Image.Image]:
    """把 DOCX 正文排版为页面图片，供异步视觉模型直接解析。

    Args:
        source (Any): DOCX 文件路径、字节数据或文件对象。
        filename (str): 用于日志展示和类型校验的文件名。

    Returns:
        list[Image.Image]: 按阅读顺序排列的 RGB 页面图片。

    Raises:
        DocumentExtractionError: DOCX、HTML 排版或页面资源预算无效时抛出。
        DocumentTextUnavailableError: DOCX 既没有可排版正文也没有有效正文图片时
            抛出。

    Note:
        Mammoth 负责把安全校验后的 OOXML 转为包含内嵌图片 data URI 的 HTML，
        PyMuPDF Story 再将 HTML 排版到 A4 页面。这样不依赖宿主机安装 Office 或
        LibreOffice。最多渲染 20 页、总计 8000 万像素，避免超大文档耗尽内存。
    """
    with log_operation(
        "DOCX页面渲染",
        file=filename or "unknown",
        emit_start=False,
        failure_level="DEBUG",
    ) as operation:
        try:
            import mammoth
        except ImportError as exc:
            raise DocumentExtractionError("缺少 DOCX 渲染依赖 mammoth") from exc

        data = _read_source_bytes(source)
        _validate_docx_archive(data)
        try:
            converted = mammoth.convert_to_html(
                io.BytesIO(data),
                convert_image=mammoth.images.data_uri,
            )
        except Exception as exc:
            raise DocumentExtractionError(f"DOCX 页面转换失败: {exc}") from exc

        html = (converted.value or "").strip()
        if not html:
            images = extract_docx_images(data, filename)
            operation.add(
                renderer="embeddedImages",
                pages=len(images),
                totalPixels=sum(image.width * image.height for image in images),
                warnings=len(converted.messages),
            )
            return images

        user_css = """
            @page { size: A4; }
            body { font-family: sans-serif; font-size: 10.5pt; line-height: 1.35; }
            img { max-width: 100%; height: auto; }
            table { border-collapse: collapse; max-width: 100%; }
            td, th { padding: 2pt 4pt; vertical-align: top; }
            p { margin: 0 0 4pt 0; }
        """
        try:
            story = fitz.Story(html=html, user_css=user_css)
            pdf_buffer = io.BytesIO()
            writer = fitz.DocumentWriter(pdf_buffer)
            media_box = fitz.paper_rect("a4")
            content_box = fitz.Rect(
                media_box.x0 + 36,
                media_box.y0 + 36,
                media_box.x1 - 36,
                media_box.y1 - 36,
            )
            more = 1
            page_count = 0
            try:
                while more:
                    if page_count >= _MAX_DOCX_VISUAL_PAGES:
                        raise DocumentExtractionError(
                            f"DOCX 渲染页数超过 {_MAX_DOCX_VISUAL_PAGES} 页视觉解析上限"
                        )
                    device = writer.begin_page(media_box)
                    more, _ = story.place(content_box)
                    story.draw(device)
                    writer.end_page()
                    page_count += 1
            finally:
                writer.close()

            rendered_pdf = fitz.open(
                stream=pdf_buffer.getvalue(),
                filetype="pdf",
            )
            images: list[Image.Image] = []
            total_pixels = 0
            try:
                matrix = fitz.Matrix(PDF_DPI / 72.0, PDF_DPI / 72.0)
                for page in rendered_pdf:
                    pixmap = page.get_pixmap(
                        matrix=matrix,
                        colorspace=fitz.csRGB,
                        alpha=False,
                    )
                    total_pixels += pixmap.width * pixmap.height
                    if total_pixels > _MAX_DOCX_VISUAL_PIXELS:
                        raise DocumentExtractionError(
                            "DOCX 渲染页面总像素超过视觉解析安全上限"
                        )
                    images.append(
                        Image.frombytes(
                            "RGB",
                            (pixmap.width, pixmap.height),
                            pixmap.samples,
                        )
                    )
            finally:
                rendered_pdf.close()
        except DocumentExtractionError:
            raise
        except Exception as exc:
            raise DocumentExtractionError(f"DOCX 页面渲染失败: {exc}") from exc

        if not images:
            raise DocumentTextUnavailableError("DOCX 未生成可供视觉解析的页面")
        operation.add(
            renderer="Mammoth+PyMuPDF",
            pages=len(images),
            totalPixels=total_pixels,
            warnings=len(converted.messages),
        )
        return images


def _validate_text_budget(text: str, document_type: DocumentType) -> str:
    """校验抽取文本非空且能由单次同步模型请求承载。

    Args:
        text (str): 抽取器返回的原始文本。
        document_type (DocumentType): 用于错误信息的真实文档类型。

    Returns:
        str: 去除首尾空白后的完整文本。

    Raises:
        DocumentTextUnavailableError: 文档没有可读文本时抛出。
        DocumentExtractionError: 文本超过同步解析字符预算时抛出。

    Note:
        同步解析要求单次 LLM 统一生成 JSON，因此本层不拆分超长文本。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise DocumentTextUnavailableError(
            f"{document_type.upper()} 未抽取到可读文本"
        )
    if len(cleaned) > SYNC_DOCUMENT_MAX_TEXT_CHARS:
        raise DocumentExtractionError(
            f"{document_type.upper()} 抽取文本超过同步解析上限 "
            f"{SYNC_DOCUMENT_MAX_TEXT_CHARS} 字符，不拆分模型请求"
        )
    return cleaned


def _normalize_pdf_plain_text(text: str) -> str:
    """保留 PDF 阅读顺序并清理布局产生的重复空白。

    Args:
        text (str): PyMuPDF 按阅读顺序提取的页面文本。

    Returns:
        str: 合并行内空白且最多保留一个连续空行的文本。
    """
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    normalized: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = not line
        if is_blank and previous_blank:
            continue
        normalized.append(line)
        previous_blank = is_blank
    return "\n".join(normalized).strip()


def _meaningful_character_count(text: str) -> int:
    """统计不受 PDF 布局空白影响的有效字符数量。

    Args:
        text (str): 待评估的 Markdown 或原生文本。

    Returns:
        int: 所有非空白字符的数量。
    """
    return sum(not character.isspace() for character in text)


def _critical_pdf_markers(text: str) -> set[str]:
    """提取用于判断 Markdown 是否漏页的身份和教育关键标识。

    Args:
        text (str): PyMuPDF 原生文本基准。

    Returns:
        set[str]: 姓名、电话、邮箱、学校和主要章节标识集合。

    Note:
        该集合只用于覆盖率比较，不直接写入最终简历字段。
    """
    markers = {marker for marker in _PDF_SECTION_MARKERS if marker in text}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines[:12]:
        labelled_name = _PDF_NAME_LABEL_RE.search(line)
        if labelled_name:
            markers.add(labelled_name.group(1))
        elif (
            _PDF_STANDALONE_NAME_RE.fullmatch(line)
            and line not in _PDF_SECTION_MARKERS
        ):
            markers.add(line)
    for line in lines:
        markers.update(_PDF_SCHOOL_RE.findall(line))
    markers.update(_PDF_PHONE_RE.findall(text))
    markers.update(_PDF_EMAIL_RE.findall(text))
    return markers


def detect_document_type(source: Any, filename: str = "") -> DocumentType:
    """根据真实内容识别 PDF、DOCX 或图片，并拒绝扩展名伪装。

    Args:
        source (Any): 文件路径、字节数据或文件对象。
        filename (str): 用于扩展名一致性校验和日志展示的文件名。

    Returns:
        DocumentType: `pdf`、`docx` 或 `image`。

    Raises:
        DocumentExtractionError: 扩展名不支持、内容无效或二者不一致时抛出。
    """
    with log_operation(
        "文档类型检测",
        file=filename or "unknown",
        level="DEBUG",
        failure_level="DEBUG",
        emit_start=False,
    ) as operation:
        data = _read_source_bytes(source)
        suffix = Path(filename).suffix.lower()
        if suffix and suffix not in {".pdf", ".docx", ".jpg", ".jpeg", ".png"}:
            raise DocumentExtractionError(f"不支持的文档扩展名: {suffix}")
        if data.startswith(b"%PDF"):
            detected: DocumentType = "pdf"
        elif data.startswith(b"PK"):
            _validate_docx_archive(data)
            detected = "docx"
        elif data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
            _decode_standalone_image(data, filename)
            detected = "image"
        else:
            raise DocumentExtractionError(
                "仅支持内容有效的 PDF、DOCX、JPEG 或 PNG 文件"
            )

        expected = {
            ".pdf": "pdf",
            ".docx": "docx",
            ".jpg": "image",
            ".jpeg": "image",
            ".png": "image",
        }.get(suffix)
        if expected and expected != detected:
            raise DocumentExtractionError(
                f"文件扩展名 {suffix} 与真实内容 {detected} 不一致"
            )
        operation.add(documentType=detected, sizeBytes=len(data))
        return detected


def extract_pdf_markdown(source: Any, filename: str = "") -> DocumentText:
    """比较两种 PDF 文本抽取结果并选择信息更完整的一份。

    Args:
        source (Any): PDF 文件路径、字节数据或文件对象。
        filename (str): 用于日志展示的文件名。

    Returns:
        DocumentText: 最终选中的完整文本及抽取质量诊断。

    Raises:
        DocumentExtractionError: PDF 损坏、加密或文本超过预算时抛出。

    Note:
        有效但无文本层的 PDF 返回 `extractor=none` 的空结果，供同步解析上层
        精确触发图片视觉降级。`pymupdf4llm` 异常或遗漏关键字段时使用原生文本。
    """
    with log_operation(
        "PDF文本抽取",
        file=filename or "unknown",
        extractor="pymupdf4llm",
        emit_start=False,
        failure_level="DEBUG",
    ) as operation:
        data = _read_source_bytes(source)
        warnings: list[str] = []
        extractor = "pymupdf4llm"
        selection_reason = "markdown_complete"
        markdown_text = ""
        with fitz.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                raise DocumentExtractionError("PDF 已加密，无法抽取文本")
            page_count = document.page_count

            try:
                plain_text = _normalize_pdf_plain_text(
                    "\n\n".join(
                        page.get_text("text", sort=True) for page in document
                    )
                )
            except Exception as exc:
                plain_text = ""
                warnings.append(
                    f"PyMuPDF 纯文本抽取失败: {type(exc).__name__}: {exc}"
                )
            try:
                import pymupdf4llm

                to_markdown = pymupdf4llm.to_markdown
                try:
                    parameters = inspect.signature(to_markdown).parameters.values()
                    supports_progress_flag = any(
                        parameter.name == "show_progress"
                        or parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters
                    )
                except (TypeError, ValueError):
                    supports_progress_flag = True
                markdown_text = (
                    to_markdown(
                        document,
                        **({"show_progress": False} if supports_progress_flag else {}),
                    )
                    or ""
                ).strip()
            except Exception as exc:
                extractor = "PyMuPDF"
                selection_reason = (
                    "pymupdf4llm_unavailable"
                    if isinstance(exc, ImportError)
                    else "pymupdf4llm_error"
                )
                warnings.append(
                    f"pymupdf4llm 抽取失败，已降级为 PyMuPDF 纯文本抽取: "
                    f"{type(exc).__name__}: {exc}"
                )

        markdown_characters = _meaningful_character_count(markdown_text)
        plain_text_characters = _meaningful_character_count(plain_text)
        if plain_text_characters == 0:
            operation.add(
                actualExtractor="none",
                selectionReason="image_only",
                pages=page_count,
                characters=0,
                markdownCharacters=markdown_characters,
                plainTextCharacters=0,
                warnings=len(warnings),
            )
            return DocumentText(
                text="",
                document_type="pdf",
                extractor="none",
                character_count=0,
                page_count=page_count,
                warnings=tuple(warnings),
            )
        compact_markdown = re.sub(r"\s+", "", markdown_text)
        missing_critical_markers = sorted(
            marker
            for marker in _critical_pdf_markers(plain_text)
            if re.sub(r"\s+", "", marker) not in compact_markdown
        )
        markdown_is_incomplete = (
            plain_text_characters - markdown_characters
            >= _PDF_FALLBACK_MIN_MISSING_CHARS
            and plain_text_characters
            >= markdown_characters * _PDF_FALLBACK_MIN_LENGTH_RATIO
        )
        markdown_misses_critical_fields = (
            bool(missing_critical_markers)
            and plain_text_characters
            >= markdown_characters * _PDF_CRITICAL_MARKER_MIN_LENGTH_RATIO
        )
        if extractor == "pymupdf4llm" and (
            markdown_is_incomplete or markdown_misses_critical_fields
        ):
            extractor = "PyMuPDF"
            selection_reason = (
                "markdown_missing_critical_fields"
                if markdown_misses_critical_fields
                else "markdown_incomplete"
            )
            warnings.append(
                "pymupdf4llm 抽取内容明显不完整，已改用 PyMuPDF 纯文本抽取"
            )

        if extractor != "pymupdf4llm":
            operation.set_completion_level("WARNING")

        text = plain_text if extractor == "PyMuPDF" else markdown_text
        cleaned = _validate_text_budget(text, "pdf")
        operation.add(
            actualExtractor=extractor,
            selectionReason=selection_reason,
            pages=page_count,
            characters=len(cleaned),
            markdownCharacters=markdown_characters,
            plainTextCharacters=plain_text_characters,
            missingCriticalMarkerCount=len(missing_critical_markers),
            warnings=len(warnings),
        )
        return DocumentText(
            text=cleaned,
            document_type="pdf",
            extractor=extractor,
            character_count=len(cleaned),
            page_count=page_count,
            warnings=tuple(warnings),
        )


def extract_docx_structured_text(source: Any, filename: str = "") -> DocumentText:
    """使用 Mammoth 提取 DOCX 文本，并拒绝不安全 OOXML 容器。

    Args:
        source (Any): DOCX 文件路径、字节数据或文件对象。
        filename (str): 用于日志展示的文件名。

    Returns:
        DocumentText: DOCX 原始文本和 Mammoth 诊断消息。

    Raises:
        DocumentExtractionError: 依赖缺失、容器不安全或文本抽取失败时抛出。
        DocumentTextUnavailableError: DOCX 没有可读文本时抛出。
    """
    with log_operation(
        "DOCX文本抽取",
        file=filename or "unknown",
        extractor="mammoth",
        emit_start=False,
        failure_level="DEBUG",
    ) as operation:
        try:
            import mammoth
        except ImportError as exc:
            raise DocumentExtractionError("缺少 DOCX 抽取依赖 mammoth") from exc

        data = _read_source_bytes(source)
        _validate_docx_archive(data)
        try:
            result = mammoth.extract_raw_text(io.BytesIO(data))
        except Exception as exc:
            raise DocumentExtractionError(f"DOCX 文本抽取失败: {exc}") from exc
        cleaned = _validate_text_budget(result.value, "docx")
        warnings = tuple(str(message) for message in result.messages)
        operation.add(characters=len(cleaned), warnings=len(warnings))
        return DocumentText(
            text=cleaned,
            document_type="docx",
            extractor="mammoth",
            character_count=len(cleaned),
            warnings=warnings,
        )


def extract_document_text(source: Any, filename: str = "") -> DocumentText:
    """识别真实类型并路由到 PDF/DOCX 文本抽取或图片视觉标记。

    Args:
        source (Any): PDF、DOCX、JPEG 或 PNG 文件路径、字节数据或文件对象。
        filename (str): 用于类型一致性校验和日志展示的文件名。

    Returns:
        DocumentText: PDF、DOCX 或独立图片的统一抽取诊断结果。图片没有文本，
            返回 `extractor=none` 供上层进入视觉解析。

    Raises:
        DocumentExtractionError: 文档类型、内容、安全限制或抽取过程无效时抛出。
        DocumentTextUnavailableError: 文档有效但没有可读文本时抛出。
    """
    with log_operation(
        "Python文本抽取",
        file=filename or "unknown",
        emit_start=False,
        failure_level="DEBUG",
    ) as operation:
        document_type = detect_document_type(source, filename)
        if document_type == "pdf":
            result = extract_pdf_markdown(source, filename)
        elif document_type == "docx":
            result = extract_docx_structured_text(source, filename)
        else:
            image = extract_standalone_image(source, filename)
            result = DocumentText(
                text="",
                document_type="image",
                extractor="none",
                character_count=0,
                page_count=1,
            )
            operation.add(width=image.width, height=image.height)
        operation.add(
            documentType=result.document_type,
            extractor=result.extractor,
            characters=result.character_count,
        )
        return result
