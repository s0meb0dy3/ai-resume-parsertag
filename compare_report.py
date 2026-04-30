"""
结果对比报告生成器
读取 benchmark.py 产出的 JSON 文件，生成：
  1. 控制台对比表格
  2. results/compare_report.md  详细 Markdown 报告
用法：
  python compare_report.py                       # 读取 OUTPUT_DIR 下所有结果
  python compare_report.py path/to/results/      # 指定目录
"""
import os
import sys
import json
import re
from pathlib import Path
from collections import defaultdict
from config import OUTPUT_DIR


# ── 字段完整性评分 ───────────────────────────────────────────

CANDIDATE_REQUIRED = ["name", "phone"]
CANDIDATE_OPTIONAL = ["email", "gender", "age", "address", "nativePlace",
                       "selfEvaluation", "expectedSalary", "expectedPosition"]

def completeness_score(result: dict) -> dict:
    """计算字段完整性（0~100）"""
    scores = {}
    c = result.get("candidate", {})

    # 必填字段
    req_filled = sum(1 for f in CANDIDATE_REQUIRED if c.get(f, ""))
    scores["required_fields"] = f"{req_filled}/{len(CANDIDATE_REQUIRED)}"

    # 可选字段
    opt_filled = sum(1 for f in CANDIDATE_OPTIONAL if c.get(f, "") and c.get(f, 0) != 0)
    scores["optional_fields"] = f"{opt_filled}/{len(CANDIDATE_OPTIONAL)}"

    # 工作经历
    works = result.get("works", [])
    scores["works_count"] = len(works)
    pos_filled = sum(1 for w in works if w.get("position", "").strip()
                     and w.get("position") not in ("职位/职责", "详见描述", "【职位提取规则见下方说明】"))
    scores["position_filled"] = f"{pos_filled}/{len(works)}" if works else "N/A"

    # 项目经历
    projects = result.get("projects", [])
    scores["projects_count"] = len(projects)
    role_filled = sum(1 for p in projects if p.get("role", "").strip()
                      and p.get("role") not in ("职位/职责", "详见描述", "【职位提取规则见下方说明】"))
    scores["role_filled"] = f"{role_filled}/{len(projects)}" if projects else "N/A"

    # 教育经历
    scores["educations_count"] = len(result.get("educations", []))

    # 综合分（满分100）
    total = 0
    total += 30 if req_filled == len(CANDIDATE_REQUIRED) else req_filled * 15
    total += min(opt_filled * 5, 30)                       # 可选字段最多30分
    total += min(scores["works_count"] * 5, 20)            # 工作经历最多20分
    total += min(scores["educations_count"] * 5, 10)       # 教育经历最多10分
    if works:
        total += int(pos_filled / len(works) * 10)         # position质量最多10分
    scores["total_score"] = min(total, 100)

    return scores


# ── position 质量评估 ────────────────────────────────────────

INVALID_POSITIONS = {"职位/职责", "详见描述", "【职位提取规则见下方说明】", "", "职责"}
POSITION_KEYWORDS = ["工程师", "经理", "负责人", "研究员", "分析师", "专员",
                      "主任", "总监", "架构师", "开发", "运维", "测试", "产品",
                      "研究生", "实习", "顾问", "科学家"]

def eval_position_quality(position: str) -> str:
    """评估单个 position 字段质量"""
    if not position or position.strip() in INVALID_POSITIONS:
        return "空/无效"
    if any(kw in position for kw in POSITION_KEYWORDS):
        return "优" if len(position) <= 30 else "良（偏长）"
    return "中（缺少职位关键词）"


# ── 加载结果 ─────────────────────────────────────────────────

def load_results(result_dir: str) -> dict:
    """
    加载目录下所有 __solution.json 文件
    返回 {pdf_stem: {solution_name: result_dict}}
    """
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


# ── 报告生成 ─────────────────────────────────────────────────

