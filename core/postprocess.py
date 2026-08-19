"""
解析结果后处理、Schema 校验与多批次合并。

职责：
  _post_process    : 字段类型规范化（gender、age、workYears 转 int，移除 other）
  _validate_schema : Schema 校验与补全（缺失字段补默认值、多余字段裁剪）
  _merge_results   : 多批次合并去重（candidate 字段合并、列表去重）
  _conform_to_template: 拼装最终 API 响应结构（含正负向标签）

调用顺序（parser.py 中）：
  分批路径：_merge_results → _post_process → _validate_schema
  单批路径：_post_process → _validate_schema
"""
from core.prompting.resume_parsing import (
    CANDIDATE_KEYS,
    EDU_KEYS,
    PROJ_KEYS,
    SCHEMA_KEYS,
    WORK_KEYS,
)
from core.resume_contract import normalize_resume_payload
from tag.tag_data import classify_education
from tag import load_tag_cfg
from tag.tag_config import MAX_SKILL_TAGS, sort_positive_tag_entries
from tag.tag_value_id_mapper import get_tag_value_id_mapper

# education / workYears 由规则层确定性计算，输出置信度会误导调用方（规则结果无概率含义）
_NO_CONF_DIM = {"education", "workYears"}
_VALID_TAG_SOURCES = {"rule", "llm", "hybrid"}


def _normalize_tag_source(value: object, default: str = "rule") -> str:
    """把历史来源值收敛到公开接口允许的三个来源枚举。"""
    source = str(value or "").strip().lower()
    if source == "ai":
        return "llm"
    return source if source in _VALID_TAG_SOURCES else default


def _build_display_maps(cfg: dict) -> tuple[dict, dict]:
    """从运行时配置构建已启用维度的展示名查找表。

    Args:
        cfg (dict): 包含 `positiveDimensions` 和 `negativeDimensions` 的标签配置。

    Returns:
        tuple[dict, dict]: 正向 `dimension -> displayName` 映射，以及负向
            `dimension/displayName -> (dimension, displayName)` 兼容映射。

    Note:
        正向映射同时是最终输出白名单，未启用或未配置维度会被过滤。
    """
    pos = {d["dimension"]: d["displayName"]
           for d in cfg.get("positiveDimensions", []) if d.get("enabled", True)}
    neg = {}
    for dimension in cfg.get("negativeDimensions", []):
        if not dimension.get("enabled", True):
            continue
        value = (dimension["dimension"], dimension["displayName"])
        neg[dimension["dimension"]] = value
        neg[dimension["displayName"]] = value
    return pos, neg


# 字段类型规范化

def _post_process(data: dict) -> dict:
    """将候选人性别、年龄和工作年限归一为最终 Schema 使用的数值。

    Args:
        data (dict): LLM 返回的简历对象。

    Returns:
        dict: 原地更新后的简历对象；顶层非字典时返回空字典。

    Note:
        该函数会修改传入字典，并移除 `candidate.other`。
    """
    if not isinstance(data, dict):
        return {}
    c = data.get("candidate", {})
    if c:
        g = c.get("gender", "")
        if g == "男":       c["gender"] = 1
        elif g == "女":     c["gender"] = 2
        else:               c["gender"] = int(g) if str(g).isdigit() else 0
        age = str(c.get("age", "0")).strip()
        c["age"] = int(age) if age.isdigit() else 0
        work_years = str(c.get("workYears", "0")).strip()
        c["workYears"] = int(work_years) if work_years.isdigit() else 0
        c.pop("other", None)
    return data


# Schema 校验

