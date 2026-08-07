"""校验黄金集清单并计算简历解析与标签指标。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any


_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_IDENTITY_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_FORBIDDEN_RAW_KEYS = {"rawText", "sourceText", "skillEvidenceText", "documentText"}
_LIST_IDENTITIES = {
    "educations": ("school", "startDate", "endDate"),
    "works": ("company", "startDate", "endDate"),
    "projects": ("projectName", "company", "startDate", "endDate"),
}


class GoldenManifestError(ValueError):
    """表示黄金集清单格式、隐私或源文件校验失败。"""


def _walk_items(value: Any, path: str = "$"):
    """递归遍历 JSON 值并返回字段路径和值。

    Args:
        value (Any): 当前 JSON 节点。
        path (str): 当前节点路径。

    Yields:
        tuple[str, Any]: 叶子节点的 JSON 路径和值。
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _FORBIDDEN_RAW_KEYS:
                yield f"{path}.{key}", item
            yield from _walk_items(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_items(item, f"{path}[{index}]")
    else:
        yield path, value


def validate_manifest(payload: dict, allow_pii: bool = False) -> None:
    """校验黄金集版本、样本键、隐私字段和阈值。

    Args:
        payload (dict): 待校验的黄金集清单。
        allow_pii (bool): 是否允许私有清单包含人工标注的个人信息。

    Raises:
        GoldenManifestError: 清单格式无效或公共清单包含 PII 时抛出。

    Note:
        即使允许 PII，原始简历全文字段仍然禁止写入清单。真实文件应只保存在
        `GOLDEN_RESUME_ROOT` 指向的外部目录。
    """
    if not isinstance(payload, dict):
        raise GoldenManifestError("黄金集清单顶层必须是对象")
    if payload.get("schemaVersion") != "1.0":
        raise GoldenManifestError("schemaVersion 必须为 1.0")
    if not str(payload.get("datasetVersion", "")).strip():
        raise GoldenManifestError("datasetVersion 不能为空")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise GoldenManifestError("cases 必须是非空数组")
    thresholds = payload.get("thresholds", {})
    if not isinstance(thresholds, dict) or any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not 0 <= value <= 1
        for value in thresholds.values()
    ):
        raise GoldenManifestError("thresholds 必须是 0 到 1 的数值对象")

    case_ids: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise GoldenManifestError(f"cases[{index}] 必须是对象")
        case_id = str(case.get("id", "")).strip()
        source = case.get("source")
        expected = case.get("expected")
        if not case_id or case_id in case_ids:
            raise GoldenManifestError(f"样本 ID 为空或重复: {case_id}")
        if not isinstance(source, dict) or not isinstance(expected, dict):
            raise GoldenManifestError(f"样本 {case_id} 缺少 source/expected 对象")
        file_name = str(source.get("file", ""))
        sha256 = str(source.get("sha256", ""))
        if (
            Path(file_name).name != file_name
            or not re.fullmatch(r"resume-[a-z0-9][a-z0-9_-]*\.(?:pdf|docx)", file_name, re.IGNORECASE)
        ):
            raise GoldenManifestError(
                f"样本 {case_id} 的 source.file 必须使用匿名 resume-* 文件名"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise GoldenManifestError(f"样本 {case_id} 的 sha256 无效")
        case_ids.add(case_id)

    for path, value in _walk_items(payload):
        if path.rsplit(".", 1)[-1] in _FORBIDDEN_RAW_KEYS:
            raise GoldenManifestError(f"清单禁止保存原始全文字段: {path}")
        if allow_pii or not isinstance(value, str):
            continue
        if _PHONE_RE.search(value) or _EMAIL_RE.search(value) or _IDENTITY_RE.search(value):
            raise GoldenManifestError(f"公共清单疑似包含 PII: {path}")
        if path.endswith(".candidate.name") and not value.startswith("候选人"):
            raise GoldenManifestError(f"公共清单姓名必须匿名化: {path}")


def load_manifest(path: str | Path, allow_pii: bool = False) -> dict:
    """读取并校验黄金集清单。

    Args:
        path (str | Path): 黄金集 JSON 路径。
        allow_pii (bool): 是否允许私有人工标注包含 PII。

    Returns:
        dict: 已通过契约与隐私校验的清单。
    """
    with Path(path).open(encoding="utf-8") as file:
        payload = json.load(file)
    validate_manifest(payload, allow_pii=allow_pii)
    return payload


def verify_source_files(manifest: dict, resume_root: str | Path) -> list[str]:
    """校验真实简历文件是否存在且 SHA-256 与清单一致。

    Args:
        manifest (dict): 已校验的黄金集清单。
        resume_root (str | Path): 保存重命名真实简历的外部目录。

    Returns:
        list[str]: 所有校验通过的样本 ID。

    Raises:
        GoldenManifestError: 文件缺失、越界或摘要不一致时抛出。
    """
    root = Path(resume_root).resolve()
    verified: list[str] = []
    for case in manifest["cases"]:
        source = (root / case["source"]["file"]).resolve()
        if source.parent != root or not source.is_file():
            raise GoldenManifestError(f"黄金集源文件不存在: {case['id']}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != case["source"]["sha256"]:
            raise GoldenManifestError(f"黄金集源文件摘要不一致: {case['id']}")
        verified.append(case["id"])
    return verified


def _unwrap_prediction(payload: dict) -> dict:
    """兼容结果文件、同步响应和异步轮询响应的嵌套层级。

    Args:
        payload (dict): 评测结果 JSON。

    Returns:
        dict: 直接包含 candidate、正向和负向标签的结果对象。
    """
    current = payload
    for _ in range(3):
        if "candidate" in current:
            return current
        data = current.get("data")
        if isinstance(data, dict):
            current = data
            continue
        result = current.get("result")
        if isinstance(result, dict):
            current = result
            continue
        break
    result = current.get("result") if isinstance(current, dict) else None
    return result if isinstance(result, dict) else current


def _normalize_scalar(value: Any) -> Any:
    """规范化用于精确比较的标量值。

    Args:
        value (Any): 解析结果或人工标注值。

    Returns:
        Any: 字符串统一全半角、大小写和布局空白，其他标量保持原值。
    """
    if not isinstance(value, str):
        return value
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", "", normalized)


def _best_list_item(expected: dict, actual_items: list[dict], section: str) -> dict:
    """按业务身份字段选择最接近的实际列表项。

    Args:
        expected (dict): 单条教育、工作或项目人工标注。
        actual_items (list[dict]): 实际解析列表。
        section (str): `educations`、`works` 或 `projects`。

    Returns:
        dict: 最佳匹配项；没有候选时返回空对象。
    """
    if not actual_items:
        return {}
    identity_keys = _LIST_IDENTITIES[section]

    def score(item: dict) -> tuple[int, int]:
        identity_score = sum(
            1 for key in identity_keys
            if key in expected
            and _normalize_scalar(expected[key]) == _normalize_scalar(item.get(key))
        )
        field_score = sum(
            1 for key, value in expected.items()
            if _normalize_scalar(value) == _normalize_scalar(item.get(key))
        )
        return identity_score, field_score

    return max(actual_items, key=score)


def _positive_tag_sets(payload: dict) -> dict[str, set[str]]:
    """把正向标签对象转换为维度到标准值集合。

    Args:
        payload (dict): 黄金标注或实际结果对象。

    Returns:
        dict[str, set[str]]: 各维度标签集合。
    """
    positive = payload.get("positiveTags", {})
    if isinstance(positive, dict) and "tagsArray" not in positive:
        return {
            str(dimension): {str(value) for value in values}
            for dimension, values in positive.items()
            if isinstance(values, list)
        }
    entries = positive.get("tagsArray", []) if isinstance(positive, dict) else []
    result: dict[str, set[str]] = defaultdict(set)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        dimension = str(entry.get("dimension", ""))
        for item in entry.get("value", []) or []:
            tag = item.get("tag") if isinstance(item, dict) else item
            if dimension and tag:
                result[dimension].add(str(tag))
    return dict(result)


def _negative_tag_set(payload: dict) -> set[tuple[str, str]]:
    """把负向标签转换为维度和标签名二元组集合。

    Args:
        payload (dict): 黄金标注或实际结果对象。

    Returns:
        set[tuple[str, str]]: 去重后的风险标签集合。
    """
    negative = payload.get("negativeTags", [])
    if isinstance(negative, dict):
        entries = negative.get("tagsArray", [])
    else:
        entries = negative
    result: set[tuple[str, str]] = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        dimension = str(entry.get("dimension", ""))
        if "tag" in entry:
            result.add((dimension, str(entry["tag"])))
        for item in entry.get("value", []) or []:
            tag = item.get("tag") if isinstance(item, dict) else item
            if dimension and tag:
                result.add((dimension, str(tag)))
    return result


def _prf(true_positive: int, false_positive: int, false_negative: int) -> dict:
    """根据计数计算 Precision、Recall 和 F1。

    Args:
        true_positive (int): 正确预测数量。
        false_positive (int): 多预测数量。
        false_negative (int): 漏预测数量。

    Returns:
        dict: 三项取值为 0 到 1 的指标。
    """
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def evaluate_predictions(manifest: dict, predictions: dict[str, dict]) -> dict:
    """计算解析字段和正负标签的聚合指标及逐样本差异。

    Args:
        manifest (dict): 已校验的黄金集清单。
        predictions (dict[str, dict]): 样本 ID 到实际结果 JSON 的映射。

    Returns:
        dict: 聚合指标、阈值结论和逐样本差异。

    Raises:
        GoldenManifestError: 任一样本缺少预测结果时抛出。
    """
    field_correct = 0
    field_total = 0
    positive_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    negative_counts = [0, 0, 0]
    diagnostics: list[dict] = []

    for case in manifest["cases"]:
        case_id = case["id"]
        if case_id not in predictions:
            raise GoldenManifestError(f"缺少预测结果: {case_id}")
        expected = case["expected"]
        actual = _unwrap_prediction(predictions[case_id])
        case_diffs: list[dict] = []
        expected_resume = expected.get("resume", {})

        for key, expected_value in expected_resume.get("candidate", {}).items():
            actual_value = (actual.get("candidate") or {}).get(key)
            field_total += 1
            if _normalize_scalar(expected_value) == _normalize_scalar(actual_value):
                field_correct += 1
            else:
                case_diffs.append({
                    "path": f"candidate.{key}",
                    "expected": expected_value,
                    "actual": actual_value,
                })

        for section in _LIST_IDENTITIES:
            actual_items = actual.get(section) or []
            for index, expected_item in enumerate(expected_resume.get(section, [])):
                matched = _best_list_item(expected_item, actual_items, section)
                for key, expected_value in expected_item.items():
                    actual_value = matched.get(key)
                    field_total += 1
                    if _normalize_scalar(expected_value) == _normalize_scalar(actual_value):
                        field_correct += 1
                    else:
                        case_diffs.append({
                            "path": f"{section}[{index}].{key}",
                            "expected": expected_value,
                            "actual": actual_value,
                        })

        expected_positive = _positive_tag_sets(expected)
        actual_positive = _positive_tag_sets(actual)
        for dimension in expected_positive.keys() | actual_positive.keys():
            expected_values = expected_positive.get(dimension, set())
            actual_values = actual_positive.get(dimension, set())
            counts = positive_counts[dimension]
            counts[0] += len(expected_values & actual_values)
            counts[1] += len(actual_values - expected_values)
            counts[2] += len(expected_values - actual_values)
            if expected_values != actual_values:
                case_diffs.append({
                    "path": f"positiveTags.{dimension}",
                    "missing": sorted(expected_values - actual_values),
                    "unexpected": sorted(actual_values - expected_values),
                })

        expected_negative = _negative_tag_set(expected)
        actual_negative = _negative_tag_set(actual)
        negative_counts[0] += len(expected_negative & actual_negative)
        negative_counts[1] += len(actual_negative - expected_negative)
        negative_counts[2] += len(expected_negative - actual_negative)
        if expected_negative != actual_negative:
            case_diffs.append({
                "path": "negativeTags",
                "missing": sorted(expected_negative - actual_negative),
                "unexpected": sorted(actual_negative - expected_negative),
            })
        diagnostics.append({"id": case_id, "differences": case_diffs})

    total_positive = [
        sum(counts[index] for counts in positive_counts.values())
        for index in range(3)
    ]
    per_dimension = {
        dimension: _prf(*counts)
        for dimension, counts in sorted(positive_counts.items())
    }
    macro_f1 = (
        sum(metrics["f1"] for metrics in per_dimension.values()) / len(per_dimension)
        if per_dimension else 1.0
    )
    metrics = {
        "fieldAccuracy": round(field_correct / field_total, 4) if field_total else 1.0,
        "positiveTags": {
            "micro": _prf(*total_positive),
            "macroF1": round(macro_f1, 4),
            "perDimension": per_dimension,
        },
        "negativeTags": _prf(*negative_counts),
    }
    thresholds = manifest.get("thresholds", {})
    checks = {
        "fieldAccuracy": metrics["fieldAccuracy"] >= thresholds.get("fieldAccuracy", 0),
        "positiveMicroF1": metrics["positiveTags"]["micro"]["f1"]
        >= thresholds.get("positiveMicroF1", 0),
        "negativeF1": metrics["negativeTags"]["f1"] >= thresholds.get("negativeF1", 0),
    }
    return {
        "datasetVersion": manifest["datasetVersion"],
        "caseCount": len(manifest["cases"]),
        "metrics": metrics,
        "thresholdChecks": checks,
        "passed": all(checks.values()),
        "diagnostics": diagnostics,
    }


def load_predictions(prediction_root: str | Path, case_ids: list[str]) -> dict[str, dict]:
    """按样本 ID 从目录加载预生成结果文件。

    Args:
        prediction_root (str | Path): 保存 `<sample-id>.json` 的目录。
        case_ids (list[str]): 黄金集样本 ID。

    Returns:
        dict[str, dict]: 样本 ID 到预测 JSON 的映射。

    Raises:
        GoldenManifestError: 预测文件缺失或不是 JSON 对象时抛出。
    """
    root = Path(prediction_root)
    predictions: dict[str, dict] = {}
    for case_id in case_ids:
        path = root / f"{case_id}.json"
        if not path.is_file():
            raise GoldenManifestError(f"预测文件不存在: {path}")
        with path.open(encoding="utf-8") as file:
            payload = json.load(file)
        if not isinstance(payload, dict):
            raise GoldenManifestError(f"预测文件顶层必须是对象: {path}")
        predictions[case_id] = payload
    return predictions
