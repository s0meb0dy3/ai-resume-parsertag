#!/usr/bin/env python3
"""校验真实简历黄金集并评估预生成解析打标结果。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.golden_evaluator import (  # noqa: E402
    GoldenManifestError,
    evaluate_predictions,
    load_manifest,
    load_predictions,
    verify_source_files,
)


def _parse_args() -> argparse.Namespace:
    """解析黄金集校验和评测命令参数。

    Returns:
        argparse.Namespace: 已解析的命令行参数。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="黄金集清单 JSON")
    parser.add_argument(
        "--resume-root",
        default=os.getenv("GOLDEN_RESUME_ROOT", ""),
        help="真实简历目录；默认读取 GOLDEN_RESUME_ROOT",
    )
    parser.add_argument("--predictions", help="按样本 ID 保存结果 JSON 的目录")
    parser.add_argument("--report", help="可选的评测报告输出路径")
    parser.add_argument("--validate-only", action="store_true", help="只校验清单和源文件")
    parser.add_argument(
        "--allow-pii",
        action="store_true",
        help="允许私有人工标注包含姓名、电话等 PII",
    )
    return parser.parse_args()


def main() -> int:
    """执行清单校验、源文件摘要校验和指标评测。

    Returns:
        int: 校验成功为 0，指标未达阈值为 1，输入错误为 2。
    """
    args = _parse_args()
    try:
        manifest = load_manifest(args.manifest, allow_pii=args.allow_pii)
        if args.resume_root:
            verify_source_files(manifest, args.resume_root)
        elif args.validate_only:
            raise GoldenManifestError("--validate-only 必须提供 --resume-root")

        if args.validate_only:
            print(json.dumps({
                "datasetVersion": manifest["datasetVersion"],
                "caseCount": len(manifest["cases"]),
                "validated": True,
            }, ensure_ascii=False, indent=2))
            return 0
        if not args.predictions:
            raise GoldenManifestError("评测必须提供 --predictions")
        predictions = load_predictions(
            args.predictions,
            [case["id"] for case in manifest["cases"]],
        )
        report = evaluate_predictions(manifest, predictions)
        output = json.dumps(report, ensure_ascii=False, indent=2)
        print(output)
        if args.report:
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(output + "\n", encoding="utf-8")
        return 0 if report["passed"] else 1
    except (GoldenManifestError, OSError, json.JSONDecodeError) as error:
        print(f"黄金集评测失败: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
