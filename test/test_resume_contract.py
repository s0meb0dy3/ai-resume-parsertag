"""验证解析和打标入口共享的标准简历数据契约。"""

import pytest

from api.schemas import ParsedResumeItem
from core.resume_contract import ResumeContractError, normalize_resume_payload


def test_null_resume_collections_are_normalized():
    """验证可空候选人对象和经历集合归一为稳定默认容器。"""
    result = normalize_resume_payload({
        "candidate": None,
        "educations": None,
        "works": None,
        "projects": None,
    })

    assert result == {
        "candidate": {
            "name": "", "phone": "", "email": "", "gender": 0, "age": 0,
            "workYears": 0,
            "address": "", "nativePlace": "", "selfEvaluation": "",
            "expectedSalary": "", "expectedPosition": "",
        },
        "educations": [],
        "works": [],
        "projects": [],
    }


@pytest.mark.parametrize("field", ["educations", "works", "projects"])
def test_collection_scalars_are_rejected_before_tagging(field):
    """验证经历集合标量在进入打标前被统一拒绝。

    Args:
        field (str): 本轮参数化验证的简历集合字段名。
    """
    with pytest.raises(ResumeContractError, match=field):
        normalize_resume_payload({field: "无"})


def test_api_schema_uses_the_same_nested_resume_contract():
    """验证 API Schema 与核心归一函数使用相同嵌套字段约束。"""
    item = ParsedResumeItem.model_validate({
        "filename": "resume.pdf",
        "resume": {"candidate": None, "works": None},
    })
    assert item.resume.works == []
    assert item.resume.candidate.name == ""
    assert item.resume.candidate.workYears == 0

    with pytest.raises(ValueError, match="works"):
        ParsedResumeItem.model_validate({
            "filename": "resume.pdf",
            "resume": {"works": 1},
        })


@pytest.mark.parametrize(
    ("value", "expected"),
    [(7, 7), ("7", 7), (None, 0), ("未知", 0), (-2, 0)],
)
def test_candidate_work_years_is_normalized_to_non_negative_integer(value, expected):
    """验证模型工作年限统一为非负整数。"""
    result = normalize_resume_payload({"candidate": {"workYears": value}})

    assert result["candidate"]["workYears"] == expected


def test_empty_work_end_date_is_not_inferred_as_present():
    """验证缺失的工作结束日期保持为空，不被推断为当前任职。"""
    result = normalize_resume_payload({
        "works": [
            {
                "company": "A公司", "position": "工程师",
                "startDate": "2020-01", "endDate": "2022-06",
            },
            {
                "company": "B公司", "position": "高级工程师",
                "startDate": "2022-07", "endDate": "",
            },
        ],
    })

    assert result["works"][1]["endDate"] == ""


def test_work_keeps_original_public_schema():
    """验证归一化不会向工作经历公开结构增加任职类型字段。"""
    result = normalize_resume_payload({
        "works": [{
            "company": "A公司",
            "position": "软件工程师（实习）",
            "description": "参与后端开发",
            "startDate": "2025-01",
            "endDate": "2025-06",
            "employmentType": "internship",
        }],
    })

    assert set(result["works"][0]) == {
        "company", "position", "description", "startDate", "endDate",
    }


@pytest.mark.parametrize("current_value", ["目前", "现在", "在职", "present", "CURRENT"])
def test_current_work_end_date_aliases_are_normalized(current_value):
    """验证中英文当前任职结束日期别名统一为至今。"""
    result = normalize_resume_payload({
        "works": [{
            "company": "A公司", "position": "工程师",
            "startDate": "2024-01", "endDate": current_value,
        }],
    })
    assert result["works"][0]["endDate"] == "至今"


def test_all_empty_work_end_dates_remain_empty():
    """验证多个或历史工作结束日期缺失时均保持为空。"""
    multiple = normalize_resume_payload({
        "works": [
            {"company": "A", "position": "工程师", "startDate": "2020-01"},
            {"company": "B", "position": "工程师", "startDate": "2022-01"},
        ],
    })
    historical = normalize_resume_payload({
        "works": [
            {"company": "A", "position": "工程师", "startDate": "2020-01"},
            {
                "company": "B", "position": "工程师", "startDate": "2022-01",
                "endDate": "2024-01",
            },
        ],
    })

    assert [work["endDate"] for work in multiple["works"]] == ["", ""]
    assert historical["works"][0]["endDate"] == ""
