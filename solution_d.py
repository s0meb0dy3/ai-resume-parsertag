"""
方案D：分页 + 分批次并发处理
链路：PDF → 逐页渲染 → 动态分批 → ThreadPoolExecutor并发推理 → 字段合并 → JSON
"""
import json_repair
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import MODEL_SERVICE_URL, API_KEY, MODEL_NAME, TEMPERATURE, MAX_TOKENS, MAX_WORKERS
from prompts import PROMPT_HR
from pdf_utils import pdf_to_batches, pil_to_base64, get_page_count, load_pdf_bytes, smart_batch_size

client = OpenAI(base_url=MODEL_SERVICE_URL, api_key=API_KEY)

BATCH_SYSTEM = PROMPT_HR + "\n注意：以下是简历的部分页面，提取其中包含的所有信息，不存在的字段填空。"


def _infer_single_batch(batch: list, total_pages: int, batch_idx: int) -> dict:
    """单批次推理"""
    content = []
    for page_idx, img in batch:
        content.append({"type": "text", "text": f"【第 {page_idx+1}/{total_pages} 页】"})
        content.append({
            "type": "image_url",
            "image_url": {"url": pil_to_base64(img), "detail": "high"}
        })
    content.append({"type": "text", "text": "请按指定格式提取以上页面的简历信息，直接输出JSON。"})

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": BATCH_SYSTEM},
                {"role": "user",   "content": content},
            ],
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            extra_body={"enable_thinking": False,
                        "chat_template_kwargs": {"enable_thinking": False}},
        )
        return json_repair.loads(response.choices[0].message.content) or {}
    except Exception as e:
        print(f"  [D] 批次{batch_idx+1} 推理失败: {e}")
        return {}


def _merge_str(a: str, b: str) -> str:
    a, b = (a or "").strip(), (b or "").strip()
    if not a: return b
    if not b: return a
    return a if len(a) >= len(b) else b


def _merge_candidate(a: dict, b: dict) -> dict:
    result = {}
    for k in set(list(a.keys()) + list(b.keys())):
        va, vb = a.get(k, ""), b.get(k, "")
        if k in ("gender", "age"):
            result[k] = va if (va and va != 0) else vb
        else:
            result[k] = _merge_str(str(va), str(vb))
    return result


def _dedup(items: list, keys: list) -> list:
    seen, out = set(), []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = tuple(str(item.get(f, "")).strip() for f in keys)
        if key not in seen and any(k for k in key):
            seen.add(key)
            out.append(item)
    return out


def _merge_results(batch_results: list) -> dict:
    merged = {"candidate": {}, "educations": [], "works": [], "projects": []}
    for r in batch_results:
        if not isinstance(r, dict):
            continue
        if r.get("candidate"):
            merged["candidate"] = _merge_candidate(merged["candidate"], r["candidate"])
        for k in ("educations", "works", "projects"):
            items = r.get(k, [])
            if isinstance(items, list):
                merged[k].extend(items)

    merged["educations"] = _dedup(merged["educations"], ["school", "major", "startDate"])
    merged["works"]      = _dedup(merged["works"],      ["company", "position", "startDate"])
    merged["projects"]   = _dedup(merged["projects"],   ["projectName", "startDate"])
    return merged


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


def run(source, verbose: bool = False) -> dict:
    """
    方案D 入口
    source: PDF文件路径(str) 或 字节流(bytes)
    """
    if isinstance(source, str):
        pdf_bytes = load_pdf_bytes(source)
    else:
        pdf_bytes = source

    total_pages = get_page_count(pdf_bytes)
    batch_sz = smart_batch_size(total_pages)
    batches = pdf_to_batches(pdf_bytes, batch_size=batch_sz)

    if verbose:
        print(f"  [D] {total_pages}页 → {len(batches)}批（每批{batch_sz}页），并发={min(MAX_WORKERS, len(batches))}")

    batch_results = [None] * len(batches)
    workers = min(MAX_WORKERS, len(batches))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_infer_single_batch, batch, total_pages, idx): idx
            for idx, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            idx = futures[future]
            batch_results[idx] = future.result()
            if verbose:
                print(f"  [D] 批次 {idx+1}/{len(batches)} 完成")

    merged = _merge_results([r for r in batch_results if r is not None])
    return _post_process(merged)


if __name__ == "__main__":
    import sys, json
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/funian/AIAgent/KC/DeepSeek-OCR/uploads/【大模型Agent算法工程师_深圳】曹杨凯 25年应届生.pdf"
    result = run(path, verbose=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
