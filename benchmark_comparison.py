"""
四方案对比测试：原始 DeepSeek-OCR vs A_multimodal vs app.py vs 新远程服务
- Original_DeepSeek:  MinIO → OCR → Markdown → LLM → JSON（solution_original.py）
- A_multimodal:       PDF → 图像渲染 → 多模态 LLM → JSON（solution_a.py）
- App_service:        HTTP 调用本地 app.py 服务（默认 localhost:21801）
- New_remote_service: HTTP 调用远程服务 10.2.5.117:21801

测试目录：uploads/test_position

用法：
  python benchmark_comparison.py                         # 默认测试 test_position 所有 PDF
  python benchmark_comparison.py --file 1.pdf             # 指定单个文件
  python benchmark_comparison.py --dir ./pdfs/            # 指定目录
  python benchmark_comparison.py --max-files 10           # 限制测试文件数（默认20）
  python benchmark_comparison.py --skip-original          # 跳过 Original_DeepSeek
  python benchmark_comparison.py --skip-app               # 跳过 App_service
  python benchmark_comparison.py --app-url http://x:port  # 指定 app.py 服务地址
"""
import os
import sys
import json
import time
import base64
import traceback
import requests
from pathlib import Path
from collections import defaultdict

# ── 路径配置 ──────────────────────────────────────────────────
BASE_DIR = os.path.dirname(__file__)
TEST_PDF_DIR = os.path.join(BASE_DIR, "uploads", "test_position")
OUTPUT_DIR = os.path.join(BASE_DIR, "results", "comparison_4way")
os.makedirs(OUTPUT_DIR, exist_ok=True)

MAX_TEST_FILES = 20

# app.py 本地服务地址（可通过 --app-url 覆盖）
APP_SERVICE_URL = os.getenv("APP_SERVICE_URL", "http://localhost:21801/v1/chat/completions")

# 远程服务地址
REMOTE_URL = "http://10.2.5.117:21801/v1/chat/completions"

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer sk-Nj51uMYWCC6bO5qg5aDa0cFa167c4aAaBdDf638f12C49434",
}

# ── 简历分类 ──────────────────────────────────────────────────
EXPLICIT_POSITION_RESUMES = {
    "31岁-5年经验-证券事务代表-猎聘简历.pdf",
    "51job_王运胜_高级天线工程师（GJB装备经验）_深圳(560184912).pdf",
    "【Java开发⼯程师_深圳 18-26K】覃日华 4年.pdf",
    "伍先生-34岁-11年经验-证券事务代表-猎聘简历.pdf",
}
IMPLICIT_POSITION_RESUMES = {
    "PKU刘旗简历.pdf",
    "e0b43fc5-7d60-4922-b223-e3c1b32a298e.pdf",
    "【大模型Agent算法工程师_深圳】曹杨凯 25年应届生.pdf",
    "【大模型Agent算法工程师_深圳】贺远航 25年应届生.pdf",
}


def get_position_category(pdf_name: str) -> str:
    base = os.path.basename(pdf_name)
    if base in EXPLICIT_POSITION_RESUMES:
        return "有明确职位"
    if base in IMPLICIT_POSITION_RESUMES:
        return "无明确职位（需推断）"
    return "未分类"


def pick_test_pdfs(source, max_files: int = MAX_TEST_FILES) -> list:
    def is_valid(p):
        return not str(p).endswith("_layouts.pdf")
    if os.path.isfile(source) and source.endswith(".pdf"):
        return [source] if is_valid(source) else []
    if os.path.isdir(source):
        pdfs = [p for p in sorted(Path(source).glob("*.pdf")) if is_valid(p)]
        return [str(p) for p in pdfs[:max_files]]
    pdfs = [p for p in sorted(Path(TEST_PDF_DIR).glob("*.pdf")) if is_valid(p)]
    return [str(p) for p in pdfs[:max_files]]


# ── 公共：读取 PDF 并编码为 base64 ────────────────────────────

def _pdf_b64(pdf_path: str) -> str:
    with open(pdf_path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _build_payload(pdf_path: str, model: str = "DeepSeek-OCR-JSON") -> dict:
    """构造与 app.py 兼容的请求体"""
    return {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "base64",
                "file": {
                    "id": Path(pdf_path).stem,
                    "type": "base64",
                    "name": os.path.basename(pdf_path),
                    "content": _pdf_b64(pdf_path),
                }
            }]
        }],
        "temperature": 0.01,
        "max_tokens": 4096,
    }


