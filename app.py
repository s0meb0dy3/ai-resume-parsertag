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
import importlib
import time
import uuid
import asyncio
import logging
import logging.handlers
import copy
import json_repair
import requests
import fitz
from dataclasses import dataclass, field
from PIL import Image
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from tag.tagger import run_tagging

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
RESULT_DIR      = Path(os.getenv("RESULT_DIR", str(_BASE_DIR / "results")))
LOG_KEEP_DAYS   = int(os.getenv("LOG_KEEP_DAYS", "7"))    # 保留天数
LOG_MAX_MB      = int(os.getenv("LOG_MAX_MB",    "50"))   # 单文件最大 MB
LOG_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)
_RUNTIME_CFG_FILE = _BASE_DIR / "tag_config_runtime.json"

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

    logger.info(f"├{'─'*20} 完整简历 JSON {'─'*17}")
    for line in json.dumps(result, ensure_ascii=False, indent=2).splitlines():
        logger.info(f"│  {line}")
    logger.info(f"└{SEP}")


def _log_tag_info(task_id: str, filename: str, tag_info: dict,
                  resume_parse_time: str, tag_elapsed: float, total_elapsed: float):
    """将打标结果完整打印到日志"""
    SEP = "─" * 60
    positive_tags = tag_info.get("positiveTags", [])
    hit_tags      = tag_info.get("hitTags", [])
    severity_icon = {"low": "[低]", "medium": "[中]", "high": "[高]"}

    logger.info(f"┌{SEP}")
    logger.info(f"│ 打标结果  文件: {filename}  taskId: {task_id}")
    logger.info(f"│ 简历解析: {resume_parse_time}  打标耗时: {tag_elapsed:.1f}s  全程: {total_elapsed:.1f}s")

    logger.info(f"├{'─'*20} 正向标签({len(positive_tags)}) {'─'*16}")
    for tag in positive_tags:
        dim = tag.get("dimension", "")
        # 数组型维度读取 {dim}ValueArray，标量型读取 value
        array_key = f"{dim}ValueArray"
        if array_key in tag:
            val = ", ".join(tag[array_key]) if tag[array_key] else ""
        else:
            raw_val = tag.get("value", "")
            val = raw_val if isinstance(raw_val, str) else ", ".join(raw_val)
        conf = f"  置信度={tag['confidence']:.2f}" if tag.get("confidence") is not None else ""
        logger.info(f"│  [{dim:12s}] {val}{conf}")
    logger.info(f"│  整体置信度: {tag_info.get('confidence', 0):.2f}")

    total_hits = sum(len(g.get("hits", [])) for g in hit_tags)
    logger.info(f"├{'─'*20} 风险标签({total_hits}) {'─'*17}")
    if hit_tags:
        for group in hit_tags:
            for hit in group.get("hits", []):
                icon = severity_icon.get(hit["severity"], "[?]")
                logger.info(f"│  {icon} [{group['category']}] {hit['tag']}")
                logger.info(f"│     证据: {hit['matchedContent'][:100]}")
        logger.info(f"│  合计扣分: {tag_info.get('deductionScore', 0)}")
    else:
        logger.info("│  无风险标签命中")

    logger.info(f"│  标签打标时间: {tag_info.get('tagParseTime', '')}")
    logger.info(f"└{SEP}")


