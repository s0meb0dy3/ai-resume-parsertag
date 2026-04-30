"""
四方案对比报告生成器
读取 benchmark_comparison.py 产出的 JSON 文件，生成详细 Markdown 报告。

用法：
  python report_comparison.py                        # 读取默认 OUTPUT_DIR
  python report_comparison.py path/to/results/      # 指定目录
"""
import os
import sys
import json
from pathlib import Path
from collections import defaultdict

BASE_DIR = os.path.dirname(__file__)
OUTPUT_DIR = os.path.join(BASE_DIR, "results", "comparison_4way")

SOLUTION_ORDER = ["Original_DeepSeek", "A_multimodal", "C_direct_pdf", "New_remote_service"]
SOLUTION_LABEL = {
    "Original_DeepSeek":  "Original (10.2.5.117:21801)",
    "A_multimodal":       "A_multimodal (本地图像)",
    "C_direct_pdf":       "C_direct_pdf (base64直传)",
    "New_remote_service": "New_remote (10.2.5.117:21801)",
}
SOLUTION_LINK = {
    "Original_DeepSeek":  "MinIO → OCR(vLLM) → Markdown → LLM → JSON",
    "A_multimodal":       "PDF → 图像渲染 → 多模态LLM → JSON",
    "C_direct_pdf":       "PDF → base64 → LLM原生解析 → JSON",
    "New_remote_service": "PDF → 图像 → 多模态LLM → JSON (远程服务)",
}

CANDIDATE_REQUIRED = ["name", "phone"]
CANDIDATE_OPTIONAL = ["email", "gender", "age", "address", "nativePlace",
                       "selfEvaluation", "expectedSalary", "expectedPosition"]


def completeness_score(result: dict) -> dict:
    scores = {}
    c = result.get("candidate", {})
    req_filled = sum(1 for f in CANDIDATE_REQUIRED if c.get(f, ""))
    scores["required_fields"] = f"{req_filled}/{len(CANDIDATE_REQUIRED)}"
    opt_filled = sum(1 for f in CANDIDATE_OPTIONAL if c.get(f, "") and c.get(f, 0) != 0)
    scores["optional_fields"] = f"{opt_filled}/{len(CANDIDATE_OPTIONAL)}"
    works = result.get("works", [])
    scores["works_count"] = len(works)
    pos_filled = sum(1 for w in works if w.get("position", "").strip()
                     and w.get("position") not in ("职位/职责", "详见描述"))
    scores["position_filled"] = f"{pos_filled}/{len(works)}" if works else "N/A"
    projects = result.get("projects", [])
    scores["projects_count"] = len(projects)
    scores["educations_count"] = len(result.get("educations", []))
    total = 0
    total += 30 if req_filled == len(CANDIDATE_REQUIRED) else req_filled * 15
    total += min(opt_filled * 5, 30)
    total += min(scores["works_count"] * 5, 20)
    total += min(scores["educations_count"] * 5, 10)
    if works:
        total += int(pos_filled / len(works) * 10)
    scores["total_score"] = min(total, 100)
    return scores


def eval_position_quality(position: str) -> str:
    INVALID = {"职位/职责", "详见描述", "", "职责"}
    KEYWORDS = ["工程师", "经理", "负责人", "研究员", "分析师", "专员",
                "主任", "总监", "架构师", "开发", "运维", "测试", "产品",
                "研究生", "实习", "顾问", "科学家"]
    if not position or position.strip() in INVALID:
        return "空/无效"
    if any(kw in position for kw in KEYWORDS):
        return "优" if len(position) <= 30 else "良（偏长）"
    return "中（缺少关键词）"


def load_results(result_dir: str) -> dict:
    data = defaultdict(dict)
    for p in Path(result_dir).glob("*__*.json"):
        if p.name == "benchmark_summary.json":
            continue
        parts = p.stem.split("__")
        if len(parts) != 2:
            continue
        pdf_stem, solution = parts
        with open(p, encoding="utf-8") as f:
            try:
                data[pdf_stem][solution] = json.load(f)
            except Exception:
                pass
    return dict(data)


