"""验证应届身份与正式工作年限的确定性计算规则。"""

from datetime import date

import pytest

from tag.tagger import (
    _detect_stability,
    _detect_timeline_overlap,
    _map_work_years,
    calculate_work_experience,
    extract_positive_tags_rule,
)


def _resume(works=None, educations=None):
    """构造工作年限测试使用的最小标准简历。

    Args:
        works (list | None): 可选工作经历列表。
        educations (list | None): 可选教育背景列表。

    Returns:
        dict: 包含四个标准顶层字段的简历对象。
    """
    return {
        "candidate": {},
        "educations": educations or [],
        "works": works or [],
        "projects": [],
    }


def test_campus_status_is_not_the_same_as_less_than_one_year():
    """验证应届身份不会与不足一年正式工作经历混为一谈。"""
    campus = calculate_work_experience(
        _resume(educations=[{"endDate": "2026-07"}]),
        as_of_date=date(2026, 8, 3),
    )
    short_social = calculate_work_experience(
        _resume(works=[{
            "company": "某科技公司", "position": "工程师",
            "startDate": "2026-01", "endDate": "2026-07",
        }]),
        as_of_date=date(2026, 8, 3),
    )

    assert campus.graduate_status == "campus"
    assert campus.full_time_months == 0
    assert campus.work_years_band == "应届"
    assert short_social.graduate_status == "social"
    assert short_social.full_time_months == 6
    assert short_social.work_years_band == "1年以下"
    assert _map_work_years(0.5) == "1年以下"


def test_alumni_without_formal_experience_has_an_explicit_band():
    """验证已过应届窗口且无正式经历时输出往届无经验。"""
    result = calculate_work_experience(
        _resume(
            works=[{
                "company": "某研究中心",
                "position": "实习工程师",
                "startDate": "2019-01",
                "endDate": "2019-07",
            }],
            educations=[{"endDate": "2020-06"}],
        ),
        as_of_date=date(2026, 8, 3),
    )

    assert result.graduate_status == "alumni"
    assert result.full_time_months == 0
    assert result.internship_months == 6
    assert result.work_years_band == "往届无经验"


@pytest.mark.parametrize(
    ("years", "expected"),
    [
        (0.99, "1年以下"),
        (1, "1-3年（满1年）"),
        (3, "3-5年（满3年）"),
        (5, "5-10年（满5年）"),
        (10, "10年及以上"),
        (15, "10年及以上"),
    ],
)
def test_work_year_bands_are_left_closed_and_right_open(years, expected):
    """验证满1、3、5、10年进入以下限命名的下一档。"""
    assert _map_work_years(years) == expected


def test_overlapping_full_time_intervals_are_merged():
    """验证重叠正式工作区间先合并再计算总月份。"""
    result = calculate_work_experience(
        _resume(works=[
            {
                "company": "A",
                "position": "开发",
                "startDate": "2020-01",
                "endDate": "2021-01",
            },
            {
                "company": "B",
                "position": "开发",
                "startDate": "2020-07",
                "endDate": "2021-07",
            },
        ]),
        as_of_date=date(2026, 8, 3),
    )
    assert result.full_time_months == 18
    assert result.work_years_band == "1-3年（满1年）"


def test_internship_is_counted_separately_and_exact_year_uses_next_band():
    """验证实习月份单列且满一年正式经历进入下一档位。"""
    result = calculate_work_experience(
        _resume(works=[
            {
                "company": "A",
                "position": "实习工程师",
                "startDate": "2019-01",
                "endDate": "2019-07",
            },
            {
                "company": "B",
                "position": "工程师",
                "startDate": "2020-01",
                "endDate": "2021-01",
            },
        ]),
        as_of_date=date(2026, 8, 3),
    )
    assert result.internship_months == 6
    assert result.full_time_months == 12
    assert result.work_years_band == "1-3年（满1年）"


def test_empty_or_unparseable_history_does_not_imply_campus_status():
    """验证空白或不可解析经历不会被推断为应届或不足一年。"""
    empty = calculate_work_experience(_resume(), as_of_date=date(2026, 8, 3))
    invalid = calculate_work_experience(
        _resume(
            works=[{
                "company": "A",
                "position": "工程师",
                "startDate": "未知",
                "endDate": "未知",
            }]
        ),
        as_of_date=date(2026, 8, 3),
    )
    assert empty.graduate_status == "unknown"
    assert empty.work_years_band == ""
    assert invalid.graduate_status == "social"
    assert invalid.work_years_band == ""
    assert invalid.calculation_quality == "unknown"

    positive_result = extract_positive_tags_rule(_resume())
    work_years = next(
        item for item in positive_result["positiveTags"]
        if item["dimension"] == "workYears"
    )
    assert work_years["workYearsValueArray"] == []