SOLUTION_ORDER = ["Original_DeepSeek", "A_multimodal", "B_binary_stream", "C_direct_pdf", "D_batch"]
SOLUTION_LABEL = {
    "Original_DeepSeek": "原始 DeepSeek-OCR",
    "A_multimodal":      "方案A 多模态图像",
    "B_binary_stream":   "方案B 二进制流",
    "C_direct_pdf":      "方案C 直传PDF",
    "D_batch":           "方案D 分批并发",
}


def print_console_table(all_data: dict, summary: list):
    """控制台输出对比表"""
    # 耗时汇总
    times = defaultdict(list)
    for rec in summary:
        if rec.get("success"):
            times[rec["solution"]].append(rec["elapsed_s"])

    print("\n" + "="*70)
    print("方案速度汇总")
    print("="*70)
    header = f"{'方案':<22} {'成功数':>6} {'平均(s)':>8} {'最快(s)':>8} {'最慢(s)':>8}"
    print(header)
    print("-"*70)
    for sol in SOLUTION_ORDER:
        t_list = times.get(sol, [])
        if not t_list:
            print(f"  {SOLUTION_LABEL.get(sol, sol):<20}  {'无数据':>6}")
            continue
        avg = sum(t_list) / len(t_list)
        print(f"  {SOLUTION_LABEL.get(sol, sol):<20} {len(t_list):>6} "
              f"{avg:>8.1f} {min(t_list):>8.1f} {max(t_list):>8.1f}")

    # 字段完整性
    print("\n" + "="*70)
    print("字段完整性对比（综合分/100）")
    print("="*70)
    for pdf_stem, solutions in all_data.items():
        print(f"\n  简历: {pdf_stem[:50]}")
        print(f"  {'方案':<22} {'综合分':>6} {'必填':>8} {'可选':>8} {'工作数':>6} {'position质量':>12}")
        print("  " + "-"*65)
        for sol in SOLUTION_ORDER:
            if sol not in solutions:
                continue
            sc = completeness_score(solutions[sol])
            works = solutions[sol].get("works", [])
            pos_qualities = [eval_position_quality(w.get("position", "")) for w in works]
            pos_summary = "优" if all(q == "优" for q in pos_qualities) else \
                          "良" if any(q.startswith("优") or q.startswith("良") for q in pos_qualities) else \
                          "差" if pos_qualities else "N/A"
            print(f"  {SOLUTION_LABEL.get(sol, sol):<22} {sc['total_score']:>6} "
                  f"{sc['required_fields']:>8} {sc['optional_fields']:>8} "
                  f"{sc['works_count']:>6} {pos_summary:>12}")


