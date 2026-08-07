#!/usr/bin/env python3
"""构造多格式简历样本并评测同步解析耗时和字段准确率。

脚本分为 `prepare`、`run` 和 `report` 三个阶段。格式变体复用同一份人工标注，
避免将模型输出当作真值；原始响应和变体文件必须保存在 Git 忽略目录中。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import statistics
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Sequence

import requests


DEFAULT_ENDPOINT = "http://127.0.0.1:21804/api/resume/sync/parse"
RENDER_DPI = 144
IMAGE_GAP_PIXELS = 24
MAX_BENCHMARK_IMAGE_PIXELS = 38_000_000
LIST_IDENTITIES = {
    "educations": ("school", "startDate", "endDate"),
    "works": ("company", "startDate", "endDate"),
    "projects": ("projectName", "company", "startDate", "endDate"),
}
SECTION_NAMES = {
    "candidate": "基础信息",
    "educations": "教育经历",
    "works": "工作经历",
    "projects": "项目经历",
}
_LOG_FILENAME_RE = re.compile(
    r"\[[^\]]+\]\s+\[[^\]]+\]\s+\[(?P<filename>sample-[^\]]+)\]"
)
_ELAPSED_RE = re.compile(r"\belapsedSeconds=(?P<seconds>\d+(?:\.\d+)?)")
_JSON_TIMING_RE = re.compile(
    r"\bjsonDecodeSeconds=(?P<decode>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s+"
    r"postProcessSeconds=(?P<post_process>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s+"
    r"conversionSeconds=(?P<conversion>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
)


def _read_json(path: Path) -> dict:
    """读取顶层为对象的 UTF-8 JSON 文件。

    Args:
        path (Path): JSON 文件路径。

    Returns:
        dict: 解析后的 JSON 对象。

    Raises:
        ValueError: JSON 顶层不是对象时抛出。
        OSError: 文件无法读取时抛出。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 顶层必须是对象: {path}")
    return payload


def _write_json(path: Path, payload: dict) -> None:
    """以稳定格式写入 UTF-8 JSON 文件。

    Args:
        path (Path): 输出文件路径。
        payload (dict): 待序列化的 JSON 对象。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    """计算文件 SHA-256 摘要。

    Args:
        path (Path): 待读取的文件路径。

    Returns:
        str: 64 位十六进制摘要。
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_text(value: Any) -> Any:
    """规范化人工标注和解析值以执行精确比较。

    Args:
        value (Any): 待比较的标量值。

    Returns:
        Any: 字符串统一全半角、大小写和空白，其他类型保持不变。
    """
    if not isinstance(value, str):
        return value
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(normalized.split())


def _effective_characters(text: str) -> int:
    """统计忽略布局空白后的文本字符数。

    Args:
        text (str): PDF 原生文本。

    Returns:
        int: 非空白字符数量。
    """
    return len("".join(text.split()))


def _extract_pdf_text(path: Path) -> tuple[str, int]:
    """使用 PyMuPDF 提取 PDF 全文和页数。

    Args:
        path (Path): PDF 文件路径。

    Returns:
        tuple[str, int]: 按页拼接的文本和 PDF 页数。
    """
    import fitz

    with fitz.open(path) as document:
        return "\n".join(page.get_text() for page in document), len(document)


def _render_pdf_pages(path: Path) -> list[Image.Image]:
    """按固定分辨率将 PDF 页面渲染为 RGB 图片。

    Args:
        path (Path): PDF 文件路径。

    Returns:
        list[Image.Image]: 按原页序排列的独立页面图片。
    """
    import fitz
    from PIL import Image

    matrix = fitz.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)
    images: list[Image.Image] = []
    with fitz.open(path) as document:
        for page in document:
            pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
            image = Image.frombytes(
                "RGB",
                (pixmap.width, pixmap.height),
                pixmap.samples,
            )
            images.append(image.copy())
    return images


