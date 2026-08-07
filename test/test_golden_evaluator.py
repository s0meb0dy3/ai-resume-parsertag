"""验证真实简历黄金集契约、隐私保护和指标计算。"""

import hashlib

import pytest

from evaluation.golden_evaluator import (
    GoldenManifestError,
    evaluate_predictions,
    validate_manifest,
    verify_source_files,
)


def _manifest() -> dict:
    """构造不含真实个人信息的最小黄金集清单。

    Returns:
        dict: 可用于契约和指标测试的清单。
    """
    return {
        "schemaVersion": "1.0",
        "datasetVersion": "test-v1",
        "thresholds": {
            "fieldAccuracy": 1.0,
            "positiveMicroF1": 1.0,
            "negativeF1": 1.0,
        },
        "cases": [{
            "id": "resume-001",
            "source": {
                "file": "resume-001.pdf",
                "sha256": "0" * 64,
            },
            "expected": {
                "resume": {
                    "candidate": {"name": "候选人甲", "age": 28},
                    "educations": [{
                        "school": "示例大学",
                        "education": "本科",
                        "startDate": "2016-09",
                        "endDate": "2020-06",
                    }],
                    "works": [],
                    "projects": [],
                },
                "positiveTags": {
                    "education": ["本科"],
                    "skills": ["Python"],
                },
                "negativeTags": [{
                    "dimension": "jobstable",
                    "tag": "工作空窗期过长",
                }],
            },
        }],
    }


def _prediction() -> dict:
    """构造与最小黄金标注完全一致的服务结果。

    Returns:
        dict: 使用同步接口嵌套层级的预测结果。
    """
    return {
        "code": 0,
        "data": {
            "result": {
                "candidate": {"name": "候选人甲", "age": 28},
                "educations": [{
                    "school": "示例大学",
                    "education": "本科",
                    "startDate": "2016-09",
                    "endDate": "2020-06",
                }],
                "works": [],
                "projects": [],
                "positiveTags": {
                    "tagsArray": [
                        {
                            "dimension": "education",
                            "value": [{"tag": "本科", "source": "rule"}],
                        },
                        {
                            "dimension": "skills",
                            "value": [{"tag": "Python", "source": "rule"}],
                        },
                    ],
                },
                "negativeTags": {
                    "tagsArray": [{
                        "dimension": "jobstable",
                        "value": [{"tag": "工作空窗期过长"}],
                    }],
                },
            },
        },
    }


def test_public_manifest_rejects_pii_and_raw_text():
    """验证公共清单拒绝电话号码、真实姓名和原始全文字段。"""
    phone_manifest = _manifest()
    phone_manifest["cases"][0]["expected"]["resume"]["candidate"]["phone"] = "13800138000"
    with pytest.raises(GoldenManifestError, match="PII"):
        validate_manifest(phone_manifest)

    name_manifest = _manifest()
    name_manifest["cases"][0]["expected"]["resume"]["candidate"]["name"] = "张三"
    with pytest.raises(GoldenManifestError, match="姓名必须匿名化"):
        validate_manifest(name_manifest)

    raw_manifest = _manifest()
    raw_manifest["cases"][0]["expected"]["rawText"] = "完整简历正文"
    with pytest.raises(GoldenManifestError, match="原始全文"):
        validate_manifest(raw_manifest, allow_pii=True)


def test_source_file_sha256_is_verified(tmp_path):
    """验证源文件必须与清单中的 SHA-256 完全一致。"""
    source = tmp_path / "resume-001.pdf"
    content = b"synthetic-pdf-placeholder"
    source.write_bytes(content)
    manifest = _manifest()
    manifest["cases"][0]["source"]["sha256"] = hashlib.sha256(content).hexdigest()

    assert verify_source_files(manifest, tmp_path) == ["resume-001"]

    manifest["cases"][0]["source"]["sha256"] = "f" * 64
    with pytest.raises(GoldenManifestError, match="摘要不一致"):
        verify_source_files(manifest, tmp_path)


def test_perfect_prediction_passes_all_thresholds():
    """验证完全一致的解析和标签结果获得满分。"""
    manifest = _manifest()
    validate_manifest(manifest)

    report = evaluate_predictions(manifest, {"resume-001": _prediction()})

    assert report["passed"] is True
    assert report["metrics"]["fieldAccuracy"] == 1.0
    assert report["metrics"]["positiveTags"]["micro"]["f1"] == 1.0
    assert report["metrics"]["negativeTags"]["f1"] == 1.0
    assert report["diagnostics"][0]["differences"] == []


def test_missing_skill_fails_positive_threshold():
    """验证技能漏标会降低召回率并使发布阈值失败。"""
    prediction = _prediction()
    tags = prediction["data"]["result"]["positiveTags"]["tagsArray"]
    tags[1]["value"] = []

    report = evaluate_predictions(_manifest(), {"resume-001": prediction})

    assert report["passed"] is False
    assert report["metrics"]["positiveTags"]["micro"]["recall"] == 0.5
    assert report["thresholdChecks"]["positiveMicroF1"] is False
