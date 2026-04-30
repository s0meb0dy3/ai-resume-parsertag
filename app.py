"""
AIHR 简历解析服务
链路：PDF → 渲染为图像 → Qwen3.5 多模态 → JSON

≤8 页：一次性调用
>8 页：分批并发 + 合并
接口与 app_ocr.py 完全兼容（model / content / response 格式）。
"""
import os
import io
import base64
import json
import time
import uuid
import asyncio
import logging
import logging.handlers
import json_repair
import requests
import fitz
from PIL import Image
from datetime import datetime
from pathlib import Path
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# ── 配置 ────────────────────────────────────────────────────────
MODEL_SERVICE_URL = os.getenv("MODEL_SERVICE_URL", "http://10.2.5.7:13001/v1")
API_KEY           = os.getenv("API_KEY") or "ECkDBnvniXH4xbVi2d5476CcB75941339e392e89Ab48Db97"
MODEL_NAME        = os.getenv("MODEL_NAME",        "Qwen3.5-397B-A17B")
TEMPERATURE       = float(os.getenv("TEMPERATURE", "0.01"))
MAX_TOKENS        = int(os.getenv("MAX_TOKENS",   "4096"))
PDF_DPI           = 144
PDF_MAX_PAGES_ONCE  = 8
BATCH_SIZE          = 2
MAX_WORKERS         = int(os.getenv("MAX_WORKERS", "24"))
API_TIMEOUT         = int(os.getenv("API_TIMEOUT", "300"))
MAX_RETRIES         = int(os.getenv("MAX_RETRIES", "2"))

