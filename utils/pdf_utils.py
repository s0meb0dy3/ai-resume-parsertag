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
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _open_pdf(source) -> fitz.Document:
    doc = fitz.open(stream=source, filetype="pdf") if isinstance(source, bytes) else fitz.open(source)
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF 已加密，需要密码才能解析")
    return doc


def _render_pages(source) -> list[tuple[int, Image.Image]]:
    """公共渲染基函数：PDF → [(page_idx, PIL Image)]，pdf_to_images / pdf_to_batches 共用。"""
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
    """仅探页数，不渲染图像；用于 parser.run 决策单批/分批路径，避免无效全量渲染。"""
    doc = _open_pdf(source)
    try:
        return doc.page_count
    finally:
        doc.close()


def pdf_to_images(source) -> list[Image.Image]:
    return [img for _, img in _render_pages(source)]


def pdf_to_batches(source, batch_size: int = 1) -> list[list[tuple[int, Image.Image]]]:
    pages = _render_pages(source)
    return [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]


def smart_batch_size(page_count: int) -> int:
    if page_count <= 2:
        return page_count
    elif page_count <= 6:
        return 2
    return 1
