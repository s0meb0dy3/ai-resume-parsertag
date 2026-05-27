"""
简历打标功能测试程序
用法：
  python test_tagging.py                          # 默认测试 test_full 第一个文件
  python test_tagging.py --all                    # 批量测试 test_full 所有文件
  python test_tagging.py --file path/to/x.pdf     # 指定单文件
  python test_tagging.py --dir path/to/dir --all  # 指定目录批量测试
  python test_tagging.py --save                   # 结果同时保存到 results/ 目录
"""
import argparse
import json
import time
import sys
from datetime import datetime
from pathlib import Path

import requests

HOST      = "http://localhost:21801"
_BASE     = Path(__file__).parent
TEST_DIR  = _BASE / "uploads" / "test_full"
RESULT_DIR = _BASE / "results"

STATUS_DESC = {0: "待处理", 1: "简历解析中", 2: "标签打标中", 3: "任务完成", 4: "任务失败"}
SEP = "─" * 70


def submit(host: str, file_path: Path, job_title: str = "", required_skills: str = "",
           job_description: str = "", job_sensitive_words: str = "") -> str:
    with open(file_path, "rb") as f:
        resp = requests.post(
            f"{host}/api/resume/async/parse",
            files={"file": (file_path.name, f, "application/pdf")},
            data={
                "jobTitle":          job_title,
                "requiredSkills":    required_skills,
                "jobDescription":    job_description,
                "jobSensitiveWords": job_sensitive_words,
            },
            timeout=15,
        )
    resp.raise_for_status()
    body = resp.json()
    if body["code"] != 0:
        raise RuntimeError(f"提交失败: {body}")
    task_id = body["data"]["taskId"]
    print(f"  ✓ 提交成功  taskId: {task_id}")
    return task_id


def poll(host: str, task_id: str, timeout: int = 300) -> dict:
    start      = time.time()
    last_status = -1
    while time.time() - start < timeout:
        resp = requests.get(
            f"{host}/api/resume/async/result",
            params={"taskId": task_id},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if body["code"] != 0:
            raise RuntimeError(f"查询失败: {body}")

        data    = body["data"]
        status  = data["status"]
        elapsed = data.get("elapsedSeconds", 0)

        if status != last_status:
            print(f"  [{STATUS_DESC.get(status, status)}] 耗时 {elapsed:.0f}s")
            last_status = status

        if status == 3:
            return data["result"]
        if status == 4:
            raise RuntimeError(f"任务失败: {data.get('errorMsg')}")

        interval = 3 if elapsed < 15 else (5 if elapsed < 60 else 10)
        time.sleep(interval)

    raise TimeoutError(f"超时（{timeout}s），请重新提交")


def save_result(result: dict, filename: str):
    """将结果保存到 results/ 目录"""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    stem     = Path(filename).stem[:40]
    safe     = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = RESULT_DIR / f"{ts}_{safe}_test.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  💾 结果已保存: {out_path.name}")


def print_result(result: dict, filename: str):
    c          = result.get("candidate", {})
    gender_map = {1: "男", 2: "女"}

    print(f"\n{'─'*25} 候选人 {'─'*25}")
    print(f"  姓名: {c.get('name','')}  性别: {gender_map.get(c.get('gender'),'未知')}  年龄: {c.get('age') or '-'}")
    print(f"  电话: {c.get('phone','')}  邮箱: {c.get('email','')}")
    print(f"  籍贯: {c.get('nativePlace','')}  住址: {c.get('address','')}")
    print(f"  期望: {c.get('expectedPosition','')}  {c.get('expectedSalary','')}")

    edus = result.get("educations", [])
    print(f"\n{'─'*25} 教育经历({len(edus)}) {'─'*23}")
    for e in edus:
        print(f"  {e.get('school','')}  {e.get('major','')}  "
              f"{e.get('education','')}  {e.get('startDate','')}~{e.get('endDate','')}")

    works = result.get("works", [])
    print(f"\n{'─'*25} 工作经历({len(works)}) {'─'*23}")
    for w in works:
        print(f"  [{w.get('position','')}]  {w.get('company','')}  "
              f"{w.get('startDate','')}~{w.get('endDate','')}")

    pos_tags = result.get("positiveTags", {}).get("tagsArray", [])
    print(f"\n{'─'*25} 正向标签 {'─'*26}")
    for tag in pos_tags:
        dim     = tag.get("dimension", "")
        display = tag.get("displayName", dim)
        values  = [v["tag"] for v in tag.get("value", []) if isinstance(v, dict)]
        val     = ", ".join(values) if values else ""
        conf    = f"  [置信度={tag['confidence']:.2f}]" if tag.get("confidence") is not None else ""
        print(f"  [{display:8s}] {val}{conf}")

    neg       = result.get("negativeTags", {})
    neg_tags  = neg.get("tagsArray", [])
    total_hits = sum(len(g.get("value", [])) for g in neg_tags)
    print(f"\n{'─'*25} 风险标签({total_hits}) {'─'*24}")
    if neg_tags:
        sev_icon = {"low": "🔵", "medium": "🟡", "high": "🔴"}
        for group in neg_tags:
            for hit in group.get("value", []):
                icon = sev_icon.get(hit.get("severity", ""), "⚪")
                print(f"  {icon} [{group.get('displayName','')}] {hit.get('tag','')}")
                print(f"     证据: {hit.get('matchedContent','')[:80]}")
        print(f"  合计扣分: {neg.get('deductionScore', 0)}")
    else:
        print("  无风险标签")

    print(f"\n  简历解析时间: {result.get('resumeParseFinishTime','')}")
    print(f"  标签打标时间: {result.get('tagParseFinishTime','')}")