# ── 目录配置 ─────────────────────────────────────────────────────
_BASE_DIR       = Path(__file__).parent          # 脚本所在目录，不受启动路径影响
LOG_DIR         = Path(os.getenv("LOG_DIR",    str(_BASE_DIR / "logs")))
UPLOAD_DIR      = Path(os.getenv("UPLOAD_DIR", str(_BASE_DIR / "uploads")))
LOG_KEEP_DAYS   = int(os.getenv("LOG_KEEP_DAYS", "7"))    # 保留天数
LOG_MAX_MB      = int(os.getenv("LOG_MAX_MB",    "50"))   # 单文件最大 MB
LOG_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── 日志配置 ────────────────────────────────────────────────────
_log_fmt = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger("aihr")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    # 控制台输出
    _console = logging.StreamHandler()
    _console.setFormatter(_log_fmt)
    logger.addHandler(_console)

    # 多 worker 模式下每个进程写独立日志文件，避免 TimedRotatingFileHandler 多进程竞争
    _pid = os.getpid()
    _today = datetime.now().strftime("%Y-%m-%d")

    # 按天滚动：每天午夜切割，保留 LOG_KEEP_DAYS 天
    _file_daily = logging.handlers.TimedRotatingFileHandler(
        filename=LOG_DIR / f"aihr_{_today}_{_pid}.log",
        when="midnight",
        interval=1,
        backupCount=LOG_KEEP_DAYS,
        encoding="utf-8",
    )
    _file_daily.suffix = "%Y-%m-%d"          # 归档文件名：aihr_2026-04-16_12345.log.2026-04-17
    _file_daily.setFormatter(_log_fmt)
    logger.addHandler(_file_daily)

    # 单文件超大保险：超过 LOG_MAX_MB 立即切割，保留 3 个备份
    _file_size = logging.handlers.RotatingFileHandler(
        filename=LOG_DIR / f"aihr_{_today}_{_pid}_overflow.log",
        maxBytes=LOG_MAX_MB * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _file_size.setFormatter(_log_fmt)
    logger.addHandler(_file_size)

# ── Prompt（与 prompts.py 一致）─────────────────────────────────
POSITION_RULES = """
## position（works）和 role（projects）字段提取规则

**规则一：有明确职位名称——保留关键词，不截断**
- 复合职位完整保留：如 "算法工程师/AI系统负责人" → "算法工程师/AI系统负责人"
- 带方向说明完整保留：如 "Java开发工程师（自研系统方向）" → "Java开发工程师（自研系统方向）"
- 带级别完整保留：如 "高级软件工程师" / "P7研发工程师"
- 去除猎聘平台冗余标签（"所在部门：" "汇报对象：" "月薪：" 等）

**规则二：无明确职位——根据职责描述推断职位关键词**
| 描述关键词 | 推断职位 |
|-----------|---------|
| 开发/研发/实现/代码/编程 | 软件工程师/开发工程师 |
| 算法/模型/训练/推理/LLM/NLP/CV | 算法工程师 |
| 架构/系统设计/微服务/分布式 | 架构师/系统工程师 |
| 测试/质量/QA/自动化测试 | 测试工程师 |
| 产品/需求/PRD/原型 | 产品经理 |
| 数据/分析/BI/报表/指标 | 数据分析师 |
| 运营/增长/用户/活动策划 | 运营专员/运营经理 |
| 销售/客户/商务/BD | 销售/客户经理 |
| 研究/论文/实验/课题/科研 | 研究员/研究生 |
| 管理/带领团队/绩效/招聘 | 团队负责人/工程经理 |
| 前端/UI/页面/组件/React/Vue | 前端工程师 |
| 后端/服务/API/数据库 | 后端工程师 |
| 运维/DevOps/部署/CI/CD | 运维工程师 |

**规则三：实习经历** → 职位后加"（实习）"，如 "软件工程师（实习）"

**规则四：科研/学术经历（无明确职位）**
→ 根据学历推断：本科→"本科"，硕士→"硕士研究生"，博士→"博士研究生/研究员"
→ 可加方向：如 "博士研究生（光学方向）"

**输出要求：position/role 只填职位名称，禁止填写职责句子或"职位/职责"等占位文字**
"""

COMPANY_SCHEMA = {
    "candidate": {
        "name":              "姓名，必要信息",
        "phone":             "电话，必要信息，格式1xxxxxxxxxx",
        "email":             "邮箱",
        "gender":            "性别 男/女 若不存在则不填",
        "age":               "年龄整数，若不存在则填0",
        "address":           "居住地信息，如 北京市朝阳区，若不存在则不填",
        "nativePlace":       "籍贯信息，重要信息，可多次确认",
        "selfEvaluation":    "自我评价，若不存在则不填",
        "expectedSalary":    "期望薪资（月薪）如 25-35K",
        "expectedPosition":  "应聘职位，若不存在则不填"
    },
    "educations": [
        {
            "school":    "学校/机构名称",
            "major":     "专业名称",
            "degree":    "学位：学士/硕士/博士，注意区分学位与学历",
            "education": "学历：初中/高中/专科/本科/硕士研究生/博士研究生",
            "startDate": "开始时间，如 2015-09",
            "endDate":   "结束时间，如 2019-07"
        }
    ],
    "works": [
        {
            "company":     "公司/机构全称，保留原文，不得改写或缩写，如公司1",
            "position":    "职位（见下方规则）",
            "description": "根据该公司下的所有项目经历，总结该阶段的核心工作职责与技术方向，不超过300字，若无任何项目或工作描述则不填",
            "startDate":   "开始时间，如 2019-07",
            "endDate":     "结束时间，如 2022-08"
        },
        {
            "company":     "公司/机构全称，保留原文，不得改写或缩写,如公司2",
            "position":    "职位（见下方规则）",
            "description": "根据该公司下的所有项目经历，总结该阶段的核心工作职责与技术方向，不超过300字，若无任何项目或工作描述则不填",
            "startDate":   "开始时间，如 2019-07",
            "endDate":     "结束时间，如 2022-08"
        }
    ],
    "projects": [
        {
            "company":     "所属公司/机构全称，保留原文，不得改写或缩写，若不存在则不填",
            "projectName": "项目完整名称，保留原文，不得改写或缩写，如项目1",
            "role":        "项目角色/职位（见下方规则）",
            "description": "项目描述，包括项目介绍、工作内容等所有相关信息，优先使用简历中的完整原始描述，若原文过于冗长或结构混乱则进行精炼总结，若不存在则不填",
            "startDate":   "开始时间，如 2020-03",
            "endDate":     "结束时间，如 2020-12"
        },
        {
            "company":     "所属公司/机构全称，保留原文，不得改写或缩写，若不存在则不填",
            "projectName": "项目完整名称，保留原文，不得改写或缩写，如项目2",
            "role":        "项目角色/职位（见下方规则）",
            "description": "项目描述，包括项目介绍、工作内容等所有相关信息，优先使用简历中的完整原始描述，若原文过于冗长或结构混乱则进行精炼总结，若不存在则不填",
            "startDate":   "开始时间，如 2020-03",
            "endDate":     "结束时间，如 2020-12"
        }
    ]
}

PROMPT_HR = f"""你是专业的简历解析助手。将给定的简历内容转换为以下 JSON 格式输出。
包含基本信息、教育经历、工作经历、项目经历，确保信息准确完整，不虚构、不推测。

重要规则——家庭成员处理：
文档中可能包含"家庭成员"或"主要社会关系"等表格，其中按行列出配偶、父母、子女等关系人。
这些人均不是候选人本人，其姓名、工作单位、联系方式等信息一律忽略，不得作为候选人信息提取。

候选人（即填表人本人）识别方法：
- 候选人信息位于文档最开头的"基本信息"表格，在"姓名"字段对应的单元格中
- 家庭成员表格中的"姓名"列出的是家属姓名（如配偶、父母），不是候选人姓名，绝对不能提取为 candidate.name
- 判断依据：若某姓名旁边标有"夫妻/配偶/父母/子女/兄弟/姐妹"等关系字样，则该姓名是家属，忽略

{POSITION_RULES}

输出格式（works[].position 和 projects[].role 严格遵守上方职位提取规则）：
{json.dumps(COMPANY_SCHEMA, ensure_ascii=False, indent=2)}

直接输出 JSON，不要添加任何说明文字或 markdown 代码块标记。
"""

BATCH_SYSTEM = PROMPT_HR + "\n注意：以下是简历的部分页面，提取其中包含的所有信息，不存在的字段填空。"

# ── 期望的 JSON 模版结构（用于校验）────────────────────────────
SCHEMA_KEYS = {"candidate", "educations", "works", "projects"}
CANDIDATE_KEYS = {"name", "phone", "email", "gender", "age", "address",
                   "nativePlace", "selfEvaluation", "expectedSalary",
                   "expectedPosition"}
EDU_KEYS = {"school", "major", "degree", "education", "startDate", "endDate"}
WORK_KEYS = {"company", "position", "description", "startDate", "endDate"}
PROJ_KEYS = {"company", "projectName", "role", "description", "startDate", "endDate"}

# ── LLM 客户端 ──────────────────────────────────────────────────
client = OpenAI(
    base_url=MODEL_SERVICE_URL,
    api_key=API_KEY,
    timeout=API_TIMEOUT,
    max_retries=MAX_RETRIES,
)

_DISABLE_THINKING = {
    "enable_thinking": False,
    "thinking": {"type": "disabled"},
    "chat_template_kwargs": {"enable_thinking": False},
}


def _llm_call(messages: list) -> str:
    """统一 LLM 调用入口，返回 content 字符串。
    若模型返回 thinking block 签名错误，自动去掉 thinking 参数重试一次。"""
    kwargs = dict(
        model=MODEL_NAME,
        messages=messages,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        extra_body=_DISABLE_THINKING,
    )
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as e:
        err = str(e)
        if "thinking block" in err or "Invalid signature" in err:
            logger.warning(f"thinking block 错误，去掉 thinking 参数重试: {e}")
            kwargs["extra_body"] = {}
            resp = client.chat.completions.create(**kwargs)
        else:
            raise
    return resp.choices[0].message.content

# ── 工具函数 ────────────────────────────────────────────────────

def pil_to_base64(img: Image.Image) -> str:
    """PIL Image → base64 data URI（PNG）"""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _open_pdf(source) -> fitz.Document:
    """打开 PDF 并检测加密，加密则立即抛出异常"""
    doc = fitz.open(stream=source, filetype="pdf") if isinstance(source, bytes) else fitz.open(source)
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF 已加密，需要密码才能解析")
    return doc


def pdf_to_images(source) -> list:
    """
    PDF → PIL Image 列表（每页一张）
    source: 文件路径(str) 或 字节流(bytes)
    """
    doc = _open_pdf(source)
    try:
        images = []
        for i in range(doc.page_count):
            zoom = PDF_DPI / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = doc[i].get_pixmap(matrix=mat, alpha=False)
            img_buf = io.BytesIO(pix.tobytes("png"))
            images.append(Image.open(img_buf).copy())
        return images
    finally:
        doc.close()


def get_page_count(source) -> int:
    """获取 PDF 页数"""
    doc = _open_pdf(source)
    try:
        return doc.page_count
    finally:
        doc.close()


def pdf_to_batches(source, batch_size: int = BATCH_SIZE) -> list:
    """
    PDF → 分批的 [(page_idx, PIL Image), ...] 列表
    """
    doc = _open_pdf(source)
    try:
        all_pages = []
        for i in range(doc.page_count):
            zoom = PDF_DPI / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = doc[i].get_pixmap(matrix=mat, alpha=False)
            img_buf = io.BytesIO(pix.tobytes("png"))
            all_pages.append((i, Image.open(img_buf).copy()))

        return [all_pages[i:i + batch_size] for i in range(0, len(all_pages), batch_size)]
    finally:
        doc.close()


def smart_batch_size(page_count: int) -> int:
    """根据页数动态决定每批大小"""
    if page_count <= 2:
        return page_count
    elif page_count <= 6:
        return 2
    else:
        return 1


# ── 后处理──────────────────────────────

def _post_process(data: dict) -> dict:
    """字段类型规范化"""
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
        c.pop("other", None)  # 移除 LLM 可能生成的多余字段
    return data


# ── JSON 模版校验 ──────────────────────────────────────────────

def _validate_schema(data: dict) -> dict:
    """
    校验解析结果是否符合 COMPANY_SCHEMA 模版。
    补充缺失字段，纠正类型错误。
    返回: (修正后的数据, 校验报告)
    """
    report = {"valid": True, "fixes": []}

    if not isinstance(data, dict):
        report["valid"] = False
        report["fixes"].append(f"顶层非 dict: {type(data).__name__}")
        return {}, report

    # 顶层缺失的 key 补空 dict/list
    for key in SCHEMA_KEYS:
        if key not in data:
            data[key] = [] if key != "candidate" else {}
            report["fixes"].append(f"补充缺失顶层字段: {key}")

    # candidate 校验
    c = data.get("candidate", {})
    if not isinstance(c, dict):
        data["candidate"] = {}
        report["fixes"].append("candidate 非 dict，已重置")
        c = {}
    # 移除不在 schema 中的多余字段
    for key in list(c.keys()):
        if key not in CANDIDATE_KEYS:
            c.pop(key)
            report["fixes"].append(f"移除 candidate 多余字段: {key}")
    for key in CANDIDATE_KEYS:
        if key not in c:
            c[key] = ""
            report["fixes"].append(f"candidate 缺失字段: {key}")

    # educations 校验
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

    # works 校验
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

    # projects 校验
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


# ── 合并逻辑───────────────────────────────────────────

def _merge_str(a: str, b: str) -> str:
    a, b = (a or "").strip(), (b or "").strip()
    if not a: return b
    if not b: return a
    return a if len(a) >= len(b) else b


_FIRST_WINS_FIELDS = {"name", "phone", "email"}  # 这些字段以首次出现为准，不用更长的


def _merge_candidate(a: dict, b: dict) -> dict:
    result = {}
    for k in set(list(a.keys()) + list(b.keys())):
        va, vb = a.get(k, ""), b.get(k, "")
        if k in ("gender", "age"):
            result[k] = va if (va and va != 0) else vb
        elif k in _FIRST_WINS_FIELDS:
            # 姓名/电话/邮箱：首次非空值优先，防止家庭成员字段覆盖
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


# ── 解析核心────────────────

def run(source, verbose: bool = False) -> dict:
    """
    1. PDF → 图像列表
    2. 构建多模态消息：所有页图像 + 请求文本
    3. 单次 LLM 调用
    4. json_repair 解析 + _post_process 规范化

    >8 页时走分批并发）
    """
    images = pdf_to_images(source)
    total_pages = len(images)

    if verbose:
        logger.debug(f"渲染 {total_pages} 页图像")

    if total_pages <= PDF_MAX_PAGES_ONCE:
        # ── 一次调用 ──────────────────────────────
        content = []
        for i, img in enumerate(images):
            if total_pages > 1:
                content.append({"type": "text", "text": f"【第 {i+1}/{total_pages} 页】"})
            content.append({
                "type": "image_url",
                "image_url": {"url": pil_to_base64(img), "detail": "high"}
            })
        content.append({
            "type": "text",
            "text": "请将以上简历内容严格按指定JSON格式输出，直接输出JSON。"
        })

        raw = _llm_call([
            {"role": "system", "content": PROMPT_HR},
            {"role": "user",   "content": content},
        ])
        if verbose:
            logger.debug(f"LLM 输出长度: {len(raw)} chars")

        result = _post_process(json_repair.loads(raw))

    else:
        # ── 分批并发 ──────────────────────────────
        batch_sz = smart_batch_size(total_pages)
        batches = pdf_to_batches(source, batch_size=batch_sz)

        if verbose:
            logger.debug(f"{total_pages}页 → {len(batches)}批（每批{batch_sz}页）")

        def _infer_single_batch(batch: list) -> dict:
            content = []
            for page_idx, img in batch:
                content.append({"type": "text", "text": f"【第 {page_idx+1}/{total_pages} 页】"})
                content.append({
                    "type": "image_url",
                    "image_url": {"url": pil_to_base64(img), "detail": "high"}
                })
            content.append({
                "type": "text",
                "text": "请按指定格式提取以上页面的简历信息，直接输出JSON。"
            })

            try:
                raw = _llm_call([
                    {"role": "system", "content": BATCH_SYSTEM},
                    {"role": "user",   "content": content},
                ])
                return json_repair.loads(raw) or {}
            except Exception as e:
                if verbose:
                    logger.warning(f"批次推理失败: {e}")
                return {}

        workers = min(MAX_WORKERS, len(batches))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(_infer_single_batch, batches))

        result = _post_process(_merge_results(results))

    # JSON 模版校验
    result, validation_report = _validate_schema(result)
    if verbose and validation_report["fixes"]:
        for fix in validation_report["fixes"]:
            logger.debug(f"校验修正: {fix}")

    return result


