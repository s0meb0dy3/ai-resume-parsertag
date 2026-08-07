"""定义解析和打标入口共用的标准简历数据契约。

本模块把允许为空的对象和集合归一为稳定默认值，并拒绝会掩盖数据错误的集合标量。
所有入口使用同一 Pydantic 模型，避免 `works=null` 或非数组值进入打标链路。
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ResumeContractError(ValueError):
    """表示简历负载无法在不丢失业务含义的前提下完成归一。"""


class _ContractModel(BaseModel):
    """为简历嵌套对象提供统一标量归一和未知字段兼容策略。"""

    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_scalar(cls, value: Any, info):
        """在字段校验前统一空值、数值和简单标量。

        Args:
            value (Any): 调用方提供的原始字段值。
            info: Pydantic 提供的当前字段校验上下文。

        Returns:
            Any: 年龄和性别使用整数，其余简单标量使用去空白字符串。

        Note:
            无法转换的年龄和性别按 0 处理，保持既有公开 JSON 默认值。
        """
        if info.field_name in {"age", "gender"}:
            if value in (None, ""):
                return 0
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool)):
            return str(value).strip()
        return value


class Candidate(_ContractModel):
    """定义候选人基础信息的稳定字段和默认值。"""

    name: str = ""
    phone: str = ""
    email: str = ""
    gender: int = 0
    age: int = 0
    address: str = ""
    nativePlace: str = ""
    selfEvaluation: str = ""
    expectedSalary: str = ""
    expectedPosition: str = ""


class Education(_ContractModel):
    """定义单条教育背景的学校、专业、学位、学历和时间字段。"""

    school: str = ""
    major: str = ""
    degree: str = ""
    education: str = ""
    startDate: str = ""
    endDate: str = ""


_CURRENT_END_DATE_VALUES = {
    "至今", "目前", "现在", "在职", "今", "current", "present", "now",
}
_START_DATE_RE = re.compile(r"^\s*(\d{4})(?:[-./年](\d{1,2}))?")


class Work(_ContractModel):
    """定义单条工作经历的公司、职位、职责和时间字段。"""

    company: str = ""
    position: str = ""
    description: str = ""
    startDate: str = ""
    endDate: str = ""

    @field_validator("endDate", mode="before")
    @classmethod
    def _normalize_current_end_date(cls, value: Any):
        """把常见的当前任职结束日期写法统一为公开值 `至今`。"""
        text = str(value or "").strip()
        return "至今" if text.casefold() in _CURRENT_END_DATE_VALUES else value

class Project(_ContractModel):
    """定义单条项目经历的归属公司、项目、角色、描述和时间字段。"""

    company: str = ""
    projectName: str = ""
    role: str = ""
    description: str = ""
    startDate: str = ""
    endDate: str = ""


class ResumeData(BaseModel):
    """定义所有解析和打标入口接受的标准简历 JSON。"""

    model_config = ConfigDict(extra="ignore")

    candidate: Candidate = Field(default_factory=Candidate)
    educations: list[Education] = Field(default_factory=list)
    works: list[Work] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize_collections(cls, value: Any):
        """将可空对象和集合归一为稳定容器并拒绝错误类型。

        Args:
            value (Any): 待校验的完整简历负载。

        Returns:
            dict: `null` 值已替换为空对象或空数组的简历副本。

        Raises:
            ValueError: 顶层不是对象，或嵌套对象、集合类型错误时抛出。
        """
        if not isinstance(value, dict):
            raise ValueError("resume 必须是 JSON object")
        normalized = dict(value)
        if normalized.get("candidate") is None:
            normalized["candidate"] = {}
        elif "candidate" in normalized and not isinstance(normalized["candidate"], dict):
            raise ValueError("resume.candidate 必须是 JSON object 或 null")
        for field_name in ("educations", "works", "projects"):
            field_value = normalized.get(field_name)
            if field_value is None:
                normalized[field_name] = []
            elif not isinstance(field_value, list):
                raise ValueError(f"resume.{field_name} 必须是 JSON array 或 null")
        return normalized

    @model_validator(mode="after")
    def _normalize_current_work(self):
        """保守地将最新且唯一的空结束日期识别为当前任职。"""
        if any(work.endDate == "至今" for work in self.works):
            return self
        empty_indexes = [
            index for index, work in enumerate(self.works)
            if not work.endDate and _work_start_key(work.startDate) is not None
            and any((work.company, work.position, work.description))
        ]
        if len(empty_indexes) != 1:
            return self
        candidate_index = empty_indexes[0]
        dated_works = [
            (index, _work_start_key(work.startDate))
            for index, work in enumerate(self.works)
            if _work_start_key(work.startDate) is not None
        ]
        if dated_works and candidate_index in {
            index for index, key in dated_works
            if key == max(item_key for _, item_key in dated_works)
        }:
            self.works[candidate_index].endDate = "至今"
        return self


def _work_start_key(value: str) -> tuple[int, int] | None:
    """提取工作开始日期的年月排序键，仅用于识别最新记录。"""
    match = _START_DATE_RE.match(str(value or ""))
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    if not 1 <= month <= 12:
        return None
    return year, month


def normalize_resume_payload(value: Any) -> dict:
    """校验简历并返回标准公开字典结构。

    Args:
        value (Any): 原始简历字典或已校验的 `ResumeData` 实例。

    Returns:
        dict: 包含 `candidate`、`educations`、`works` 和 `projects` 的标准字典。

    Raises:
        ResumeContractError: 任一对象或集合无法按契约归一时抛出。
    """
    try:
        if isinstance(value, ResumeData):
            model = value
        else:
            model = ResumeData.model_validate(value)
    except Exception as exc:
        raise ResumeContractError(str(exc)) from exc
    return model.model_dump()