def run_single(host: str, file_path: Path, job_title: str, required_skills: str,
               job_description: str, job_sensitive_words: str, save: bool) -> bool:
    print(f"\n{'='*70}")
    print(f"文件: {file_path.name}")
    print(f"岗位: {job_title or '(未填)'}  技能要求: {required_skills or '(未填)'}")
    if job_sensitive_words:
        print(f"敏感词: {job_sensitive_words}")
    print(f"{'='*70}")

    try:
        t0      = time.time()
        task_id = submit(host, file_path, job_title, required_skills,
                         job_description, job_sensitive_words)
        result  = poll(host, task_id)
        elapsed = time.time() - t0
        print_result(result, file_path.name)
        if save:
            save_result(result, file_path.name)
        print(f"\n  ✅ 全流程耗时: {elapsed:.1f}s")
        return True
    except Exception as e:
        print(f"\n  ❌ 失败: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="简历打标测试程序")
    parser.add_argument("--host",   default=HOST,        help=f"服务地址（默认 {HOST}）")
    parser.add_argument("--file",   default=None,        help="指定单个 PDF 文件路径")
    parser.add_argument("--dir",    default=str(TEST_DIR), help=f"测试目录（默认 {TEST_DIR}）")
    parser.add_argument("--all",    action="store_true", help="批量测试目录下所有 PDF")
    parser.add_argument("--save",   action="store_true", help="结果保存到 results/ 目录")
    parser.add_argument("--job",    default="",          help="目标岗位名称")
    parser.add_argument("--skills", default="",          help="必要技能，逗号分隔")
    parser.add_argument("--jd",     default="",          help="岗位描述")
    parser.add_argument("--words",  default="",          help="自定义敏感词，逗号分隔")
    args = parser.parse_args()

    host     = args.host
    test_dir = Path(args.dir)

    # 健康检查
    try:
        r = requests.get(f"{host}/health", timeout=5)
        r.raise_for_status()
        print(f"✓ 服务在线: {host}")
    except Exception as e:
        print(f"✗ 服务不可达 ({host}): {e}")
        sys.exit(1)

    if args.file:
        files = [Path(args.file)]
    elif args.all:
        files = sorted(test_dir.glob("*.pdf"))
        if not files:
            print(f"✗ 未找到 PDF 文件: {test_dir}")
            sys.exit(1)
        print(f"发现 {len(files)} 份简历，目录: {test_dir}")
    else:
        files = sorted(test_dir.glob("*.pdf"))[:1]
        if not files:
            print(f"✗ 未找到 PDF 文件: {test_dir}")
            sys.exit(1)
        print(f"默认使用: {files[0].name}")
        print("使用 --all 测试全部文件，--file <path> 指定文件，--save 保存结果")

    success = fail = 0
    for f in files:
        ok = run_single(host, f, args.job, args.skills, args.jd, args.words, args.save)
        if ok:
            success += 1
        else:
            fail += 1

    if len(files) > 1:
        print(f"\n{'='*70}")
        print(f"批量结果：成功 {success} / 失败 {fail} / 共 {len(files)}")


if __name__ == "__main__":
    main()
