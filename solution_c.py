"""
方案C：直接传入 PDF 文件（base64 整体编码）
链路：PDF → base64 → file 类型消息块 → LLM 原生解析 → JSON
超大/多页 PDF 自动降级到方案D分批处理
"""
import json_repair
from openai import OpenAI
from config import MODEL_SERVICE_URL, API_KEY, MODEL_NAME, TEMPERATURE, MAX_TOKENS, PDF_MAX_PAGES_ONCE
from prompts import PROMPT_HR
from pdf_utils import pdf_to_base64, get_page_count, load_pdf_bytes

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


def _run_direct(source, verbose: bool) -> dict:
    """直传 PDF（file 类型内容块）"""
    b64 = pdf_to_base64(source)
    if verbose:
        size_kb = len(b64) * 3 // 4 / 1024
        print(f"  [C] 直传PDF，编码大小: {size_kb:.0f} KB")

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": PROMPT_HR},
            {
                "role": "user",
                "content": [
                    {
                        "type": "file",
                        "file": {
                            "filename": "resume.pdf",
                            "file_data": f"data:application/pdf;base64,{b64}"
                        }
                    },
                    {
                        "type": "text",
                        "text": "请将以上简历按指定JSON格式输出，直接输出JSON。"
                    }
                ]
            }
        ],
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        extra_body={"enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False}},
    )
    return json_repair.loads(response.choices[0].message.content)


def run(source, verbose: bool = False) -> dict:
    """
    方案C 入口
    source: PDF文件路径(str) 或 字节流(bytes)
    """
    if isinstance(source, str):
        pdf_bytes = load_pdf_bytes(source)
    else:
        pdf_bytes = source

    pages = get_page_count(pdf_bytes)
    if verbose:
        print(f"  [C] PDF共 {pages} 页")

    if pages > PDF_MAX_PAGES_ONCE:
        if verbose:
            print(f"  [C] 超过 {PDF_MAX_PAGES_ONCE} 页，降级到方案D分批")
        from solution_d import run as run_d
        return run_d(pdf_bytes, verbose=verbose)

    try:
        result = _run_direct(pdf_bytes, verbose)
        # 若模型不支持 file 类型，捕获异常降级图像通道
    except Exception as e:
        if verbose:
            print(f"  [C] file类型不支持，降级图像通道: {e}")
        from solution_a import run as run_a
        return run_a(pdf_bytes, verbose=verbose)

    return _post_process(result)


if __name__ == "__main__":
    import sys, json
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/funian/AIAgent/KC/DeepSeek-OCR/uploads/【大模型Agent算法工程师_深圳】曹杨凯 25年应届生.pdf"
    result = run(path, verbose=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