def _conform_to_template(task_id: str, resume: dict, tag_info: dict, resume_parse_time: str) -> dict:
    """
    按模版严格序列化最终输出，与 docs/resume_result_template.json (v3) 完全一致。
    顶层字段: taskId, resumeParseFinishTime, tagParseFinishTime,
              candidate, educations, works, projects, positiveTags, negativeTags
    positiveTags.tagsArray 每项: {dimension, displayName, [confidence], value:[{tag,source}]}
    negativeTags: {hitCount, deductionScore, tagsArray:[{dimension,displayName,value:[{tag,severity,matchedContent,source}]}]}
    confidence 出现维度: companyBg | skills | industry（education / workYears 不含）
    """
    from tag.tag_data import classify_education
    from tag.tag_config import POSITIVE_DIMENSIONS, NEGATIVE_DIMENSIONS

    _POSITIVE_DISPLAY = {d["dimension"]: d["displayName"] for d in POSITIVE_DIMENSIONS}
    _NO_CONF_DIM = {"education", "workYears"}

    _NEGATIVE_DIM = {d["displayName"]: (d["dimension"], d["displayName"]) for d in NEGATIVE_DIMENSIONS}

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
        return {
            "school":    e.get("school", ""),
            "major":     e.get("major", ""),
            "degree":    e.get("degree", ""),
            "education": e.get("education", ""),
            "startDate": e.get("startDate", ""),
            "endDate":   e.get("endDate", ""),
        }

    def _work(w):
        return {
            "company":     w.get("company", ""),
            "position":    w.get("position", ""),
            "description": w.get("description", ""),
            "startDate":   w.get("startDate", ""),
            "endDate":     w.get("endDate", ""),
        }

    def _proj(p):
        return {
            "company":     p.get("company", ""),
            "projectName": p.get("projectName", ""),
            "role":        p.get("role", ""),
            "description": p.get("description", ""),
            "startDate":   p.get("startDate", ""),
            "endDate":     p.get("endDate", ""),
        }

    # ── positiveTags.tagsArray ───────────────────────────────────────
    overall_conf = round(tag_info.get("confidence", 0), 2)
    tags_array = []
    edu_entry = None
    source_default = "ai"

    for tag in tag_info.get("positiveTags", []):
        dim     = tag.get("dimension", "")
        arr_key = f"{dim}ValueArray"
        values  = tag.get(arr_key, [])
        src     = tag.get("source", source_default)

        entry: dict = {
            "dimension":   dim,
            "displayName": _POSITIVE_DISPLAY.get(dim, dim),
        }
        if dim not in _NO_CONF_DIM:
            # companyBg / skills / industry：使用 LLM 给出的置信度，缺省用整体置信度
            entry["confidence"] = tag["confidence"] if tag.get("confidence") is not None else overall_conf
        entry["value"] = [{"tag": v, "source": src} for v in values]
        tags_array.append(entry)
        if dim == "education":
            edu_entry = entry

    # ── education QS 等级补全 ────────────────────────────────────────
    if edu_entry is not None:
        schools  = [e.get("school", "") for e in resume.get("educations", [])]
        degrees  = [e.get("education", "") for e in resume.get("educations", [])]
        rule_edu = classify_education(schools, degrees)
        existing = {v["tag"] for v in edu_entry["value"]}
        for v in rule_edu:
            if v not in existing:
                edu_entry["value"].append({"tag": v, "source": "rule"})

    # ── negativeTags.tagsArray ───────────────────────────────────────
    neg_array = []
    for group in tag_info.get("hitTags", []):
        category = group.get("category", "")
        dim_code, display = _NEGATIVE_DIM.get(category, (category, category))
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

    tag_parse_time = tag_info.get("tagParseTime", "")

    return {
        "taskId":               task_id,
        "resumeParseFinishTime": resume_parse_time,
        "tagParseFinishTime":   tag_parse_time,
        "candidate":  candidate,
        "educations": [_edu(e) for e in resume.get("educations", [])],
        "works":      [_work(w) for w in resume.get("works", [])],
        "projects":   [_proj(p) for p in resume.get("projects", [])],
        "positiveTags": {
            "tagsArray": tags_array,
        },
        "negativeTags": {
            "hitCount":      tag_info.get("hitCount", 0),
            "deductionScore": tag_info.get("deductionScore", 0),
            "tagsArray":     neg_array,
        },
    }


def _save_result(task_id: str, filename: str, result: dict):
    """将最终完整结果（简历 + tagInfo）保存为 JSON 文件到 RESULT_DIR"""
    stem = Path(filename).stem[:40]  # 文件名前40字符，避免路径过长
    safe_stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = RESULT_DIR / f"{ts}_{safe_stem}_{task_id[:8]}.json"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"结果已保存: {out_path.name}")
    except Exception as e:
        logger.warning(f"结果保存失败: {e}")


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
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    yield
    cleanup_task.cancel()
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


# ── 标签配置 CRUD 辅助函数 ─────────────────────────────────────────

def _cfg_load() -> dict:
    """加载运行时配置：优先 JSON 文件，回退到 tag_config.py 默认值。"""
    if _RUNTIME_CFG_FILE.exists():
        with open(_RUNTIME_CFG_FILE, encoding="utf-8") as f:
            return json.load(f)
    from tag import tag_config as _tc
    importlib.reload(_tc)
    return {
        "positiveDimensions": copy.deepcopy(_tc.POSITIVE_DIMENSIONS),
        "negativeDimensions": copy.deepcopy(_tc.NEGATIVE_DIMENSIONS),
        "severityDeduction":  dict(_tc.SEVERITY_DEDUCTION),
    }

def _cfg_save(cfg: dict):
    """原子写入配置文件（tmp → rename）。"""
    tmp = _RUNTIME_CFG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    tmp.replace(_RUNTIME_CFG_FILE)