def test_year_only_work_dates_are_marked_as_partial_precision():
    """验证仅包含年份的可计算经历被标记为部分精度。"""
    result = calculate_work_experience(
        _resume(works=[{
            "company": "A", "position": "工程师",
            "startDate": "2020", "endDate": "2023",
        }]),
        as_of_date=date(2026, 8, 3),
    )

    assert result.full_time_months == 36
    assert result.work_years_band == "3-5年（满3年）"
    assert result.calculation_quality == "partial"


def _stability_rules() -> dict:
    """构造仅启用工作空窗检测的测试规则。

    Returns:
        dict: 关闭跳槽和短任职、空窗阈值为六个月的规则。
    """
    return {
        "frequent_job_change": {"enabled": False},
        "short_tenure": {"enabled": False},
        "gap_period": {
            "enabled": True,
            "threshold_months": 6,
            "severity": "low",
        },
    }


def test_internship_does_not_create_employment_gap_before_formal_work():
    """验证在校实习结束到首份正式工作之间不会产生工作空窗风险。"""
    hit_tags = []
    _detect_stability(
        [
            {
                "company": "华为成都研究所",
                "position": "无线通信 FPGA 助理工（实习）",
                "startDate": "2014-07",
                "endDate": "2015-08",
            },
            {
                "company": "深圳市太赫兹科学技术创新研究院",
                "position": "信号处理算法工程师",
                "startDate": "2016-07",
                "endDate": "2020-07",
            },
        ],
        [{"startDate": "2013-09", "endDate": "2016-07"}],
        hit_tags,
        _stability_rules(),
    )

    assert hit_tags == []


def test_education_covered_months_are_removed_from_employment_gap():
    """验证两份正式工作之间的在校月份不计入工作空窗。"""
    hit_tags = []
    _detect_stability(
        [
            {
                "company": "A公司", "position": "工程师",
                "startDate": "2018-01", "endDate": "2020-01",
            },
            {
                "company": "B公司", "position": "工程师",
                "startDate": "2020-09", "endDate": "2022-01",
            },
        ],
        [{"startDate": "2019-09", "endDate": "2020-09"}],
        hit_tags,
        _stability_rules(),
    )

    assert hit_tags == []


def test_real_uncovered_employment_gap_still_triggers_risk():
    """验证没有教育覆盖的真实长空窗仍按阈值输出风险标签。"""
    hit_tags = []
    _detect_stability(
        [
            {
                "company": "A公司", "position": "工程师",
                "startDate": "2018-01", "endDate": "2020-01",
            },
            {
                "company": "B公司", "position": "工程师",
                "startDate": "2020-09", "endDate": "2022-01",
            },
        ],
        [],
        hit_tags,
        _stability_rules(),
    )

    assert [item["tag"] for item in hit_tags] == ["工作空窗期过长"]
    assert "空窗约8个月" in hit_tags[0]["matchedContent"]


@pytest.mark.parametrize(
    ("position", "description"),
    [
        ("软件工程师（实习）", "参与项目开发"),
        ("技术顾问", "兼职支持系统建设"),
    ],
)
def test_non_full_time_work_does_not_trigger_timeline_overlap(position, description):
    """验证实习和兼职与正式工作并行时不产生诚信重叠风险。"""
    hit_tags = []
    _detect_timeline_overlap(
        [
            {
                "company": "A公司", "position": position,
                "description": description,
                "startDate": "2021-01", "endDate": "2021-12",
            },
            {
                "company": "B公司", "position": "正式工程师",
                "startDate": "2021-06", "endDate": "2022-06",
            },
        ],
        hit_tags,
        {
            "timeline_overlap": {
                "enabled": True,
                "threshold_months": 2,
                "severity": "high",
            },
        },
    )

    assert hit_tags == []


def test_overlapping_full_time_work_still_triggers_timeline_risk():
    """验证过滤非正式经历后，两段正式工作重叠仍按原规则命中。"""
    hit_tags = []
    _detect_timeline_overlap(
        [
            {
                "company": "A公司", "position": "工程师",
                "startDate": "2020-01", "endDate": "2021-12",
            },
            {
                "company": "B公司", "position": "高级工程师",
                "startDate": "2021-06", "endDate": "2022-06",
            },
        ],
        hit_tags,
        {
            "timeline_overlap": {
                "enabled": True,
                "threshold_months": 2,
                "severity": "high",
            },
        },
    )

    assert len(hit_tags) == 1
    assert hit_tags[0]["dimension"] == "integrity"
    assert hit_tags[0]["ruleKey"] == "timeline_overlap"
    assert hit_tags[0]["tag"] == "工作时间线重叠"