def _call_service(url: str, pdf_path: str, verbose: bool = False) -> dict:
    """通用 HTTP 调用，解析响应中的 JSON 结果"""
    payload = _build_payload(pdf_path)
    t0 = time.time()
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=300)
    resp.raise_for_status()
    data = resp.json()
    raw = data["choices"][0]["message"]["content"]
    if verbose:
        print(f"  耗时: {time.time()-t0:.1f}s, 结果长度: {len(raw)} chars")
    return json.loads(raw)


# ── 方案1：Original_DeepSeek ─────────────────────────────────

def run_original_deepseek(pdf_path: str, verbose: bool = False) -> dict:
    """MinIO → OneAPI(10.2.5.7:13001) → OCR → LLM → JSON"""
    import solution_original
    return solution_original.run(pdf_path, verbose=verbose)


# ── 方案2：A_multimodal ──────────────────────────────────────

def run_a_multimodal(pdf_path: str, verbose: bool = False) -> dict:
    """PDF → 图像 → 多模态 LLM → JSON"""
    import solution_a
    return solution_a.run(pdf_path, verbose=verbose)


# ── 方案3：App_service（本地 app.py）────────────────────────

def run_app_service(pdf_path: str, verbose: bool = False) -> dict:
    """HTTP 调用本地 app.py 服务"""
    try:
        return _call_service(APP_SERVICE_URL, pdf_path, verbose=verbose)
    except requests.exceptions.ConnectionError as e:
        raise ConnectionError(f"app.py 服务不可达 ({APP_SERVICE_URL}): {e}")
    except requests.exceptions.Timeout:
        raise TimeoutError(f"app.py 服务响应超时（300s）")


# ── 方案4：New_remote_service ────────────────────────────────

def run_new_remote(pdf_path: str, verbose: bool = False) -> dict:
    """HTTP 调用远程服务 10.2.5.117:21801"""
    try:
        return _call_service(REMOTE_URL, pdf_path, verbose=verbose)
    except requests.exceptions.ConnectionError as e:
        raise ConnectionError(f"远程服务不可达 ({REMOTE_URL}): {e}")
    except requests.exceptions.Timeout:
        raise TimeoutError(f"远程服务响应超时（300s）")


# ── 方案注册表 ────────────────────────────────────────────────

SOLUTIONS = {
    "Original_DeepSeek":  run_original_deepseek,
    "A_multimodal":       run_a_multimodal,
    "App_service":        run_app_service,
    "New_remote_service": run_new_remote,
}

SOLUTION_LABEL = {
    "Original_DeepSeek":  "Original  (OneAPI 10.2.5.7:13001)",
    "A_multimodal":       "A_multimodal (本地图像)",
    "App_service":        f"App_service  ({APP_SERVICE_URL})",
    "New_remote_service": "New_remote   (10.2.5.117:21801)",
}


# ── 主测试流程 ────────────────────────────────────────────────

def run_one(solution_name: str, fn, pdf_path: str) -> dict:
    record = {
        "solution":          solution_name,
        "pdf":               os.path.basename(pdf_path),
        "position_category": get_position_category(pdf_path),
        "success":           False,
        "elapsed_s":         0.0,
        "result":            None,
        "error":             None,
    }
    t0 = time.time()
    try:
        result = fn(pdf_path, verbose=True)
        record["elapsed_s"] = round(time.time() - t0, 2)
        record["success"]   = True
        record["result"]    = result
    except Exception as e:
        record["elapsed_s"] = round(time.time() - t0, 2)
        record["error"]     = str(e)
        traceback.print_exc()
    return record


def run_benchmark(pdf_paths: list) -> list:
    all_records = []
    total = len(pdf_paths) * len(SOLUTIONS)
    done = 0
    for pdf_path in pdf_paths:
        category = get_position_category(pdf_path)
        print(f"\n{'='*60}")
        print(f"测试文件：{os.path.basename(pdf_path)}")
        print(f"分类：{category}")
        print(f"{'='*60}")
        for name, fn in SOLUTIONS.items():
            done += 1
            print(f"\n[{done}/{total}] 方案 {SOLUTION_LABEL[name]} ...")
            record = run_one(name, fn, str(pdf_path))
            all_records.append(record)
            status = f"✓ {record['elapsed_s']}s" if record["success"] else f"✗ {record['error']}"
            print(f"  → {status}")
    return all_records


