"""
PDF 渲染工具模块。

职责：
  _open_pdf      : 统一打开 PDF（支持文件路径和 bytes），处理加密校验
  _render_pages  : 公共渲染基函数 → [(page_idx, PIL Image)]，两路径共用
  pdf_to_images  : PDF → PIL Image 列表（单次调用路径）
  pdf_to_batches : PDF → [(page_idx, PIL Image)] 分批列表（多批并发路径）
  get_page_count : 仅探页数，不渲染（用于决策单批/分批，避免无效渲染）
  smart_batch_size: 根据总页数动态计算每批页数
  pil_to_base64  : PIL Image → data URI（Base64 PNG）
"""
import io
import base64

import fitz
from PIL import Image

from config import PDF_DPI


def pil_to_base64(img: Image.Image) -> str:
    """将 PIL 图像编码为 OpenAI 多模态消息使用的 PNG data URI。

    Args:
        img (Image.Image): 已渲染的 PDF 页面图像。

    Returns:
        str: `data:image/png;base64,` 前缀的 base64 图像字符串。
    """
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _open_pdf(source) -> fitz.Document:
    """从字节或本地路径打开未加密 PDF。

    Args:
        source (bytes | str | Path): PDF 字节或 PyMuPDF 支持的本地路径。

    Returns:
        fitz.Document: 由调用方关闭的 PDF 文档句柄。

    Raises:
        ValueError: PDF 需要密码时抛出。
        fitz.FileDataError: 输入不是可解析 PDF 时抛出。
    """
    doc = fitz.open(stream=source, filetype="pdf") if isinstance(source, bytes) else fitz.open(source)
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF 已加密，需要密码才能解析")
    return doc


def _render_pages(source) -> list[tuple[int, Image.Image]]:
    """以 `PDF_DPI` 渲染 PDF 的全部页面并保留原始页索引。

    Args:
        source (bytes | str | Path): PDF 字节或本地路径。

    Returns:
        list[tuple[int, Image.Image]]: 从 0 开始的页索引和独立 PIL 图像。

    Raises:
        ValueError: PDF 已加密时抛出。
        fitz.FileDataError: PDF 无法打开或渲染时抛出。

    Note:
        所有页面会在返回前一次性渲染到内存。
    """
    doc = _open_pdf(source)
    try:
        # PDF 坐标系以 72 DPI（1点 = 1/72英寸）为基准；zoom 换算为目标分辨率
        zoom = PDF_DPI / 72.0
        mat  = fitz.Matrix(zoom, zoom)
        return [
            # alpha=False：跳过 Alpha 通道，节省约 25% 内存且 LLM 不需要透明度信息
            # .copy()：doc.close() 会释放 pixmap 内存，PIL Image 必须持有独立副本才能在 finally 后继续使用
            (i, Image.open(io.BytesIO(doc[i].get_pixmap(matrix=mat, alpha=False).tobytes("png"))).copy())
            for i in range(doc.page_count)
        ]
    finally:
        doc.close()


def get_page_count(source) -> int:
    """读取 PDF 页数而不渲染图像。

    Args:
        source (bytes | str | Path): PDF 字节或本地路径。

    Returns:
        int: PDF 总页数，用于选择单批或分批推理路径。
    """
    doc = _open_pdf(source)
    try:
        return doc.page_count
    finally:
        doc.close()


def pdf_to_images(source) -> list[Image.Image]:
    """将 PDF 全部页面转换为按页序排列的 PIL 图像。

    Args:
        source (bytes | str | Path): PDF 字节或本地路径。

    Returns:
        list[Image.Image]: 不包含页索引的页面图像列表。
    """
    return [img for _, img in _render_pages(source)]


def pdf_to_batches(source, batch_size: int = 1) -> list[list[tuple[int, Image.Image]]]:
    """渲染 PDF 全部页面并按固定页数切分批次。

    Args:
        source (bytes | str | Path): PDF 字节或本地路径。
        batch_size (int): 每个批次的最大页数。

    Returns:
        list[list[tuple[int, Image.Image]]]: 保留原始页索引的批次列表。
    """
    pages = _render_pages(source)
    return [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]


def smart_batch_size(page_count: int) -> int:
    """根据 PDF 总页数选择长文档推理的每批页数。

    Args:
        page_count (int): PDF 总页数。

    Returns:
        int: 1–2 页时返回原页数，3–6 页返回 2，更长文档返回 1。
    """
    if page_count <= 2:
        return page_count
    elif page_count <= 6:
        return 2
    return 1
