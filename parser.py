"""
PDF 简历解析核心模块。

职责：
  - 维护全局 OpenAI 兼容客户端（单例）
  - _llm_call：统一 LLM 调用入口，自动处理 thinking block 签名错误
  - run：PDF → 结构化 JSON 主流程
      ≤ PDF_MAX_PAGES_ONCE 页：单批一次性调用
      > PDF_MAX_PAGES_ONCE 页：分批并发推理 + 智能合并

推理策略：
  - 图像渲染采用 144 DPI，平衡清晰度与 token 消耗
  - 批量路径使用 ThreadPoolExecutor，并发数受 MAX_WORKERS 限制

思考模式：
  - extra_body 传入 _THINKING_DISABLE_PARAMS，关闭 CoT 推理链
  - thinking block 签名校验失败时自动去掉 extra_body 重试
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import json_repair
from openai import OpenAI

from langfuse_client import observe
from config import (
    MODEL_SERVICE_URL, API_KEY, MODEL_NAME, TEMPERATURE, MAX_TOKENS,
    API_TIMEOUT, MAX_RETRIES, MAX_WORKERS, PDF_MAX_PAGES_ONCE, BATCH_SIZE,
    UPLOAD_DIR, RESULT_DIR, _THINKING_DISABLE_PARAMS,
)
from logger_manager import logger
from prompts import PROMPT_HR, BATCH_SYSTEM
from pdf_utils import pdf_to_images, pdf_to_batches, get_page_count, smart_batch_size, pil_to_base64
from postprocess import _post_process, _validate_schema, _merge_results
from file_utils import get_pdf_source_from_content

# ── LLM 客户端 ────────────────────────────────────────────────────

client = OpenAI(
    base_url=MODEL_SERVICE_URL,
    api_key=API_KEY,
    timeout=API_TIMEOUT,
    max_retries=MAX_RETRIES,
)

# 由 config 根据 MODEL_NAME 自动选择（Qwen / Kimi / DeepSeek 参数名不同）
_DISABLE_THINKING = _THINKING_DISABLE_PARAMS


@observe()
def _llm_call(messages: list) -> str:
    """
    统一 LLM 调用入口。

    thinking block 签名校验失败时自动去掉 extra_body 重试一次，
    兼容部分服务端对 chat_template_kwargs 校验不一致的情况。
    """
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


# ── 解析核心 ──────────────────────────────────────────────────────

@observe()
def run(source, verbose: bool = False) -> dict:
    """
    PDF → 结构化 JSON 主流程。

    :param source:  PDF 来源：文件路径字符串、Path 对象或 bytes（内存中的 PDF 内容）
    :param verbose: True 时输出分页/分批调试日志
    :return:        经过后处理和 Schema 校验的结构化简历 dict
    """
    # 先探页数，决策路径后再按需渲染，避免超长简历先全量渲染再丢弃
    total_pages = get_page_count(source)

    if verbose:
        logger.debug(f"渲染 {total_pages} 页图像")

    if total_pages <= PDF_MAX_PAGES_ONCE:
        images  = pdf_to_images(source)
        content = []
        for i, img in enumerate(images):
            if total_pages > 1:
                content.append({"type": "text", "text": f"【第 {i+1}/{total_pages} 页】"})
            content.append({"type": "image_url", "image_url": {"url": pil_to_base64(img), "detail": "high"}})
        content.append({"type": "text", "text": "请将以上简历内容严格按指定JSON格式输出，直接输出JSON。"})
        raw    = _llm_call([{"role": "system", "content": PROMPT_HR}, {"role": "user", "content": content}])
        result = _post_process(json_repair.loads(raw))

    else:
        batch_sz = smart_batch_size(total_pages)
        batches  = pdf_to_batches(source, batch_size=batch_sz)
        if verbose:
            logger.debug(f"{total_pages}页 → {len(batches)}批（每批{batch_sz}页）")

        def _infer_batch(batch: list) -> dict:
            """将单批次页面图像发送给 LLM，返回该批次的解析结果；失败时返回 {}。"""
            content = []
            for page_idx, img in batch:
                content.append({"type": "text", "text": f"【第 {page_idx+1}/{total_pages} 页】"})
                content.append({"type": "image_url", "image_url": {"url": pil_to_base64(img), "detail": "high"}})
            content.append({"type": "text", "text": "请按指定格式提取以上页面的简历信息，直接输出JSON。"})
            try:
                raw = _llm_call([{"role": "system", "content": BATCH_SYSTEM}, {"role": "user", "content": content}])
                return json_repair.loads(raw) or {}
            except Exception as e:
                if verbose:
                    logger.warning(f"批次推理失败: {e}")
                return {}

        workers = min(MAX_WORKERS, len(batches))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_infer_batch, batches))

        result = _post_process(_merge_results(results))

    result, report = _validate_schema(result)
    if verbose and report["fixes"]:
        for fix in report["fixes"]:
            logger.debug(f"校验修正: {fix}")

    return result


# ── 日志辅助 ──────────────────────────────────────────────────────

def _log_result(result: dict, filename: str, elapsed: float):
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

    logger.debug(f"├{'─'*20} 完整简历 JSON {'─'*17}")
    for line in json.dumps(result, ensure_ascii=False, indent=2).splitlines():
        logger.debug(f"│  {line}")
    logger.debug(f"└{SEP}")


def _parse_with_logging(source, filename: str = "unknown.pdf") -> dict:
    t0     = time.time()
    result = run(source, verbose=True)
    _log_result(result, filename, time.time() - t0)
    return result


# ── 文件维护 ──────────────────────────────────────────────────────

def _cleanup_uploads(max_age_hours: int = 24 * 15):
    cutoff  = time.time() - max_age_hours * 3600
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


def _save_result(task_id: str, filename: str, result: dict):
    stem      = Path(filename).stem[:40]
    safe_stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = RESULT_DIR / f"{ts}_{safe_stem}_{task_id[:8]}.json"
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info(f"结果已保存: {out_path.name}")
    except Exception as e:
        logger.warning(f"结果保存失败: {e}")


# ── 批量处理 ──────────────────────────────────────────────────────

def _process_one(item: dict) -> dict:
    source = item.get("_source")
    t0     = time.time()
    try:
        result = run(source, verbose=False)
        return {"index": item.get("index", 0), "filename": item.get("filename", ""),
                "success": True, "result": result, "elapsed_s": round(time.time() - t0, 2), "error": None}
    except Exception as e:
        return {"index": item.get("index", 0), "filename": item.get("filename", ""),
                "success": False, "result": None, "elapsed_s": round(time.time() - t0, 2), "error": str(e)}


def _batch_process(resume_items: list, max_concurrency: int) -> list:
    work_items = []
    for i, item in enumerate(resume_items):
        source, filename = get_pdf_source_from_content([item])
        if source is None:
            work_items.append({"index": i, "filename": item.get("file", {}).get("name", f"unknown_{i}.pdf"), "_source": None})
        else:
            work_items.append({"index": i, "filename": filename, "_source": source})

    results = [None] * len(work_items)
    workers = min(max_concurrency, len(work_items))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch") as pool:
        future_map = {pool.submit(lambda it: (it["index"], _process_one(it)), item): item["index"]
                      for item in work_items}
        for future in as_completed(future_map):
            idx, result = future.result()
            results[idx] = result

    return results
