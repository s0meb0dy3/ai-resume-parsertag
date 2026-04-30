"""
方案B：PDF 二进制流 + 双通道路由
- 数字化PDF → pdfplumber 文本提取 → 纯文本LLM（快速通道 <1s）
- 扫描件PDF → fitz 内存渲染图像 → 多模态LLM（完整通道）
全程内存流，无临时文件落盘
"""
import json_repair
from openai import OpenAI
from config import MODEL_SERVICE_URL, API_KEY, MODEL_NAME, TEMPERATURE, MAX_TOKENS
from prompts import PROMPT_HR
from pdf_utils import detect_pdf_type, extract_text, pdf_to_images, pil_to_base64, load_pdf_bytes

client = OpenAI(base_url=MODEL_SERVICE_URL, api_key=API_KEY)


def _post_process(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    c = data.get("candidate", {})
    if c:
        g = c.get("gender", "")
        if g == "男":       c["gender"] = 1
        elif g == "女":     c["gender"] = 2
        else:               c["gender"] = int(g) if str(g).isdigit() else 0
        age = str(c.get("age", "0")).strip()
        c["age"] = int(age) if age.isdigit() else 0
        c.setdefault("other", "")
    return data


def _run_text_channel(text: str, verbose: bool) -> dict:
    """快速通道：纯文本 → LLM"""
    if verbose:
        print(f"  [B] 文本通道，文本长度: {len(text)} chars")
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": PROMPT_HR},
            {"role": "user",   "content": text},
        ],
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        extra_body={"enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False}},
    )
    return json_repair.loads(response.choices[0].message.content)


def _run_image_channel(source, verbose: bool) -> dict:
    """完整通道：图像 → 多模态LLM"""
    images = pdf_to_images(source)
    if verbose:
        print(f"  [B] 图像通道，渲染 {len(images)} 页")
    content = []
    for i, img in enumerate(images):
        if len(images) > 1:
            content.append({"type": "text", "text": f"【第 {i+1}/{len(images)} 页】"})
        content.append({
            "type": "image_url",
            "image_url": {"url": pil_to_base64(img), "detail": "high"}
        })
    content.append({"type": "text", "text": "请将以上简历按指定JSON格式输出，直接输出JSON。"})
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": PROMPT_HR},
            {"role": "user",   "content": content},
        ],
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        extra_body={"enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False}},
    )
    return json_repair.loads(response.choices[0].message.content)


def run(source, verbose: bool = False) -> dict:
    """
    方案B 入口
    source: PDF文件路径(str) 或 字节流(bytes)
    """
    # 统一转为 bytes，全程内存
    if isinstance(source, str):
        pdf_bytes = load_pdf_bytes(source)
    else:
        pdf_bytes = source

    pdf_type = detect_pdf_type(pdf_bytes)
    if verbose:
        print(f"  [B] PDF类型: {pdf_type}")

    if pdf_type == "digital":
        text = extract_text(pdf_bytes)
        if text.strip():
            result = _run_text_channel(text, verbose)
        else:
            # 文本提取为空则降级图像通道
            if verbose:
                print("  [B] 文本为空，降级图像通道")
            result = _run_image_channel(pdf_bytes, verbose)
    else:
        result = _run_image_channel(pdf_bytes, verbose)

    return _post_process(result)


if __name__ == "__main__":
    import sys, json
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/funian/AIAgent/KC/DeepSeek-OCR/uploads/【大模型Agent算法工程师_深圳】曹杨凯 25年应届生.pdf"
    result = run(path, verbose=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
