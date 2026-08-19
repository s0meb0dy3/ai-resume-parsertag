"""执行简历正向标签提取和负向风险检测。

正向标签受运行时 `allowedValues` 和独立 ID 映射共同约束；教育背景、工作年限等
确定性信息优先由本地规则计算。负向标签由专用规则、自定义关键词和可选 LLM
检测组成，最终保持既有对外标签 JSON 结构。
"""
import copy
import re
import json
import pathlib
import json_repair
from dataclasses import dataclass
from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from typing import Optional

from .tag_data import (
    classify_education, classify_companies, classify_industry,
    EDUCATION_HIERARCHY,
)
from .tag_config import (
    MAX_INDUSTRY_TAGS,
    MAX_SKILL_TAGS,
    NEGATIVE_DIMENSIONS,
    POSITIVE_DIMENSIONS,
    SEVERITY_DEDUCTION,
    sort_positive_tag_entries,
)
from core.prompting.common import TAGGER_SYSTEM
from core.prompting.negative_tags import (
    GENERIC_NEG_DIM_PROMPT,
    NEGATIVE_PROMPT_HEADER,
    NEGATIVE_TAG_SCHEMA,
)
from core.prompting.positive_tags import (
    DIM_PROMPT,
    GENERIC_DIM_PROMPT,
    POSITIVE_PROMPT_HEADER,
    POSITIVE_TAG_SCHEMA,
)
from core.prompting.positive_value_guidance import build_positive_value_guidance
from config import logger  # 统一使用带 ContextVar 上下文的 logger
from logger.langfuse_client import observe, update_current_observation
from logger.operation import log_operation
from core.resume_contract import normalize_resume_payload
from .tag_value_id_mapper import get_tag_value_id_mapper

_RUNTIME_CFG_FILE = pathlib.Path(__file__).parent.parent / "schemas" / "tag_config_runtime.json"
_RAW_EVIDENCE_RESERVED_SKILL_SLOTS = 3

