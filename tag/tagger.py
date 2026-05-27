"""
简历打标模块：正向标签提取 + 敏感词/风险检测
"""
import re
import json
import logging
import pathlib
import json_repair
from datetime import datetime
from dateutil.relativedelta import relativedelta
from typing import Optional

from .tag_data import (
    classify_education, classify_companies, classify_industry,
    EDUCATION_HIERARCHY,
)
from .tag_config import POSITIVE_DIMENSIONS, NEGATIVE_DIMENSIONS, SEVERITY_DEDUCTION

logger = logging.getLogger("aihr")

_RUNTIME_CFG_FILE = pathlib.Path(__file__).parent / "tag_config_runtime.json"


def _tagger_load_cfg() -> dict:
    """读取运行时配置：优先 JSON 文件，回退到 tag_config 默认值。"""
    if _RUNTIME_CFG_FILE.exists():
        with open(_RUNTIME_CFG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {
        "positiveDimensions": POSITIVE_DIMENSIONS,
        "negativeDimensions": NEGATIVE_DIMENSIONS,
        "severityDeduction":  dict(SEVERITY_DEDUCTION),
    }

# ── 正向标签提取 ──────────────────────────────────────────────────

_POSITIVE_TAG_PROMPT_TPL = """你是专业的 HR 招聘评估助手。根据候选人简历，提取结构化正向标签。

## 输入
### 候选人简历（已结构化）
{resume_json}

### 目标岗位参考
- 职位：{job_title}
- 必要技能：{required_skills}
- 岗位描述：{job_description}

## 输出要求
输出以下 JSON，禁止添加说明文字或 markdown 标记。
所有维度均使用数组字段，命名规则：{{dimension}}ValueArray。

{{{{
  "positiveTags": [
    {{{{
      "dimension": "education",
      "educationValueArray": ["<{edu_values}>", "...更多适用等级"],
      "source": "ai"
    }}}},
    {{{{
      "dimension": "companyBg",
      "companyBgValueArray": ["<{company_bg_values}>", "..."],
      "source": "ai",
      "confidence": <0.0-1.0>
    }}}},
    {{{{
      "dimension": "workYears",
      "workYearsValueArray": ["<{work_years_values}>"],
      "source": "ai"
    }}}},
    {{{{
      "dimension": "skills",
      "skillsValueArray": ["技能1", "技能2", "..."],
      "source": "ai"
    }}}},
    {{{{
      "dimension": "industry",
      "industryValueArray": ["行业1", "行业2"],
      "source": "ai",
      "confidence": <0.0-1.0>
    }}}}
  ],
  "confidence": <所有positiveTags中confidence字段的均值，保留2位小数>
}}}}

## 各维度打标规则

**education**（educationValueArray 为字符串数组，无 confidence，并行输出所有适用等级）
- 候选值：{edu_values}
- 层级关系：C9 ⊂ 985 ⊂ 211 ⊂ 双一流，命中高级别需同时输出下级标签
  - 若学校属于 C9，输出 ["C9","985","211","双一流"]
  - 若学校属于 985（非 C9），输出 ["985","211","双一流"]
  - 若学校属于 211（非 985），输出 ["211","双一流"]
- 海外：QS前50 → ["海外名校(QS50)","海外名校(QS100)"]；QS51-100 → ["海外名校(QS100)"]
- 未命中任何院校层级时，按最高学历补充：博士研究生→"博士"，硕士研究生→"硕士"，本科→"本科"，专科及以下→"专科及以下"
- 遍历所有 educations[].school，合并去重后输出
- 禁止输出候选值以外的值（如"博士研究生"、"硕士研究生"应映射为"博士"/"硕士"）

**companyBg**（companyBgValueArray 为字符串数组，携带整体 confidence，单条记录）
- 候选值：{company_bg_values}
- 头部大厂：阿里/腾讯/字节/百度/华为/美团/京东/拼多多/网易/快手/小红书
- confidence 取所有匹配项的最高值

**workYears**（workYearsValueArray 为单元素字符串数组，无 confidence，从 works[] 计算累计年限后映射）
- 候选值：{work_years_values}
- 0-1年→应届 / 1-3年→2-3年 / 3-5年→3-5年 / 5-10年→5-10年 / 10-15年→10-15年 / 15年+→15年+

**skills**（skillsValueArray 为字符串数组，无 confidence）
- 合并 works[].description + projects[].description 提取核心技术栈
- 去重、归一化（如"python"→"Python"），按职能大类（前端/后端/数据/AI/移动端/运维/产品/管理）归集

**industry**（industryValueArray 为字符串数组，携带整体 confidence，单条记录）
- 从公司业务和项目描述推断，最多输出2个行业
- 候选值：{industry_values}
- confidence 取所有命中行业的最高值"""


def _build_positive_prompt(pos_dims: list, resume_json: str,
                           job_title: str, required_skills: str,
                           job_description: str) -> str:
    """根据运行时正向维度配置生成完整 LLM prompt，枚举值实时从配置读取。"""
    def _pipe(dim: str) -> str:
        for d in pos_dims:
            if d["dimension"] == dim:
                return "|".join(v for v in (d.get("allowedValues") or []) if v)
        return ""

    return _POSITIVE_TAG_PROMPT_TPL.format(
        edu_values=_pipe("education"),
        company_bg_values=_pipe("companyBg"),
        work_years_values=_pipe("workYears"),
        industry_values=_pipe("industry"),
        resume_json=resume_json,
        job_title=job_title,
        required_skills=required_skills,
        job_description=job_description,
    )


def _parse_date(date_str: str) -> Optional[datetime]:
    """解析日期字符串，支持 2019-07、2019/07、至今、present 等格式"""
    if not date_str:
        return None
    s = date_str.strip().lower()
    if s in ("至今", "present", "now", "今", "current", ""):
        return datetime.now()
    for fmt in ("%Y-%m", "%Y/%m", "%Y.%m", "%Y年%m月", "%Y"):
        try:
            return datetime.strptime(s.replace("年", "-").replace("月", ""), fmt.replace("年", "-").replace("月", ""))
        except ValueError:
            pass
    m = re.match(r"(\d{4})", s)
    if m:
        return datetime(int(m.group(1)), 1, 1)
    return None


def _calc_work_years(works: list) -> float:
    """计算总工作年限（月数之和 / 12）"""
    total_months = 0
    for w in works:
        start = _parse_date(w.get("startDate", ""))
        end = _parse_date(w.get("endDate", ""))
        if start and end and end >= start:
            rd = relativedelta(end, start)
            total_months += rd.years * 12 + rd.months
    return total_months / 12


def _map_work_years(years: float) -> str:
    if years < 1:
        return "应届"
    elif years < 3:
        return "2-3年"
    elif years < 5:
        return "3-5年"
    elif years < 10:
        return "5-10年"
    elif years < 15:
        return "10-15年"
    else:
        return "15年+"


def _build_allowed(dim: str, pos_dims=None) -> set:
    dims = pos_dims if pos_dims is not None else POSITIVE_DIMENSIONS
    return {v for d in dims if d["dimension"] == dim for v in d["allowedValues"]}


_ALLOWED_EDU        = _build_allowed("education")
_ALLOWED_WORKYEARS  = _build_allowed("workYears")
_ALLOWED_COMPANY_BG = _build_allowed("companyBg")
_ALLOWED_INDUSTRY   = _build_allowed("industry")

_COMPANY_BG_ALIASES: dict[str, str] = {
    "国企央企":  "国企/央企",
    "国有企业":  "国企/央企",
}

_DIMENSION_ORDER = [d["dimension"] for d in POSITIVE_DIMENSIONS]


def _normalize_work_years_str(v: str) -> str:
    """去除年/+前多余空格，如 '10-15 年' → '10-15年'，'15 年+' → '15年+'。"""
    return re.sub(r"\s+(?=[年+])", "", v.strip())


def _expand_edu_values(raw_values: list[str]) -> list[str]:
    """将单一学历标签展开为层级标签列表（去重保序）。"""
    expanded: list[str] = []
    seen: set[str] = set()
    for v in raw_values:
        for ev in EDUCATION_HIERARCHY.get(v, [v]):
            if ev not in seen:
                seen.add(ev)
                expanded.append(ev)
    return expanded


def extract_positive_tags_rule(resume: dict) -> dict:
    """
    规则层正向标签提取（education / workYears / companyBg / industry）。
    LLM 提取失败时作为降级兜底。
    所有维度均使用 {dimension}ValueArray 数组字段。
    """
    tags = []

    # education → educationValueArray（并行输出所有适用层级）
    schools = [e.get("school", "") for e in resume.get("educations", [])]
    degrees = [e.get("education", "") for e in resume.get("educations", [])]
    edu_tags = classify_education(schools, degrees)
    if edu_tags:
        tags.append({"dimension": "education", "educationValueArray": edu_tags, "source": "ai"})

    # workYears → workYearsValueArray（单元素数组）
    works = resume.get("works", [])
    years = _calc_work_years(works)
    tags.append({"dimension": "workYears", "workYearsValueArray": [_map_work_years(years)], "source": "ai"})

    # companyBg → companyBgValueArray
    companies = [w.get("company", "") for w in works]
    company_results = classify_companies(companies)
    if company_results:
        tags.append({
            "dimension": "companyBg",
            "companyBgValueArray": [ct["value"] for ct in company_results],
            "source": "ai",
            "confidence": round(max(ct["confidence"] for ct in company_results), 2),
        })

    # industry → industryValueArray
    all_text = " ".join([
        w.get("description", "") for w in works
    ] + [
        p.get("description", "") for p in resume.get("projects", [])
    ])
    industry_results = classify_industry(all_text)
    if industry_results:
        tags.append({
            "dimension": "industry",
            "industryValueArray": [ind["value"] for ind in industry_results],
            "source": "ai",
            "confidence": round(max(ind["confidence"] for ind in industry_results), 2),
        })

    confs = [t["confidence"] for t in tags if "confidence" in t]
    overall_conf = round(sum(confs) / len(confs), 2) if confs else 0.70

    return {"positiveTags": tags, "confidence": overall_conf}


def _normalize_positive_tags(tags: list, resume: dict, pos_dims=None) -> list:
    """
    将 LLM 输出归一化为标准格式：
    - 所有维度使用 {dimension}ValueArray 数组字段
    - education：展开层级标签（985→[985,211,双一流]）
    - companyBg / industry：多条合并为单条
    - rankLevel：已废弃，丢弃
    """
    if pos_dims is None:
        pos_dims = _tagger_load_cfg()["positiveDimensions"]
    allowed_workyears = _build_allowed("workYears", pos_dims)
    normalized: list[dict] = []
    company_bg_items: list[dict] = []
    industry_items: list[dict] = []

    for t in tags:
        dim = t.get("dimension", "")

        if dim == "rankLevel":
            continue  # 已废弃此维度

        elif dim == "education":
            if "educationValueArray" in t:
                raw = t["educationValueArray"]
            elif "value" in t and isinstance(t["value"], str):
                raw = [t["value"]]
            else:
                raw = []

            normalized.append({
                "dimension": "education",
                "educationValueArray": _expand_edu_values(raw),
                "source": t.get("source", "ai"),
            })

        elif dim == "workYears":
            if "workYearsValueArray" in t:
                val_arr = t["workYearsValueArray"]
            elif "value" in t:
                val_arr = [t["value"]] if isinstance(t["value"], str) else t["value"]
            else:
                val_arr = []
            val_arr = [_normalize_work_years_str(v) for v in val_arr
                       if _normalize_work_years_str(v) in allowed_workyears]
            normalized.append({
                "dimension": "workYears",
                "workYearsValueArray": val_arr,
                "source": t.get("source", "ai"),
            })

        elif dim == "companyBg":
            if "companyBgValueArray" in t or "value" in t:
                company_bg_items.append(t)

        elif dim == "industry":
            if "industryValueArray" in t or "value" in t:
                industry_items.append(t)

        elif dim == "skills":
            if "value" in t and isinstance(t["value"], list):
                val = t["value"]
                new_t = {k: v for k, v in t.items() if k != "value"}
                new_t["skillsValueArray"] = val
                t = new_t
            normalized.append(t)

        else:
            normalized.append(t)

    # 合并 companyBg → 单条
    if company_bg_items:
        all_vals: list[str] = []
        all_confs: list[float] = []
        for item in company_bg_items:
            if "companyBgValueArray" in item:
                all_vals.extend(item["companyBgValueArray"])
            elif "value" in item:
                all_vals.append(item["value"])
            if "confidence" in item:
                all_confs.append(item["confidence"])
        seen: set = set()
        merged_vals = [v for v in all_vals if not (v in seen or seen.add(v))]
        merged: dict = {"dimension": "companyBg", "companyBgValueArray": merged_vals, "source": "ai"}
        if all_confs:
            merged["confidence"] = round(max(all_confs), 2)
        normalized.append(merged)

    # 合并 industry → 单条
    if industry_items:
        all_vals = []
        all_confs = []
        for item in industry_items:
            if "industryValueArray" in item:
                all_vals.extend(item["industryValueArray"])
            elif "value" in item:
                all_vals.append(item["value"])
            if "confidence" in item:
                all_confs.append(item["confidence"])
        seen = set()
        merged_vals = [v for v in all_vals if not (v in seen or seen.add(v))]
        merged = {"dimension": "industry", "industryValueArray": merged_vals, "source": "ai"}
        if all_confs:
            merged["confidence"] = round(max(all_confs), 2)
        normalized.append(merged)

    # 补充缺失的 education
    if not any(t.get("dimension") == "education" for t in normalized):
        schools = [e.get("school", "") for e in resume.get("educations", [])]
        degrees = [e.get("education", "") for e in resume.get("educations", [])]
        edu_tags = classify_education(schools, degrees)
        if edu_tags:
            normalized.append({"dimension": "education", "educationValueArray": edu_tags, "source": "ai"})

    # 补充缺失的 workYears
    if not any(t.get("dimension") == "workYears" for t in normalized):
        years = _calc_work_years(resume.get("works", []))
        normalized.insert(0, {
            "dimension": "workYears",
            "workYearsValueArray": [_map_work_years(years)],
            "source": "ai",
        })

    return normalized


def extract_positive_tags_llm(resume: dict, job_info: dict, llm_call_fn) -> dict:
    """
    LLM 层正向标签提取，失败时降级到规则层。
    llm_call_fn: 接受 messages list，返回字符串
    """
    pos_dims = _tagger_load_cfg()["positiveDimensions"]
    prompt = _build_positive_prompt(
        pos_dims,
        resume_json=json.dumps(resume, ensure_ascii=False, indent=2),
        job_title=job_info.get("jobTitle", ""),
        required_skills=job_info.get("requiredSkills", ""),
        job_description=job_info.get("jobDescription", ""),
    )
    try:
        raw = llm_call_fn([
            {"role": "system", "content": "你是专业的 HR 招聘评估助手，只输出 JSON。"},
            {"role": "user",   "content": prompt},
        ])
        result = json_repair.loads(raw)
        if isinstance(result, dict) and "positiveTags" in result:
            result["positiveTags"] = _normalize_positive_tags(
                result["positiveTags"], resume,
                _tagger_load_cfg()["positiveDimensions"],
            )
            confs = [t["confidence"] for t in result["positiveTags"] if "confidence" in t]
            result["confidence"] = round(sum(confs) / len(confs), 2) if confs else 0.70
            return result
    except Exception:
        logger.warning("LLM 正向打标失败，降级到规则层")

    return extract_positive_tags_rule(resume)


# ── 敏感词/风险检测 ───────────────────────────────────────────────

def _neg_cfg_map(neg_dims=None) -> dict[str, dict]:
    """返回 {displayName: config} 查找表，优先使用运行时配置。"""
    dims = neg_dims if neg_dims is not None else _tagger_load_cfg()["negativeDimensions"]
    return {d["displayName"]: d for d in dims}


def detect_sensitive_tags(resume: dict, job_info: dict) -> dict:
    """
    规则层敏感词/风险检测（4个维度，参数从运行时配置读取）。
    返回: {"hitTags": [...], "hitCount": N, "deductionScore": -N}
    """
    _runtime = _tagger_load_cfg()
    hit_tags: list[dict] = []
    works = resume.get("works", [])
    candidate = resume.get("candidate", {})
    cfg = _neg_cfg_map(_runtime["negativeDimensions"])

    # 1. 职业稳定性风险
    stab = cfg.get("职业稳定性风险", {})
    if stab.get("enabled", True):
        _detect_stability(works, hit_tags, stab.get("rules", {}))

    # 2. 诚信与合规风险
    comp = cfg.get("诚信与合规风险", {})
    if comp.get("enabled", True):
        _detect_timeline_overlap(works, hit_tags, comp.get("rules", {}))

    # 3. 地域相关（自定义敏感词）
    loc = cfg.get("地域相关", {})
    if loc.get("enabled", True):
        sw_cfg = loc.get("rules", {}).get("sensitive_words", {})
        if sw_cfg.get("enabled", True):
            sensitive_words = _parse_sensitive_words(job_info.get("jobSensitiveWords", ""))
            if sensitive_words:
                _detect_custom_words(resume, candidate, sensitive_words, hit_tags,
                                     sw_cfg.get("severity", "high"))

    # 4. 匹配度硬伤
    match = cfg.get("匹配度硬伤", {})
    if match.get("enabled", True):
        _detect_education_risk(resume.get("educations", []), hit_tags,
                               match.get("rules", {}))

    deduction = sum(_runtime["severityDeduction"].get(t["severity"], -5) for t in hit_tags)

    grouped: dict[str, list] = {}
    for t in hit_tags:
        cat = t["category"]
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append({
            "tag": t["tag"],
            "severity": t["severity"],
            "matchedContent": t["matchedContent"],
        })

    return {
        "hitTags": [{"category": cat, "hits": hits} for cat, hits in grouped.items()],
        "hitCount": len(hit_tags),
        "deductionScore": deduction,
    }


def _detect_stability(works: list, hit_tags: list, rules: dict):
    """频繁跳槽 / 最近一段偏短 / 空窗期"""
    if len(works) < 2:
        return

    parsed = []
    for w in works:
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
                "category": "职业稳定性风险",
                "tag": "频繁跳槽",
                "severity": severity,
                "matchedContent": f"近{window_years}年内{len(recent_items)}段工作经历：{companies}",
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
                "category": "职业稳定性风险",
                "tag": "最近任职时长偏短",
                "severity": severity,
                "matchedContent": f"{last_work.get('company', '')} {last_work.get('startDate', '')}~{last_work.get('endDate', '')}，仅{last_months}个月",
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
                rd = relativedelta(curr_start, prev_end)
                gap_months = rd.years * 12 + rd.months
                if gap_months >= threshold_months:
                    hit_tags.append({
                        "category": "职业稳定性风险",
                        "tag": "工作空窗期过长",
                        "severity": severity,
                        "matchedContent": f"{parsed[i-1][2].get('company','')} 离职后至 {parsed[i][2].get('company','')} 入职，空窗约{gap_months}个月",
                    })


def _detect_timeline_overlap(works: list, hit_tags: list, rules: dict):
    """检测工作时间线重叠"""
    ol = rules.get("timeline_overlap", {})
    if not ol.get("enabled", True):
        return
    threshold_months = ol.get("threshold_months", 2)
    severity = ol.get("severity", "high")

    parsed = []
    for w in works:
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
                    "category": "诚信与合规风险",
                    "tag": "工作时间线重叠",
                    "severity": severity,
                    "matchedContent": f"{prev_w.get('company','')}({prev_w.get('startDate','')}~{prev_w.get('endDate','')}) 与 {curr_w.get('company','')}({curr_w.get('startDate','')}~{curr_w.get('endDate','')}) 时间重叠{overlap_months}个月",
                })