def _validate_schema(data: dict) -> tuple:
    """补齐简历顶层和子项字段，并记录自动修复摘要。

    Args:
        data (dict): 待校验和归一的简历对象。

    Returns:
        tuple[dict, dict]: 归一后简历以及包含 `valid` 和 `fixes` 的报告。
            顶层非字典时简历结果为空字典。

    Note:
        该函数会原地修改传入字典；自动修复过的结果会标记为非完全有效。
    """
    report = {"valid": True, "fixes": []}

    if not isinstance(data, dict):
        report["valid"] = False
        report["fixes"].append(f"顶层非 dict: {type(data).__name__}")
        return {}, report

    for key in SCHEMA_KEYS:
        if key not in data:
            data[key] = [] if key != "candidate" else {}
            report["fixes"].append(f"补充缺失顶层字段: {key}")

    c = data.get("candidate", {})
    if not isinstance(c, dict):
        data["candidate"] = {}
        report["fixes"].append("candidate 非 dict，已重置")
        c = {}
    for key in list(c.keys()):
        if key not in CANDIDATE_KEYS:
            c.pop(key)
            report["fixes"].append(f"移除 candidate 多余字段: {key}")
    for key in CANDIDATE_KEYS:
        if key not in c:
            c[key] = 0 if key in {"gender", "age", "workYears"} else ""
            report["fixes"].append(f"candidate 缺失字段: {key}")

    if not isinstance(data.get("educations"), list):
        data["educations"] = []
        report["fixes"].append("educations 非 list，已重置")
    for i, edu in enumerate(data["educations"]):
        if not isinstance(edu, dict):
            data["educations"][i] = {}
            continue
        for key in EDU_KEYS:
            if key not in edu:
                edu[key] = ""

    if not isinstance(data.get("works"), list):
        data["works"] = []
        report["fixes"].append("works 非 list，已重置")
    for i, work in enumerate(data["works"]):
        if not isinstance(work, dict):
            data["works"][i] = {}
            continue
        for key in WORK_KEYS:
            if key not in work:
                work[key] = ""

    if not isinstance(data.get("projects"), list):
        data["projects"] = []
        report["fixes"].append("projects 非 list，已重置")
    for i, proj in enumerate(data["projects"]):
        if not isinstance(proj, dict):
            data["projects"][i] = {}
            continue
        for key in PROJ_KEYS:
            if key not in proj:
                proj[key] = ""

    if report["fixes"]:
        report["valid"] = False
    return data, report


# 多批次合并

