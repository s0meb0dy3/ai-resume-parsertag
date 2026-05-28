"""
本地 PDF 简历上传工具

用法:
  python upload_resume.py resume.pdf
  python upload_resume.py resume.pdf --url http://10.2.5.117:21801
  python upload_resume.py resume.pdf --json          # 输出完整 JSON
  python upload_resume.py resume.pdf --out result.json  # 保存到文件
"""
import argparse
import base64
import json
import sys
import time
from pathlib import Path

import requests

DEFAULT_URL = "http://localhost:21804"
TIMEOUT = 300


def parse_resume(pdf_path: Path, server: str) -> dict:
    pdf_bytes = pdf_path.read_bytes()
    payload = {
        "model": "DeepSeek-OCR",
        "stream": False,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "base64",
                "file": {
                    "name": pdf_path.name,
                    "content": base64.b64encode(pdf_bytes).decode(),
                }
            }]
        }]
    }

    resp = requests.post(
        f"{server}/v1/chat/completions",
        json=payload,
        timeout=TIMEOUT,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def print_summary(result: dict):
    c = result.get("candidate", {})
    gender_map = {1: "男", 2: "女"}
    SEP = "─" * 50

    print(f"\n┌{SEP}")
    print(f"│ 基本信息")
    print(f"│  姓名: {c.get('name', '')}  性别: {gender_map.get(c.get('gender'), '-')}  年龄: {c.get('age') or '-'}")
    print(f"│  电话: {c.get('phone', '')}  邮箱: {c.get('email', '')}")
    print(f"│  地址: {c.get('address', '')}  籍贯: {c.get('nativePlace', '')}")
    print(f"│  期望薪资: {c.get('expectedSalary', '')}  应聘职位: {c.get('expectedPosition', '')}")
    if c.get("selfEvaluation"):
        print(f"│  自评: {c['selfEvaluation'][:80]}{'...' if len(c.get('selfEvaluation','')) > 80 else ''}")

    edus = result.get("educations", [])
    print(f"├{'─'*18} 教育经历({len(edus)}) {'─'*18}")
    for e in edus:
        print(f"│  {e.get('school', '')}  {e.get('major', '')}  "
              f"{e.get('education', '')}  {e.get('startDate', '')}~{e.get('endDate', '')}")

    works = result.get("works", [])
    print(f"├{'─'*18} 工作经历({len(works)}) {'─'*18}")
    for w in works:
        print(f"│  [{w.get('position', '')}]  {w.get('company', '')}  "
              f"{w.get('startDate', '')}~{w.get('endDate', '')}")

    projs = result.get("projects", [])
    print(f"├{'─'*18} 项目经历({len(projs)}) {'─'*18}")
    for p in projs:
        print(f"│  [{p.get('role', '')}]  {p.get('projectName', '')}  "
              f"{p.get('startDate', '')}~{p.get('endDate', '')}")

    print(f"└{SEP}\n")


def main():
    parser = argparse.ArgumentParser(description="本地 PDF 简历上传解析工具")
    parser.add_argument("pdf", help="PDF 文件路径")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"服务地址，默认 {DEFAULT_URL}")
    parser.add_argument("--json", action="store_true", dest="full_json", help="输出完整 JSON")
    parser.add_argument("--out", default=None, help="将 JSON 结果保存到指定文件")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"[错误] 文件不存在: {pdf_path}", file=sys.stderr)
        sys.exit(1)
    if pdf_path.suffix.lower() != ".pdf":
        print(f"[错误] 仅支持 PDF 文件: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    size_kb = pdf_path.stat().st_size // 1024
    print(f"上传: {pdf_path.name}  ({size_kb} KB) → {args.url}")

    t0 = time.time()
    try:
        result = parse_resume(pdf_path, args.url.rstrip("/"))
    except requests.exceptions.ConnectionError:
        print(f"[错误] 无法连接服务: {args.url}", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("[错误] 请求超时，PDF 页数较多时可适当增大 TIMEOUT", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)

    elapsed = time.time() - t0
    print(f"解析完成，耗时 {elapsed:.1f}s")

    if args.full_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_summary(result)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已保存: {out_path}")


if __name__ == "__main__":
    main()