def _parse_sensitive_words(raw: str) -> list[str]:
    if not raw:
        return []
    return [w.strip() for w in re.split(r"[,，;；\s]+", raw) if w.strip()]


def _detect_custom_words(resume: dict, candidate: dict, words: list[str],
                         hit_tags: list, severity: str = "high"):
    """自定义敏感词全文匹配（地域相关）"""
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

    for word in words:
        if word and word in full_text:
            idx = full_text.find(word)
            snippet = full_text[max(0, idx - 10): idx + len(word) + 20].strip()
            hit_tags.append({
                "category": "地域相关",
                "tag": word,
                "severity": severity,
                "matchedContent": f"...{snippet}...",
            })


def _detect_education_risk(educations: list, hit_tags: list, rules: dict):
    """检测培训机构背景（匹配度硬伤）"""
    ti = rules.get("training_institution", {})
    if not ti.get("enabled", True):
        return
    keywords = ti.get("keywords", ["培训", "学院（培训）", "训练营", "bootcamp", "在线课程", "慕课"])
    severity = ti.get("severity", "medium")

    for edu in educations:
        school = edu.get("school", "")
        if any(kw in school.lower() for kw in keywords):
            hit_tags.append({
                "category": "匹配度硬伤",
                "tag": "疑似培训机构背景",
                "severity": severity,
                "matchedContent": f"教育经历中包含：{school}",
            })


# ── 统一打标入口 ──────────────────────────────────────────────────

def run_tagging(resume: dict, job_info: dict, llm_call_fn=None) -> dict:
    """
    两阶段打标：
    1. 正向标签提取（LLM 优先，降级规则）
    2. 敏感词/风险检测（规则层）
    返回完整 tagInfo 字段。
    """
    if llm_call_fn:
        positive_result = extract_positive_tags_llm(resume, job_info, llm_call_fn)
    else:
        positive_result = extract_positive_tags_rule(resume)

    sensitive_result = detect_sensitive_tags(resume, job_info)

    return {
        "positiveTags":   positive_result["positiveTags"],
        "confidence":     positive_result["confidence"],
        "hitTags":        sensitive_result["hitTags"],
        "hitCount":       sensitive_result["hitCount"],
        "deductionScore": sensitive_result["deductionScore"],
        "tagParseTime":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