# ── 文件获取──────────────────────────

def download_file(url, filename):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    content = response.content
    ct = response.headers.get("Content-Type", "")
    logger.debug(f"[download] status={response.status_code} Content-Type={ct!r} size={len(content)}")
    # 用 PDF 魔数校验，防止 MinIO 返回 HTML 错误页被当成 PDF
    if not content.startswith(b"%PDF"):
        preview = content[:200].decode("utf-8", errors="replace")
        raise ValueError(
            f"下载内容非有效 PDF (Content-Type: {ct!r}, size: {len(content)}, "
            f"preview: {preview!r})"
        )
    save_path = UPLOAD_DIR / filename
    with open(save_path, "wb") as f:
        f.write(content)
    return str(save_path)


def get_pdf_source_from_content(content_items):
    content_json = {}
    for content in (content_items if isinstance(content_items, list) else [content_items]):
        item = json_repair.loads(content) if isinstance(content, str) else content
        if isinstance(item, dict) and item.get("type", "text") != "text":
            content_json = item
            break

    if not content_json:
        return None, None

    ctype = content_json.get("type", "")
    file_data = content_json.get("file", {})
    filename = file_data.get("name", "resume.pdf")

    if ctype == "url":
        source = download_file(file_data.get("path", ""), filename)
    elif ctype == "file":
        source = file_data.get("path", "")
    elif ctype == "base64":
        source = base64.b64decode(file_data.get("content", ""))
    else:
        source = None

    return source, filename