_SKILL_NEGATIVE_PREFIX_PATTERNS = [
    re.compile(
        r"(?:不(?:熟悉|了解|掌握|会用|会|擅长)|未曾?(?:使用|接触|掌握|实践|应用|学习)|"
        r"没有(?:使用|接触|掌握|实践|相关|项目)?|无(?:相关|实战|项目|使用)?经验|"
        r"计划(?:学习|掌握|使用)|准备(?:学习|掌握)|希望(?:学习|掌握)|"
        r"了解|仅(?:了解|听说|接触)|略懂)"
        r"[^。！？；;，,：:\n]{0,20}$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:(?:岗位|任职|职位)(?:职责|要求)|优先(?:考虑|条件)?|加分项)"
        r"[^。！？；;\n]{0,28}$",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(?:no experience (?:with|in)|not familiar with|have not used|never used|"
        r"plan(?:ning)? to learn|want to learn|job requirements?|required skills?|"
        r"nice to have|preferred skills?)[^.!?;,\n]{0,28}$",
        flags=re.IGNORECASE,
    ),
]
_SKILL_NEGATIVE_SUFFIX_PATTERNS = [
    re.compile(r"^[\s:：,，、-]{0,4}(?:经验不足|尚未掌握|待学习|仅作了解|不了解|不熟悉)"),
    re.compile(
        r"^[\s:：,，、-]{0,4}(?:experience required|is required|preferred only)",
        flags=re.IGNORECASE,
    ),
]
_SKILL_COURSE_CONTEXT_RE = re.compile(
    r"(?:主修课程|所学课程(?:包括)?|专业课程|核心课程)"
    r"[^。！？；;]{0,220}$",
    flags=re.IGNORECASE,
)

# 仅在修改时间变化时重新反序列化，避免每次打标重复读取完整配置。
_cfg_cache: dict = {}
_cfg_cache_mtime: float = -1.0


def _tagger_load_cfg() -> dict:
    """按文件修改时间加载并缓存运行时标签配置。

    Returns:
        运行时配置的浅拷贝；配置文件不存在时返回静态默认配置。

    Note:
        浅拷贝只隔离顶层键赋值，嵌套列表仍应视为只读。文件修改时间未变化时
        不重复执行 JSON 反序列化。
    """
    global _cfg_cache, _cfg_cache_mtime
    if _RUNTIME_CFG_FILE.exists():
        mtime = _RUNTIME_CFG_FILE.stat().st_mtime
        if _cfg_cache and mtime == _cfg_cache_mtime:
            return copy.copy(_cfg_cache)
        with open(_RUNTIME_CFG_FILE, encoding="utf-8") as f:
            _cfg_cache = json.load(f)
        _cfg_cache_mtime = mtime
    else:
        _cfg_cache = {
            "positiveDimensions": POSITIVE_DIMENSIONS,
            "negativeDimensions": NEGATIVE_DIMENSIONS,
            "severityDeduction":  dict(SEVERITY_DEDUCTION),
        }
        _cfg_cache_mtime = -1.0
    return copy.copy(_cfg_cache)

# 正向标签提取


def _build_positive_prompt(pos_dims: list, resume_json: str,
                           job_title: str, required_skills: str,
                           job_description: str) -> str:
    """根据启用的运行时维度构造正向标签提示词。

    Args:
        pos_dims (list): 当前正向标签维度配置。
        resume_json (str): 序列化后的结构化简历。
        job_title (str): 目标岗位名称。
        required_skills (str): 岗位必要技能文本。
        job_description (str): 岗位描述文本。

    Returns:
        str: 包含简历、岗位信息、提取规则和输出示例的完整提示词。

    Note:
        已知维度使用专用模板，自定义维度使用通用模板并扩充 schema 示例。
        禁用或删除维度会在下一次打标时反映到提示词中。
    """
    sections = []
    schema = copy.deepcopy(POSITIVE_TAG_SCHEMA)
    custom_schema_entries: list[dict] = []
    mapper = get_tag_value_id_mapper()

    for d in pos_dims:
        if not d.get("enabled", True):
            continue
        dim    = d["dimension"]
        allowed_values = [v for v in (d.get("allowedValues") or []) if v]
        values = "|".join(allowed_values)
        skill_records: list[dict] = []
        if dim == "skills":
            allowed_skill_values = set(allowed_values)
            for record in mapper.records("skills"):
                if record["value"] not in allowed_skill_values:
                    continue
                unique_aliases = [
                    alias for alias in record.get("aliases", [])
                    if (
                        (resolved := mapper.resolve("skills", alias))
                        and resolved["id"] == record["id"]
                    )
                ]
                skill_records.append({**record, "aliases": unique_aliases})
            values = values or "（当前简历无技能证据候选）"
            for schema_entry in schema["positiveTags"]:
                if schema_entry.get("dimension") == "skills":
                    schema_entry["skillsValueArray"] = allowed_values[:3]
                    break
        value_guidance = build_positive_value_guidance(
            dim,
            allowed_values,
            skill_records,
        )

        if dim in DIM_PROMPT:
            tpl = DIM_PROMPT[dim]
            sections.append(tpl.format(
                values=values,
                value_guidance=value_guidance,
            ))
        else:
            sections.append(GENERIC_DIM_PROMPT.format(
                dimension=dim,
                display_name=d.get("displayName", dim),
                values=values or "（allowedValues 为空，自由提取相关标签）",
                value_guidance=value_guidance,
            ))
            # 自定义维度使用占位值说明动态字段结构，避免模型遗漏该维度。
            custom_schema_entries.append({
                "dimension": dim,
                f"{dim}ValueArray": ["示例标签A"],
                "source": "llm",
                "confidence": 0.85,
            })

    # 自定义维度必须进入示例，否则模型可能只返回固定维度。
    if custom_schema_entries:
        schema["positiveTags"].extend(custom_schema_entries)

    return POSITIVE_PROMPT_HEADER.format(
        resume_json=resume_json,
        job_title=job_title,
        required_skills=required_skills,
        job_description=job_description,
        schema_example=json.dumps(schema, ensure_ascii=False, indent=2),
    ) + "\n\n".join(sections)


def _parse_date(date_str: str, as_of_date: date | None = None) -> Optional[datetime]:
    """将简历中的常见年月文本解析为日期。

    Args:
        date_str (str): 年月、年份或“至今”等日期文本。
        as_of_date (date | None): 进行中经历使用的统计基准日期。

    Returns:
        Optional[datetime]: 可识别时返回日期；空值或无法识别时返回 `None`。

    Note:
        “至今”、`present` 等进行中标记按调用时的当前时间计算。仅含年份时
        归一为当年 1 月 1 日。
    """
    if not date_str:
        return None
    s = date_str.strip().lower()
    if s in ("至今", "present", "now", "今", "current", ""):
        current = as_of_date or date.today()
        return datetime(current.year, current.month, current.day)
    for fmt in ("%Y-%m", "%Y/%m", "%Y.%m", "%Y年%m月", "%Y"):
        try:
            return datetime.strptime(s.replace("年", "-").replace("月", ""), fmt.replace("年", "-").replace("月", ""))
        except ValueError:
            pass
    m = re.match(r"(\d{4})", s)
    if m:
        return datetime(int(m.group(1)), 1, 1)
    return None


def _has_month_precision(date_str: str) -> bool:
    """判断可解析的简历日期是否明确包含月份。

    Args:
        date_str (str): 工作或教育经历的原始日期文本。

    Returns:
        bool: 包含月份或属于“至今”等当前时间标记时返回 `True`。
    """
    value = str(date_str or "").strip().lower()
    if value in {"至今", "present", "now", "今", "current"}:
        return True
    return bool(re.match(r"^\d{4}(?:[-/.]\d{1,2}|年\s*\d{1,2}月?)", value))


_NON_FULL_TIME_KEYWORDS = (
    "实习", "intern", "兼职", "part-time", "part time", "校园", "学生会", "科研助理"
)


@dataclass(frozen=True)
class WorkExperienceSummary:
    """保存身份判定、正式工作时长和计算质量。

    Attributes:
        full_time_months (int): 合并重叠区间后的正式工作月份。
        internship_months (int): 实习和兼职经历累计月份。
        graduate_status (str): `campus`、`alumni`、`social` 或 `unknown`
            身份结果。
        work_years_band (str): 与 `workYears.allowedValues` 一致的年限档位；没有
            可计算的应届或正式工作证据时为空字符串。
        calculation_quality (str): `complete`、`partial` 或 `unknown`。
        as_of_date (str): 本次计算使用的 ISO 格式基准日期。
    """

    full_time_months: int
    internship_months: int
    graduate_status: str
    work_years_band: str
    calculation_quality: str
    as_of_date: str


def _is_non_full_time(work: dict) -> bool:
    """根据职位和描述识别实习、兼职等非正式工作经历。

    Args:
        work (dict): 单条标准工作经历。

    Returns:
        bool: 命中非正式工作关键词时返回 `True`。
    """
    text = " ".join(str(work.get(key, "")) for key in ("position", "description")).lower()
    return any(keyword in text for keyword in _NON_FULL_TIME_KEYWORDS)


def _months_between(start: datetime, end: datetime) -> int:
    """计算两个年月之间不小于零的整月差。

    Args:
        start (datetime): 经历开始年月。
        end (datetime): 经历结束年月。

    Returns:
        int: 忽略日期日部分的整月差，结束早于开始时返回 0。
    """
    return max(0, (end.year - start.year) * 12 + end.month - start.month)


def _merged_interval_months(intervals: list[tuple[int, int]]) -> int:
    """合并重叠的年月区间并计算去重后的总月份。

    Args:
        intervals (list[tuple[int, int]]): 使用连续月份索引表示的起止区间。

    Returns:
        int: 区间合并后的正式工作总月份。
    """
    if not intervals:
        return 0
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(max(0, end - start) for start, end in merged)


def _to_month_index(value: datetime) -> int:
    """把年月转换为可排序和求差的连续月份索引。

    Args:
        value (datetime): 需要转换的年月。

    Returns:
        int: 以公元 0 年 1 月为起点的连续月份索引。
    """
    return value.year * 12 + value.month - 1


def _map_work_months(months: int) -> str:
    """将正式工作月份映射为配置约定的年限档位。

    Args:
        months (int): 合并重叠区间后的正式工作月份。

    Returns:
        str: `1年以下` 至 `10年及以上` 的离散标签。

    Note:
        本函数不判断应届身份，档位上界采用左闭右开区间。
    """
    if months < 12:
        return "1年以下"
    if months < 36:
        return "1-3年（满1年）"
    if months < 60:
        return "3-5年（满3年）"
    if months < 120:
        return "5-10年（满5年）"
    return "10年及以上"


def calculate_work_experience(
    resume: dict,
    as_of_date: date | None = None,
    graduate_window_before_months: int = 12,
    graduate_window_after_months: int = 18,
) -> WorkExperienceSummary:
    """根据教育和工作时间线确定性计算应届身份与正式工作时长。

    Args:
        resume (dict): 已按统一契约归一的结构化简历。
        as_of_date (date | None): 统计基准日期；为空时使用当前日期。
        graduate_window_before_months (int): 基准日期前允许的应届毕业月份窗口。
        graduate_window_after_months (int): 基准日期后允许的应届毕业月份窗口。

    Returns:
        WorkExperienceSummary: 身份、正式和实习月份、年限档位及质量摘要。

    Note:
        只有毕业时间位于应届窗口且没有正式工作证据时才判为 `campus`；
        毕业时间早于应届窗口且没有正式工作证据时判为 `alumni`。
        正式工作区间先合并重叠月份，实习和兼职不会进入正式年限档位。
    """
    current = as_of_date or date.today()
    full_time_intervals: list[tuple[int, int]] = []
    internship_months = 0
    invalid_intervals = 0
    partial_intervals = 0
    formal_work_evidence = False

    for work in resume.get("works", []):
        if not isinstance(work, dict):
            continue
        non_full_time = _is_non_full_time(work)
        if not non_full_time and any(str(work.get(key, "")).strip() for key in ("company", "position", "description")):
            formal_work_evidence = True
        start = _parse_date(work.get("startDate", ""), current)
        end = _parse_date(work.get("endDate", ""), current)
        if not start or not end or end < start:
            invalid_intervals += 1
            continue
        if not _has_month_precision(work.get("startDate", "")) or not _has_month_precision(
            work.get("endDate", "")
        ):
            partial_intervals += 1
        if non_full_time:
            internship_months += _months_between(start, end)
        else:
            full_time_intervals.append((_to_month_index(start), _to_month_index(end)))

    full_time_months = _merged_interval_months(full_time_intervals)
    graduation_dates = [
        parsed
        for education in resume.get("educations", [])
        if isinstance(education, dict)
        for parsed in [_parse_date(education.get("endDate", ""), current)]
        if parsed is not None
    ]
    latest_graduation = max(graduation_dates) if graduation_dates else None
    window_start = datetime(current.year, current.month, 1) - relativedelta(
        months=graduate_window_before_months
    )
    window_end = datetime(current.year, current.month, 1) + relativedelta(
        months=graduate_window_after_months
    )
    is_campus = bool(
        latest_graduation
        and window_start <= latest_graduation <= window_end
        and not formal_work_evidence
    )
    is_alumni_without_experience = bool(
        latest_graduation
        and latest_graduation < window_start
        and not formal_work_evidence
    )
    if invalid_intervals:
        quality = "partial" if full_time_intervals else "unknown"
    else:
        quality = "partial" if partial_intervals else "complete"
    return WorkExperienceSummary(
        full_time_months=full_time_months,
        internship_months=internship_months,
        graduate_status=(
            "campus"
            if is_campus
            else (
                "alumni"
                if is_alumni_without_experience
                else ("social" if formal_work_evidence else "unknown")
            )
        ),
        work_years_band=(
            "应届"
            if is_campus
            else (
                "往届无经验"
                if is_alumni_without_experience
                else (_map_work_months(full_time_months) if full_time_intervals else "")
            )
        ),
        calculation_quality=quality,
        as_of_date=current.isoformat(),
    )


def _calc_work_years(works: list, as_of_date: date | None = None) -> float:
    """为旧调用方计算合并重叠区间后的正式工作年数。

    Args:
        works (list): 工作经历字典列表。
        as_of_date (date | None): 进行中经历使用的统计基准日期。

    Returns:
        float: 合并重叠区间后的正式工作月份除以 12。

    Note:
        实习、兼职、无效日期和结束早于开始的经历不进入正式工作年数。
    """
    return calculate_work_experience(
        {"candidate": {}, "educations": [], "works": works or [], "projects": []},
        as_of_date=as_of_date,
    ).full_time_months / 12


def _map_work_years(years: float) -> str:
    """将数值工作年限映射为配置约定的离散标签。

    Args:
        years (float): 按工作经历累计得到的年数。

    Returns:
        str: 与 `workYears` 允许值一致的工作年限标签。

    Note:
        本函数只映射正式工作时长，不负责判断“应届”身份。小数年按已完成
        的整月向下取整，避免不足一年因四舍五入提前进入满一年档位。
    """
    return _map_work_months(int(max(0, years) * 12))


def _build_allowed(dim: str, pos_dims=None) -> set:
    """读取指定正向维度的允许值集合。

    Args:
        dim (str): 正向标签维度名。
        pos_dims (list | None): 本次调用的运行时配置；为空时使用静态默认配置。

    Returns:
        set: 该维度配置中的 `allowedValues` 集合。
    """
    dims = pos_dims if pos_dims is not None else POSITIVE_DIMENSIONS
    return {v for d in dims if d["dimension"] == dim for v in d["allowedValues"]}


def _canonicalize_values(
    dimension: str,
    values: list,
    pos_dims: list,
    max_items: int | None = None,
) -> list[str]:
    """把模型标签归一为当前允许值并限制输出数量。

    Args:
        dimension (str): 固定正向标签维度。
        values (list): 模型返回的标准值或别名列表。
        pos_dims (list): 本次打标使用的运行时正向维度配置。
        max_items (int | None): 最多保留的唯一标签数；为空时不限制。

    Returns:
        list[str]: 按模型首次出现顺序排列的标准允许值。

    Note:
        未识别或歧义别名只记录拒绝日志，不会进入公开标签 JSON。
    """
    allowed = _build_allowed(dimension, pos_dims)
    mapper = get_tag_value_id_mapper()
    resolved, unknown = mapper.resolve_many(dimension, [str(value) for value in values if value])
    canonical: list[str] = []
    for item in resolved:
        if item["value"] in allowed and item["value"] not in canonical:
            canonical.append(item["value"])
            if max_items is not None and len(canonical) >= max_items:
                break
    if unknown:
        logger.info(f"[标签值约束] dimension={dimension} rejected={unknown}")
    return canonical




def _normalize_work_years_str(v: str) -> str:
    """去除工作年限标签在“年”和加号前的多余空格。

    Args:
        v: 模型生成的工作年限标签。

    Returns:
        可与运行时允许值直接比较的标签文本。
    """
    return re.sub(r"\s+(?=[年+])", "", v.strip())


def _expand_edu_values(raw_values: list[str]) -> list[str]:
    """按学历层级表展开标签并保持首次出现顺序。

    Args:
        raw_values: 模型或规则层产生的原始学历标签。

    Returns:
        展开下级学历标签后的去重列表。
    """
    expanded: list[str] = []
    seen: set[str] = set()
    for v in raw_values:
        # 兼容历史半角/全角括号写法，标准输出统一为“海外名校QS50/QS100”。
        canonical_value = re.sub(r"[()（）]", "", v)
        for ev in EDUCATION_HIERARCHY.get(canonical_value, [canonical_value]):
            if ev not in seen:
                seen.add(ev)
                expanded.append(ev)
    return expanded


def _collect_skill_evidence_text(
    resume: dict,
    skill_evidence_text: str = "",
) -> str:
    """汇总适合执行确定性技能匹配的简历字段。

    Args:
        resume (dict): 已解析并归一化的简历字典。
        skill_evidence_text (str): Python 从源文档抽取的内部完整文本。

    Returns:
        str: 由候选人、专业、职位、工作和项目技术描述拼接的证据文本。

    Note:
        结构化字段不主动加入公司名、学校名和联系方式。同步解析与打标可额外
        传入完整原文，补足统一 JSON 省略的真实技能；该文本不会进入公开 JSON。
    """
    candidate = resume.get("candidate") or {}
    parts = [
        candidate.get("selfEvaluation", ""),
        candidate.get("expectedPosition", ""),
    ]
    for education in resume.get("educations") or []:
        parts.append(education.get("major", ""))
    for work in resume.get("works") or []:
        parts.extend([
            work.get("position", ""),
            work.get("description", ""),
        ])
    for project in resume.get("projects") or []:
        parts.extend([
            project.get("projectName", ""),
            project.get("role", ""),
            project.get("description", ""),
        ])
    if skill_evidence_text:
        parts.append(skill_evidence_text)
    return "\n".join(str(part) for part in parts if part)


def _collect_business_evidence_text(resume: dict) -> str:
    """汇总公司资质和行业分类需要的业务证据字段。

    Args:
        resume (dict): 已解析并归一化的简历字典。

    Returns:
        str: 候选人简介、公司、职位和项目业务描述拼接文本。
    """
    candidate = resume.get("candidate") or {}
    parts = [
        candidate.get("selfEvaluation", ""),
        candidate.get("expectedPosition", ""),
    ]
    for work in resume.get("works") or []:
        parts.extend([
            work.get("company", ""),
            work.get("position", ""),
            work.get("description", ""),
        ])
    for project in resume.get("projects") or []:
        parts.extend([
            project.get("company", ""),
            project.get("projectName", ""),
            project.get("role", ""),
            project.get("description", ""),
        ])
    return "\n".join(str(part) for part in parts if part)


def _contains_skill_term(text: str, term: str) -> bool:
    """判断技能标准值或别名是否作为完整术语出现在证据文本中。

    Args:
        text (str): 已拼接的技能证据文本。
        term (str): 技能标准值或唯一别名。

    Returns:
        bool: 至少一次术语命中具有正向使用证据时返回 `True`。

    Note:
        含空格的术语允许原文使用连续空白。英文和数字术语增加边界；长度不
        超过两个字符的短术语还把常见技术连接符视为边界字符。每次命中会检查
        前后语境，排除否定、学习计划、岗位要求和加分项等非能力证据。
    """
    value = str(term or "").strip()
    if not value:
        return False
    pattern = re.escape(value).replace(r"\ ", r"\s+")
    if re.search(r"[A-Za-z0-9]", value):
        compact_value = re.sub(r"\s+", "", value)
        boundary_chars = (
            r"A-Za-z0-9_+./-" if len(compact_value) <= 2 else r"A-Za-z0-9"
        )
        pattern = rf"(?<![{boundary_chars}]){pattern}(?![{boundary_chars}])"
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        before = text[max(0, match.start() - 240):match.start()]
        after = text[match.end():min(len(text), match.end() + 32)]
        has_negative_prefix = any(
            negative.search(before)
            for negative in _SKILL_NEGATIVE_PREFIX_PATTERNS
        )
        has_negative_suffix = any(
            negative.search(after)
            for negative in _SKILL_NEGATIVE_SUFFIX_PATTERNS
        )
        has_course_only_context = _SKILL_COURSE_CONTEXT_RE.search(before) is not None
        if (
            not has_negative_prefix
            and not has_negative_suffix
            and not has_course_only_context
        ):
            return True
    return False


def _is_skill_alias_eligible(record: dict, term: str) -> bool:
    """判断技能别名是否足够具体，可用于无模型的规则匹配。

    Args:
        record (dict): 包含技能标准值、ID 和别名的映射记录。
        term (str): 当前待判断的标准值或别名。

    Returns:
        bool: 标准值或满足保守语义约束的别名返回 `True`。

    Note:
        标准值始终允许。纯英文别名依赖术语边界；中英混合别名必须直接包含
        标准技能词；纯中文别名至少包含五个汉字，以过滤“接口开发”“信号源”
        等容易跨领域误报的宽泛短语。
    """
    value = str(record.get("value", "")).strip()
    alias = str(term or "").strip()
    if not alias:
        return False
    if alias == value:
        return True

    has_ascii = bool(re.search(r"[A-Za-z0-9]", alias))
    chinese_characters = re.findall(r"[\u3400-\u9fff]", alias)
    if has_ascii and not chinese_characters:
        return True
    if has_ascii:
        normalized_value = re.sub(r"\s+", "", value).casefold()
        normalized_alias = re.sub(r"\s+", "", alias).casefold()
        return normalized_value in normalized_alias
    return len(chinese_characters) >= 5


def _extract_skills_rule(
    resume: dict,
    max_items: int = MAX_SKILL_TAGS,
    skill_evidence_text: str = "",
) -> list[str]:
    """使用最新技能库的标准值和唯一别名提取本地技能标签。

    Args:
        resume (dict): 已解析并归一化的简历字典。
        max_items (int): 按技能库顺序最多保留的技能数量。
        skill_evidence_text (str): Python 从源文档抽取的内部完整文本。

    Returns:
        list[str]: 按 `skill_300_new.xlsx` 映射顺序排列的标准技能值。

    Note:
        多个技能共享的歧义别名不会由映射器解析，因此不会触发任一技能。
        唯一但宽泛的短中文别名也不参与规则匹配。结果只包含既有标准值，不
        改变标签与 ID 的绑定关系。
    """
    evidence = _collect_skill_evidence_text(resume, skill_evidence_text)
    if not evidence or max_items <= 0:
        return []
    mapper = get_tag_value_id_mapper()
    matched: list[str] = []
    for record in mapper.records("skills"):
        for term in [record["value"], *record.get("aliases", [])]:
            if not _is_skill_alias_eligible(record, term):
                continue
            resolved = mapper.resolve("skills", term)
            if not resolved or resolved["id"] != record["id"]:
                continue
            if _contains_skill_term(evidence, term):
                matched.append(record["value"])
                break
        if len(matched) >= max_items:
            break
    return matched


def _prepare_positive_prompt_dimensions(
    resume: dict,
    pos_dims: list,
    skill_evidence_text: str = "",
) -> tuple[list, dict[str, int] | None]:
    """为本次正向 Prompt 生成证据支持的技能候选集。

    Args:
        resume (dict): 已解析并归一化的简历字典。
        pos_dims (list): 包含完整技能允许值的运行时正向维度配置。
        skill_evidence_text (str): Python 从源文档抽取的内部完整文本。

    Returns:
        tuple[list, dict[str, int] | None]: Prompt 专用维度深拷贝及技能压缩统计；
        技能维度不存在或已禁用时，统计信息返回 `None`。

    Note:
        仅修改深拷贝中的 `skills.allowedValues`。调用方必须继续使用原始
        `pos_dims` 校验模型结果，确保 300 技能库和 ID 映射契约不受影响。
    """
    prompt_dimensions = copy.deepcopy(pos_dims)
    skill_dimension = next(
        (
            dimension
            for dimension in prompt_dimensions
            if dimension.get("dimension") == "skills"
            and dimension.get("enabled", True)
        ),
        None,
    )
    if skill_dimension is None:
        return prompt_dimensions, None

    skill_records = get_tag_value_id_mapper().records("skills")
    library_size = len(skill_records)
    configured_values = {
        value
        for value in (skill_dimension.get("allowedValues") or [])
        if value
    }
    evidence_values = _extract_skills_rule(
        resume,
        max_items=library_size,
        skill_evidence_text=skill_evidence_text,
    )
    candidates = [
        value for value in evidence_values if value in configured_values
    ]
    skill_dimension["allowedValues"] = candidates
    return prompt_dimensions, {
        "skillLibrarySize": library_size,
        "promptCandidates": len(candidates),
        "omitted": max(0, library_size - len(candidates)),
    }


def extract_positive_tags_rule(
    resume: dict,
    skill_evidence_text: str = "",
) -> dict:
    """使用本地规则提取固定维度的正向标签。

    Args:
        resume: 已解析并归一化的简历字典。
        skill_evidence_text: Python 从源文档抽取的内部完整文本。

    Returns:
        包含 ``positiveTags`` 和整体 ``confidence`` 的结果字典。

    Note:
        本函数是 LLM 提取失败时的降级路径，生成 education、companyBg、
        workYears、skills 和 industry。维度值统一使用 ``{dimension}ValueArray``。
    """
    tags = []

    # 教育背景由结构化学校和最高学历确定，模型不可用时保持同一计算口径。
    schools = [e.get("school", "") for e in resume.get("educations", [])]
    degrees = [e.get("education", "") for e in resume.get("educations", [])]
    edu_tags = classify_education(schools, degrees)
    if edu_tags:
        tags.append({"dimension": "education", "educationValueArray": edu_tags, "source": "rule"})

    # workYears → workYearsValueArray（单元素数组）
    works = resume.get("works", [])
    work_summary = calculate_work_experience(resume)
    work_year_values = (
        [work_summary.work_years_band] if work_summary.work_years_band else []
    )
    tags.append({
        "dimension": "workYears",
        "workYearsValueArray": work_year_values,
        "source": "rule",
    })

    # companyBg → companyBgValueArray
    companies = [w.get("company", "") for w in works]
    all_text = _collect_business_evidence_text(resume)
    company_results = classify_companies(companies, evidence_text=all_text)
    if company_results:
        tags.append({
            "dimension": "companyBg",
            "companyBgValueArray": [ct["value"] for ct in company_results],
            "source": "rule",
            "confidence": round(max(ct["confidence"] for ct in company_results), 2),
        })

    # 技能只使用最新技能库的标准值和唯一别名，避免降级结果产生库外标签。
    skill_values = _extract_skills_rule(
        resume,
        skill_evidence_text=skill_evidence_text,
    )
    if skill_values:
        tags.append({
            "dimension": "skills",
            "skillsValueArray": skill_values,
            "source": "rule",
        })

    # industry → industryValueArray
    industry_results = classify_industry(all_text)
    if industry_results:
        tags.append({
            "dimension": "industry",
            "industryValueArray": [ind["value"] for ind in industry_results],
            "source": "rule",
            "confidence": round(max(ind["confidence"] for ind in industry_results), 2),
        })

    confs = [t["confidence"] for t in tags if "confidence" in t]
    overall_conf = round(sum(confs) / len(confs), 2) if confs else 0.70

    pos_dims = _tagger_load_cfg()["positiveDimensions"]
    return {
        "positiveTags": _normalize_positive_tags(
            tags,
            resume,
            pos_dims,
            skill_evidence_text=skill_evidence_text,
        ),
        "confidence": overall_conf,
    }


def _normalize_positive_tags(
    tags: list,
    resume: dict,
    pos_dims=None,
    skill_evidence_text: str = "",
    llm_assisted: bool = False,
) -> list:
    """将模型正向标签归一化为 API 使用的维度数组格式。

    Args:
        tags: 模型返回的原始正向标签列表。
        resume: 用于补齐学历和工作年限维度的结构化简历。
        pos_dims: 当前运行时正向维度配置；为空时按需读取配置文件。
        skill_evidence_text: Python 从源文档抽取的内部完整文本。
        llm_assisted: 输入标签是否来自一次成功解析的模型响应。

    Returns:
        过滤禁用维度并完成层级展开、合并和缺省补齐的标签列表。

    Note:
        废弃的 ``rankLevel`` 会被丢弃。companyBg 和 industry 各合并为一项，
        其他维度统一使用 ``{dimension}ValueArray`` 字段。
    """
    if pos_dims is None:
        pos_dims = _tagger_load_cfg()["positiveDimensions"]
    normalized: list[dict] = []
    company_bg_items: list[dict] = []
    industry_items: list[dict] = []
    business_evidence = _collect_business_evidence_text(resume)

    for t in tags:
        dim = t.get("dimension", "")

        if dim == "rankLevel":
            continue  # 已废弃此维度

        elif dim == "education":
            continue  # 教育背景由下方确定性规则统一覆盖，不接受模型分类。

        elif dim == "workYears":
            continue  # 工作年限由下方确定性规则统一覆盖，不接受模型分桶。

        elif dim == "companyBg":
            if "companyBgValueArray" in t or "value" in t:
                company_bg_items.append(t)

        elif dim == "industry":
            if "industryValueArray" in t or "value" in t:
                industry_items.append(t)

        elif dim == "skills":
            raw = t.get("skillsValueArray", t.get("value", []))
            raw = raw if isinstance(raw, list) else [raw]
            skill_record_count = len(get_tag_value_id_mapper().records("skills"))
            model_values = _canonicalize_values(
                "skills",
                raw,
                pos_dims,
                max_items=skill_record_count,
            )
            structured_evidence_values = _extract_skills_rule(
                resume,
                max_items=skill_record_count,
            )
            evidence_values = _extract_skills_rule(
                resume,
                max_items=skill_record_count,
                skill_evidence_text=skill_evidence_text,
            )
            evidence_set = set(evidence_values)
            values = [value for value in model_values if value in evidence_set]
            has_verified_model_values = bool(values)
            rejected_values = [
                value for value in model_values if value not in evidence_set
            ]
            if rejected_values:
                logger.info(f"[技能证据约束] rejected={rejected_values}")

            # 模型只读取公开 Resume JSON；为仅存在于原文的技能预留少量位置，
            # 避免模型列表已满时再次截掉同步解析专门保留的证据。
            model_priority_values = values[:MAX_SKILL_TAGS]
            structured_evidence_set = set(structured_evidence_values)
            raw_only_supplements = [
                value for value in evidence_values
                if value not in structured_evidence_set
                and value not in model_priority_values
            ][:_RAW_EVIDENCE_RESERVED_SKILL_SLOTS]
            if raw_only_supplements:
                model_value_limit = MAX_SKILL_TAGS - len(raw_only_supplements)
                values = (
                    model_priority_values[:model_value_limit]
                    + raw_only_supplements
                )
                logger.info(
                    f"[技能原文补充] supplemented={raw_only_supplements}"
                )
            else:
                values = model_priority_values
            for value in evidence_values:
                if value not in values:
                    values.append(value)
                if len(values) >= MAX_SKILL_TAGS:
                    break
            normalized.append({
                "dimension": "skills",
                "skillsValueArray": values,
                "source": (
                    "hybrid"
                    if llm_assisted and has_verified_model_values
                    else "rule"
                ),
                **({"confidence": t["confidence"]} if "confidence" in t else {}),
            })

        else:
            # 自定义维度：将 LLM 可能输出的 "value" 字段规范化为 {dim}ValueArray
            arr_key = f"{dim}ValueArray"
            if arr_key not in t and "value" in t:
                raw = t.get("value", [])
                new_t = {k: v for k, v in t.items() if k != "value"}
                new_t[arr_key] = raw if isinstance(raw, list) else [raw]
                t = new_t
            if llm_assisted:
                t["source"] = "llm"
            normalized.append(t)

    # 公司背景必须通过事实源或公司自身明确证据复核，模型只负责候选召回。
    companies = [
        work.get("company", "")
        for work in resume.get("works") or []
    ]
    rule_company_values = classify_companies(
        companies,
        evidence_text=business_evidence,
    )
    # 合并 companyBg → 单条；没有规则证据的模型值会被拒绝。
    if company_bg_items or rule_company_values:
        model_vals: list[str] = []
        for item in company_bg_items:
            if "companyBgValueArray" in item:
                model_vals.extend(item["companyBgValueArray"])
            elif "value" in item:
                model_vals.append(item["value"])
        canonical_model_vals = _canonicalize_values(
            "companyBg",
            model_vals,
            pos_dims,
        )
        rule_vals = _canonicalize_values(
            "companyBg",
            [item["value"] for item in rule_company_values],
            pos_dims,
        )
        rule_set = set(rule_vals)
        rejected_model_vals = [
            value for value in canonical_model_vals if value not in rule_set
        ]
        if rejected_model_vals:
            logger.info(f"[公司背景证据约束] rejected={rejected_model_vals}")
        merged: dict = {
            "dimension": "companyBg",
            "companyBgValueArray": rule_vals,
            "source": (
                "hybrid"
                if llm_assisted and bool(set(canonical_model_vals) & rule_set)
                else "rule"
            ),
        }
        rule_confidences = [
            item["confidence"] for item in rule_company_values
        ]
        if rule_confidences:
            merged["confidence"] = round(max(rule_confidences), 2)
        normalized.append(merged)

    # 行业模型值必须通过强证据规则复核，规则同时补齐模型漏项。
    rule_industry_results = classify_industry(business_evidence)
    rule_industry_values = [
        item["value"] for item in rule_industry_results
    ]
    if industry_items or rule_industry_values:
        all_vals = []
        all_confs = []
        for item in industry_items:
            if "industryValueArray" in item:
                all_vals.extend(item["industryValueArray"])
            elif "value" in item:
                all_vals.append(item["value"])
            if "confidence" in item:
                all_confs.append(item["confidence"])
        allowed_by_rule = set(rule_industry_values)
        evidence_backed_model_values = [
            value for value in all_vals if value in allowed_by_rule
        ]
        merged_vals = _canonicalize_values(
            "industry",
            [*evidence_backed_model_values, *rule_industry_values],
            pos_dims,
            max_items=MAX_INDUSTRY_TAGS,
        )
        merged = {
            "dimension": "industry",
            "industryValueArray": merged_vals,
            "source": (
                "hybrid"
                if llm_assisted and bool(evidence_backed_model_values)
                else "rule"
            ),
        }
        rule_confidences = [
            item["confidence"] for item in rule_industry_results
        ]
        if all_confs or rule_confidences:
            merged["confidence"] = round(
                max([*all_confs, *rule_confidences]),
                2,
            )
        normalized.append(merged)

    # 模型漏掉技能维度时仍从简历中的明确术语补齐，空证据不创建空维度。
    if not any(item.get("dimension") == "skills" for item in normalized):
        evidence_values = _extract_skills_rule(
            resume,
            skill_evidence_text=skill_evidence_text,
        )
        if evidence_values:
            normalized.append({
                "dimension": "skills",
                "skillsValueArray": evidence_values,
                "source": "rule",
            })

    # 教育背景始终由结构化学校和最高学历覆盖模型结果。
    schools = [e.get("school", "") for e in resume.get("educations", [])]
    degrees = [e.get("education", "") for e in resume.get("educations", [])]
    edu_tags = classify_education(schools, degrees)
    if edu_tags:
        normalized.append({
            "dimension": "education",
            "educationValueArray": _canonicalize_values(
                "education", _expand_edu_values(edu_tags), pos_dims
            ),
            "source": "rule",
        })

    # 工作年限始终由确定性计算覆盖模型结果。
    work_summary = calculate_work_experience(resume)
    raw_work_values = (
        [work_summary.work_years_band] if work_summary.work_years_band else []
    )
    work_values = _canonicalize_values(
        "workYears", raw_work_values, pos_dims, max_items=1
    )
    normalized.append({
        "dimension": "workYears",
        "workYearsValueArray": work_values,
        "source": "rule",
    })

    # 过滤掉配置中已删除或禁用的维度，防止 LLM 幻觉/规则兜底产生的残留标签进入响应
    enabled_dims = {d["dimension"] for d in pos_dims if d.get("enabled", True)}
    normalized = [t for t in normalized if t.get("dimension") in enabled_dims]

    return sort_positive_tag_entries(normalized)


@observe()
def extract_positive_tags_llm(
    resume: dict,
    job_info: dict,
    llm_call_fn,
    skill_evidence_text: str = "",
) -> dict:
    """调用模型提取正向标签，并在失败时回退到本地规则。

    Args:
        resume: 已解析并归一化的简历字典。
        job_info: 包含岗位名称、技能要求和岗位描述的字典。
        llm_call_fn: 接受消息列表并返回模型正文字符串的调用函数。
        skill_evidence_text: Python 从源文档抽取的内部完整文本。

    Returns:
        包含标准化 ``positiveTags`` 和整体 ``confidence`` 的结果字典。

    Note:
        模型调用、JSON 修复或响应结构校验失败均不会中断打标，而是记录警告并
        使用 ``extract_positive_tags_rule`` 生成降级结果。
    """
    # 配置只读一次，避免同一调用链多次 IO
    cfg      = _tagger_load_cfg()
    pos_dims = cfg["positiveDimensions"]
    prompt_dims, prompt_skill_metrics = _prepare_positive_prompt_dimensions(
        resume,
        pos_dims,
        skill_evidence_text=skill_evidence_text,
    )
    prompt = _build_positive_prompt(
        prompt_dims,
        resume_json=json.dumps(resume, ensure_ascii=False, indent=2),
        job_title=job_info.get("jobTitle", ""),
        required_skills=job_info.get("requiredSkills", ""),
        job_description=job_info.get("jobDescription", ""),
    )
    if prompt_skill_metrics is not None:
        logger.debug(
            "[正向技能Prompt压缩] "
            f"skillLibrarySize={prompt_skill_metrics['skillLibrarySize']} "
            f"promptCandidates={prompt_skill_metrics['promptCandidates']} "
            f"omitted={prompt_skill_metrics['omitted']} "
            f"promptCharacters={len(prompt)}"
        )
    try:
        raw = llm_call_fn([
            {"role": "system", "content": TAGGER_SYSTEM},
            {"role": "user",   "content": prompt},
        ])
        result = json_repair.loads(raw)
        if isinstance(result, dict) and "positiveTags" in result:
            result["positiveTags"] = _normalize_positive_tags(
                result["positiveTags"],
                resume,
                pos_dims,
                skill_evidence_text=skill_evidence_text,
                llm_assisted=True,
            )
            confs = [t["confidence"] for t in result["positiveTags"] if "confidence" in t]
            result["confidence"] = round(sum(confs) / len(confs), 2) if confs else 0.70
            update_current_observation(
                input={"resume": resume, "jobInfo": job_info},
                output={"positiveResult": result},
            )
            return result
    except Exception:
        logger.warning("LLM 正向打标失败，降级到规则层")

    rule_result = extract_positive_tags_rule(
        resume,
        skill_evidence_text=skill_evidence_text,
    )
    update_current_observation(
        input={"resume": resume, "jobInfo": job_info},
        output={"positiveResult": rule_result, "degraded": True},
    )
    return rule_result


# 敏感词和风险检测

# 固定风险维度使用专用检测函数，其他维度使用通用关键词执行器。
_HARDCODED_NEG_DIMS = frozenset({"jobstable", "integrity", "location", "matchRisk"})


def _collect_all_text(resume: dict) -> str:
    """拼接规则检测所需的候选人和经历文本字段。

    Args:
        resume: 已解析的简历字典。

    Returns:
        以空格连接且排除空值的全文匹配字符串。
    """
    c = resume.get("candidate", {})
    parts = [c.get(f, "") for f in ("address", "nativePlace", "selfEvaluation", "expectedPosition")]
    for edu in resume.get("educations", []):
        parts.extend([edu.get("school", ""), edu.get("major", "")])
    for work in resume.get("works", []):
        parts.extend([work.get("company", ""), work.get("position", ""), work.get("description", "")])
    for proj in resume.get("projects", []):
        parts.append(proj.get("description", ""))
    return " ".join(p for p in parts if p)


def _detect_keyword_rules(resume: dict, dim_cfg: dict, hit_tags: list):
    """执行运行时自定义负向维度的关键词规则。

    Args:
        resume: 已解析的简历字典。
        dim_cfg: 包含显示名、规则和搜索字段的负向维度配置。
        hit_tags: 就地追加命中记录的列表。

    Note:
        ``searchIn`` 为空时默认全文检索，也可限定到受支持的候选人或经历字段。
        同一规则命中多个关键词时只追加一次，避免重复扣分。
    """
    category = dim_cfg.get("displayName", dim_cfg.get("dimension", ""))

    _field_extractors: dict[str, callable] = {
        "works.company":              lambda r: [w.get("company", "")     for w in r.get("works", [])],
        "works.description":          lambda r: [w.get("description", "") for w in r.get("works", [])],
        "works.position":             lambda r: [w.get("position", "")    for w in r.get("works", [])],
        "educations.school":          lambda r: [e.get("school", "")      for e in r.get("educations", [])],
        "educations.major":           lambda r: [e.get("major", "")       for e in r.get("educations", [])],
        "projects.description":       lambda r: [p.get("description", "") for p in r.get("projects", [])],
        "candidate.address":          lambda r: [r.get("candidate", {}).get("address", "")],
        "candidate.nativePlace":      lambda r: [r.get("candidate", {}).get("nativePlace", "")],
        "candidate.selfEvaluation":   lambda r: [r.get("candidate", {}).get("selfEvaluation", "")],
    }

    for rule_key, rule in dim_cfg.get("rules", {}).items():
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        keywords = [kw for kw in rule.get("keywords", []) if kw]
        if not keywords:
            continue

        search_in = rule.get("searchIn", ["all"])
        if isinstance(search_in, str):
            search_in = [search_in]

        if "all" in search_in:
            full_text = _collect_all_text(resume)
        else:
            texts = []
            for field_key in search_in:
                extractor = _field_extractors.get(field_key)
                if extractor:
                    texts.extend(extractor(resume))
            full_text = " ".join(t for t in texts if t)

        tag_name = rule.get("tagName", rule_key)
        severity = rule.get("severity", "medium")

        for kw in keywords:
            if kw in full_text:
                idx     = full_text.find(kw)
                snippet = full_text[max(0, idx - 10): idx + len(kw) + 20].strip()
                hit_tags.append({
                    "dimension":    dim_cfg.get("dimension", ""),
                    "ruleKey":      rule_key,
                    "category":      category,
                    "tag":           tag_name,
                    "severity":      severity,
                    "matchedContent": f"...{snippet}...",
                    "source":        "rule",
                })
                break  # 同一规则匹配一次即可，避免重复计分


def _build_negative_prompt(neg_dims_llm: list, resume_json: str) -> str:
    """根据启用的 LLM 负向维度构造风险检测提示词。

    Args:
        neg_dims_llm: ``detectionType`` 为 ``llm`` 的负向维度配置。
        resume_json: 序列化后的结构化简历。

    Returns:
        包含检测规则和输出 schema 示例的完整提示词。

    Note:
        规则缺少 ``description`` 时使用 ``tagName`` 作为语义检测说明。
    """
    sections = []
    for d in neg_dims_llm:
        if not d.get("enabled", True):
            continue
        rules_lines = []
        for rule_key, rule in d.get("rules", {}).items():
            if not isinstance(rule, dict) or not rule.get("enabled", True):
                continue
            tag_name    = rule.get("tagName", rule_key)
            severity    = rule.get("severity", "medium")
            description = rule.get("description", tag_name)
            rule_number = len(rules_lines) + 1
            rules_lines.append(
                f"{rule_number}. {tag_name}（严重程度：{severity}）：{description}"
            )
        if not rules_lines:
            continue
        sections.append(GENERIC_NEG_DIM_PROMPT.format(
            dimension=d["dimension"],
            display_name=d.get("displayName", d["dimension"]),
            rules_text="\n".join(rules_lines),
        ))

    return NEGATIVE_PROMPT_HEADER.format(
        resume_json=resume_json,
        schema_example=json.dumps(NEGATIVE_TAG_SCHEMA, ensure_ascii=False, indent=2),
    ) + "\n\n".join(sections)


@observe()
def extract_negative_tags_llm(resume: dict, neg_dims_llm: list, llm_call_fn) -> list[dict]:
    """调用模型检测配置为 LLM 类型的负向标签。

    Args:
        resume: 已解析并归一化的简历字典。
        neg_dims_llm: 当前启用的 LLM 负向维度配置。
        llm_call_fn: 接受消息列表并返回模型正文字符串的调用函数。

    Returns:
        转换为 ``hitTags`` 分组格式的命中列表；失败时返回空列表。

    Note:
        模型输出仅接受配置白名单中的维度，未知维度会被丢弃。调用或解析失败
        只跳过 LLM 负向维度，不影响规则检测结果。
    """
    prompt = _build_negative_prompt(
        neg_dims_llm,
        json.dumps(resume, ensure_ascii=False, indent=2),
    )
    # displayName 查找表，用于将 LLM 输出的 dimension 转换为 category（displayName）
    dim_to_display = {d["dimension"]: d.get("displayName", d["dimension"]) for d in neg_dims_llm}

    try:
        raw    = llm_call_fn([
            {"role": "system", "content": TAGGER_SYSTEM},
            {"role": "user",   "content": prompt},
        ])
        result = json_repair.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("非 dict 输出")

        hit_groups: list[dict] = []
        for group in result.get("negativeTags", []):
            dim  = group.get("dimension", "")
            # 白名单过滤：LLM 幻觉输出的未知维度直接丢弃
            if dim not in dim_to_display:
                continue
            hits = [
                {**h, "source": "llm"}
                for h in group.get("hits", [])
                if isinstance(h, dict) and h.get("tag")
            ]
            if not hits:
                continue
            hit_groups.append({
                "dimension": dim,
                "category": dim_to_display[dim],
                "hits": hits,
            })
        update_current_observation(
            input={"resume": resume, "negDims": neg_dims_llm},
            output={"negativeHits": hit_groups},
        )
        return hit_groups

    except Exception:
        logger.warning("LLM 负向打标失败，跳过 llm 类型维度")
        update_current_observation(
            input={"resume": resume, "negDims": neg_dims_llm},
            output={"negativeHits": []},
        )
        return []


def _neg_cfg_map(neg_dims=None) -> dict[str, dict]:
    """按稳定维度标识构建负向配置查找表。

    Args:
        neg_dims: 本次调用指定的负向维度；为空时读取运行时配置。

    Returns:
        以 ``dimension`` 为键、完整维度配置为值的字典。

    Note:
        不使用可经 API 修改的 ``displayName`` 作为键，避免改名导致规则失效。
    """
    dims = neg_dims if neg_dims is not None else _tagger_load_cfg()["negativeDimensions"]
    return {d["dimension"]: d for d in dims}


def detect_sensitive_tags(resume: dict, job_info: dict) -> dict:
    """执行专用和自定义关键词规则的负向标签检测。

    Args:
        resume: 已解析并归一化的简历字典。
        job_info: 包含请求级敏感词等岗位信息的字典。

    Returns:
        包含分组命中 ``hitTags``、命中数 ``hitCount`` 和扣分
        ``deductionScore`` 的字典。

    Note:
        专用维度由固定检测函数处理，其他运行时维度使用通用关键词执行器。
        扣分直接按每条命中的严重级别累加。
    """
    _runtime = _tagger_load_cfg()
    hit_tags: list[dict] = []
    works = resume.get("works", [])
    candidate = resume.get("candidate", {})
    cfg = _neg_cfg_map(_runtime["negativeDimensions"])

    # 1. 职业稳定性风险
    stab = cfg.get("jobstable", {})
    if stab and stab.get("enabled", True):   # stab={} 表示维度已被删除，直接跳过
        _detect_stability(
            works,
            resume.get("educations", []),
            hit_tags,
            stab.get("rules", {}),
        )

    # 2. 诚信与合规风险
    comp = cfg.get("integrity", {})
    if comp and comp.get("enabled", True):
        _detect_timeline_overlap(works, hit_tags, comp.get("rules", {}))

    # 3. 地域相关：配置预设词与请求级敏感词合并。
    loc = cfg.get("location", {})
    if loc and loc.get("enabled", True):
        sw_cfg = loc.get("rules", {}).get("sensitive_words", {})
        if sw_cfg.get("enabled", True):
            configured_words = [
                word.strip()
                for word in sw_cfg.get("keywords", [])
                if isinstance(word, str) and word.strip()
            ]
            request_words = _parse_sensitive_words(
                job_info.get("jobSensitiveWords", "")
            )
            sensitive_words = list(dict.fromkeys([*configured_words, *request_words]))
            if sensitive_words:
                _detect_custom_words(
                    resume,
                    candidate,
                    sensitive_words,
                    hit_tags,
                    severity=sw_cfg.get("severity", "high"),
                    tag_name=sw_cfg.get("tagName", "敏感地域"),
                    category=loc.get("displayName", "地域相关"),
                )

    # 4. 匹配度硬伤
    match = cfg.get("matchRisk", {})
    if match and match.get("enabled", True):
        _detect_education_risk(resume.get("educations", []), hit_tags,
                               match.get("rules", {}))

    # 5. API 新增的自定义维度 → 通用关键词规则执行器
    for dim_cfg in _runtime["negativeDimensions"]:
        if dim_cfg["dimension"] in _HARDCODED_NEG_DIMS:
            continue  # 硬编码维度已由专用函数处理
        if not dim_cfg.get("enabled", True):
            continue
        _detect_keyword_rules(resume, dim_cfg, hit_tags)

    deduction = sum(_runtime["severityDeduction"].get(t["severity"], -5) for t in hit_tags)

    grouped: dict[str, dict] = {}
    for t in hit_tags:
        cat = t["category"]
        group_key = t.get("dimension") or cat
        if group_key not in grouped:
            grouped[group_key] = {
                "dimension": t.get("dimension", ""),
                "category": cat,
                "hits": [],
            }
        grouped[group_key]["hits"].append({
            "tag": t["tag"],
            "ruleKey": t.get("ruleKey", ""),
            "severity": t["severity"],
            "matchedContent": t["matchedContent"],
            "source": t.get("source", "rule"),
        })

    return {
        "hitTags": list(grouped.values()),
        "hitCount": len(hit_tags),
        "deductionScore": deduction,
    }


def _uncovered_gap_months(
    previous_end: datetime,
    current_start: datetime,
    educations: list,
) -> int:
    """计算两段正式工作之间未被在校经历覆盖的月份。

    Args:
        previous_end (datetime): 前一段正式工作的结束年月。
        current_start (datetime): 后一段正式工作的开始年月。
        educations (list): 结构化教育背景列表。

    Returns:
        int: 原始空窗月份扣除有效教育区间并集后的剩余月份。

    Note:
        教育区间和工作区间都使用左闭右开的月份索引。日期缺失、顺序错误或
        不与空窗相交的教育记录不参与扣减。
    """
    gap_start = _to_month_index(previous_end)
    gap_end = _to_month_index(current_start)
    if gap_end <= gap_start:
        return 0

    covered_intervals: list[tuple[int, int]] = []
    for education in educations:
        if not isinstance(education, dict):
            continue
        education_start = _parse_date(education.get("startDate", ""))
        education_end = _parse_date(education.get("endDate", ""))
        if not education_start or not education_end or education_end < education_start:
            continue
        covered_start = max(gap_start, _to_month_index(education_start))
        covered_end = min(gap_end, _to_month_index(education_end))
        if covered_end > covered_start:
            covered_intervals.append((covered_start, covered_end))

    covered_months = _merged_interval_months(covered_intervals)
    return max(0, gap_end - gap_start - covered_months)


def _detect_stability(
    works: list,
    educations: list,
    hit_tags: list,
    rules: dict,
):
    """检测频繁跳槽、短任职和工作空窗期。

    Args:
        works: 工作经历字典列表。
        educations: 用于排除在校月份的教育背景字典列表。
        hit_tags: 就地追加职业稳定性风险记录的列表。
        rules: 职业稳定性维度下的运行时规则配置。

    Note:
        实习、兼职等非正式经历不参与跳槽、短任职和空窗检测。少于两段有效
        正式经历时不计算空窗；教育区间覆盖的月份不计入工作空窗。
    """
    parsed = []
    for w in works:
        if not isinstance(w, dict) or _is_non_full_time(w):
            continue
        start = _parse_date(w.get("startDate", ""))
        end = _parse_date(w.get("endDate", ""))
        if start and end:
            parsed.append((start, end, w))

    if not parsed:
        return

    parsed.sort(key=lambda x: x[0])

    # 频繁跳槽
    fcj = rules.get("frequent_job_change", {})
    if fcj.get("enabled", True):
        window_years = fcj.get("window_years", 3)
        threshold = fcj.get("threshold", 4)
        severity = fcj.get("severity", "medium")
        recent_cutoff = datetime.now().replace(year=datetime.now().year - window_years)
        recent_items = [(s, e, w) for s, e, w in parsed if s >= recent_cutoff]
        if len(recent_items) >= threshold:
            companies = ", ".join(w.get("company", "") for _, _, w in recent_items)
            hit_tags.append({
                "dimension": "jobstable",
                "ruleKey": "frequent_job_change",
                "category": "职业稳定性风险",
                "tag": fcj.get("tagName", "频繁跳槽"),
                "severity": severity,
                "matchedContent": f"近{window_years}年内{len(recent_items)}段工作经历：{companies}",
                "source": "rule",
            })

    # 最近一段任职时长偏短
    st = rules.get("short_tenure", {})
    if st.get("enabled", True):
        threshold_months = st.get("threshold_months", 3)
        severity = st.get("severity", "medium")
        last_start, last_end, last_work = parsed[-1]
        rd = relativedelta(last_end, last_start)
        last_months = rd.years * 12 + rd.months
        if last_months <= threshold_months:
            hit_tags.append({
                "dimension": "jobstable",
                "ruleKey": "short_tenure",
                "category": "职业稳定性风险",
                "tag": st.get("tagName", "最近任职时长偏短"),
                "severity": severity,
                "matchedContent": f"{last_work.get('company', '')} {last_work.get('startDate', '')}~{last_work.get('endDate', '')}，仅{last_months}个月",
                "source": "rule",
            })

    # 空窗期
    gp = rules.get("gap_period", {})
    if gp.get("enabled", True):
        threshold_months = gp.get("threshold_months", 6)
        severity = gp.get("severity", "low")
        for i in range(1, len(parsed)):
            prev_end = parsed[i - 1][1]
            curr_start = parsed[i][0]
            if curr_start > prev_end:
                gap_months = _uncovered_gap_months(
                    prev_end,
                    curr_start,
                    educations,
                )
                if gap_months >= threshold_months:
                    hit_tags.append({
                        "dimension": "jobstable",
                        "ruleKey": "gap_period",
                        "category": "职业稳定性风险",
                        "tag": gp.get("tagName", "工作空窗期过长"),
                        "severity": severity,
                        "matchedContent": f"{parsed[i-1][2].get('company','')} 离职后至 {parsed[i][2].get('company','')} 入职，空窗约{gap_months}个月",
                        "source": "rule",
                    })


def _detect_timeline_overlap(works: list, hit_tags: list, rules: dict):
    """检测相邻工作经历是否达到时间重叠阈值。

    Args:
        works: 工作经历字典列表。
        hit_tags: 就地追加诚信风险记录的列表。
        rules: 诚信维度下的运行时规则配置。

    Note:
        实习、兼职等非正式经历不参与诚信重叠检测。其余经历先按开始日期排序，
        仅比较排序后的相邻项；无法解析日期的经历忽略。
    """
    ol = rules.get("timeline_overlap", {})
    if not ol.get("enabled", True):
        return
    threshold_months = ol.get("threshold_months", 2)
    severity = ol.get("severity", "high")

    parsed = []
    for w in works:
        if not isinstance(w, dict) or _is_non_full_time(w):
            continue
        start = _parse_date(w.get("startDate", ""))
        end = _parse_date(w.get("endDate", ""))
        if start and end:
            parsed.append((start, end, w))

    parsed.sort(key=lambda x: x[0])
    for i in range(1, len(parsed)):
        _, prev_e, prev_w = parsed[i - 1]
        curr_s, _, curr_w = parsed[i]
        if curr_s < prev_e:
            rd = relativedelta(prev_e, curr_s)
            overlap_months = rd.years * 12 + rd.months
            if overlap_months >= threshold_months:
                hit_tags.append({
                    "dimension": "integrity",
                    "ruleKey": "timeline_overlap",
                    "category": "诚信与合规风险",
                    "tag": ol.get("tagName", "工作时间线重叠"),
                    "severity": severity,
                    "matchedContent": f"{prev_w.get('company','')}({prev_w.get('startDate','')}~{prev_w.get('endDate','')}) 与 {curr_w.get('company','')}({curr_w.get('startDate','')}~{curr_w.get('endDate','')}) 时间重叠{overlap_months}个月",
                    "source": "rule",
                })


def _parse_sensitive_words(raw: str) -> list[str]:
    """按中英文逗号、分号或空白拆分敏感词文本。

    Args:
        raw: 调用方传入的敏感词字符串。

    Returns:
        去除首尾空白和空元素后的敏感词列表。
    """
    if not raw:
        return []
    return [w.strip() for w in re.split(r"[,，;；\s]+", raw) if w.strip()]


def _detect_custom_words(
    resume: dict,
    candidate: dict,
    words: list[str],
    hit_tags: list,
    severity: str = "high",
    tag_name: str = "敏感地域",
    category: str = "地域相关",
):
    """在指定简历文本字段中匹配配置和请求级敏感地域词。

    Args:
        resume: 已解析的完整简历字典。
        candidate: 简历中的候选人基本信息。
        words: 已拆分并去空的敏感词列表。
        hit_tags: 就地追加地域相关命中记录的列表。
        severity: 风险标签严重级别。
        tag_name: 可绑定稳定 ID 的风险标签标准值。
        category: 当前负向维度展示名称。

    Note:
        多个敏感词只产生一个稳定风险标签，具体词和原文保留在命中证据中。
    """
    full_text_parts = [
        candidate.get("address", ""),
        candidate.get("nativePlace", ""),
        candidate.get("selfEvaluation", ""),
        candidate.get("expectedPosition", ""),
    ]
    for edu in resume.get("educations", []):
        full_text_parts.append(edu.get("school", ""))
    for work in resume.get("works", []):
        full_text_parts.extend([work.get("company", ""), work.get("description", "")])
    for proj in resume.get("projects", []):
        full_text_parts.append(proj.get("description", ""))

    full_text = " ".join(p for p in full_text_parts if p)

    matched_evidence: list[str] = []
    for word in words:
        if word and word in full_text:
            idx = full_text.find(word)
            snippet = full_text[max(0, idx - 10): idx + len(word) + 20].strip()
            matched_evidence.append(f"命中“{word}”：...{snippet}...")

    if matched_evidence:
        hit_tags.append({
            "dimension": "location",
            "ruleKey": "sensitive_words",
            "category": category,
            "tag": tag_name,
            "severity": severity,
            "matchedContent": "；".join(matched_evidence),
            "source": "rule",
        })


def _detect_education_risk(educations: list, hit_tags: list, rules: dict):
    """按院校名称关键词检测疑似培训机构背景。

    Args:
        educations: 教育经历字典列表。
        hit_tags: 就地追加匹配度风险记录的列表。
        rules: 匹配度维度下的运行时规则配置。

    Note:
        每条教育经历独立匹配，多个经历命中会分别产生风险记录。
    """
    ti = rules.get("training_institution", {})
    if not ti.get("enabled", True):
        return
    keywords = ti.get("keywords", ["培训", "学院（培训）", "训练营", "bootcamp", "在线课程", "慕课"])
    severity = ti.get("severity", "medium")

    for edu in educations:
        school = edu.get("school", "")
        if any(kw in school.lower() for kw in keywords):
            hit_tags.append({
                "dimension": "matchRisk",
                "ruleKey": "training_institution",
                "category": "匹配度硬伤",
                "tag": ti.get("tagName", "疑似培训机构背景"),
                "severity": severity,
                "matchedContent": f"教育经历中包含：{school}",
                "source": "rule",
            })


# 统一打标入口

@observe()
def run_tagging(
    resume: dict,
    job_info: dict,
    llm_call_fn=None,
    skill_evidence_text: str = "",
) -> dict:
    """执行正向提取、规则风险检测和可选的 LLM 风险检测。

    Args:
        resume: 已解析并归一化的简历字典。
        job_info: 岗位信息和请求级敏感词配置。
        llm_call_fn: 可选的模型调用函数；为空时仅执行本地规则。
        skill_evidence_text: Python 从源文档抽取的内部完整文本，仅用于技能校验。

    Returns:
        完整 ``tagInfo`` 字典，包含正向标签、置信度、负向分组、命中数、
        扣分和打标完成时间。

    Note:
        正向模型失败会回退规则层；LLM 负向检测失败则跳过对应维度。LLM 负向
        命中在规则结果基础上继续累加数量和扣分。
    """
    with log_operation(
        "标签打标",
        emit_start=False,
        failure_level="DEBUG",
    ) as operation:
        resume = normalize_resume_payload(resume)
        job_info = job_info or {}
        if llm_call_fn:
            positive_result = extract_positive_tags_llm(
                resume,
                job_info,
                llm_call_fn,
                skill_evidence_text=skill_evidence_text,
            )
        else:
            positive_result = extract_positive_tags_rule(
                resume,
                skill_evidence_text=skill_evidence_text,
            )

        sensitive_result = detect_sensitive_tags(resume, job_info)

    # LLM 负向检测：只对 detectionType='llm' 且 enabled=True 的维度调用
        if llm_call_fn:
            cfg = _tagger_load_cfg()
            neg_dims_llm = [
                d for d in cfg["negativeDimensions"]
                if d.get("detectionType", "rule") == "llm" and d.get("enabled", True)
            ]
            if neg_dims_llm:
                llm_hit_groups = extract_negative_tags_llm(resume, neg_dims_llm, llm_call_fn)
                if llm_hit_groups:
                    severity_map = cfg.get("severityDeduction", {"low": -5, "medium": -10, "high": -20})
                    sensitive_result["hitTags"].extend(llm_hit_groups)
                    for group in llm_hit_groups:
                        for hit in group.get("hits", []):
                            sensitive_result["hitCount"] += 1
                            sensitive_result["deductionScore"] += severity_map.get(
                                hit.get("severity", "medium"), -10
                            )

        result = {
            "positiveTags": positive_result["positiveTags"],
            "confidence": positive_result["confidence"],
            "hitTags": sensitive_result["hitTags"],
            "hitCount": sensitive_result["hitCount"],
            "deductionScore": sensitive_result["deductionScore"],
            "tagParseTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        operation.add(
            positiveDimensions=len(result["positiveTags"]),
            negativeHits=result["hitCount"],
        )
        update_current_observation(
            input={
                "resume": resume,
                "jobInfo": job_info,
                "skillEvidenceText": skill_evidence_text,
            },
            output={"tagInfo": result},
        )
        return result
