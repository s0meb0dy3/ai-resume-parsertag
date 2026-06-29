"""
解析结果后处理、Schema 校验与多批次合并。

职责：
  _post_process    : 字段类型规范化（gender int、age int、移除 other）
  _validate_schema : Schema 校验与补全（缺失字段补默认值、多余字段裁剪）
  _merge_results   : 多批次合并去重（candidate 字段合并、列表去重）
  _conform_to_template: 拼装最终 API 响应结构（含正负向标签）

调用顺序（parser.py 中）：
  分批路径：_merge_results → _post_process → _validate_schema
  单批路径：_post_process → _validate_schema
"""
from core.prompts import SCHEMA_KEYS, CANDIDATE_KEYS, EDU_KEYS, WORK_KEYS, PROJ_KEYS
from tag.tag_data import classify_education
from tag import load_tag_cfg

# education / workYears 由规则层确定性计算，输出置信度会误导调用方（规则结果无概率含义）
_NO_CONF_DIM = {"education", "workYears"}


def _build_display_maps(cfg: dict) -> tuple[dict, dict]:
    """从运行时配置构建 displayName 查找表，只包含 enabled=True 的维度。
    pos 同时作为"允许输出"的白名单：不在其中的维度在 _conform_to_template 中被过滤掉。
    """
    pos = {d["dimension"]: d["displayName"]
           for d in cfg.get("positiveDimensions", []) if d.get("enabled", True)}
    neg = {d["displayName"]: (d["dimension"], d["displayName"])
           for d in cfg.get("negativeDimensions", []) if d.get("enabled", True)}
    return pos, neg


# ── 字段类型规范化 ────────────────────────────────────────────────

def _post_process(data: dict) -> dict:
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
        c.pop("other", None)
    return data


# ── Schema 校验 ───────────────────────────────────────────────────

def _validate_schema(data: dict) -> tuple:
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
            c[key] = ""
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


# ── 多批次合并 ────────────────────────────────────────────────────

def _merge_str(a: str, b: str) -> str:
    a, b = (a or "").strip(), (b or "").strip()
    if not a: return b
    if not b: return a
    # 取较长值：跨批次合并时，字段聚焦的那页通常描述更完整，长度更长
    return a if len(a) >= len(b) else b


# 身份字段以首页提取为准：封面页格式最规范，后续页重复出现时往往不完整或截断
_FIRST_WINS = {"name", "phone", "email"}


def _merge_candidate(a: dict, b: dict) -> dict:
    result = {}
    for k in set(list(a.keys()) + list(b.keys())):
        va, vb = a.get(k, ""), b.get(k, "")
        if k in ("gender", "age"):
            result[k] = va if (va and va != 0) else vb
        elif k in _FIRST_WINS:
            result[k] = str(va).strip() if str(va).strip() else str(vb).strip()
        else:
            result[k] = _merge_str(str(va), str(vb))
    return result


def _dedup(items: list, keys: list) -> list:
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


# ── 最终输出序列化 ────────────────────────────────────────────────

def _conform_to_template(task_id: str, resume: dict, tag_info: dict, resume_parse_time: str) -> dict:
    # 每次调用时读取最新配置（mtime 缓存，文件未变则无额外 IO）
    _positive_display, _negative_dim = _build_display_maps(load_tag_cfg())
    c = resume.get("candidate") or {}
    candidate = {
        "name":             c.get("name", ""),
        "phone":            c.get("phone", ""),
        "email":            c.get("email", ""),
        "gender":           c.get("gender", 0),
        "age":              c.get("age", 0),
        "address":          c.get("address", ""),
        "nativePlace":      c.get("nativePlace", ""),
        "selfEvaluation":   c.get("selfEvaluation", ""),
        "expectedSalary":   c.get("expectedSalary", ""),
        "expectedPosition": c.get("expectedPosition", ""),
    }

    def _edu(e):
        return {k: e.get(k, "") for k in ("school", "major", "degree", "education", "startDate", "endDate")}

    def _work(w):
        return {k: w.get(k, "") for k in ("company", "position", "description", "startDate", "endDate")}

    def _proj(p):
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
        values  = tag.get(arr_key, [])
        src     = tag.get("source", "ai")
        entry: dict = {
            "dimension":   dim,
            "displayName": _positive_display[dim],
        }
        if dim not in _NO_CONF_DIM:
            entry["confidence"] = tag["confidence"] if tag.get("confidence") is not None else overall_conf
        entry["value"] = [{"tag": v, "source": src} for v in values]
        # 跳过 value 为空的非固定维度（LLM 输出空数组表示无匹配，无需透出）
        if not entry["value"] and dim not in ("education", "workYears"):
            continue
        tags_array.append(entry)
        if dim == "education":
            edu_entry = entry

    if edu_entry is not None:
        schools  = [e.get("school", "") for e in resume.get("educations", [])]
        degrees  = [e.get("education", "") for e in resume.get("educations", [])]
        rule_edu = classify_education(schools, degrees)
        existing = {v["tag"] for v in edu_entry["value"]}
        for v in rule_edu:
            if v not in existing:
                edu_entry["value"].append({"tag": v, "source": "rule"})

    neg_array = []
    for group in tag_info.get("hitTags", []):
        category = group.get("category", "")
        dim_code, display = _negative_dim.get(category, (category, category))
        neg_array.append({
            "dimension":   dim_code,
            "displayName": display,
            "value": [
                {
                    "tag":            h.get("tag", ""),
                    "severity":       h.get("severity", ""),
                    "matchedContent": h.get("matchedContent", ""),
                    "source":         "ai",
                }
                for h in group.get("hits", [])
            ],
        })

    return {
        "taskId":                task_id,
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
            "hitCount":       tag_info.get("hitCount", 0),
            "deductionScore": tag_info.get("deductionScore", 0),
            "tagsArray":      neg_array,
        },
    }
