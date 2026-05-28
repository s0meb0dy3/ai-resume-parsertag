"""
文件获取工具模块。

职责：
  - download_file             : 从 HTTP URL 下载 PDF 并校验魔数，保存到 UPLOAD_DIR
  - get_pdf_source_from_content: 从 OpenAI 兼容消息的 content 数组中提取 PDF 来源
      支持 type=url / file / base64 三种方式，统一返回 (source, filename) 元组
"""
import base64
from pathlib import Path

import json_repair
import requests

from config import UPLOAD_DIR, DOWNLOAD_TIMEOUT
from logger_manager import logger


def download_file(url: str, filename: str) -> str:
    response = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
    response.raise_for_status()
    content = response.content
    ct = response.headers.get("Content-Type", "")
    logger.debug(f"[download] status={response.status_code} Content-Type={ct!r} size={len(content)}")
    # 用 PDF 魔数校验，防止 MinIO 返回 HTML 错误页被当成 PDF
    if not content.startswith(b"%PDF"):
        preview = content[:200].decode("utf-8", errors="replace")
        raise ValueError(
            f"下载内容非有效 PDF (Content-Type: {ct!r}, size: {len(content)}, preview: {preview!r})"
        )
    save_path = UPLOAD_DIR / Path(filename).name  # 只取文件名，防止路径穿越
    with open(save_path, "wb") as f:
        f.write(content)
    return str(save_path)


def get_pdf_source_from_content(content_items):
    content_json = {}
    for content in (content_items if isinstance(content_items, list) else [content_items]):
        item = json_repair.loads(content) if isinstance(content, str) else content
        if isinstance(item, dict) and item.get("type", "text") != "text":
            content_json = item
            break

    if not content_json:
        return None, None

    ctype     = content_json.get("type", "")
    file_data = content_json.get("file", {})
    filename  = file_data.get("name", "resume.pdf")

    if ctype == "url":
        source = download_file(file_data.get("path", ""), filename)
    elif ctype == "file":
        source = file_data.get("path", "")
    elif ctype == "base64":
        source = base64.b64decode(file_data.get("content", ""))
    else:
        source = None

    return source, filename
