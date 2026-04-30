"""
基准测试主程序：对同一批PDF分别运行五个方案，收集结果与耗时。
测试集覆盖两类简历：
  - 「有明确职位」：平台结构化简历（猎聘/51job），position/role 字段原文清晰
  - 「无明确职位」：应届生/学术简历，position/role 需由描述推断

用法：
  python benchmark.py                        # 自动选取 TEST_PDF_DIR 所有简历
  python benchmark.py path/to/resume.pdf     # 指定单份
  python benchmark.py dir/with/pdfs/         # 指定目录

注意：Original_DeepSeek 方案需要 DeepSeek-OCR 服务运行在 DEEPSEEK_OCR_URL，
      若服务不可达将记录为失败（不影响其余方案测试）。
"""
import os
import sys
import json
import time
import traceback
from pathlib import Path
from config import TEST_PDF_DIR, OUTPUT_DIR

import solution_original
import solution_a
import solution_b
import solution_c
import solution_d

SOLUTIONS = {
    "Original_DeepSeek": solution_original.run,
    "A_multimodal":      solution_a.run,
    "C_direct_pdf":      solution_c.run,
}

MAX_TEST_FILES = 20   # 最多测试几份简历

# ── 简历分类标注 ────────────────────────────────────────────────
# 有明确职位：平台结构化简历，position/role 字段原文已清晰标注
EXPLICIT_POSITION_RESUMES = {
    "31岁-5年经验-证券事务代表-猎聘简历.pdf",
    "51job_王运胜_高级天线工程师（GJB装备经验）_深圳(560184912).pdf",
    "【Java开发⼯程师_深圳 18-26K】覃日华 4年.pdf",
    "伍先生-34岁-11年经验-证券事务代表-猎聘简历.pdf",
}
# 无明确职位：应届生/学术/描述型简历，position/role 需推断
IMPLICIT_POSITION_RESUMES = {
    "PKU刘旗简历.pdf",
    "e0b43fc5-7d60-4922-b223-e3c1b32a298e.pdf",
    "【大模型Agent算法工程师_深圳】曹杨凯 25年应届生.pdf",
    "【大模型Agent算法工程师_深圳】贺远航 25年应届生.pdf",
}


def get_position_category(pdf_name: str) -> str:
    """返回简历的职位字段分类标签"""
    base = os.path.basename(pdf_name)
    if base in EXPLICIT_POSITION_RESUMES:
        return "有明确职位"
    if base in IMPLICIT_POSITION_RESUMES:
        return "无明确职位（需推断）"
    return "未分类"


def pick_test_pdfs(source) -> list:
    """根据入参选取测试PDF列表（自动排除 _layouts.pdf 可视化文件）"""
    def is_valid(p):
        return not str(p).endswith("_layouts.pdf")

    if os.path.isfile(source) and source.endswith(".pdf"):
        return [source] if is_valid(source) else []
    if os.path.isdir(source):
        pdfs = [p for p in sorted(Path(source).glob("*.pdf")) if is_valid(p)]
        return [str(p) for p in pdfs[:MAX_TEST_FILES]]
    # 默认
    pdfs = [p for p in sorted(Path(TEST_PDF_DIR).glob("*.pdf")) if is_valid(p)]
    return [str(p) for p in pdfs[:MAX_TEST_FILES]]


def run_one(solution_name: str, fn, pdf_path: str) -> dict:
    """运行单个方案，返回结果记录"""
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
    """对所有PDF运行所有方案，返回记录列表"""
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
            print(f"\n[{done}/{total}] 方案 {name} ...")
            record = run_one(name, fn, str(pdf_path))
            all_records.append(record)
            status = f"✓ {record['elapsed_s']}s" if record["success"] else f"✗ {record['error']}"
            print(f"  → {status}")
    return all_records


def save_results(records: list):
    """将每份简历每个方案的结果保存为独立JSON"""
    for rec in records:
        if not rec["success"]:
            continue
        pdf_stem = Path(rec["pdf"]).stem
        fname = f"{pdf_stem}__{rec['solution']}.json"
        out_path = os.path.join(OUTPUT_DIR, fname)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(rec["result"], f, ensure_ascii=False, indent=2)

    # 保存汇总性能记录（不含完整result，只含元数据）
    summary = [
        {k: v for k, v in r.items() if k != "result"}
        for r in records
    ]
    summary_path = os.path.join(OUTPUT_DIR, "benchmark_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存至: {OUTPUT_DIR}")
    print(f"汇总文件: {summary_path}")
    return summary_path


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        pdf_paths = pick_test_pdfs(TEST_PDF_DIR)
    elif len(args) == 1:
        pdf_paths = pick_test_pdfs(args[0])
    else:
        # 多个文件/目录参数
        pdf_paths = []
        for a in args:
            pdf_paths.extend(pick_test_pdfs(a))

    if not pdf_paths:
        print(f"未找到PDF文件，请检查路径: {args or TEST_PDF_DIR}")
        sys.exit(1)

    print(f"测试PDF数量: {len(pdf_paths)}")
    print(f"测试方案: {list(SOLUTIONS.keys())}")

    records = run_benchmark(pdf_paths)
    summary_path = save_results(records)

    # 输出简要统计
    from collections import defaultdict
    print("\n\n" + "="*60)
    print("基准测试完成，速度汇总（全部简历）：")
    print("="*60)
    times = defaultdict(list)
    success_count = defaultdict(int)
    for r in records:
        times[r["solution"]].append(r["elapsed_s"])
        if r["success"]:
            success_count[r["solution"]] += 1

    for name in SOLUTIONS:
        t_list = times[name]
        if t_list:
            avg = sum(t_list) / len(t_list)
            print(f"  {name:<22} 成功:{success_count[name]}/{len(t_list)}  "
                  f"平均:{avg:.1f}s  最快:{min(t_list):.1f}s  最慢:{max(t_list):.1f}s")

    # 分类统计：有明确职位 vs 无明确职位
    for category_label in ["有明确职位", "无明确职位（需推断）"]:
        cat_records = [r for r in records if r.get("position_category") == category_label]
        if not cat_records:
            continue
        print(f"\n── 分类：{category_label} ──")
        pdfs_in_cat = sorted({r["pdf"] for r in cat_records})
        print(f"   简历: {', '.join(pdfs_in_cat)}")
        cat_times = defaultdict(list)
        cat_success = defaultdict(int)
        for r in cat_records:
            cat_times[r["solution"]].append(r["elapsed_s"])
            if r["success"]:
                cat_success[r["solution"]] += 1
        for name in SOLUTIONS:
            t_list = cat_times[name]
            if t_list:
                avg = sum(t_list) / len(t_list)
                print(f"  {name:<22} 成功:{cat_success[name]}/{len(t_list)}  平均:{avg:.1f}s")

    print(f"\n运行 compare_report.py 生成详细对比报告")
