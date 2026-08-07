"""验证正向标签在中间结果、最终响应和日志中的统一展示顺序。"""

import asyncio
from unittest.mock import patch

from api.routes_openai import sync_tag_resumes
from api.schemas import SyncTagRequest
from core.postprocess import _conform_to_template
from core.tasks import _log_resume_tag_result
from tag.tag_config import (
    MAX_SKILL_TAGS,
    SKILL_ALLOWED_VALUES,
    sort_positive_tag_entries,
)
from tag.tagger import _normalize_positive_tags, _tagger_load_cfg


EXPECTED_DIMENSIONS = [
    "education",
    "companyBg",
    "workYears",
    "skills",
    "industry",
]

EXPECTED_DISPLAY_NAMES = [
    "教育背景",
    "公司背景",
    "工作年限",
    "技能标签",
    "行业背景",
]


def _scrambled_positive_tags() -> list[dict]:
    """构造包含全部内置维度但顺序混乱的正向标签。

    Returns:
        list[dict]: 用于排序、响应和日志测试的正向标签列表。
    """
    return [
        {
            "dimension": "industry",
            "industryValueArray": ["人工智能/数据智能"],
            "source": "ai",
            "confidence": 0.85,
        },
        {
            "dimension": "skills",
            "skillsValueArray": ["Python"],
            "source": "ai",
        },
        {
            "dimension": "workYears",
            "workYearsValueArray": ["1-3年（满1年）"],
            "source": "rule",
        },
        {
            "dimension": "companyBg",
            "companyBgValueArray": ["行业龙头"],
            "source": "ai",
            "confidence": 0.90,
        },
        {
            "dimension": "education",
            "educationValueArray": ["985", "本科"],
            "source": "ai",
        },
    ]


def test_positive_tag_sorter_uses_product_display_order():
    """验证统一排序器按五个内置维度的产品顺序返回新列表。"""
    original = _scrambled_positive_tags()
    sorted_tags = sort_positive_tag_entries(original)

    assert [item["dimension"] for item in sorted_tags] == EXPECTED_DIMENSIONS
    assert [item["dimension"] for item in original] != EXPECTED_DIMENSIONS


def test_normalized_positive_tags_keep_required_dimension_order():
    """验证工作年限规则覆盖后不会再次移动到教育背景之前。"""
    resume = {
        "candidate": {"name": "张三"},
        "educations": [{
            "school": "北京大学",
            "education": "本科",
            "startDate": "2015-09",
            "endDate": "2019-07",
        }],
        "works": [{
            "company": "测试公司",
            "position": "开发工程师",
            "startDate": "2020-01",
            "endDate": "2022-01",
        }],
        "projects": [],
    }
    normalized = _normalize_positive_tags(
        _scrambled_positive_tags(),
        resume,
        _tagger_load_cfg()["positiveDimensions"],
    )

    assert [item["dimension"] for item in normalized] == EXPECTED_DIMENSIONS


def test_final_response_uses_required_display_order_and_names():
    """验证最终 API 标签数组按指定顺序输出对应中文显示名。"""
    result = _conform_to_template(
        "task-id",
        {
            "candidate": {"name": "张三"},
            "educations": [{"school": "北京大学", "education": "本科"}],
            "works": [],
            "projects": [],
        },
        {
            "positiveTags": _scrambled_positive_tags(),
            "confidence": 0.88,
            "hitTags": [],
            "hitCount": 0,
            "deductionScore": 0,
            "tagParseTime": "2026-08-03 16:30:00",
        },
        "2026-08-03 16:29:00",
    )
    entries = result["positiveTags"]["tagsArray"]

    assert [item["dimension"] for item in entries] == EXPECTED_DIMENSIONS
    assert [item["displayName"] for item in entries] == EXPECTED_DISPLAY_NAMES


def test_positive_tag_log_uses_required_display_order_and_names():
    """验证可读打标日志按指定顺序展示五个中文维度名称。"""
    tag_info = {
        "positiveTags": _scrambled_positive_tags(),
        "confidence": 0.88,
        "hitTags": [],
        "hitCount": 0,
        "deductionScore": 0,
        "tagParseTime": "2026-08-03 16:30:00",
    }

    with patch("core.tasks.logger.debug") as debug:
        _log_resume_tag_result(
            "task-id",
            "张三.pdf",
            tag_info,
            "2026-08-03 16:29:00",
            1.2,
            2.3,
        )

    tag_lines = [
        str(call.args[0])
        for call in debug.call_args_list
        if str(call.args[0]).startswith("│  [")
    ]
    assert [line.split("]", 1)[0][4:] for line in tag_lines] == (
        EXPECTED_DISPLAY_NAMES
    )


def test_final_response_limits_skill_tags_to_twelve():
    """验证最终响应对绕过归一层的技能列表仍执行 12 项上限。"""
    source_skills = list(SKILL_ALLOWED_VALUES[:MAX_SKILL_TAGS + 3])
    result = _conform_to_template(
        "task-id",
        {
            "candidate": {"name": "张三"},
            "educations": [],
            "works": [],
            "projects": [],
        },
        {
            "positiveTags": [{
                "dimension": "skills",
                "skillsValueArray": source_skills,
                "source": "ai",
            }],
            "confidence": 0.88,
            "hitTags": [],
            "hitCount": 0,
            "deductionScore": 0,
            "tagParseTime": "2026-08-03 16:30:00",
        },
        "2026-08-03 16:29:00",
    )
    values = result["positiveTags"]["tagsArray"][0]["value"]

    assert len(values) == MAX_SKILL_TAGS
    assert [item["tag"] for item in values] == source_skills[:MAX_SKILL_TAGS]


def test_sync_tag_route_writes_readable_tag_summary():
    """验证同步仅打标接口也调用统一的可读标签结果日志。"""
    request = SyncTagRequest.model_validate({
        "resumes": [{
            "filename": "张三.pdf",
            "resumeParseFinishTime": "2026-08-03 16:30:00",
            "resume": {
                "candidate": {"name": "张三"},
                "educations": [],
                "works": [],
                "projects": [],
            },
        }],
    })
    tag_info = {
        "positiveTags": _scrambled_positive_tags(),
        "confidence": 0.88,
        "hitTags": [],
        "hitCount": 0,
        "deductionScore": 0,
        "tagParseTime": "2026-08-03 16:30:01",
    }

    with (
        patch("api.routes_openai.tag_sync_resume_with_model", return_value=tag_info),
        patch("api.routes_openai._log_resume_tag_result") as readable_log,
    ):
        response = asyncio.run(sync_tag_resumes(request))

    assert response.status_code == 200
    readable_log.assert_called_once()
    args = readable_log.call_args.args
    assert len(args[0]) == 36
    assert args[1:4] == (
        "张三.pdf",
        tag_info,
        "2026-08-03 16:30:00",
    )
    assert args[4] >= 0
    assert args[5] == args[4]