def save_results(records: list):
    for rec in records:
        if not rec["success"]:
            continue
        pdf_stem = Path(rec["pdf"]).stem
        fname = f"{pdf_stem}__{rec['solution']}.json"
        out_path = os.path.join(OUTPUT_DIR, fname)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(rec["result"], f, ensure_ascii=False, indent=2)

    summary = [
        {k: v for k, v in r.items() if k != "result"}
        for r in records
    ]
    summary_path = os.path.join(OUTPUT_DIR, "benchmark_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存至: {OUTPUT_DIR}")
    return summary_path


def print_console_stats(records: list):
    print("\n\n" + "="*70)
    print("四方案对比测试完成")
    print("="*70)

    times = defaultdict(list)
    success_count = defaultdict(int)
    for r in records:
        times[r["solution"]].append(r["elapsed_s"])
        if r["success"]:
            success_count[r["solution"]] += 1

    print(f"\n  {'方案':<36} {'成功':>6} {'平均(s)':>8} {'最快(s)':>8} {'最慢(s)':>8}")
    print("  " + "-"*72)
    for name, label in SOLUTION_LABEL.items():
        t_list = times.get(name, [])
        if not t_list:
            continue
        avg = sum(t_list) / len(t_list)
        print(f"  {label:<34} {success_count[name]:>4}/{len(t_list)}  "
              f"{avg:>8.1f} {min(t_list):>8.1f} {max(t_list):>8.1f}")

    # 分类统计
    for category_label in ["有明确职位", "无明确职位（需推断）"]:
        cat_records = [r for r in records if r.get("position_category") == category_label]
        if not cat_records:
            continue
        print(f"\n── 分类：{category_label} ──")
        cat_times = defaultdict(list)
        cat_success = defaultdict(int)
        for r in cat_records:
            cat_times[r["solution"]].append(r["elapsed_s"])
            if r["success"]:
                cat_success[r["solution"]] += 1
        for name, label in SOLUTION_LABEL.items():
            t_list = cat_times.get(name, [])
            if not t_list:
                continue
            avg = sum(t_list) / len(t_list)
            print(f"  {label:<34} {cat_success[name]:>4}/{len(t_list)}  平均:{avg:.1f}s")


# ── 入口 ──────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="四方案对比测试")
    parser.add_argument("--file",         type=str, help="指定单个 PDF")
    parser.add_argument("--dir",          type=str, help="指定 PDF 目录")
    parser.add_argument("--max-files",    type=int, default=MAX_TEST_FILES, help="最大测试文件数")
    parser.add_argument("--skip-original",action="store_true", help="跳过 Original_DeepSeek")
    parser.add_argument("--skip-app",     action="store_true", help="跳过 App_service")
    parser.add_argument("--app-url",      type=str, help="覆盖 app.py 服务地址")
    args = parser.parse_args()

    # 覆盖 app.py 服务地址
    if args.app_url:
        global APP_SERVICE_URL
        APP_SERVICE_URL = args.app_url
        SOLUTION_LABEL["App_service"] = f"App_service  ({APP_SERVICE_URL})"

    # 收集测试文件
    if args.file:
        pdf_paths = pick_test_pdfs(args.file, args.max_files)
    elif args.dir:
        pdf_paths = pick_test_pdfs(args.dir, args.max_files)
    else:
        pdf_paths = pick_test_pdfs(TEST_PDF_DIR, args.max_files)

    if not pdf_paths:
        print(f"未找到 PDF 文件，请检查: {TEST_PDF_DIR}")
        sys.exit(1)

    print(f"测试 PDF: {len(pdf_paths)} 个")
    for p in pdf_paths:
        print(f"  - {os.path.basename(p)}")

    # 移除跳过的方案
    if args.skip_original:
        SOLUTIONS.pop("Original_DeepSeek", None)
        SOLUTION_LABEL.pop("Original_DeepSeek", None)
    if args.skip_app:
        SOLUTIONS.pop("App_service", None)
        SOLUTION_LABEL.pop("App_service", None)

    print(f"测试方案: {list(SOLUTION_LABEL.values())}")

    records = run_benchmark(pdf_paths)
    save_results(records)
    print_console_stats(records)


if __name__ == "__main__":
    main()
