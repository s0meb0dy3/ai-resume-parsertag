"""
PDF 处理工具：渲染、文本提取、类型检测
"""
import base64
import io
import fitz                     # PyMuPDF
import pdfplumber
from PIL import Image
from config import PDF_DPI, DIGITAL_CHAR_THRESH


# ── 类型检测 ─────────────────────────────────────────────────

def detect_pdf_type(source) -> str:
    """
    检测 PDF 类型：digital（数字化）或 scanned（扫描件）
    source: 文件路径(str) 或 字节流(bytes)
    """
    try:
        if isinstance(source, bytes):
            doc = fitz.open(stream=source, filetype="pdf")
        else:
            doc = fitz.open(source)

        total_chars = 0
        sample = min(doc.page_count, 3)
        for i in range(sample):
            total_chars += len(doc[i].get_text("text").strip())
        doc.close()

        avg = total_chars / max(sample, 1)
        return "digital" if avg > DIGITAL_CHAR_THRESH else "scanned"
    except Exception:
        return "scanned"


def get_page_count(source) -> int:
    """获取 PDF 页数"""
    if isinstance(source, bytes):
        doc = fitz.open(stream=source, filetype="pdf")
    else:
        doc = fitz.open(source)
    n = doc.page_count
    doc.close()
    return n


# ── 文本提取（数字化 PDF 快速通道）─────────────────────────────

def extract_text(source) -> str:
    """
    数字化 PDF 直接提取文本（pdfplumber 更准确保留表格结构）
    source: 文件路径(str) 或 字节流(bytes)
    """
    try:
        if isinstance(source, bytes):
            buf = io.BytesIO(source)
            with pdfplumber.open(buf) as pdf:
                pages_text = []
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        pages_text.append(f"--- 第{i+1}页 ---\n{text.strip()}")
                return "\n\n".join(pages_text)
        else:
            with pdfplumber.open(source) as pdf:
                pages_text = []
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        pages_text.append(f"--- 第{i+1}页 ---\n{text.strip()}")
                return "\n\n".join(pages_text)
    except Exception as e:
        print(f"[pdf_utils] 文本提取失败: {e}")
        return ""


# ── 图像渲染 ─────────────────────────────────────────────────

def render_page(page, dpi: int = PDF_DPI) -> Image.Image:
    """单页渲染为 PIL Image（内存流，不落盘）"""
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    buf = io.BytesIO(pix.tobytes("png"))
    return Image.open(buf).copy()


def pdf_to_images(source, dpi: int = PDF_DPI) -> list:
    """
    PDF → PIL Image 列表（每页一张）
    source: 文件路径(str) 或 字节流(bytes)
    """
    if isinstance(source, bytes):
        doc = fitz.open(stream=source, filetype="pdf")
    else:
        doc = fitz.open(source)

    images = [render_page(doc[i], dpi) for i in range(doc.page_count)]
    doc.close()
    return images


def pdf_to_batches(source, batch_size: int = 2, dpi: int = PDF_DPI) -> list:
    """
    PDF → 分批的 [(page_idx, PIL Image), ...] 列表
    Returns: list of batches
    """
    if isinstance(source, bytes):
        doc = fitz.open(stream=source, filetype="pdf")
    else:
        doc = fitz.open(source)

    all_pages = [(i, render_page(doc[i], dpi)) for i in range(doc.page_count)]
    doc.close()

    return [all_pages[i:i + batch_size] for i in range(0, len(all_pages), batch_size)]


# ── base64 编码 ──────────────────────────────────────────────

def pil_to_base64(img: Image.Image) -> str:
    """PIL Image → base64 data URI（PNG）"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def pdf_to_base64(source) -> str:
    """PDF 文件或字节 → base64 字符串"""
    if isinstance(source, bytes):
        data = source
    else:
        with open(source, "rb") as f:
            data = f.read()
    return base64.b64encode(data).decode()


def load_pdf_bytes(path: str) -> bytes:
    """文件路径 → bytes"""
    with open(path, "rb") as f:
        return f.read()


# ── 动态批次大小 ─────────────────────────────────────────────

def smart_batch_size(page_count: int) -> int:
    """根据页数动态决定每批大小"""
    if page_count <= 2:
        return page_count
    elif page_count <= 6:
        return 2
    else:
        return 1
