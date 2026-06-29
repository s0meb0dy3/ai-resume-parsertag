"""
简历打标模块：正向标签提取 + 敏感词/风险检测
"""
import copy
import re
import json
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
from core.prompts import (
    POSITIVE_PROMPT_HEADER, DIM_PROMPT, GENERIC_DIM_PROMPT, POSITIVE_TAG_SCHEMA,
    NEGATIVE_PROMPT_HEADER, GENERIC_NEG_DIM_PROMPT, NEGATIVE_TAG_SCHEMA,
    TAGGER_SYSTEM,
)
from config import logger  # 统一使用带 ContextVar 上下文的 logger
from logger.langfuse_client import observe

_RUNTIME_CFG_FILE = pathlib.Path(__file__).parent.parent / "schemas" / "tag_config_runtime.json"

# mtime 缓存：只在文件真正修改后才重新读取，stat() 极轻量，避免每次打标都做 JSON 反序列化
_cfg_cache: dict = {}
_cfg_cache_mtime: float = -1.0


def _tagger_load_cfg() -> dict:
    """读取运行时配置（mtime 缓存：文件未修改时直接返回上次结果，避免并发时重复 IO）。
    返回浅拷贝：防止调用方对顶层键赋值时意外污染缓存（嵌套列表仍为只读约定）。
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

# ── 正向标签提取 ──────────────────────────────────────────────────


def _build_positive_prompt(pos_dims: list, resume_json: str,
                           job_title: str, required_skills: str,
                           job_description: str) -> str:
    """按运行时 pos_dims 动态拼装 LLM prompt。
    已知维度使用详细提取指令，API 新增的自定义维度自动使用通用指令模板。
    新增/删除/禁用维度后下次打标 prompt 立即反映变更，无需改代码。
    """
    sections = []
    custom_schema_entries: list[dict] = []

    for d in pos_dims:
        if not d.get("enabled", True):
            continue
        dim    = d["dimension"]
        values = "|".join(v for v in (d.get("allowedValues") or []) if v)

        if dim in DIM_PROMPT:
            tpl = DIM_PROMPT[dim]
            sections.append(tpl.format(values=values) if "{values}" in tpl else tpl)
        else:
            sections.append(GENERIC_DIM_PROMPT.format(
                dimension=dim,
                display_name=d.get("displayName", dim),
                values=values or "（allowedValues 为空，自由提取相关标签）",
            ))
            # 向 schema 示例追加自定义维度占位条目，确保 LLM 知道输出格式
            custom_schema_entries.append({
                "dimension": dim,
                f"{dim}ValueArray": ["示例标签A"],
                "source": "ai",
                "confidence": 0.85,
            })

    # 若存在自定义维度，动态扩充 schema 示例（深拷贝避免污染原始常量）
    if custom_schema_entries:
        schema = copy.deepcopy(POSITIVE_TAG_SCHEMA)
        schema["positiveTags"].extend(custom_schema_entries)
    else:
        schema = POSITIVE_TAG_SCHEMA

    return POSITIVE_PROMPT_HEADER.format(
        resume_json=resume_json,
        job_title=job_title,
        required_skills=required_skills,
        job_description=job_description,
        schema_example=json.dumps(schema, ensure_ascii=False, indent=2),
    ) + "\n\n".join(sections)


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
            # 自定义维度：将 LLM 可能输出的 "value" 字段规范化为 {dim}ValueArray
            arr_key = f"{dim}ValueArray"
            if arr_key not in t and "value" in t:
                raw = t.get("value", [])
                new_t = {k: v for k, v in t.items() if k != "value"}
                new_t[arr_key] = raw if isinstance(raw, list) else [raw]
                t = new_t
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

    # 过滤掉配置中已删除或禁用的维度，防止 LLM 幻觉/规则兜底产生的残留标签进入响应
    enabled_dims = {d["dimension"] for d in pos_dims if d.get("enabled", True)}
    normalized = [t for t in normalized if t.get("dimension") in enabled_dims]

    return normalized


@observe()
def extract_positive_tags_llm(resume: dict, job_info: dict, llm_call_fn) -> dict:
    """
    LLM 层正向标签提取，失败时降级到规则层。
    llm_call_fn: 接受 messages list，返回字符串
    """
    # 配置只读一次，避免同一调用链多次 IO
    cfg      = _tagger_load_cfg()
    pos_dims = cfg["positiveDimensions"]
    prompt   = _build_positive_prompt(
        pos_dims,
        resume_json=json.dumps(resume, ensure_ascii=False, indent=2),
        job_title=job_info.get("jobTitle", ""),
        required_skills=job_info.get("requiredSkills", ""),
        job_description=job_info.get("jobDescription", ""),
    )
    try:
        raw = llm_call_fn([
            {"role": "system", "content": TAGGER_SYSTEM},
            {"role": "user",   "content": prompt},
        ])
        result = json_repair.loads(raw)
        if isinstance(result, dict) and "positiveTags" in result:
            result["positiveTags"] = _normalize_positive_tags(
                result["positiveTags"], resume, pos_dims  # 复用已读取的配置，不重复 IO
            )
            confs = [t["confidence"] for t in result["positiveTags"] if "confidence" in t]
            result["confidence"] = round(sum(confs) / len(confs), 2) if confs else 0.70
            return result
    except Exception:
        logger.warning("LLM 正向打标失败，降级到规则层")

    return extract_positive_tags_rule(resume)


# ── 敏感词/风险检测 ───────────────────────────────────────────────

# 4 个硬编码算法维度（专用检测函数）；其他维度走通用关键词执行器
_HARDCODED_NEG_DIMS = frozenset({"jobstable", "integrity", "location", "matchRisk"})


def _collect_all_text(resume: dict) -> str:
    """收集简历所有文本字段，用于全文关键词匹配。"""
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
    """通用关键词匹配规则执行器，适用于 API 新增的自定义负向维度。

    rule 字段：
      keywords  (list[str])  必填，命中任一关键词即触发
      searchIn  (list[str])  可选，默认 ["all"]；支持字段路径：
                             all / works.company / works.description / works.position /
                             educations.school / educations.major / projects.description /
                             candidate.address / candidate.nativePlace / candidate.selfEvaluation
      tagName   (str)        标签展示名，默认等于 ruleKey
      severity  (str)        low / medium / high，默认 medium
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
                    "category":      category,
                    "tag":           tag_name,
                    "severity":      severity,
                    "matchedContent": f"...{snippet}...",
                })
                break  # 同一规则匹配一次即可，避免重复计分


