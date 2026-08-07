"""从简历原文提取供模型核对的确定性字段候选。

本模块不生成公开 Resume JSON，也不直接覆盖模型结果。候选仅附加到单次解析
Prompt，帮助模型复核容易因版式或长上下文遗漏的姓名、联系方式、学校、日期和
章节边界。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from logger.operation import log_operation


_NAME_LABEL_RE = re.compile(
    r"(?:姓名|name)\s*[:：]?\s*"
    r"(?P<name>[\u4e00-\u9fff·]{2,6}|[A-Za-z][A-Za-z .'-]{1,40})",
    re.IGNORECASE,
)
_STANDALONE_CHINESE_NAME_RE = re.compile(r"^[\u4e00-\u9fff·]{2,4}$")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?86[-\s]?)?(?P<phone>1[3-9](?:[-\s]?\d){9})(?!\d)"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_DATE_RE = re.compile(
    r"(?<!\d)(?P<date>(?:19|20)\d{2}"
    r"(?:\s*(?:年|[-./])\s*(?:0?[1-9]|1[0-2])\s*月?)?)(?!\d)"
)
_SCHOOL_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z·]{2,32}(?:大学|学院|学校)"
)
_SCHOOL_PREFIX_RE = re.compile(
    r"^(?:(?:毕业|就读)于|教育背景|教育经历|学历背景|毕业院校|学校|院校)+"
)
_MARKDOWN_EDGE_RE = re.compile(r"^[\s#>*`_-]+|[\s#*`_:：-]+$")
_NAME_EXCLUSIONS = {
    "个人简历",
    "求职简历",
    "基本信息",
    "个人信息",
    "教育背景",
    "教育经历",
    "学历背景",
    "工作经历",
    "工作经验",
    "项目经历",
    "项目经验",
    "专业技能",
    "技能清单",
    "自我评价",
    "个人优势",
    "核心优势",
    "求职意向",
    "联系方式",
}
_NAME_SCAN_STOP_HEADINGS = {
    "家庭成员",
    "主要社会关系",
    "家庭关系",
    "教育背景",
    "教育经历",
    "学历背景",
    "学习经历",
    "工作经历",
    "工作经验",
    "职业经历",
    "任职经历",
    "项目经历",
    "项目经验",
    "项目实践",
    "专业技能",
    "技能清单",
    "技能特长",
    "技术能力",
    "自我评价",
    "个人评价",
    "个人总结",
    "自我介绍",
    "个人优势",
    "核心优势",
}
_SECTION_HEADINGS = (
    ("candidate", {"基本信息", "个人信息", "个人资料", "基本资料"}),
    ("education", {"教育背景", "教育经历", "学历背景", "学习经历"}),
    ("works", {"工作经历", "工作经验", "职业经历", "任职经历"}),
    ("projects", {"项目经历", "项目经验", "项目实践"}),
    ("skills", {"专业技能", "技能清单", "技能特长", "技术能力"}),
    (
        "selfEvaluation",
        {"自我评价", "个人评价", "个人总结", "自我介绍", "个人优势", "核心优势"},
    ),
)


@dataclass(frozen=True)
class DeterministicResumeHints:
    """保存从同一份原文中提取的有限候选集合。

    Attributes:
        names (tuple[str, ...]): 文档开头或姓名标签附近的姓名候选。
        phones (tuple[str, ...]): 规范化为 11 位数字的中国大陆手机号候选。
        emails (tuple[str, ...]): 去重后的邮箱候选。
        dates (tuple[str, ...]): 规范化为年或年月的日期候选。
        schools (tuple[str, ...]): 名称以大学、学院或学校结尾的院校候选。
        section_boundaries (tuple[tuple[str, str, int], ...]): 标准章节、原始标题
            和从 1 开始的行号。
    """

    names: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    dates: tuple[str, ...] = ()
    schools: tuple[str, ...] = ()
    section_boundaries: tuple[tuple[str, str, int], ...] = ()

    def to_prompt_payload(self) -> dict:
        """转换为可稳定序列化的 Prompt 内部对象。

        Returns:
            dict: 仅包含候选值和章节位置的字典，不包含公开 Resume 字段。
        """
        return {
            "nameCandidates": list(self.names),
            "phoneCandidates": list(self.phones),
            "emailCandidates": list(self.emails),
            "dateCandidates": list(self.dates),
            "schoolCandidates": list(self.schools),
            "sectionBoundaries": [
                {"section": section, "heading": heading, "line": line_number}
                for section, heading, line_number in self.section_boundaries
            ],
        }


def _append_unique(values: list[str], value: str, limit: int) -> None:
    """在数量预算内追加非空且尚未出现的候选值。

    Args:
        values (list[str]): 按原文顺序累计的候选列表。
        value (str): 当前待追加的候选值。
        limit (int): 该候选类型允许写入 Prompt 的最大数量。
    """
    cleaned = value.strip()
    if cleaned and cleaned not in values and len(values) < limit:
        values.append(cleaned)


def _clean_heading(line: str) -> str:
    """移除 Markdown 标记并得到用于章节匹配的单行标题。

    Args:
        line (str): Python 文本抽取器返回的原始行。

    Returns:
        str: 已去除常见 Markdown 边界符号和首尾空白的标题。
    """
    return _MARKDOWN_EDGE_RE.sub("", line.strip()).strip()


def _normalize_date(value: str) -> str:
    """将常见年月分隔符归一为 `YYYY-MM`，仅有年份时保持年份。

    Args:
        value (str): 正则从原文匹配到的日期文本。

    Returns:
        str: 去空白并统一年月分隔符后的日期候选。
    """
    normalized = re.sub(r"\s+", "", value)
    return (
        normalized.replace("年", "-")
        .replace("月", "")
        .replace("/", "-")
        .replace(".", "-")
    )


def extract_deterministic_resume_hints(
    text: str,
    filename: str = "unknown",
) -> DeterministicResumeHints:
    """从完整原文提取有限候选，供同一次 LLM 解析调用复核。

    Args:
        text (str): PDF 或 DOCX Python 抽取器返回的完整原文。
        filename (str): 日志关联使用的规范化文件名。

    Returns:
        DeterministicResumeHints: 按原文顺序去重并受数量预算限制的候选集合。

    Note:
        姓名候选只读取前 12 个非空行，降低家庭成员表和项目人员名单的误命中；
        其他候选即使存在误报，也只能作为模型核对线索，不能直接成为最终字段。
    """
    with log_operation(
        "确定性字段候选提取",
        file=filename,
        level="DEBUG",
        failure_level="DEBUG",
        emit_start=False,
    ) as operation:
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        names: list[str] = []
        phones: list[str] = []
        emails: list[str] = []
        dates: list[str] = []
        schools: list[str] = []
        sections: list[tuple[str, str, int]] = []

        for line in lines[:12]:
            cleaned_line = _clean_heading(line)
            if cleaned_line in _NAME_SCAN_STOP_HEADINGS:
                break
            labelled_name = _NAME_LABEL_RE.search(cleaned_line)
            if labelled_name:
                _append_unique(names, labelled_name.group("name"), 5)
            elif (
                _STANDALONE_CHINESE_NAME_RE.fullmatch(cleaned_line)
                and cleaned_line not in _NAME_EXCLUSIONS
            ):
                _append_unique(names, cleaned_line, 5)

        for match in _PHONE_RE.finditer(text or ""):
            _append_unique(
                phones,
                re.sub(r"\D", "", match.group("phone")),
                5,
            )
        for match in _EMAIL_RE.finditer(text or ""):
            _append_unique(emails, match.group(0).lower(), 5)
        for match in _DATE_RE.finditer(text or ""):
            _append_unique(dates, _normalize_date(match.group("date")), 48)
        for match in _SCHOOL_RE.finditer(text or ""):
            school = _SCHOOL_PREFIX_RE.sub("", match.group(0)).strip()
            _append_unique(schools, school, 12)

        for line_number, line in enumerate(lines, start=1):
            heading = _clean_heading(line)
            for section, aliases in _SECTION_HEADINGS:
                if heading in aliases:
                    boundary = (section, heading, line_number)
                    if boundary not in sections and len(sections) < 12:
                        sections.append(boundary)
                    break

        result = DeterministicResumeHints(
            names=tuple(names),
            phones=tuple(phones),
            emails=tuple(emails),
            dates=tuple(dates),
            schools=tuple(schools),
            section_boundaries=tuple(sections),
        )
        operation.add(
            nameCandidates=len(result.names),
            phoneCandidates=len(result.phones),
            emailCandidates=len(result.emails),
            dateCandidates=len(result.dates),
            schoolCandidates=len(result.schools),
            sectionBoundaries=len(result.section_boundaries),
        )
        return result
