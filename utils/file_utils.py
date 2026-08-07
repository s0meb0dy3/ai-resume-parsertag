"""获取、下载并归一 PDF、DOCX 和图片简历来源。

本模块支持 OpenAI 兼容内容中的 `url`、`file` 和 `base64` 来源。网络传输 URL
保持原值，仅对日志、保存路径和结果文件名执行一次百分号解码及基础名清理，避免
中文文件名不可读、重复解码和路径穿越。
"""
import base64
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import json_repair
import requests

from config import DOWNLOAD_TIMEOUT, MAX_FILE_SIZE_MB, UPLOAD_DIR
from logger.logger_manager import logger
from logger.operation import log_operation


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


class _NormalizedFilename(str):
    """标记已经过单次 URL 解码的内部文件名，防止跨层重复解码。"""


def normalize_filename(filename: str, default: str = "resume.pdf") -> str:
    """对展示文件名解码一次，并移除路径和日志注入字符。

    Args:
        filename (str): 原始文件名、URL 路径或完整 HTTP URL。
        default (str): 输入清理后为空时使用的默认文件名。

    Returns:
        str: 最长 180 字符的安全基础名；返回值携带内部已归一标记。

    Note:
        该处理不修改实际下载 URL，也不会对已归一结果执行第二次百分号解码。
    """
    raw = str(filename or default)
    decoded = raw if isinstance(filename, _NormalizedFilename) else unquote(raw)
    if decoded.startswith("/") or urlsplit(decoded).scheme in {"http", "https"}:
        decoded = urlsplit(decoded).path
    decoded = decoded.replace("\\", "/").rsplit("/", 1)[-1]
    decoded = _CONTROL_RE.sub(" ", decoded)
    decoded = re.sub(r"\s+", " ", decoded).strip(" .")
    return _NormalizedFilename(decoded[:180] or default)


def download_document_file(url: str, filename: str) -> str:
    """按调用方原始 URL 下载并校验支持的简历文件。

    Args:
        url (str): 调用方提供的 HTTP 或 HTTPS 文件地址。
        filename (str): 保存和日志使用的文件名；目录部分会被移除。

    Returns:
        str: 下载文件在 `UPLOAD_DIR` 中的本地路径。

    Raises:
        requests.RequestException: 下载超时、连接失败或 HTTP 状态异常时抛出。
        ValueError: 响应体超过大小限制或不属于有效 PDF、DOCX、JPEG、PNG 时抛出。
        OSError: 目标文件无法写入时抛出。

    Note:
        该函数会产生本地文件副作用，并使用 `filename` 的基础名防止
        目标路径穿越。
    """
    display_filename = normalize_filename(filename)
    with log_operation(
        "下载简历文档",
        file=display_filename,
        emit_start=False,
        failure_level="DEBUG",
    ) as operation:
        response = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
        response.raise_for_status()
        content = response.content
        if len(content) > MAX_FILE_SIZE_MB * 1024 * 1024:
            raise ValueError(f"下载文档超过 {MAX_FILE_SIZE_MB}MB 限制")
        content_type = response.headers.get("Content-Type", "")
        # 下载完成后复用解析入口的真实类型和资源预算，避免 URL 路径绕过校验。
        from core.document_text import detect_document_type

        document_type = detect_document_type(content, display_filename)
        save_path = UPLOAD_DIR / display_filename
        with open(save_path, "wb") as file:
            file.write(content)
        operation.add(
            httpStatus=response.status_code,
            contentType=content_type,
            sizeBytes=len(content),
            documentType=document_type,
        )
        return str(save_path)


def download_file(url: str, filename: str) -> str:
    """通过旧下载函数名调用通用简历文件下载逻辑。

    Args:
        url (str): 调用方提供的 HTTP 或 HTTPS 文件地址。
        filename (str): 保存和日志使用的文件名。

    Returns:
        str: 下载文件在 `UPLOAD_DIR` 中的本地路径。
    """
    return download_document_file(url, filename)


def get_document_source_from_content(content_items):
    """从 OpenAI 兼容内容中获取第一个受支持的简历来源。

    Args:
        content_items (Any): 单个条目或条目列表；字符串条目先通过
            `json_repair` 解析。

    Returns:
        tuple[Any | None, str | None]: PDF、DOCX 或图片来源和文件名。URL 返回本地
            路径，`file` 返回原路径，`base64` 返回字节；无可用条目时
            返回 `(None, None)`。

    Raises:
        ValueError: JSON 字符串或 base64 内容无法解析时抛出。
        requests.RequestException: URL 来源下载失败时抛出。
    """
    items = content_items if isinstance(content_items, list) else [content_items]
    with log_operation(
        "获取简历文档来源",
        items=len(items),
        level="DEBUG",
        failure_level="DEBUG",
        emit_start=False,
    ) as operation:
        content_json = {}
        for content in items:
            item = json_repair.loads(content) if isinstance(content, str) else content
            if isinstance(item, dict) and item.get("type", "text") != "text":
                content_json = item
                break

        if not content_json:
            operation.add(found=False)
            return None, None

        ctype = content_json.get("type", "")
        file_data = content_json.get("file", {})
        filename = normalize_filename(file_data.get("name", "resume.pdf"))

        if ctype == "url":
            source = download_document_file(file_data.get("path", ""), filename)
        elif ctype == "file":
            source = file_data.get("path", "")
            if not source or not Path(source).is_file():
                raise ValueError(f"本地文档不存在: {source!r}")
        elif ctype == "base64":
            try:
                source = base64.b64decode(file_data.get("content", ""), validate=True)
            except Exception as exc:
                raise ValueError("base64 文档内容无效") from exc
        else:
            source = None

        operation.add(found=source is not None, sourceType=ctype or "unknown", file=filename)
        return source, filename


def get_pdf_source_from_content(content_items):
    """通过旧 PDF 来源函数名调用通用简历来源解析逻辑。

    Args:
        content_items: 单个 OpenAI 兼容内容条目或条目列表。

    Returns:
        tuple[Any | None, str | None]: 文档来源和归一后的文件名。
    """
    return get_document_source_from_content(content_items)