# ── FastAPI ─────────────────────────────────────────────────────

# 全局线程池：供 async 端点卸载阻塞调用，避免占用 uvicorn worker 事件循环
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="resume")


def _log_result(result: dict, filename: str, elapsed: float):
    """将解析结果完整打印到日志"""
    c     = result.get("candidate", {})
    edus  = result.get("educations", [])
    works = result.get("works", [])
    projs = result.get("projects", [])
    gender_map = {1: "男", 2: "女"}
    SEP = "─" * 60

    logger.info(f"┌{SEP}")
    logger.info(f"│ 解析结果  文件: {filename}  耗时: {elapsed:.2f}s")
    logger.info(f"├{'─'*20} 基本信息 {'─'*20}")
    logger.info(f"│  姓名: {c.get('name','')}  性别: {gender_map.get(c.get('gender'), '-')}  "
                f"年龄: {c.get('age') or '-'}  电话: {c.get('phone','')}")
    logger.info(f"│  邮箱: {c.get('email','')}  地址: {c.get('address','')}  籍贯: {c.get('nativePlace','')}")
    logger.info(f"│  期望薪资: {c.get('expectedSalary','')}  应聘职位: {c.get('expectedPosition','')}")
    if c.get("selfEvaluation"):
        logger.info(f"│  自评: {c['selfEvaluation']}")

    logger.info(f"├{'─'*20} 教育经历({len(edus)}) {'─'*18}")
    for e in edus:
        logger.info(f"│  {e.get('school','')}  {e.get('major','')}  "
                    f"{e.get('education','')} / {e.get('degree','')}  "
                    f"{e.get('startDate','')} ~ {e.get('endDate','')}")

    logger.info(f"├{'─'*20} 工作经历({len(works)}) {'─'*18}")
    for w in works:
        logger.info(f"│  [{w.get('position','')}]  {w.get('company','')}  "
                    f"{w.get('startDate','')} ~ {w.get('endDate','')}")
        if w.get("description"):
            for line in w["description"].splitlines():
                logger.info(f"│    {line}")

    logger.info(f"├{'─'*20} 项目经历({len(projs)}) {'─'*18}")
    for p in projs:
        logger.info(f"│  [{p.get('role','')}]  {p.get('projectName','')}  "
                    f"{p.get('company','')}  {p.get('startDate','')} ~ {p.get('endDate','')}")
        if p.get("description"):
            for line in p["description"].splitlines():
                logger.info(f"│    {line}")

    logger.info(f"├{'─'*20} 完整 JSON {'─'*21}")
    for line in json.dumps(result, ensure_ascii=False, indent=2).splitlines():
        logger.info(f"│  {line}")
    logger.info(f"└{SEP}")


