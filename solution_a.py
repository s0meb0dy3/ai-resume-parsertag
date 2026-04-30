"""
方案A：原生多模态 - PDF逐页渲染为图像，整体一次多模态API调用
链路：PDF → 图像列表 → Qwen3.5多模态 → JSON
"""
import json_repair
from openai import OpenAI
from config import MODEL_SERVICE_URL, API_KEY, MODEL_NAME, TEMPERATURE, MAX_TOKENS
from prompts import PROMPT_HR
from pdf_utils import pdf_to_images, pil_to_base64

client = OpenAI(base_url=MODEL_SERVICE_URL, api_key=API_KEY)


def _post_process(data: dict) -> dict:
    """字段类型规范化"""
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


def run(source, verbose: bool = False) -> dict:
    """
    方案A 入口
    source: PDF文件路径(str) 或 字节流(bytes)
    """
    images = pdf_to_images(source)
    if verbose:
        print(f"  [A] 渲染 {len(images)} 页图像")

    # 构建多模态消息内容：所有页图像 + 请求文本
    content = []
    for i, img in enumerate(images):
        if len(images) > 1:
            content.append({"type": "text", "text": f"【第 {i+1}/{len(images)} 页】"})
        content.append({
            "type": "image_url",
            "image_url": {"url": pil_to_base64(img), "detail": "high"}
        })
    content.append({
        "type": "text",
        "text": "请将以上简历内容严格按指定JSON格式输出，直接输出JSON。"
    })

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

    raw = response.choices[0].message.content
    if verbose:
        print(f"  [A] 输出长度: {len(raw)} chars")
    return _post_process(json_repair.loads(raw))


if __name__ == "__main__":
    import sys, json
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/funian/AIAgent/KC/DeepSeek-OCR/uploads/【大模型Agent算法工程师_深圳】曹杨凯 25年应届生.pdf"
    result = run(path, verbose=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