def _create_scanned_pdf(source: Path, output: Path) -> None:
    """把文本 PDF 转换为只包含页面图片的扫描型 PDF。

    Args:
        source (Path): 有文本层的源 PDF。
        output (Path): 扫描型 PDF 输出路径。

    Raises:
        ValueError: 输出仍包含可提取文本时抛出。

    Note:
        页面尺寸沿用源 PDF，图片使用 JPEG 压缩控制文件大小。该变换只改变承载
        格式，不改变页面可见内容，因此继续复用源简历的人工标注。
    """
    import fitz

    images = _render_pdf_pages(source)
    target = fitz.open()
    try:
        with fitz.open(source) as original:
            for index, image in enumerate(images):
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=92, optimize=True)
                page = target.new_page(
                    width=original[index].rect.width,
                    height=original[index].rect.height,
                )
                page.insert_image(page.rect, stream=buffer.getvalue())
        target.save(output, deflate=True, garbage=4)
    finally:
        target.close()
        for image in images:
            image.close()

    extracted_text, _ = _extract_pdf_text(output)
    if _effective_characters(extracted_text):
        raise ValueError(f"扫描 PDF 仍包含文本层: {output}")


def _create_text_docx(text: str, output: Path) -> None:
    """将完整 PDF 原生文本写入普通 DOCX。

    Args:
        text (str): 源 PDF 的完整原生文本。
        output (Path): DOCX 输出路径。

    Raises:
        ValueError: 保存后段落文本与源文本非空白字符不一致时抛出。

    Note:
        DOCX 用于验证文本抽取链路，不用于验证原 PDF 的版式复现能力。
    """
    from docx import Document
    from docx.shared import Pt

    document = Document()
    style = document.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(9)
    for line in text.splitlines():
        document.add_paragraph(line)
    document.save(output)

    saved = Document(output)
    saved_text = "\n".join(paragraph.text for paragraph in saved.paragraphs)
    if _normalize_text(saved_text) != _normalize_text(text):
        raise ValueError(f"DOCX 文本变体内容校验失败: {output}")