def _cfg_response(cfg: dict) -> JSONResponse:
    """统一返回格式，附加 updatedAt。"""
    return JSONResponse(content={
        "code": 0,
        "msg":  "success",
        "data": {
            **cfg,
            "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    })


# ── GET /api/tags/config ──────────────────────────────────────────

@app.get("/api/tags/config")
async def get_tags_config():
    """查询完整标签配置（含正向维度、负向维度、扣分权重）。"""
    async with _cfg_lock:
        cfg = _cfg_load()
    return _cfg_response(cfg)


# ── 正向维度 CRUD ──────────────────────────────────────────────────

@app.post("/api/tags/config/positive")
async def add_positive_dimension(body: dict):
    """
    新增正向标签维度。
    Body: {"dimension":"newDim","displayName":"新维度","enabled":true,"allowedValues":[]}
    """
    dim = (body.get("dimension") or "").strip()
    if not dim:
        return JSONResponse(content={"code": 1001, "msg": "dimension 不能为空"})
    async with _cfg_lock:
        cfg = _cfg_load()
        if any(d["dimension"] == dim for d in cfg["positiveDimensions"]):
            return JSONResponse(content={"code": 1002, "msg": f"dimension '{dim}' 已存在"})
        cfg["positiveDimensions"].append({
            "dimension":     dim,
            "displayName":   body.get("displayName", dim),
            "enabled":       body.get("enabled", True),
            "allowedValues": body.get("allowedValues", []),
        })
        _cfg_save(cfg)
    return _cfg_response(cfg)


@app.put("/api/tags/config/positive/{dimension}")
async def update_positive_dimension(dimension: str, body: dict):
    """
    更新正向标签维度。
    Body 字段均可选: {"displayName":"...","enabled":true,"allowedValues":["C9","985"]}
    """
    async with _cfg_lock:
        cfg = _cfg_load()
        target = next((d for d in cfg["positiveDimensions"] if d["dimension"] == dimension), None)
        if target is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        if "displayName"   in body: target["displayName"]   = body["displayName"]
        if "enabled"       in body: target["enabled"]       = bool(body["enabled"])
        if "allowedValues" in body: target["allowedValues"] = list(body["allowedValues"])
        _cfg_save(cfg)
    return _cfg_response(cfg)


@app.delete("/api/tags/config/positive/{dimension}")
async def delete_positive_dimension(dimension: str):
    """删除正向标签维度。"""
    async with _cfg_lock:
        cfg = _cfg_load()
        before = len(cfg["positiveDimensions"])
        cfg["positiveDimensions"] = [d for d in cfg["positiveDimensions"] if d["dimension"] != dimension]
        if len(cfg["positiveDimensions"]) == before:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        _cfg_save(cfg)
    return _cfg_response(cfg)


# ── 负向维度 CRUD ──────────────────────────────────────────────────

@app.put("/api/tags/config/negative/{dimension}")
async def update_negative_dimension(dimension: str, body: dict):
    """
    更新负向维度元信息（displayName / enabled）。
    Body 字段均可选: {"displayName":"...","enabled":true}
    """
    async with _cfg_lock:
        cfg = _cfg_load()
        target = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if target is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        if "displayName" in body: target["displayName"] = body["displayName"]
        if "enabled"     in body: target["enabled"]     = bool(body["enabled"])
        _cfg_save(cfg)
    return _cfg_response(cfg)


@app.post("/api/tags/config/negative/{dimension}/rules")
async def add_negative_rule(dimension: str, body: dict):
    """
    向负向维度新增一条规则。
    Body: {"ruleKey":"new_rule","tagName":"规则名","enabled":true,"severity":"medium",...}
    """
    rule_key = (body.get("ruleKey") or "").strip()
    if not rule_key:
        return JSONResponse(content={"code": 1001, "msg": "ruleKey 不能为空"})
    async with _cfg_lock:
        cfg = _cfg_load()
        dim = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if dim is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        rules = dim.setdefault("rules", {})
        if rule_key in rules:
            return JSONResponse(content={"code": 1002, "msg": f"ruleKey '{rule_key}' 已存在"})
        rule = {k: v for k, v in body.items() if k != "ruleKey"}
        rule.setdefault("tagName",  rule_key)
        rule.setdefault("enabled",  True)
        rule.setdefault("severity", "medium")
        rules[rule_key] = rule
        _cfg_save(cfg)
    return _cfg_response(cfg)


@app.put("/api/tags/config/negative/{dimension}/rules/{rule_key}")
async def update_negative_rule(dimension: str, rule_key: str, body: dict):
    """
    更新负向维度某条规则的参数。Body 字段均可选，只传需要改的字段。
    示例: {"enabled":false,"threshold":5,"severity":"high"}
    """
    async with _cfg_lock:
        cfg = _cfg_load()
        dim = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if dim is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        rules = dim.get("rules", {})
        if rule_key not in rules:
            return JSONResponse(content={"code": 1004, "msg": f"ruleKey '{rule_key}' 不存在"})
        rules[rule_key].update({k: v for k, v in body.items() if k != "ruleKey"})
        _cfg_save(cfg)
    return _cfg_response(cfg)


@app.delete("/api/tags/config/negative/{dimension}/rules/{rule_key}")
async def delete_negative_rule(dimension: str, rule_key: str):
    """删除负向维度某条规则。"""
    async with _cfg_lock:
        cfg = _cfg_load()
        dim = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if dim is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        if rule_key not in dim.get("rules", {}):
            return JSONResponse(content={"code": 1004, "msg": f"ruleKey '{rule_key}' 不存在"})
        del dim["rules"][rule_key]
        _cfg_save(cfg)
    return _cfg_response(cfg)


# ── 扣分权重 & 重置 ───────────────────────────────────────────────

@app.put("/api/tags/config/severity")
async def update_severity(body: dict):
    """
    更新严重程度扣分权重。
    Body: {"low":-5,"medium":-10,"high":-20}
    """
    allowed = {"low", "medium", "high"}
    invalid = [k for k in body if k not in allowed]
    if invalid:
        return JSONResponse(content={"code": 1001, "msg": f"无效字段: {invalid}，仅支持 low/medium/high"})
    async with _cfg_lock:
        cfg = _cfg_load()
        cfg["severityDeduction"].update({k: int(v) for k, v in body.items()})
        _cfg_save(cfg)
    return _cfg_response(cfg)


@app.post("/api/tags/config/reset")
async def reset_tags_config():
    """重置为 tag_config.py 默认配置（删除运行时 JSON 覆盖文件）。"""
    async with _cfg_lock:
        if _RUNTIME_CFG_FILE.exists():
            _RUNTIME_CFG_FILE.unlink()
        cfg = _cfg_load()
    return _cfg_response(cfg)


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
            },
            {
                "id": "AIHR-Resume",
                "object": "model",
                "created": 1748000000,
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

    # 无内容或纯文本探测：返回标准 OpenAI 格式，供渠道健康检测通过
    def _is_probe(c) -> bool:
        if not c:
            return True
        if isinstance(c, list) and len(c) == 0:
            return True
        if isinstance(c, str):
            if "### Task" in c:
                return True
            parsed = json_repair.loads(c)
            if isinstance(parsed, str):
                return True
        return False

    if _is_probe(contents):
        return JSONResponse(status_code=200, content={
            "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1},
        })

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

    # ── DeepSeek-OCR：仅解析，不打标（兼容旧调用）─────────────────
    if model == "DeepSeek-OCR":
        source, filename = get_pdf_source_from_content(contents)
        if source is None:
            return JSONResponse(status_code=400,
                content={"error": "无法从请求中获取 PDF，请检查 content.type 字段"})
        t2 = time.time()
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                executor, _parse_with_logging, source, filename or "unknown.pdf"
            )
        except Exception as e:
            logger.error(f"PDF 解析失败: {e}")
            return JSONResponse(status_code=400, content={"error": f"PDF 解析失败: {str(e)}"})
        generated_text = json.dumps(result, ensure_ascii=False, indent=2)
        all_text_content = [generated_text]
        logger.info(f"{model} | {len(generated_text)} chars | {time.time() - t2:.2f}s")

    # ── DeepSeek-OCR-JSON：解析 + 打标，复用 _run_parse_and_tag 流水线 ──
    elif model == "DeepSeek-OCR-JSON":
        source, filename = get_pdf_source_from_content(contents)
        if source is None:
            return JSONResponse(status_code=400,
                content={"error": "无法从请求中获取 PDF，请检查 content.type 字段"})

        task_id = str(uuid.uuid4())
        job_info = {
            "jobTitle":          request.get("jobTitle", "") or "",
            "requiredSkills":    request.get("requiredSkills", "") or "",
            "jobDescription":    request.get("jobDescription", "") or "",
            "jobSensitiveWords": request.get("jobSensitiveWords", "") or "",
        }
        rec = TaskRecord(task_id=task_id)
        async with _task_lock:
            _task_store[task_id] = rec

        async_mode = request.get("async_mode", False)
        if async_mode:
            # 异步模式：立即返回 taskId，通过 GET /api/resume/async/result 轮询
            asyncio.create_task(
                _run_parse_and_tag(task_id, source, filename or "resume.pdf", job_info)
            )
            generated_text = json.dumps(
                {"taskId": task_id, "status": 0, "statusDesc": "待处理", "estimatedSeconds": 90},
                ensure_ascii=False,
            )
            logger.info(f"{model} async_mode=true taskId={task_id}")
        else:
            # 同步模式：等待完整流水线（parse+tag），返回 v3 格式 JSON
            await _run_parse_and_tag(task_id, source, filename or "resume.pdf", job_info)
            async with _task_lock:
                rec = _task_store.get(task_id)
            if rec and rec.status == 3:
                generated_text = json.dumps(rec.result, ensure_ascii=False, indent=2)
                logger.info(f"{model} sync taskId={task_id} | {len(generated_text)} chars")
            elif rec and rec.status == 4:
                return JSONResponse(status_code=400,
                    content={"error": rec.error_msg or "处理失败"})
            else:
                generated_text = "{}"
        all_text_content = [generated_text]

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

    # 整个阻塞处理过程卸载到
    # 线程池
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


# ── 异步任务基础设施 ────────────────────────────────────────────

TASK_TTL_MINUTES = int(os.getenv("TASK_TTL_MINUTES", "10"))

_STATUS_DESC = {
    0: "待处理",
    1: "简历解析中",
    2: "标签打标中",
    3: "任务完成",
    4: "任务失败",
}

ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png"}
MAX_FILE_SIZE_MB    = int(os.getenv("MAX_FILE_SIZE_MB", "20"))


@dataclass
class TaskRecord:
    task_id:           str
    status:            int = 0
    created_at:        float = field(default_factory=time.time)
    completed_at:      Optional[float] = None
    result:            Optional[dict] = None
    error_msg:         Optional[str] = None
    estimated_seconds: int = 90
    extra:             Optional[str] = None


_task_store: dict[str, TaskRecord] = {}
_task_lock = asyncio.Lock()
_cfg_lock  = asyncio.Lock()


async def _set_task_field(task_id: str, **kwargs):
    async with _task_lock:
        rec = _task_store.get(task_id)
        if rec:
            for k, v in kwargs.items():
                setattr(rec, k, v)


def _cleanup_task_store():
    cutoff = time.time() - TASK_TTL_MINUTES * 60
    expired = [tid for tid, t in list(_task_store.items()) if t.created_at < cutoff]
    for tid in expired:
        _task_store.pop(tid, None)
    if expired:
        logger.info(f"task_store 清理 {len(expired)} 条过期任务")


async def _periodic_cleanup():
    while True:
        await asyncio.sleep(60)
        _cleanup_task_store()


async def _run_parse_and_tag(task_id: str, source, filename: str, job_info: dict):
    """两阶段异步流水线：阶段一解析 → 阶段二打标
    source 可为 bytes（multipart 上传）、本地路径 str、HTTP URL str 或 base64 bytes，
    与 /api/resume/async/parse 和 /v1/chat/completions 共用同一条流水线。
    """
    loop = asyncio.get_running_loop()
    pipeline_start = time.time()
    try:
        # 阶段一：简历解析
        await _set_task_field(task_id, status=1)
        resume_start = time.time()

        resume = await loop.run_in_executor(
            executor, _parse_with_logging, source, filename
        )
        resume_parse_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        phase1_elapsed = time.time() - resume_start
        logger.info(f"[{task_id}] 阶段一完成，耗时 {phase1_elapsed:.1f}s")

        # 阶段二：正向标签 + 敏感词检测（并行）
        await _set_task_field(task_id, status=2)
        tag_start = time.time()

        def _do_tagging():
            return run_tagging(resume, job_info, llm_call_fn=_llm_call)

        tag_info = await loop.run_in_executor(executor, _do_tagging)
        tag_elapsed = time.time() - tag_start
        total_elapsed = time.time() - pipeline_start

        # 打标日志（完整结构化输出）
        _log_tag_info(task_id, filename, tag_info, resume_parse_time, tag_elapsed, total_elapsed)

        # 按模版严格序列化最终输出
        result = _conform_to_template(task_id, resume, tag_info, resume_parse_time)

        # 读取 extra（透传字段）
        async with _task_lock:
            _rec = _task_store.get(task_id)
        _extra = _rec.extra if _rec else None

        # 构造与 GET /api/resume/async/result 完全一致的完整 API 响应并写入日志
        completed_at = time.time()
        full_api_response = {
            "code": 0,
            "msg": "success",
            "data": {
                "taskId":           task_id,
                "status":           3,
                "statusDesc":       "任务完成",
                "elapsedSeconds":   round(completed_at - pipeline_start, 1),
                "estimatedSeconds": 90,
                "extra":            _extra,
                "result":           result,
            },
        }
        SEP = "─" * 60
        logger.info(f"┌{SEP}")
        logger.info(f"│ GET /api/resume/async/result  文件: {filename}  taskId: {task_id}")
        for line in json.dumps(full_api_response, ensure_ascii=False, indent=2).splitlines():
            logger.info(f"│  {line}")
        logger.info(f"└{SEP}")

        # 持久化到 results/ 目录
        _save_result(task_id, filename, result)

        await _set_task_field(task_id, status=3, result=result, completed_at=completed_at)

    except Exception as e:
        logger.error(f"[{task_id}] 任务失败: {e}")
        await _set_task_field(task_id, status=4, error_msg=str(e), completed_at=time.time())


# ── 异步对外接口 ────────────────────────────────────────────────

@app.post("/api/resume/async/parse")
async def async_parse_resume(
    file: UploadFile = File(...),
    requestId: Optional[str] = Form(None),
    jobTitle: Optional[str] = Form(None),
    requiredSkills: Optional[str] = Form(None),
    jobDescription: Optional[str] = Form(None),
    jobSensitiveWords: Optional[str] = Form(None),
):
    """上传简历，触发异步解析+打标，立即返回 taskId"""
    # 防重复提交
    if requestId:
        async with _task_lock:
            for rec in _task_store.values():
                if getattr(rec, "request_id", None) == requestId:
                    return JSONResponse(content={
                        "code": 0, "msg": "success",
                        "data": {"taskId": rec.task_id, "status": rec.status, "estimatedSeconds": rec.estimated_seconds}
                    })

    # 文件格式校验
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return JSONResponse(content={"code": 1001, "msg": f"文件格式不支持: {suffix}"})

    file_bytes = await file.read()

    # 文件大小校验
    if len(file_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
        return JSONResponse(content={"code": 1002, "msg": f"文件大小超限（最大 {MAX_FILE_SIZE_MB}MB）"})

    # 仅支持 PDF（当前解析链路）
    if suffix != ".pdf":
        return JSONResponse(content={"code": 1001, "msg": "当前仅支持 PDF 格式"})

    task_id = str(uuid.uuid4())
    rec = TaskRecord(task_id=task_id)
    if requestId:
        rec.request_id = requestId  # type: ignore[attr-defined]

    async with _task_lock:
        _task_store[task_id] = rec

    job_info = {
        "jobTitle":          jobTitle or "",
        "requiredSkills":    requiredSkills or "",
        "jobDescription":    jobDescription or "",
        "jobSensitiveWords": jobSensitiveWords or "",
    }

    asyncio.create_task(_run_parse_and_tag(task_id, file_bytes, file.filename or "resume.pdf", job_info))

    logger.info(f"[{task_id}] 任务入队 file={file.filename} jobTitle={jobTitle}")

    return JSONResponse(content={
        "code": 0, "msg": "success",
        "data": {"taskId": task_id, "status": 0, "estimatedSeconds": 90}
    })


@app.get("/api/resume/async/result")
async def get_async_result(taskId: str):
    """查询任务状态，完成后返回完整解析+打标结果"""
    async with _task_lock:
        rec = _task_store.get(taskId)

    if rec is None:
        return JSONResponse(content={
            "code": 2001,
            "msg": "taskId 不存在或已过期（TTL 10min）",
            "data": None
        })

    elapsed = round(time.time() - rec.created_at, 1)
    data: dict = {
        "taskId":           taskId,
        "status":           rec.status,
        "statusDesc":       _STATUS_DESC.get(rec.status, "未知"),
        "elapsedSeconds":   elapsed,
        "estimatedSeconds": rec.estimated_seconds,
        "extra":            rec.extra,
    }

    if rec.status == 3:
        data["result"] = rec.result
    elif rec.status == 4:
        data["errorMsg"] = rec.error_msg

    return JSONResponse(content={"code": 0, "msg": "success", "data": data})


@app.post("/api/resume/async/batch")
async def async_batch_parse(request: dict):
    """
    批量异步提交简历（JSON raw body，仅支持 type=url）。
    立即返回每份文件的 taskId，通过 GET /api/resume/async/result 轮询。
    单次最多提交 20 份，后台并发执行，互不阻塞。

    请求体示例：
    {
      "resumes": [
        {"type": "url", "file": {"name": "a.pdf", "path": "http://minio:9000/resumes/a.pdf", "extra": ""}},
      ],
      "jobTitle": "算法工程师"
    }
    """
    resumes_raw = request.get("resumes", [])
    if not resumes_raw:
        return JSONResponse(content={"code": 1001, "msg": "resumes 数组不能为空"})
    if not isinstance(resumes_raw, list):
        return JSONResponse(content={"code": 1001, "msg": "resumes 必须是数组"})
    for item in resumes_raw:
        if not isinstance(item, dict) or item.get("type") != "url":
            t = item.get("type") if isinstance(item, dict) else type(item).__name__
            return JSONResponse(content={"code": 1001, "msg": f"resumes 仅支持 type=url，不支持: {t}"})
    if len(resumes_raw) > 20:
        return JSONResponse(content={"code": 1003, "msg": f"共 {len(resumes_raw)} 份，单次最多提交 20 份"})

    job_info = {
        "jobTitle":          request.get("jobTitle", "") or "",
        "requiredSkills":    request.get("requiredSkills", "") or "",
        "jobDescription":    request.get("jobDescription", "") or "",
        "jobSensitiveWords": request.get("jobSensitiveWords", "") or "",
    }

    loop = asyncio.get_running_loop()
    tasks = []
    for item in resumes_raw:
        file_meta = item.get("file", {})
        filename  = file_meta.get("name", "resume.pdf")
        extra     = file_meta.get("extra") or item.get("extra") or None
        try:
            source, fn = await loop.run_in_executor(executor, get_pdf_source_from_content, [item])
        except Exception as e:
            tasks.append({"filename": filename, "extra": extra, "code": 1001, "msg": f"文件下载失败: {str(e)}", "taskId": None})
            continue
        if source is None:
            tasks.append({"filename": filename, "extra": extra, "code": 1001, "msg": "无法下载 PDF，请检查 URL 是否可访问", "taskId": None})
            continue
        task_id = str(uuid.uuid4())
        rec = TaskRecord(task_id=task_id, extra=extra)
        async with _task_lock:
            _task_store[task_id] = rec
        asyncio.create_task(_run_parse_and_tag(task_id, source, fn or filename, job_info))
        logger.info(f"[batch] 任务入队 taskId={task_id} file={fn or filename} extra={extra}")
        tasks.append({"filename": fn or filename, "extra": extra, "code": 0, "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    submitted = sum(1 for t in tasks if t["code"] == 0)
    return JSONResponse(content={
        "code": 0, "msg": "success",
        "total": len(resumes_raw), "submitted": submitted,
        "failed": len(resumes_raw) - submitted,
        "estimatedSeconds": 90, "tasks": tasks,
    })

@app.post("/api/resume/async/batch/multiform")

async def async_batch_parse_json(
    files:             Optional[List[UploadFile]] = File(None),
    resumes:           Optional[str]  = Form(None),
    jobTitle:          Optional[str]  = Form(None),
    requiredSkills:    Optional[str]  = Form(None),
    jobDescription:    Optional[str]  = Form(None),
    jobSensitiveWords: Optional[str]  = Form(None),
):
    """
    批量异步提交，支持两种文件来源，可在同一请求中混用：

      files  字段（File 类型，可多行）：直接上传 PDF 二进制
      resumes字段（Text 类型，JSON 字符串，type=url）：
        [
          {"type": "url", "file": {"name": "a.pdf", "path": "http://minio:9000/resumes/a.pdf"}},
          {"type": "url", "file": {"name": "b.pdf", "path": "http://minio:9000/resumes/b.pdf"}}
        ]

    两个字段均可选，但至少提供其中一个。合并后总数不超过 20 份。
    """
    if not files and not resumes:
        return JSONResponse(content={"code": 1001, "msg": "files 或 resumes 至少提供一个"})

    job_info = {
        "jobTitle":          jobTitle or "",
        "requiredSkills":    requiredSkills or "",
        "jobDescription":    jobDescription or "",
        "jobSensitiveWords": jobSensitiveWords or "",
    }

    # ── 解析 resumes JSON 字符串 ─────────────────────────────────
    resume_items: list = []
    if resumes:
        try:
            resume_items = json.loads(resumes)
            if not isinstance(resume_items, list):
                return JSONResponse(content={"code": 1001, "msg": "resumes 必须是 JSON 数组"})
        except Exception:
            return JSONResponse(content={"code": 1001, "msg": "resumes 不是合法的 JSON 字符串"})
        # 只允许 type=url
        for item in resume_items:
            if item.get("type") != "url":
                return JSONResponse(content={"code": 1001,
                    "msg": f"resumes 仅支持 type=url，不支持: {item.get('type')}"})

    total_count = len(files or []) + len(resume_items)
    if total_count > 20:
        return JSONResponse(content={"code": 1003, "msg": f"合并后共 {total_count} 份，单次最多提交 20 份"})

    loop = asyncio.get_running_loop()
    tasks = []

    # ── 处理二进制上传文件 ────────────────────────────────────────
    for upload in (files or []):
        filename = upload.filename or "resume.pdf"
        suffix   = Path(filename).suffix.lower()

        if suffix not in ALLOWED_EXTENSIONS or suffix != ".pdf":
            tasks.append({"filename": filename, "code": 1001,
                          "msg": f"格式不支持: {suffix}", "taskId": None})
            continue

        file_bytes = await upload.read()
        if len(file_bytes) > MAX_FILE_SIZE_MB * 1024 * 1024:
            tasks.append({"filename": filename, "code": 1002,
                          "msg": f"文件超过 {MAX_FILE_SIZE_MB}MB", "taskId": None})
            continue

        task_id = str(uuid.uuid4())
        async with _task_lock:
            _task_store[task_id] = TaskRecord(task_id=task_id)

        asyncio.create_task(_run_parse_and_tag(task_id, file_bytes, filename, job_info))
        logger.info(f"[batch/multiform] 任务入队 taskId={task_id} file={filename} type=upload")
        tasks.append({"filename": filename, "code": 0,
                      "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    # ── 处理 URL（从 MinIO / HTTP 下载）──────────────────────────
    for item in resume_items:
        file_meta = item.get("file", {})
        filename  = file_meta.get("name", "resume.pdf")
        extra     = file_meta.get("extra") or item.get("extra") or None

        try:
            source, fn = await loop.run_in_executor(
                executor, get_pdf_source_from_content, [item]
            )
        except Exception as e:
            tasks.append({"filename": filename, "extra": extra, "code": 1001,
                          "msg": f"文件下载失败: {str(e)}", "taskId": None})
            continue

        if source is None:
            tasks.append({"filename": filename, "extra": extra, "code": 1001,
                          "msg": "无法下载 PDF，请检查 URL 是否可访问", "taskId": None})
            continue

        task_id = str(uuid.uuid4())
        rec = TaskRecord(task_id=task_id, extra=extra)
        async with _task_lock:
            _task_store[task_id] = rec

        asyncio.create_task(_run_parse_and_tag(task_id, source, fn or filename, job_info))
        logger.info(f"[batch/multiform] 任务入队 taskId={task_id} file={fn or filename} type=url extra={extra}")
        tasks.append({"filename": fn or filename, "extra": extra, "code": 0,
                      "taskId": task_id, "status": 0, "statusDesc": "待处理"})

    submitted = sum(1 for t in tasks if t["code"] == 0)
    return JSONResponse(content={
        "code":      0,
        "msg":       "success",
        "total":     total_count,
        "submitted": submitted,
        "failed":    total_count - submitted,
        "estimatedSeconds": 90,
        "tasks":     tasks,
    })


@app.get("/api/tasks")
async def list_tasks(status: Optional[int] = None, include_result: bool = False):
    """
    查询内存中全部任务的状态摘要。
    ?status=0/1/2/3/4       按状态过滤（不传则返回全部）
    ?include_result=true    status=3 时同时返回完整 result JSON（默认 false）
    结果按创建时间倒序排列，最多返回 200 条（TTL 10min 内）。
    """
    async with _task_lock:
        snapshot = list(_task_store.items())

    now = time.time()
    tasks = []
    for task_id, rec in sorted(snapshot, key=lambda x: x[1].created_at, reverse=True):
        if status is not None and rec.status != status:
            continue

        item: dict = {
            "taskId":         task_id,
            "status":         rec.status,
            "statusDesc":     _STATUS_DESC.get(rec.status, "未知"),
            "elapsedSeconds": round(now - rec.created_at, 1),
            "createdAt":      datetime.fromtimestamp(rec.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            "extra":          rec.extra,
        }

        if rec.status == 3 and rec.result:
            c = rec.result.get("candidate", {})
            item["candidateName"]   = c.get("name", "")
            item["resumeParseTime"] = rec.result.get("resumeParseFinishTime", "")
            item["tagParseTime"]    = rec.result.get("tagParseFinishTime", "")
            item["completedAt"]     = (
                datetime.fromtimestamp(rec.completed_at).strftime("%Y-%m-%d %H:%M:%S")
                if rec.completed_at else ""
            )
            if include_result:
                item["result"] = rec.result
        elif rec.status == 4:
            item["errorMsg"] = rec.error_msg

        tasks.append(item)

    return JSONResponse(content={
        "code": 0,
        "msg":  "success",
        "data": {
            "total": len(tasks),
            "tasks": tasks,
        },
    })


# ── 算法侧内部接口（供 Java 后端编排调用）──────────────────────

@app.post("/algorithm/resume/parse")
async def algorithm_parse(request: dict):
    """内部接口：同步解析简历，返回结构化 JSON（供 Java 后端调用）"""
    source, filename = get_pdf_source_from_content(request.get("content", []))
    if source is None:
        return JSONResponse(status_code=400, content={"error": "无法获取 PDF"})
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(executor, _parse_with_logging, source, filename or "resume.pdf")
    return JSONResponse(content=result)


@app.post("/algorithm/positive-tag/extract")
async def algorithm_positive_tag(request: dict):
    """内部接口：从结构化简历提取正向标签"""
    from tag.tagger import extract_positive_tags_llm
    resume = request.get("resume", {})
    job_info = request.get("jobInfo", {})
    if not resume:
        return JSONResponse(status_code=400, content={"error": "缺少 resume 字段"})

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        executor,
        lambda: extract_positive_tags_llm(resume, job_info, _llm_call)
    )
    return JSONResponse(content=result)


@app.post("/algorithm/sensitive-tag/detect")
async def algorithm_sensitive_tag(request: dict):
    """内部接口：敏感词/风险检测"""
    from tag.tagger import detect_sensitive_tags
    resume = request.get("resume", {})
    job_info = request.get("jobInfo", {})
    if not resume:
        return JSONResponse(status_code=400, content={"error": "缺少 resume 字段"})
    result = detect_sensitive_tags(resume, job_info)
    return JSONResponse(content=result)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("SERVICE_PORT", "21801"))
    uvicorn.run(app, host="0.0.0.0", port=port)