def generate_markdown_report(all_data: dict, summary: list, output_path: str):
    """生成详细 Markdown 报告"""
    lines = []
    lines.append("# PDF 简历解析方案对比报告\n")
    lines.append(f"> 生成时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"> 新方案模型：`http://10.2.5.7:13001/v1` / `Qwen3.5-397B-A17B`\n")
    lines.append(f"> 原始方案：`DeepSeek-OCR vLLM`（OCR）+ `Qwen LLM`（结构化）两阶段流水线\n")
    lines.append(f"> 测试简历数：{len(all_data)}\n")
    lines.append("\n---\n")
    lines.append("## 方案概览\n")
    lines.append("| 方案 | 链路 | 说明 |")
    lines.append("|------|------|------|")
    lines.append("| 原始 DeepSeek-OCR | PDF → OCR(vLLM) → Markdown → LLM → JSON | 专用OCR两步流水线，需独立GPU |")
    lines.append("| 方案A 多模态图像 | PDF → 图像渲染 → 多模态LLM → JSON | 单步调用，图像直送视觉LLM |")
    lines.append("| 方案B 二进制流 | PDF → bytes内存流 → 智能路由 → JSON | 数字化PDF走文本通道，扫描件走图像 |")
    lines.append("| 方案C 直传PDF | PDF → base64编码 → LLM原生解析 → JSON | 最简实现，依赖模型file支持 |")
    lines.append("| 方案D 分批并发 | PDF → 分页渲染 → 并发批推理 → 合并 → JSON | 多页长简历最优，4线程并发 |")
    lines.append("\n---\n")

    # ── 一、速度汇总 ─────────────────────────────────────────
    lines.append("## 一、速度汇总\n")
    lines.append("| 方案 | 成功数 | 平均耗时(s) | 最快(s) | 最慢(s) |")
    lines.append("|------|--------|------------|--------|--------|")
    times = defaultdict(list)
    for rec in summary:
        if rec.get("success"):
            times[rec["solution"]].append(rec["elapsed_s"])
    for sol in SOLUTION_ORDER:
        t_list = times.get(sol, [])
        if not t_list:
            lines.append(f"| {SOLUTION_LABEL.get(sol, sol)} | 0 | - | - | - |")
            continue
        avg = sum(t_list) / len(t_list)
        lines.append(f"| {SOLUTION_LABEL.get(sol, sol)} | {len(t_list)} | "
                     f"{avg:.1f} | {min(t_list):.1f} | {max(t_list):.1f} |")
    lines.append("")

    # ── 二、字段完整性对比 ────────────────────────────────────
    lines.append("## 二、字段完整性对比\n")
    for pdf_stem, solutions in all_data.items():
        lines.append(f"### 简历：{pdf_stem}\n")
        lines.append("| 方案 | 综合分 | 必填字段 | 可选字段 | 工作数 | 项目数 | 教育数 | position完整率 |")
        lines.append("|------|--------|---------|---------|-------|-------|-------|--------------|")
        for sol in SOLUTION_ORDER:
            if sol not in solutions:
                continue
            sc = completeness_score(solutions[sol])
            lines.append(
                f"| {SOLUTION_LABEL.get(sol, sol)} | **{sc['total_score']}** | "
                f"{sc['required_fields']} | {sc['optional_fields']} | "
                f"{sc['works_count']} | {sc['projects_count']} | "
                f"{sc['educations_count']} | {sc['position_filled']} |"
            )
        lines.append("")

    # ── 三、position 字段详细对比 ────────────────────────────
    lines.append("## 三、position / role 字段提取对比\n")
    for pdf_stem, solutions in all_data.items():
        lines.append(f"### 简历：{pdf_stem}\n")

        # 收集所有方案提取到的公司名，作为行索引
        all_companies = []
        seen = set()
        for sol in SOLUTION_ORDER:
            for w in solutions.get(sol, {}).get("works", []):
                c = w.get("company", "").strip()
                if c and c not in seen:
                    all_companies.append(c)
                    seen.add(c)

        if all_companies:
            lines.append("**工作经历 position 对比：**\n")
            lines.append("| 公司 | " + " | ".join(SOLUTION_LABEL.get(s, s) for s in SOLUTION_ORDER) + " |")
            lines.append("|------|" + "|".join(["---"] * len(SOLUTION_ORDER)) + "|")
            for company in all_companies:
                row = [company[:20]]
                for sol in SOLUTION_ORDER:
                    works = solutions.get(sol, {}).get("works", [])
                    pos = next((w.get("position", "") for w in works
                                if w.get("company", "").strip() == company), "—")
                    quality = eval_position_quality(pos)
                    icon = "✅" if quality == "优" else "⚠️" if "良" in quality else "❌"
                    row.append(f"{icon} {pos[:20]}" if pos else "—")
                lines.append("| " + " | ".join(row) + " |")
            lines.append("")

        # projects role
        all_projects = []
        seen = set()
        for sol in SOLUTION_ORDER:
            for p in solutions.get(sol, {}).get("projects", []):
                name = p.get("projectName", "").strip()
                if name and name not in seen:
                    all_projects.append(name)
                    seen.add(name)

        if all_projects:
            lines.append("**项目经历 role 对比：**\n")
            lines.append("| 项目 | " + " | ".join(SOLUTION_LABEL.get(s, s) for s in SOLUTION_ORDER) + " |")
            lines.append("|------|" + "|".join(["---"] * len(SOLUTION_ORDER)) + "|")
            for proj in all_projects[:5]:  # 最多显示5个项目
                row = [proj[:20]]
                for sol in SOLUTION_ORDER:
                    projects = solutions.get(sol, {}).get("projects", [])
                    role = next((p.get("role", "") for p in projects
                                 if p.get("projectName", "").strip() == proj), "—")
                    quality = eval_position_quality(role)
                    icon = "✅" if quality == "优" else "⚠️" if "良" in quality else "❌"
                    row.append(f"{icon} {role[:20]}" if role else "—")
                lines.append("| " + " | ".join(row) + " |")
            lines.append("")

    # ── 四、关键字段逐行对比（candidate）────────────────────
    lines.append("## 四、candidate 基本信息字段逐项对比\n")
    key_fields = ["name", "phone", "email", "gender", "age",
                  "address", "nativePlace", "expectedPosition", "expectedSalary"]
    for pdf_stem, solutions in all_data.items():
        lines.append(f"### 简历：{pdf_stem}\n")
        lines.append("| 字段 | " + " | ".join(SOLUTION_LABEL.get(s, s) for s in SOLUTION_ORDER) + " |")
        lines.append("|------|" + "|".join(["---"] * len(SOLUTION_ORDER)) + "|")
        for field in key_fields:
            row = [f"`{field}`"]
            for sol in SOLUTION_ORDER:
                val = solutions.get(sol, {}).get("candidate", {}).get(field, "")
                display = str(val)[:25] if val and val != 0 else "—"
                row.append(display)
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    # ── 五、方案优劣总结 ─────────────────────────────────────
    lines.append("## 五、方案综合优劣总结\n")
    lines.append("| 方案 | 链路 | 优势 | 劣势 | 推荐场景 |")
    lines.append("|------|------|------|------|---------|")
    lines.append("| 原始 DeepSeek-OCR | PDF → OCR(vLLM) → Markdown → LLM → JSON | 专用OCR精度高，复杂排版/公式解析好 | 需独立GPU服务，两次LLM调用，路径依赖服务端 | 扫描件/复杂表格 |")
    lines.append("| 方案A 多模态图像 | PDF → 图像渲染 → 多模态LLM → JSON | 跨页语义理解好，一次调用 | 图像token消耗大 | 1~4页标准简历 |")
    lines.append("| 方案B 二进制流 | PDF → bytes内存流 → 智能路由 → JSON | 数字化PDF极速通道，无临时文件 | 扫描件仍需渲染 | 数字化PDF为主 |")
    lines.append("| 方案C 直传PDF | PDF → base64编码 → LLM原生解析 → JSON | 调用最简，代码最短 | 模型需原生支持file类型 | 短简历+支持file接口 |")
    lines.append("| 方案D 分批并发 | PDF → 分页渲染 → 并发批推理 → 合并 → JSON | 不受页数限制，GPU利用率高 | 合并逻辑复杂 | 多页/长简历 |")

    report = "\n".join(lines)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nMarkdown报告已生成: {output_path}")
    return report


if __name__ == "__main__":
    result_dir = sys.argv[1] if len(sys.argv) > 1 else OUTPUT_DIR

    print(f"读取结果目录: {result_dir}")
    all_data = load_results(result_dir)
    summary  = load_summary(result_dir)

    if not all_data:
        print("未找到结果文件，请先运行 benchmark.py")
        sys.exit(1)

    print(f"找到 {len(all_data)} 份简历的对比数据")

    print_console_table(all_data, summary)

    report_path = os.path.join(result_dir, "compare_report.md")
    generate_markdown_report(all_data, summary, report_path)