def load_summary(result_dir: str) -> list:
    path = os.path.join(result_dir, "benchmark_summary.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def generate_report(all_data: dict, summary: list, output_path: str):
    lines = []
    lines.append("# 四方案简历解析对比报告\n")
    lines.append(f"> 生成时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"> 测试简历数：{len(all_data)}\n")
    lines.append(f"> 远程服务：`http://10.2.5.117:21801`\n")
    lines.append("\n---\n")

    # 方案概览
    lines.append("## 方案概览\n")
    lines.append("| 方案 | 链路 |")
    lines.append("|------|------|")
    for sol in SOLUTION_ORDER:
        if sol in all_data.get(next(iter(all_data)), {}) or any(sol in v for v in all_data.values()):
            lines.append(f"| {SOLUTION_LABEL[sol]} | {SOLUTION_LINK[sol]} |")
    lines.append("")

    # 一、速度汇总
    lines.append("## 一、速度汇总\n")
    lines.append("| 方案 | 成功数 | 平均耗时(s) | 最快(s) | 最慢(s) |")
    lines.append("|------|--------|------------|--------|--------|")
    times = defaultdict(list)
    success_count = defaultdict(int)
    for rec in summary:
        if rec.get("success"):
            times[rec["solution"]].append(rec["elapsed_s"])
            success_count[rec["solution"]] += 1
    for sol in SOLUTION_ORDER:
        t_list = times.get(sol, [])
        if not t_list:
            continue
        avg = sum(t_list) / len(t_list)
        lines.append(f"| {SOLUTION_LABEL[sol]} | {len(t_list)} | "
                     f"{avg:.1f} | {min(t_list):.1f} | {max(t_list):.1f} |")
    lines.append("")

    # 二、字段完整性
    lines.append("## 二、字段完整性对比\n")
    for pdf_stem, solutions in all_data.items():
        lines.append(f"### {pdf_stem}\n")
        lines.append("| 方案 | 综合分 | 必填字段 | 可选字段 | 工作数 | 项目数 | 教育数 | position完整率 |")
        lines.append("|------|--------|---------|---------|-------|-------|-------|--------------|")
        for sol in SOLUTION_ORDER:
            if sol not in solutions:
                continue
            sc = completeness_score(solutions[sol])
            lines.append(
                f"| {SOLUTION_LABEL[sol]} | **{sc['total_score']}** | "
                f"{sc['required_fields']} | {sc['optional_fields']} | "
                f"{sc['works_count']} | {sc['projects_count']} | "
                f"{sc['educations_count']} | {sc['position_filled']} |"
            )
        lines.append("")

    # 三、position/role 对比
    lines.append("## 三、position / role 字段对比\n")
    for pdf_stem, solutions in all_data.items():
        lines.append(f"### {pdf_stem}\n")
        # works
        all_companies = []
        seen = set()
        for sol in SOLUTION_ORDER:
            for w in solutions.get(sol, {}).get("works", []):
                c = w.get("company", "").strip()
                if c and c not in seen:
                    all_companies.append(c)
                    seen.add(c)
        if all_companies:
            lines.append("**工作经历：**\n")
            for sol in SOLUTION_ORDER:
                if sol not in solutions:
                    continue
                lines.append(f"**{SOLUTION_LABEL[sol]}**\n")
                for w in solutions[sol].get("works", []):
                    pos = w.get("position", "—")
                    q = eval_position_quality(pos)
                    lines.append(f"- {w.get('company', '?')} → `{pos}` ({q})")
                lines.append("")

        # projects
        all_projects = []
        seen = set()
        for sol in SOLUTION_ORDER:
            for p in solutions.get(sol, {}).get("projects", []):
                name = p.get("projectName", "").strip()
                if name and name not in seen:
                    all_projects.append(name)
                    seen.add(name)
        if all_projects:
            lines.append("**项目经历：**\n")
            for sol in SOLUTION_ORDER:
                if sol not in solutions:
                    continue
                lines.append(f"**{SOLUTION_LABEL[sol]}**\n")
                for p in solutions[sol].get("projects", []):
                    role = p.get("role", "—")
                    q = eval_position_quality(role)
                    lines.append(f"- {p.get('projectName', '?')} → `{role}` ({q})")
                lines.append("")

    # 四、candidate 基本信息
    lines.append("## 四、candidate 基本信息对比\n")
    key_fields = ["name", "phone", "email", "gender", "age",
                  "address", "nativePlace", "expectedPosition", "expectedSalary"]
    for pdf_stem, solutions in all_data.items():
        lines.append(f"### {pdf_stem}\n")
        lines.append("| 字段 | " + " | ".join(
            SOLUTION_LABEL[s] for s in SOLUTION_ORDER if s in solutions) + " |")
        lines.append("|------|" + "|".join(["---"] * sum(1 for s in SOLUTION_ORDER if s in solutions)) + "|")
        for field in key_fields:
            row = [f"`{field}`"]
            for sol in SOLUTION_ORDER:
                if sol not in solutions:
                    continue
                val = solutions.get(sol, {}).get("candidate", {}).get(field, "")
                display = str(val)[:30] if val and val != 0 else "—"
                row.append(display)
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    # 五、一致性分析
    lines.append("## 五、方案一致性分析\n")
    for pdf_stem, solutions in all_data.items():
        sols = list(solutions.keys())
        if len(sols) < 2:
            continue
        ref = sols[0]
        ref_c = solutions[ref].get("candidate", {})
        lines.append(f"### {pdf_stem}\n")
        for sol in sols[1:]:
            c = solutions[sol].get("candidate", {})
            match = 0
            total = 0
            for k in key_fields:
                total += 1
                if str(ref_c.get(k, "")).strip() == str(c.get(k, "")).strip():
                    match += 1
            pct = f"{match}/{total}"
            lines.append(f"- vs {SOLUTION_LABEL[sol]}: candidate 字段匹配 {pct}")
        lines.append("")

    report = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n报告已生成: {output_path}")


# ── 控制台输出 ────────────────────────────────────────────────

def print_console(all_data: dict, summary: list):
    times = defaultdict(list)
    success_count = defaultdict(int)
    for rec in summary:
        if rec.get("success"):
            times[rec["solution"]].append(rec["elapsed_s"])
            success_count[rec["solution"]] += 1

    print(f"\n{'='*70}")
    print(f"速度汇总")
    print(f"{'='*70}")
    print(f"  {'方案':<28} {'成功':>6} {'平均(s)':>8} {'最快(s)':>8} {'最慢(s)':>8}")
    print("  " + "-"*65)
    for sol in SOLUTION_ORDER:
        t_list = times.get(sol, [])
        if not t_list:
            continue
        avg = sum(t_list) / len(t_list)
        print(f"  {SOLUTION_LABEL[sol]:<26} {success_count[sol]:>4}/{len(t_list)}  "
              f"{avg:>8.1f} {min(t_list):>8.1f} {max(t_list):>8.1f}")

    # 分类统计
    for cat in ["有明确职位", "无明确职位（需推断）"]:
        cat_recs = [r for r in summary if r.get("position_category") == cat]
        if not cat_recs:
            continue
        cat_times = defaultdict(list)
        cat_success = defaultdict(int)
        for r in cat_recs:
            if r.get("success"):
                cat_times[r["solution"]].append(r["elapsed_s"])
                cat_success[r["solution"]] += 1
        print(f"\n── 分类：{cat} ──")
        for sol in SOLUTION_ORDER:
            t_list = cat_times.get(sol, [])
            if not t_list:
                continue
            avg = sum(t_list) / len(t_list)
            print(f"  {SOLUTION_LABEL[sol]:<26} {cat_success[sol]:>4}/{len(t_list)}  "
                  f"平均:{avg:.1f}s")

    # 字段完整性
    print(f"\n{'='*70}")
    print(f"字段完整性对比")
    print(f"{'='*70}")
    for pdf_stem, solutions in all_data.items():
        print(f"\n  {pdf_stem[:60]}")
        print(f"  {'方案':<28} {'综合分':>6} {'必填':>8} {'可选':>8} {'工作':>4} {'项目':>4} {'教育':>4}")
        print("  " + "-"*65)
        for sol in SOLUTION_ORDER:
            if sol not in solutions:
                continue
            sc = completeness_score(solutions[sol])
            print(f"  {SOLUTION_LABEL[sol]:<26} {sc['total_score']:>6} "
                  f"{sc['required_fields']:>8} {sc['optional_fields']:>8} "
                  f"{sc['works_count']:>4} {sc['projects_count']:>4} {sc['educations_count']:>4}")


if __name__ == "__main__":
    result_dir = sys.argv[1] if len(sys.argv) > 1 else OUTPUT_DIR
    print(f"读取结果目录: {result_dir}")
    all_data = load_results(result_dir)
    summary = load_summary(result_dir)
    if not all_data:
        print("未找到结果文件，请先运行 benchmark_comparison.py")
        sys.exit(1)
    print(f"找到 {len(all_data)} 份简历的对比数据")
    print_console(all_data, summary)
    report_path = os.path.join(result_dir, "comparison_report.md")
    generate_report(all_data, summary, report_path)