def _stack_page_images(source: Path) -> Image.Image:
    """将 PDF 全部页面按页序垂直拼接为单张长图。

    Args:
        source (Path): 源 PDF 文件路径。

    Returns:
        Image.Image: 白底 RGB 长图。

    Raises:
        ValueError: 页面拼接后的像素数超过服务视觉输入预算时抛出。
    """
    from PIL import Image

    pages = _render_pdf_pages(source)
    width = max(page.width for page in pages)
    height = sum(page.height for page in pages) + IMAGE_GAP_PIXELS * (len(pages) - 1)
    if width * height > MAX_BENCHMARK_IMAGE_PIXELS:
        for page in pages:
            page.close()
        raise ValueError(f"拼接图片超过像素预算: {width}x{height}")

    canvas = Image.new("RGB", (width, height), "white")
    offset = 0
    for page in pages:
        canvas.paste(page, ((width - page.width) // 2, offset))
        offset += page.height + IMAGE_GAP_PIXELS
        page.close()
    return canvas


def _create_stacked_image(source: Path, output: Path, image_format: str) -> tuple[int, int]:
    """生成包含全部 PDF 页面的 PNG 或 JPEG 长图。

    Args:
        source (Path): 源 PDF 文件路径。
        output (Path): 图片输出路径。
        image_format (str): `PNG` 或 `JPEG`。

    Returns:
        tuple[int, int]: 输出图片宽度和高度。
    """
    image = _stack_page_images(source)
    try:
        if image_format == "PNG":
            image.save(output, format="PNG", optimize=True)
        else:
            image.save(output, format="JPEG", quality=92, optimize=True)
        return image.size
    finally:
        image.close()


def _load_and_verify_sources(manifest_path: Path, resume_root: Path) -> list[dict]:
    """读取金标准并校验三份源 PDF 的文件摘要。

    Args:
        manifest_path (Path): 私有人工标注清单路径。
        resume_root (Path): 匿名源简历目录。

    Returns:
        list[dict]: 附加路径、页数、全文和字符数的样本对象。

    Raises:
        ValueError: 样本数不是 3、源文件缺失或摘要不一致时抛出。
    """
    manifest = _read_json(manifest_path)
    cases = manifest.get("cases", [])
    if len(cases) != 3:
        raise ValueError("当前 10 份格式基准要求恰好 3 份人工标注源简历")

    sources: list[dict] = []
    for case in cases:
        source_meta = case["source"]
        source_path = resume_root / source_meta["file"]
        if not source_path.is_file():
            raise ValueError(f"源简历不存在: {case['id']}")
        if _sha256(source_path) != source_meta["sha256"]:
            raise ValueError(f"源简历摘要不一致: {case['id']}")
        text, pages = _extract_pdf_text(source_path)
        sources.append({
            "caseId": case["id"],
            "path": source_path,
            "text": text,
            "pages": pages,
            "characters": _effective_characters(text),
        })
    return sorted(sources, key=lambda item: item["characters"])


def prepare_benchmark(manifest_path: Path, resume_root: Path, output_root: Path) -> dict:
    """生成 10 份短、中、长多格式等价简历及匿名测试计划。

    Args:
        manifest_path (Path): 私有人工标注清单路径。
        resume_root (Path): 三份匿名 PDF 所在目录。
        output_root (Path): Git 忽略的基准工作目录。

    Returns:
        dict: 可由容器内 `run` 阶段直接读取的匿名测试计划。
    """
    output_root.mkdir(parents=True, exist_ok=True)
    samples_root = output_root / "samples"
    samples_root.mkdir(parents=True, exist_ok=True)
    short, medium, long = _load_and_verify_sources(manifest_path, resume_root)
    by_length = {"short": short, "medium": medium, "long": long}
    definitions = [
        ("sample-01", "short", "textPdf", "pdf"),
        ("sample-02", "medium", "textPdf", "pdf"),
        ("sample-03", "long", "textPdf", "pdf"),
        ("sample-04", "short", "scannedPdf", "pdf"),
        ("sample-05", "long", "scannedPdf", "pdf"),
        ("sample-06", "medium", "docx", "docx"),
        ("sample-07", "long", "docx", "docx"),
        ("sample-08", "short", "png", "png"),
        ("sample-09", "long", "png", "png"),
        ("sample-10", "medium", "jpeg", "jpg"),
    ]

    samples: list[dict] = []
    for sample_id, length, format_name, extension in definitions:
        source = by_length[length]
        filename = f"{sample_id}-{format_name}-{length}.{extension}"
        output = samples_root / filename
        dimensions: list[int] | None = None
        if format_name == "textPdf":
            shutil.copyfile(source["path"], output)
        elif format_name == "scannedPdf":
            _create_scanned_pdf(source["path"], output)
        elif format_name == "docx":
            _create_text_docx(source["text"], output)
        elif format_name in {"png", "jpeg"}:
            image_format = "PNG" if format_name == "png" else "JPEG"
            dimensions = list(_create_stacked_image(source["path"], output, image_format))
        samples.append({
            "sampleId": sample_id,
            "caseId": source["caseId"],
            "filename": filename,
            "format": format_name,
            "length": length,
            "sourcePages": source["pages"],
            "sourceCharacters": source["characters"],
            "expectedParserMode": (
                "textFirst" if format_name in {"textPdf", "docx"} else "visualFallback"
            ),
            "sizeBytes": output.stat().st_size,
            "sha256": _sha256(output),
            "imageDimensions": dimensions,
        })

    plan = {
        "schemaVersion": "1.0",
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sampleCount": len(samples),
        "renderDpi": RENDER_DPI,
        "samples": samples,
    }
    _write_json(output_root / "benchmark_plan.json", plan)
    print(f"已生成 {len(samples)} 份基准样本: {output_root}")
    return plan


def run_benchmark(
    benchmark_root: Path,
    endpoint: str,
    request_timeout: int,
    sample_ids: list[str] | None = None,
) -> dict:
    """串行调用同步解析接口并保存逐份原始响应和耗时。

    Args:
        benchmark_root (Path): 包含 `benchmark_plan.json` 和 `samples` 的目录。
        endpoint (str): 同步仅解析接口 URL。
        request_timeout (int): 单份请求超时秒数。
        sample_ids (list[str] | None): 可选的待重跑样本 ID；为空时执行全部样本。

    Returns:
        dict: 不包含候选人 PII 的逐样本测量结果。

    Note:
        每份简历单独请求且 `max_concurrency=1`。不做自动重试，以免掩盖真实失败
        和首次调用时延。包含结构化简历的响应仅写入私有 `responses` 目录。
    """
    plan = _read_json(benchmark_root / "benchmark_plan.json")
    responses_root = benchmark_root / "responses"
    responses_root.mkdir(parents=True, exist_ok=True)
    selected_ids = set(sample_ids or [])
    planned_samples = [
        sample
        for sample in plan["samples"]
        if not selected_ids or sample["sampleId"] in selected_ids
    ]
    planned_ids = {sample["sampleId"] for sample in planned_samples}
    if selected_ids and selected_ids != planned_ids:
        missing = sorted(selected_ids - planned_ids)
        raise ValueError(f"测试计划中不存在样本: {', '.join(missing)}")

    measurements_path = benchmark_root / "measurements.json"
    existing_measurements: dict[str, dict] = {}
    if measurements_path.is_file():
        previous_result = _read_json(measurements_path)
        existing_measurements = {
            item["sampleId"]: item
            for item in previous_result.get("measurements", [])
        }
    session = requests.Session()
    session.trust_env = False

    for index, sample in enumerate(planned_samples, start=1):
        sample_path = benchmark_root / "samples" / sample["filename"]
        payload = {
            "resumes": [{
                "type": "file",
                "file": {
                    "name": sample["filename"],
                    "path": str(sample_path),
                    "extra": sample["sampleId"],
                },
            }],
            "max_concurrency": 1,
        }
        started_at = datetime.now().astimezone()
        started_counter = time.perf_counter()
        body: dict = {}
        error = ""
        http_status = 0
        try:
            response = session.post(endpoint, json=payload, timeout=request_timeout)
            http_status = response.status_code
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("接口响应顶层不是 JSON 对象")
        except (requests.RequestException, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        completed_at = datetime.now().astimezone()
        wall_seconds = round(time.perf_counter() - started_counter, 3)
        response_path = responses_root / f"{sample['sampleId']}.json"
        previous_measurement = existing_measurements.get(sample["sampleId"])
        previous_attempts = int((previous_measurement or {}).get("attemptCount", 1))
        if response_path.is_file() and previous_measurement:
            history_path = (
                responses_root
                / "history"
                / f"{sample['sampleId']}-attempt-{previous_attempts}.json"
            )
            history_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(response_path, history_path)
        _write_json(response_path, {"response": body, "transportError": error})
        result_items = body.get("results", []) if isinstance(body, dict) else []
        first_item = result_items[0] if result_items else {}
        first_data = first_item.get("data", {}) if isinstance(first_item, dict) else {}
        success = (
            not error
            and http_status == 200
            and first_item.get("code") == 0
            and isinstance(first_data.get("resume"), dict)
        )
        measurement = {
            "sampleId": sample["sampleId"],
            "attemptCount": previous_attempts + 1 if previous_measurement else 1,
            "priorFailedAttempts": (
                int((previous_measurement or {}).get("priorFailedAttempts", 0))
                + int(
                    bool(previous_measurement)
                    and previous_measurement["status"] != "success"
                )
            ),
            "status": "success" if success else "failed",
            "startedAt": started_at.isoformat(timespec="seconds"),
            "completedAt": completed_at.isoformat(timespec="seconds"),
            "wallSeconds": wall_seconds,
            "apiElapsedSeconds": body.get("elapsed_s"),
            "resumeParseFinishTime": first_data.get("resumeParseFinishTime"),
            "httpStatus": http_status,
            "error": error or first_data.get("errorMsg", ""),
        }
        existing_measurements[sample["sampleId"]] = measurement
        print(
            f"[{index}/{len(planned_samples)}] {sample['sampleId']} "
            f"status={measurement['status']} wallSeconds={wall_seconds}"
        )

    measurements = [
        existing_measurements[sample["sampleId"]]
        for sample in plan["samples"]
        if sample["sampleId"] in existing_measurements
    ]
    result = {
        "schemaVersion": "1.0",
        "endpoint": endpoint,
        "completedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sampleCount": len(measurements),
        "measurements": measurements,
    }
    _write_json(measurements_path, result)
    return result


def _best_list_item(expected: dict, actual_items: list[dict], section: str) -> dict:
    """按业务身份字段选择与人工标注最接近的实际列表项。

    Args:
        expected (dict): 单条教育、工作或项目人工标注。
        actual_items (list[dict]): 实际解析的同类记录。
        section (str): `educations`、`works` 或 `projects`。

    Returns:
        dict: 最佳匹配项；实际列表为空时返回空对象。
    """
    if not actual_items:
        return {}
    identity_keys = LIST_IDENTITIES[section]

    def score(item: dict) -> tuple[int, int]:
        """计算一条实际记录与人工标注的身份字段和全部字段匹配数。

        Args:
            item (dict): 一条实际教育、工作或项目记录。

        Returns:
            tuple[int, int]: 身份字段匹配数和全部标注字段匹配数。
        """
        identity_score = sum(
            1
            for key in identity_keys
            if key in expected
            and _normalize_text(expected[key]) == _normalize_text(item.get(key))
        )
        field_score = sum(
            1
            for key, value in expected.items()
            if _normalize_text(value) == _normalize_text(item.get(key))
        )
        return identity_score, field_score

    return max(actual_items, key=score)


def _compare_resume_fields(expected: dict, actual: dict) -> dict:
    """计算一份简历各部分人工标注字段的规范化精确匹配率。

    Args:
        expected (dict): 人工标注的 `resume` 对象。
        actual (dict): 同步接口输出的 `resume` 对象。

    Returns:
        dict: 各部分和整体的正确数、总数及准确率。

    Note:
        金标准只标注用于回归的关键项目，实际结果中的额外记录不计错；该指标衡量
        已标注字段正确率，不代表未经标注字段的事实准确率。
    """
    counts: dict[str, list[int]] = {
        section: [0, 0] for section in SECTION_NAMES
    }
    candidate = actual.get("candidate") or {}
    for key, expected_value in (expected.get("candidate") or {}).items():
        counts["candidate"][1] += 1
        if _normalize_text(expected_value) == _normalize_text(candidate.get(key)):
            counts["candidate"][0] += 1

    for section in LIST_IDENTITIES:
        actual_items = [
            item for item in (actual.get(section) or []) if isinstance(item, dict)
        ]
        for expected_item in expected.get(section, []) or []:
            matched = _best_list_item(expected_item, actual_items, section)
            for key, expected_value in expected_item.items():
                counts[section][1] += 1
                if _normalize_text(expected_value) == _normalize_text(matched.get(key)):
                    counts[section][0] += 1

    sections = {
        section: {
            "correct": values[0],
            "total": values[1],
            "accuracy": round(values[0] / values[1], 4) if values[1] else 1.0,
        }
        for section, values in counts.items()
    }
    correct = sum(values[0] for values in counts.values())
    total = sum(values[1] for values in counts.values())
    return {
        "correct": correct,
        "total": total,
        "accuracy": round(correct / total, 4) if total else 1.0,
        "sections": sections,
    }


def _duration_summary(values: list[float], precision: int = 3) -> dict:
    """汇总一组成功请求的耗时统计。

    Args:
        values (list[float]): 请求耗时秒数。
        precision (int): 汇总值保留的小数位数。

    Returns:
        dict: 平均值、中位数、最小值和最大值；空列表返回空对象。
    """
    if not values:
        return {}
    return {
        "mean": round(statistics.mean(values), precision),
        "median": round(statistics.median(values), precision),
        "min": round(min(values), precision),
        "max": round(max(values), precision),
    }


def _load_operation_timings(log_root: Path | None) -> dict[str, dict]:
    """从调试日志提取每个匿名样本最后一次成功解析的分段耗时。

    Args:
        log_root (Path | None): 包含轮转日志的根目录；为空时不读取日志。

    Returns:
        dict[str, dict]: 文件名到文本抽取、LLM 和 JSON 转换耗时的映射。

    Note:
        同名样本可能因上游失败而重跑，按日志文件修改时间和行顺序保留最后一次
        完成记录。只有完成日志参与统计，失败尝试不会覆盖成功分段耗时。
    """
    if log_root is None or not log_root.is_dir():
        return {}
    paths = sorted(
        log_root.rglob("debug*.log"),
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
    )
    timings: dict[str, dict] = defaultdict(dict)
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            filename_match = _LOG_FILENAME_RE.search(line)
            if not filename_match:
                continue
            filename = filename_match.group("filename")
            elapsed_match = _ELAPSED_RE.search(line)
            if "[Python文本抽取完成]" in line and elapsed_match:
                timings[filename]["extractionSeconds"] = float(
                    elapsed_match.group("seconds")
                )
            elif "[LLM单模型调用完成]" in line and elapsed_match:
                timings[filename]["llmSeconds"] = float(
                    elapsed_match.group("seconds")
                )
            elif (
                "[本地JSON解码与契约校验完成]" in line
                or "[JSON转换完成]" in line
            ):
                json_match = _JSON_TIMING_RE.search(line)
                if json_match:
                    timings[filename].update({
                        "jsonDecodeSeconds": float(json_match.group("decode")),
                        "postProcessSeconds": float(
                            json_match.group("post_process")
                        ),
                        "jsonConversionSeconds": float(
                            json_match.group("conversion")
                        ),
                    })
    return dict(timings)


def _group_summary(samples: list[dict], group_key: str) -> dict:
    """按格式或长度汇总平均耗时和平均样本准确率。

    Args:
        samples (list[dict]): 已完成准确率计算的匿名样本结果。
        group_key (str): `format` 或 `length`。

    Returns:
        dict: 分组后的样本数、成功数、耗时和准确率。
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        groups[str(sample[group_key])].append(sample)
    result: dict[str, dict] = {}
    for group, items in sorted(groups.items()):
        successful = [item for item in items if item["status"] == "success"]
        result[group] = {
            "sampleCount": len(items),
            "successCount": len(successful),
            "meanWallSeconds": (
                round(statistics.mean(item["wallSeconds"] for item in successful), 3)
                if successful else None
            ),
            "meanFieldAccuracy": round(
                statistics.mean(item["fieldAccuracy"] for item in items),
                4,
            ),
        }
    return result


def _percentage(value: float | None) -> str:
    """将 0 到 1 的指标格式化为两位小数百分比。

    Args:
        value (float | None): 指标值。

    Returns:
        str: 百分比文本；空值返回 `-`。
    """
    return "-" if value is None else f"{value * 100:.2f}%"


def _seconds(value: float | None, precision: int = 3) -> str:
    """将可空秒数格式化为固定精度文本。

    Args:
        value (float | None): 耗时秒数。
        precision (int): 小数位数。

    Returns:
        str: 带 `s` 后缀的耗时；空值返回 `-`。
    """
    return "-" if value is None else f"{value:.{precision}f}s"


def _render_markdown_report(report: dict) -> str:
    """将匿名基准结果渲染为可提交的 Markdown 报告。

    Args:
        report (dict): 已去除 PII 的基准报告对象。

    Returns:
        str: Markdown 文档正文。
    """
    format_names = {
        "textPdf": "文本 PDF",
        "scannedPdf": "扫描 PDF",
        "docx": "DOCX",
        "png": "PNG",
        "jpeg": "JPEG",
    }
    length_names = {"short": "短", "medium": "中", "long": "长"}
    lines = [
        "# 同步解析多格式基准测试报告",
        "",
        f"测试时间：{report['completedAt']}",
        "",
        "## 测试口径",
        "",
        "1. 使用 3 份人工标注真实简历，按原文等价转换为 10 份匿名样本。",
        "2. 覆盖文本 PDF、扫描 PDF、普通 DOCX、PNG 和 JPEG；逐份串行请求同步仅解析接口。",
        "3. 准确率为人工标注字段的规范化精确匹配率，空白、大小写和全半角差异不计错。",
        "4. 金标准只标注关键项目，模型输出的额外未标注记录不计错；单次运行不代表模型稳定性。",
        "",
        "## 总体结果",
        "",
        f"- 最终成功：`{report['successCount']}/{report['sampleCount']}`",
        f"- 首次尝试成功：`{report['firstAttemptSuccessCount']}/{report['sampleCount']}`",
        f"- 上游模型瞬时失败后重跑次数：`{report['priorFailedAttempts']}`",
        f"- 整体字段准确率：`{_percentage(report['overallFieldAccuracy'])}`",
        f"- 平均墙钟耗时：`{report['wallDuration']['mean']:.3f}s`",
        f"- 中位墙钟耗时：`{report['wallDuration']['median']:.3f}s`",
        f"- 最短/最长：`{report['wallDuration']['min']:.3f}s / {report['wallDuration']['max']:.3f}s`",
        f"- 平均本地 JSON 解码与契约校验耗时：`{_seconds(report['jsonConversionDuration'].get('mean'), 6)}`",
        "",
        "## 逐份结果",
        "",
        "| 样本 | 格式 | 长度 | 完成时间 | 预处理 | LLM语义结构化 | JSON解码 | 结构归一 | 本地解码校验 | 接口耗时 | 整体准确率 | 状态 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for sample in report["samples"]:
        lines.append(
            "| {sample} | {format_name} | {length} | {completed} | "
            "{extraction} | {llm} | {json_decode} | {post_process} | "
            "{json_conversion} | {api} | {overall} | {status} |".format(
                sample=sample["sampleId"],
                format_name=format_names[sample["format"]],
                length=length_names[sample["length"]],
                completed=sample["completedAt"],
                extraction=_seconds(sample.get("extractionSeconds")),
                llm=_seconds(sample.get("llmSeconds")),
                json_decode=_seconds(sample.get("jsonDecodeSeconds"), 6),
                post_process=_seconds(sample.get("postProcessSeconds"), 6),
                json_conversion=_seconds(
                    sample.get("jsonConversionSeconds"),
                    6,
                ),
                api=_seconds(sample.get("apiElapsedSeconds")),
                overall=_percentage(sample["fieldAccuracy"]),
                status="成功" if sample["status"] == "success" else "失败",
            )
        )

    lines.extend(["", "## 分组结果", "", "### 按格式", ""])
    lines.extend([
        "| 格式 | 样本数 | 成功数 | 平均耗时 | 平均样本准确率 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    for group, values in report["byFormat"].items():
        mean_duration = values["meanWallSeconds"]
        lines.append(
            f"| {format_names[group]} | {values['sampleCount']} | {values['successCount']} | "
            f"{mean_duration:.3f}s | {_percentage(values['meanFieldAccuracy'])} |"
        )

    lines.extend(["", "### 按长度", ""])
    lines.extend([
        "| 长度 | 样本数 | 成功数 | 平均耗时 | 平均样本准确率 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ])
    for group, values in report["byLength"].items():
        mean_duration = values["meanWallSeconds"]
        lines.append(
            f"| {length_names[group]} | {values['sampleCount']} | {values['successCount']} | "
            f"{mean_duration:.3f}s | {_percentage(values['meanFieldAccuracy'])} |"
        )

    lines.extend([
        "",
        "## 解释限制",
        "",
        "- DOCX 样本由完整 PDF 原生文本生成，用于验证 DOCX 文本链路，不代表复杂 Word 版式覆盖率。",
        "- PNG/JPEG 为全部页面垂直拼接的长图；扫描 PDF 为逐页栅格化后的等价文件。",
        f"- 共 {report['priorFailedAttempts']} 次请求遇到上游模型网关 `503 model_not_found`，通道恢复后只重跑失败样本；耗时统计使用成功尝试。",
        "- 每份有效结果只运行一次。模型存在非确定性，发布判断还需至少 3 次重复测试并检查最差值。",
        "- 原始响应、人工标注和变体文件保存在 Git 忽略目录，不进入本报告。",
        "- `本地解码校验` 等于 JSON 文本解码加字段后处理、Schema 补全和 Pydantic 契约归一，不包含 LLM 语义结构化。",
        "",
    ])
    return "\n".join(lines)


def build_report(
    manifest_path: Path,
    benchmark_root: Path,
    private_report_path: Path,
    markdown_report_path: Path,
    timing_log_root: Path | None = None,
) -> dict:
    """计算 10 份样本的准确率并输出私有 JSON 和匿名 Markdown。

    Args:
        manifest_path (Path): 私有人工标注清单路径。
        benchmark_root (Path): 包含计划、测量和原始响应的工作目录。
        private_report_path (Path): 私有详细 JSON 报告路径。
        markdown_report_path (Path): 可提交的匿名 Markdown 报告路径。
        timing_log_root (Path | None): 可选的服务调试日志根目录。

    Returns:
        dict: 不包含候选人字段值和差异明细的汇总报告。
    """
    manifest = _read_json(manifest_path)
    expected_by_case = {
        case["id"]: case["expected"]["resume"] for case in manifest["cases"]
    }
    plan = _read_json(benchmark_root / "benchmark_plan.json")
    measurements = _read_json(benchmark_root / "measurements.json")
    measured_by_id = {
        item["sampleId"]: item for item in measurements["measurements"]
    }
    operation_timings = _load_operation_timings(timing_log_root)
    samples: list[dict] = []
    aggregate_correct = 0
    aggregate_total = 0

    for planned in plan["samples"]:
        measurement = measured_by_id[planned["sampleId"]]
        raw = _read_json(
            benchmark_root / "responses" / f"{planned['sampleId']}.json"
        )
        response = raw.get("response") or {}
        result_items = response.get("results", []) if isinstance(response, dict) else []
        first_item = result_items[0] if result_items else {}
        data = first_item.get("data", {}) if isinstance(first_item, dict) else {}
        actual = data.get("resume") if isinstance(data.get("resume"), dict) else {}
        comparison = _compare_resume_fields(
            expected_by_case[planned["caseId"]],
            actual,
        )
        aggregate_correct += comparison["correct"]
        aggregate_total += comparison["total"]
        samples.append({
            **planned,
            **measurement,
            "fieldCorrect": comparison["correct"],
            "fieldTotal": comparison["total"],
            "fieldAccuracy": comparison["accuracy"],
            "sections": comparison["sections"],
            "actualCounts": {
                "educations": len(actual.get("educations") or []),
                "works": len(actual.get("works") or []),
                "projects": len(actual.get("projects") or []),
            },
            **operation_timings.get(planned["filename"], {}),
        })

    successful_durations = [
        sample["wallSeconds"]
        for sample in samples
        if sample["status"] == "success"
    ]
    json_conversion_durations = [
        sample["jsonConversionSeconds"]
        for sample in samples
        if isinstance(sample.get("jsonConversionSeconds"), (int, float))
    ]
    report = {
        "schemaVersion": "1.0",
        "datasetVersion": manifest["datasetVersion"],
        "completedAt": measurements["completedAt"],
        "sampleCount": len(samples),
        "successCount": sum(sample["status"] == "success" for sample in samples),
        "failedCount": sum(sample["status"] != "success" for sample in samples),
        "firstAttemptSuccessCount": sum(
            sample["status"] == "success"
            and int(sample.get("priorFailedAttempts", 0)) == 0
            for sample in samples
        ),
        "priorFailedAttempts": sum(
            sample.get("priorFailedAttempts", 0) for sample in samples
        ),
        "overallFieldAccuracy": round(
            aggregate_correct / aggregate_total,
            4,
        ) if aggregate_total else 1.0,
        "fieldCorrect": aggregate_correct,
        "fieldTotal": aggregate_total,
        "wallDuration": _duration_summary(successful_durations),
        "jsonConversionDuration": _duration_summary(
            json_conversion_durations,
            precision=6,
        ),
        "byFormat": _group_summary(samples, "format"),
        "byLength": _group_summary(samples, "length"),
        "samples": samples,
    }
    _write_json(private_report_path, report)
    markdown_report_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_report_path.write_text(
        _render_markdown_report(report),
        encoding="utf-8",
    )
    print(f"私有报告: {private_report_path}")
    print(f"匿名报告: {markdown_report_path}")
    return report


def _build_parser() -> argparse.ArgumentParser:
    """构造三阶段命令行参数解析器。

    Returns:
        argparse.ArgumentParser: 已配置 `prepare`、`run` 和 `report` 子命令的解析器。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="生成 10 份格式等价样本")
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--resume-root", type=Path, required=True)
    prepare.add_argument("--output-root", type=Path, required=True)

    run = subparsers.add_parser("run", help="逐份串行调用同步解析接口")
    run.add_argument("--benchmark-root", type=Path, required=True)
    run.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    run.add_argument("--request-timeout", type=int, default=900)
    run.add_argument(
        "--sample-ids",
        nargs="+",
        help="只重跑指定匿名样本 ID，并保留其他样本的已有结果",
    )

    report = subparsers.add_parser("report", help="计算字段准确率并生成匿名报告")
    report.add_argument("--manifest", type=Path, required=True)
    report.add_argument("--benchmark-root", type=Path, required=True)
    report.add_argument("--private-report", type=Path, required=True)
    report.add_argument("--markdown-report", type=Path, required=True)
    report.add_argument(
        "--timing-log-root",
        type=Path,
        help="可选的服务调试日志根目录，用于提取 LLM 和 JSON 转换耗时",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行选定的基准测试阶段。

    Args:
        argv (Sequence[str] | None): 可选命令行参数；为空时读取进程参数。

    Returns:
        int: 阶段成功返回 0；异常由调用环境显示并返回非零状态。
    """
    args = _build_parser().parse_args(argv)
    if args.command == "prepare":
        prepare_benchmark(args.manifest, args.resume_root, args.output_root)
    elif args.command == "run":
        run_benchmark(
            args.benchmark_root,
            args.endpoint,
            args.request_timeout,
            args.sample_ids,
        )
    else:
        build_report(
            args.manifest,
            args.benchmark_root,
            args.private_report,
            args.markdown_report,
            args.timing_log_root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