def _parse_with_logging(source, filename: str = "unknown.pdf") -> dict:
    """同步包装器，供 run_in_executor 或直接测试调用。
    内部完成计时和完整结构化日志输出，无论哪种调用方式均输出一致的完整日志。"""
    t0 = time.time()
    result = run(source, verbose=True)
    _log_result(result, filename, time.time() - t0)
    return result


def _cleanup_uploads(max_age_hours: int = 24 * 15):
    """清理 uploads 目录中超过 max_age_hours 小时的文件，防止磁盘持续增长"""
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for f in UPLOAD_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
            except Exception as e:
                logger.warning(f"清理 uploads 失败: {f.name} - {e}")
    if removed:
        logger.info(f"uploads 清理完成，删除 {removed} 个过期文件")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _cleanup_uploads()   # 启动时清理一次过期文件
    yield
    executor.shutdown(wait=False)


app = FastAPI(title="AIHR Resume Parser", version="3.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "DeepSeek-OCR",
                "object": "model",
                "created": 1677610602,
                "owned_by": "user"
            },
            {
                "id": "DeepSeek-OCR-JSON",
                "object": "model",
                "created": 1698957206,
                "owned_by": "user"
            }
        ]
    }


@app.post("/v1/chat/completions")
async def create_chat_completion(request: dict):
    model = request.get("model", "DeepSeek-OCR")
    stream = request.get("stream", False)
    messages = request.get("messages", [])
    contents = messages[-1].get("content", []) if messages else []

    # 过滤探测消息，返回空响应而非 None（避免 FastAPI 产生 500）
    if isinstance(contents, str) and "### Task" in contents:
        return JSONResponse(status_code=200, content={"choices": [{"message": {"role": "assistant", "content": ""}}]})

    logger.info(f"请求 model={model}, type={contents[0].get('type') if isinstance(contents, list) else type(contents).__name__}")

    # 解析 content
    if isinstance(contents, str):
        text_json = json_repair.loads(contents)
        if isinstance(text_json, str):
            return JSONResponse(
                status_code=200,
                media_type="application/json; charset=utf-8",
                content=json.dumps({"type": "text", "text": contents}, ensure_ascii=False, indent=2)
            )
        elif "text" != text_json.get("type", "text"):
            contents = [text_json]
        else:
            contents = [{"type": "text", "text": str(contents)}]

    # 提取 PDF 并解析 — 阻塞调用卸载到线程池，释放事件循环
    if model in ("DeepSeek-OCR", "DeepSeek-OCR-JSON"):
        source, filename = get_pdf_source_from_content(contents)
        if source is None:
            return JSONResponse(
                status_code=400,
                content={"error": "无法从请求中获取 PDF，请检查 content.type 字段"}
            )

        t2 = time.time()
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                executor, _parse_with_logging, source, filename or "unknown.pdf"
            )
        except Exception as e:
            logger.error(f"PDF 解析失败: {e}")
            return JSONResponse(
                status_code=400,
                content={"error": f"PDF 解析失败: {str(e)}"}
            )
        generated_text = json.dumps(result, ensure_ascii=False, indent=2)
        all_text_content = [generated_text]
        logger.info(f"{model} | {len(generated_text)} chars | {time.time() - t2:.2f}s")
    else:
        all_text_content = [""]
        generated_text = ""

    # 构造响应
    prompt_tokens = sum(len(t) for t in all_text_content)
    completion_tokens = len(generated_text)

    content_body = {
        "id": f"ocr-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(datetime.now().timestamp()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": generated_text},
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        },
    }

    async def generate_stream(data):
        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    if stream:
        return StreamingResponse(generate_stream(content_body), media_type="text/event-stream")
    else:
        return JSONResponse(
            status_code=200,
            media_type="application/json; charset=utf-8",
            content=content_body,
        )