def _merge_str(a: str, b: str) -> str:
    """合并两个分批字段，空值优先让位且冲突时保留较长值。

    Args:
        a (str): 已合并批次的字段值。
        b (str): 新批次的字段值。

    Returns:
        str: 去除首尾空白后选定的字段值。
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a: return b
    if not b: return a
    # 取较长值：跨批次合并时，字段聚焦的那页通常描述更完整，长度更长
    return a if len(a) >= len(b) else b


# 身份字段以首页提取为准：封面页格式最规范，后续页重复出现时往往不完整或截断
_FIRST_WINS = {"name", "phone", "email"}


def _merge_candidate(a: dict, b: dict) -> dict:
    """按身份字段优先级合并两个候选人对象。

    Args:
        a (dict): 已合并的候选人字段。
        b (dict): 新批次提取的候选人字段。

    Returns:
        dict: 姓名、电话和邮箱优先首次非空值，其他文本按长度合并的结果。
    """
    result = {}
    for k in set(list(a.keys()) + list(b.keys())):
        va, vb = a.get(k, ""), b.get(k, "")
        if k in ("gender", "age", "workYears"):
            result[k] = va if (va and va != 0) else vb
        elif k in _FIRST_WINS:
            result[k] = str(va).strip() if str(va).strip() else str(vb).strip()
        else:
            result[k] = _merge_str(str(va), str(vb))
    return result


def _dedup(items: list, keys: list) -> list:
    """按组合业务键对经历列表去重并过滤全空项。

    Args:
        items (list): 待去重的教育、工作或项目对象列表。
        keys (list): 构成去重键的字段名列表。

    Returns:
        list: 保持首次出现顺序的唯一非空对象列表。
    """
    seen, out = set(), []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = tuple(str(item.get(f, "")).strip() for f in keys)
        # any(k for k in key)：过滤全空条目（所有关键字段均为空字符串）
        if key not in seen and any(k for k in key):
            seen.add(key)
            out.append(item)
    return out


def _merge_results(batch_results: list) -> dict:
    """合并长 PDF 各页面批次的局部简历结果。

    Args:
        batch_results (list): 按页面顺序排列的局部简历对象。

    Returns:
        dict: 已合并候选人字段并对三类经历去重的简历对象。
    """
    merged = {"candidate": {}, "educations": [], "works": [], "projects": []}
    for r in batch_results:
        if not isinstance(r, dict):
            continue
        if r.get("candidate"):
            merged["candidate"] = _merge_candidate(merged["candidate"], r["candidate"])
        for k in ("educations", "works", "projects"):
            items = r.get(k, [])
            if isinstance(items, list):
                merged[k].extend(items)
    merged["educations"] = _dedup(merged["educations"], ["school", "major", "startDate"])
    merged["works"]      = _dedup(merged["works"],      ["company", "position", "startDate"])
    merged["projects"]   = _dedup(merged["projects"],   ["projectName", "startDate"])
    return merged


# 最终输出序列化

def _conform_to_template(task_id: str, resume: dict, tag_info: dict, resume_parse_time: str) -> dict:
    """将解析简历和打标中间结果转为最终 API 模板。

    Args:
        task_id (str): 当前任务 UUID。
        resume (dict): 已归一的结构化简历。
        tag_info (dict): 打标器返回的正向标签、风险命中和扣分。
        resume_parse_time (str): 简历解析完成时间字符串。

    Returns:
        dict: 包含任务时间、简历主体、正向标签和负向标签的 API 结果。

    Note:
        输出维度使用当前运行时配置白名单，未启用维度不会进入结果。
    """
    # 最终边界再次归一，保证外部直接调用和解析主链使用同一 Resume 契约。
    resume = normalize_resume_payload(resume)
    # 每次调用时读取最新配置（mtime 缓存，文件未变则无额外 IO）
    runtime_cfg = load_tag_cfg()
    mapper = get_tag_value_id_mapper()
    _positive_display, _negative_dim = _build_display_maps(runtime_cfg)
    allowed_by_dimension = {
        dimension["dimension"]: set(dimension.get("allowedValues", []))
        for dimension in runtime_cfg.get("positiveDimensions", [])
        if dimension.get("enabled", True)
    }
    c = resume.get("candidate") or {}
    candidate = {
        "name":             c.get("name", ""),
        "phone":            c.get("phone", ""),
        "email":            c.get("email", ""),
        "gender":           c.get("gender", 0),
        "age":              c.get("age", 0),
        "workYears":        c.get("workYears", 0),
        "address":          c.get("address", ""),
        "nativePlace":      c.get("nativePlace", ""),
        "selfEvaluation":   c.get("selfEvaluation", ""),
        "expectedSalary":   c.get("expectedSalary", ""),
        "expectedPosition": c.get("expectedPosition", ""),
    }

    def _edu(e):
        """裁剪单条教育经历为输出模板允许的字段。

        Args:
            e: 已归一化的教育经历字典。

        Returns:
            仅保留模板字段的教育经历字典。
        """
        return {k: e.get(k, "") for k in ("school", "major", "degree", "education", "startDate", "endDate")}

    def _work(w):
        """裁剪单条工作经历为输出模板允许的字段。

        Args:
            w: 已归一化的工作经历字典。

        Returns:
            仅保留模板字段的工作经历字典。
        """
        return {
            k: w.get(k, "")
            for k in ("company", "position", "description", "startDate", "endDate")
        }

    def _proj(p):
        """裁剪单条项目经历为输出模板允许的字段。

        Args:
            p: 已归一化的项目经历字典。

        Returns:
            仅保留模板字段的项目经历字典。
        """
        return {k: p.get(k, "") for k in ("company", "projectName", "role", "description", "startDate", "endDate")}

    overall_conf = round(tag_info.get("confidence", 0), 2)
    tags_array   = []
    edu_entry    = None

    for tag in tag_info.get("positiveTags", []):
        dim = tag.get("dimension", "")
        # 维度已从配置删除或被禁用时跳过，防止 LLM/规则层输出的残留标签污染响应
        if dim not in _positive_display:
            continue
        arr_key = f"{dim}ValueArray"
        values = [
            value for value in tag.get(arr_key, [])
            if value in allowed_by_dimension.get(dim, set())
        ]
        if dim == "skills":
            values = values[:MAX_SKILL_TAGS]
        src     = _normalize_tag_source(tag.get("source"), default="rule")
        entry: dict = {
            "dimensionId": mapper.resolve_dimension(dim)["id"],
            "dimension":   dim,
            "displayName": _positive_display[dim],
        }
        if dim not in _NO_CONF_DIM:
            entry["confidence"] = tag["confidence"] if tag.get("confidence") is not None else overall_conf
        entry["value"] = [
            {
                "tagId": mapper.resolve(dim, value)["id"],
                "tag": value,
                "source": src,
            }
            for value in values
        ]
        # 跳过 value 为空的非固定维度（LLM 输出空数组表示无匹配，无需透出）
        if not entry["value"] and dim not in ("education", "workYears"):
            continue
        tags_array.append(entry)
        if dim == "education":
            edu_entry = entry

    # 最终边界再次用确定性规则覆盖教育背景，防止外部中间结果绕过归一层。
    schools = [e.get("school", "") for e in resume.get("educations", [])]
    degrees = [e.get("education", "") for e in resume.get("educations", [])]
    rule_edu = [
        value for value in classify_education(schools, degrees)
        if value in allowed_by_dimension.get("education", set())
    ]
    if "education" in _positive_display and rule_edu:
        if edu_entry is None:
            edu_entry = {
                "dimensionId": mapper.resolve_dimension("education")["id"],
                "dimension": "education",
                "displayName": _positive_display["education"],
                "value": [],
            }
            tags_array.append(edu_entry)
        edu_entry["value"] = [
            {
                "tagId": mapper.resolve("education", value)["id"],
                "tag": value,
                "source": "rule",
            }
            for value in rule_edu
        ]
    elif edu_entry is not None:
        edu_entry["value"] = []

    # 最终响应再次排序，兼容外部直接传入未归一 `tag_info` 的调用路径。
    tags_array = sort_positive_tag_entries(tags_array)

    neg_array = []
    for group in tag_info.get("hitTags", []):
        category = group.get("category", "")
        lookup_key = group.get("dimension") or category
        dim_code, display = _negative_dim.get(lookup_key, (category, category))
        dimension_mapping = mapper.resolve_dimension(dim_code)
        if dimension_mapping is None:
            continue
        mapped_hits = []
        for hit in group.get("hits", []):
            value_mapping = (
                mapper.resolve_rule(dim_code, hit.get("ruleKey", ""))
                or mapper.resolve(dim_code, hit.get("tag", ""))
            )
            if value_mapping is None:
                continue
            mapped_hits.append({
                "tagId": value_mapping["id"],
                "tag": hit.get("tag", ""),
                "severity": hit.get("severity", ""),
                "matchedContent": hit.get("matchedContent", ""),
                "source": _normalize_tag_source(
                    hit.get("source"), default="rule"
                ),
            })
        if not mapped_hits:
            continue
        neg_array.append({
            "dimensionId": dimension_mapping["id"],
            "dimension":   dim_code,
            "displayName": display,
            "value": mapped_hits,
        })

    visible_negative_hits = sum(len(group["value"]) for group in neg_array)
    visible_deduction = sum(
        runtime_cfg.get("severityDeduction", {}).get(hit["severity"], -5)
        for group in neg_array for hit in group["value"]
    )

    return {
        "taskId":                task_id,
        "mappingVersion":        mapper.version,
        "resumeParseFinishTime": resume_parse_time,
        "tagParseFinishTime":    tag_info.get("tagParseTime", ""),
        "candidate":   candidate,
        "educations":  [_edu(e) for e in resume.get("educations", [])],
        "works":       [_work(w) for w in resume.get("works", [])],
        "projects":    [_proj(p) for p in resume.get("projects", [])],
        "positiveTags": {
            "tagsArray": tags_array,
        },
        "negativeTags": {
            "hitCount":       visible_negative_hits,
            "deductionScore": visible_deduction,
            "tagsArray":      neg_array,
        },
    }
