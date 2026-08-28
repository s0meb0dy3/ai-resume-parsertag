"""验证确定性简历候选提取及其 Prompt 边界。"""

from core.prompting.resume_parsing import (
    PROMPT_HR,
    RESUME_PROMPT_CACHE_KEY,
    TEXT_RESUME_USER_PREFIX,
    build_text_resume_user_prompt,
)
from core.resume_hints import extract_deterministic_resume_hints


def test_extract_deterministic_resume_hints_from_structured_text():
    """验证姓名、联系方式、院校、日期和章节边界按原文顺序提取。"""
    text = """姓名：张三
电话：138 0013 8000
邮箱：ZHANG.SAN@example.com
教育经历
2015年09月 - 2019.07
电子科技大学
工作经历
2019/08 - 2023-06
A公司 Java工程师
项目经历
支付平台
"""

    hints = extract_deterministic_resume_hints(text, "张三.docx")

    assert hints.names == ("张三",)
    assert hints.phones == ("13800138000",)
    assert hints.emails == ("zhang.san@example.com",)
    assert hints.dates == ("2015-09", "2019-07", "2019-08", "2023-06")
    assert hints.schools == ("电子科技大学",)
    assert hints.section_boundaries == (
        ("education", "教育经历", 4),
        ("works", "工作经历", 7),
        ("projects", "项目经历", 10),
    )


def test_name_candidates_stop_before_family_member_section():
    """验证家庭成员区域中的姓名不会进入候选人姓名候选。"""
    text = """王小明
软件工程师
家庭成员
李小红
关系：配偶
"""

    hints = extract_deterministic_resume_hints(text, "resume.pdf")

    assert hints.names == ("王小明",)
    assert "李小红" not in hints.names


def test_name_candidates_stop_before_personal_advantages_section():
    """验证首页“个人优势”标题不会被误识别为候选人姓名。"""
    text = """刘江波
个人优势
十年软件研发和项目管理经验
"""

    hints = extract_deterministic_resume_hints(text, "resume.pdf")

    assert hints.names == ("刘江波",)
    assert hints.section_boundaries == (
        ("selfEvaluation", "个人优势", 2),
    )


def test_text_resume_prompt_keeps_fixed_prefix_and_marks_hints_untrusted():
    """验证动态原文位于固定前缀之后且候选不会被描述为最终事实。"""
    hints = {
        "nameCandidates": ["张三"],
        "phoneCandidates": [],
        "emailCandidates": [],
        "dateCandidates": ["2020-01"],
        "schoolCandidates": ["测试大学"],
        "sectionBoundaries": [],
    }

    prompt = build_text_resume_user_prompt("张三\n测试大学", hints)

    assert prompt.startswith(TEXT_RESUME_USER_PREFIX)
    assert prompt.index("张三\n测试大学") < prompt.index('"nameCandidates"')
    assert "只用于防遗漏核对，不是最终事实" in prompt
    assert "候选错误或无原文支持时忽略" in prompt
    assert "Python 确定性候选只用于提醒复核" not in PROMPT_HR
    assert RESUME_PROMPT_CACHE_KEY.startswith("resume-parse-")