# ── 并发批量处理 ────────────────────────────────────────────────

def _process_one(item: dict) -> dict:
    """处理单个 PDF 条目"""
    source = item.get("_source")
    t0 = time.time()
    try:
        result = run(source, verbose=False)
        return {
            "index": item.get("index", 0),
            "filename": item.get("filename", ""),
            "success": True,
            "result": result,
            "elapsed_s": round(time.time() - t0, 2),
            "error": None,
        }
    except Exception as e:
        return {
            "index": item.get("index", 0),
            "filename": item.get("filename", ""),
            "success": False,
            "result": None,
            "elapsed_s": round(time.time() - t0, 2),
            "error": str(e),
        }


def _batch_process(resume_items: list, max_concurrency: int) -> list:
    """同步批量处理，供 run_in_executor 调用，不阻塞事件循环"""
    from concurrent.futures import as_completed

    # 获取 PDF 来源
    work_items = []
    for i, item in enumerate(resume_items):
        source, filename = get_pdf_source_from_content([item])
        if source is None:
            work_items.append({
                "index": i, "filename": item.get("file", {}).get("name", f"unknown_{i}.pdf"),
                "_source": None,
            })
        else:
            work_items.append({
                "index": i, "filename": filename, "_source": source,
            })

    results = [None] * len(work_items)

    def _run(item):
        return item["index"], _process_one(item)

    workers = min(max_concurrency, len(work_items))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch") as batch_pool:
        future_to_idx = {batch_pool.submit(_run, item): item["index"] for item in work_items}
        for future in as_completed(future_to_idx):
            idx, result = future.result()
            results[idx] = result

    return results


@app.post("/v1/batch/resumes")
async def batch_parse_resumes(request: dict):
    """
    并发批量解析简历。
    整个处理过程卸载到线程池，不阻塞 uvicorn 事件循环。
    """
    t0 = time.time()
    resume_items = request.get("resumes", [])
    max_concurrency = min(request.get("max_concurrency", MAX_WORKERS), 30)

    if not resume_items:
        return JSONResponse(
            status_code=400,
            content={"error": "resumes 数组不能为空"}
        )
    if len(resume_items) > 50:
        return JSONResponse(
            status_code=400,
            content={"error": "单次最多处理 50 份简历"}
        )

    # 整个阻塞处理过程卸载到线程池
    results = await asyncio.get_running_loop().run_in_executor(
        executor, _batch_process, resume_items, max_concurrency,
    )

    success_count = sum(1 for r in results if r and r["success"])
    failed_count = len(results) - success_count

    return JSONResponse(
        status_code=200,
        content={
            "total": len(results),
            "success": success_count,
            "failed": failed_count,
            "elapsed_s": round(time.time() - t0, 2),
            "results": results,
        },
    )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("SERVICE_PORT", "21801"))
    uvicorn.run(app, host="0.0.0.0", port=port)