def _build_negative_prompt(neg_dims_llm: list, resume_json: str) -> str:
    """按 detectionType='llm' 的负向维度动态拼装 LLM prompt。
    每条 rule 需提供 description 字段描述语义检测标准；缺省时回退到 tagName。
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
            rules_lines.append(f"- {tag_name}（{severity}）：{description}")
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
    """LLM 层负向标签提取，仅处理 detectionType='llm' 的维度。
    返回已转换为 hitTags 格式的列表：[{"category": displayName, "hits": [...]}]
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
            hits = [h for h in group.get("hits", []) if isinstance(h, dict) and h.get("tag")]
            if not hits:
                continue
            hit_groups.append({"category": dim_to_display[dim], "hits": hits})
        return hit_groups

    except Exception:
        logger.warning("LLM 负向打标失败，跳过 llm 类型维度")
        return []


def _neg_cfg_map(neg_dims=None) -> dict[str, dict]:
    """返回 {dimension: config} 查找表，优先使用运行时配置。
    使用 dimension（稳定标识符）而非 displayName（可通过 API 修改），
    防止改名后检测逻辑静默失效。
    """
    dims = neg_dims if neg_dims is not None else _tagger_load_cfg()["negativeDimensions"]
    return {d["dimension"]: d for d in dims}


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
    stab = cfg.get("jobstable", {})
    if stab and stab.get("enabled", True):   # stab={} 表示维度已被删除，直接跳过
        _detect_stability(works, hit_tags, stab.get("rules", {}))

    # 2. 诚信与合规风险
    comp = cfg.get("integrity", {})
    if comp and comp.get("enabled", True):
        _detect_timeline_overlap(works, hit_tags, comp.get("rules", {}))

    # 3. 地域相关（jobSensitiveWords 来自请求，非配置文件关键词）
    loc = cfg.get("location", {})
    if loc and loc.get("enabled", True):
        sw_cfg = loc.get("rules", {}).get("sensitive_words", {})
        if sw_cfg.get("enabled", True):
            sensitive_words = _parse_sensitive_words(job_info.get("jobSensitiveWords", ""))
            if sensitive_words:
                _detect_custom_words(resume, candidate, sensitive_words, hit_tags,
                                     sw_cfg.get("severity", "high"))

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
    # 少于 2 段经历无法计算跳槽频率和空窗期，直接跳过
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

@observe()
def run_tagging(resume: dict, job_info: dict, llm_call_fn=None) -> dict:
    """
    三阶段打标：
    1. 正向标签提取（LLM 优先，降级规则）
    2. 负向规则检测（4 个硬编码维度 + 自定义关键词维度）
    3. 负向 LLM 检测（detectionType='llm' 的维度，有 llm_call_fn 时执行）
    返回完整 tagInfo 字段。
    """
    if llm_call_fn:
        positive_result = extract_positive_tags_llm(resume, job_info, llm_call_fn)
    else:
        positive_result = extract_positive_tags_rule(resume)

    sensitive_result = detect_sensitive_tags(resume, job_info)

    # LLM 负向检测：只对 detectionType='llm' 且 enabled=True 的维度调用
    if llm_call_fn:
        cfg          = _tagger_load_cfg()
        neg_dims_llm = [
            d for d in cfg["negativeDimensions"]
            if d.get("detectionType", "rule") == "llm" and d.get("enabled", True)
        ]
        if neg_dims_llm:
            llm_hit_groups = extract_negative_tags_llm(resume, neg_dims_llm, llm_call_fn)
            if llm_hit_groups:
                severity_map = cfg.get("severityDeduction", {"low": -5, "medium": -10, "high": -20})
                sensitive_result["hitTags"].extend(llm_hit_groups)
                # 重新累加 hitCount / deductionScore
                for group in llm_hit_groups:
                    for h in group.get("hits", []):
                        sensitive_result["hitCount"]       += 1
                        sensitive_result["deductionScore"] += severity_map.get(
                            h.get("severity", "medium"), -10
                        )

    return {
        "positiveTags":   positive_result["positiveTags"],
        "confidence":     positive_result["confidence"],
        "hitTags":        sensitive_result["hitTags"],
        "hitCount":       sensitive_result["hitCount"],
        "deductionScore": sensitive_result["deductionScore"],
        "tagParseTime":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
