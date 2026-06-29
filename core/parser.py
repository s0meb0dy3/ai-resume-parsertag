"""
简历解析核心模块。

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
from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError

from logger.langfuse_client import observe
from config import (
    MODEL_SERVICE_URL, API_KEY, MODEL_NAME, TEMPERATURE, MAX_TOKENS,
    API_TIMEOUT, MAX_RETRIES, MAX_WORKERS, PDF_MAX_PAGES_ONCE,
    UPLOAD_DIR, RESULT_DIR, FALLBACK_MODELS,
    get_current_model_name, get_thinking_disable_params,
    reset_current_model_name, set_current_model_name,
)
from logger.logger_manager import logger
from core.prompts import PROMPT_HR, BATCH_SYSTEM
from utils.pdf_utils import pdf_to_images, pdf_to_batches, get_page_count, smart_batch_size, pil_to_base64
from core.postprocess import _post_process, _validate_schema, _merge_results
from utils.file_utils import get_pdf_source_from_content

# ── LLM 客户端 ────────────────────────────────────────────────────

client = OpenAI(
    base_url=MODEL_SERVICE_URL,
    api_key=API_KEY,
    timeout=API_TIMEOUT,
    max_retries=MAX_RETRIES,
)

# 默认模型保留为兜底，具体请求可通过 config 的模型上下文覆盖。


def _llm_try_model(messages: list, model_name: str) -> str:
    """
    单模型调用，含 thinking block 自动重试。
    成功返回文本；不可重试的错误直接 raise；可回退的错误也 raise（由调用方决定是否换模型）。
    """
    kwargs = dict(
        model=model_name,
        messages=messages,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
        extra_body=get_thinking_disable_params(model_name),
    )
    try:
        resp = client.chat.completions.create(**kwargs)
    except APITimeoutError:
        # 超时是可回退错误，直接向上传递让 _llm_call 决定是否换模型
        raise
    except APIConnectionError as e:
        # 服务器整体不通（非超时），换模型名无意义，直接抛出
        logger.error(f"[LLM] model={model_name} 无法连接 {MODEL_SERVICE_URL}: {e}")
        raise
    except APIStatusError as e:
        if e.status_code in (401, 403):
            # 鉴权失败，换模型无效，直接抛出
            hint = "认证失败，请检查 API_KEY 配置" if e.status_code == 401 else "无权限，请检查 API_KEY 或账户权限"
            logger.error(f"[LLM] model={model_name} {hint}: {e.message}")
            raise
        # 其他 HTTP 错误（404/429/500/502/503）允许回退
        raise
    except Exception as e:
        err = str(e)
        if "thinking block" in err or "Invalid signature" in err:
            logger.warning(f"[LLM] model={model_name} thinking block 错误，去掉 thinking 参数重试")
            kwargs["extra_body"] = {}
            resp = client.chat.completions.create(**kwargs)
        else:
            raise
    usage = resp.usage
    if usage:
        logger.info(
            f"[LLM] model={model_name} "
            f"prompt={usage.prompt_tokens} completion={usage.completion_tokens} total={usage.total_tokens}"
        )
    return resp.choices[0].message.content


@observe()
def _llm_call(messages: list) -> str:
    """
    统一 LLM 调用入口，支持模型回退链。

    主模型（当前 ContextVar）失败时依次尝试 FALLBACK_MODELS；
    服务器整体不通（APIConnectionError）或鉴权失败（401/403）时不回退。
    """
    primary    = get_current_model_name()
    all_models = [primary] + FALLBACK_MODELS
    last_error: Exception | None = None

    for i, model_name in enumerate(all_models):
        if i == 0:
            logger.info(f"[LLM] model={model_name} max_tokens={MAX_TOKENS}")
        else:
            logger.warning(f"[LLM] 回退到备用模型 [{i}/{len(FALLBACK_MODELS)}]: {model_name}")
        _has_next = i < len(all_models) - 1
        _next_tip = "尝试下一个备用模型" if _has_next else "已无可用模型"
        try:
            return _llm_try_model(messages, model_name)
        except APITimeoutError as e:
            # 必须在 APIConnectionError 之前捕获（子类）
            logger.warning(f"[LLM] model={model_name} 超时 (timeout={API_TIMEOUT}s)，{_next_tip}")
            last_error = e
        except APIConnectionError:
            raise  # 服务器整体不通，终止回退链
        except APIStatusError as e:
            if e.status_code in (401, 403):
                raise  # 鉴权失败，终止回退链
            hint = {404: "模型不存在", 429: "频率超限", 500: "内部错误", 502: "网关错误", 503: "服务不可用"}.get(
                e.status_code, f"HTTP {e.status_code}"
            )
            logger.warning(f"[LLM] model={model_name} {hint}，{_next_tip}")
            last_error = e
        except Exception as e:
            logger.warning(f"[LLM] model={model_name} 异常: {e}，{_next_tip}")
            last_error = e

    logger.error(f"[LLM] 所有模型均不可用 {all_models}，最后错误: {last_error}")
    raise last_error


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
        raw = _llm_call([{"role": "system", "content": PROMPT_HR}, {"role": "user", "content": content}])
        if not raw or not raw.strip():
            logger.error(f"[解析失败] 模型返回空内容，pages={total_pages}，请检查模型服务或 prompt 配置")
            raise ValueError("LLM 返回空响应")
        parsed = json_repair.loads(raw)
        if not parsed:
            logger.error(f"[解析失败] JSON 修复后仍为空，原始输出片段: {raw[:200]!r}")
            raise ValueError("JSON 解析结果为空")
        result = _post_process(parsed)

    else:
        batch_sz = smart_batch_size(total_pages)
        batches  = pdf_to_batches(source, batch_size=batch_sz)
        if verbose:
            logger.debug(f"{total_pages}页 → {len(batches)}批（每批{batch_sz}页）")

        model_name = get_current_model_name()

        def _infer_batch(batch: list) -> dict:
            content = []
            for page_idx, img in batch:
                content.append({"type": "text", "text": f"【第 {page_idx+1}/{total_pages} 页】"})
                content.append({"type": "image_url", "image_url": {"url": pil_to_base64(img), "detail": "high"}})
            content.append({"type": "text", "text": "请按指定格式提取以上页面的简历信息，直接输出JSON。"})
            # ContextVar 是线程局部的；线程池中每个线程必须独立设置当前模型名，不能依赖外层协程的值
            token = set_current_model_name(model_name)
            try:
                raw = _llm_call([{"role": "system", "content": BATCH_SYSTEM}, {"role": "user", "content": content}])
                if not raw or not raw.strip():
                    logger.warning(f"[解析失败] 分批推理返回空内容，页码={[idx+1 for idx, _ in batch]}")
                    return {}
                parsed = json_repair.loads(raw)
                if not parsed:
                    logger.warning(f"[解析失败] 分批 JSON 解析为空，页码={[idx+1 for idx, _ in batch]}，原始片段: {raw[:200]!r}")
                    return {}
                return parsed
            except Exception as e:
                logger.error(f"[解析失败] 分批推理异常，页码={[idx+1 for idx, _ in batch]}: {e}")
                return {}
            finally:
                reset_current_model_name(token)

        workers = min(MAX_WORKERS, len(batches))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_infer_batch, batches))

        result = _post_process(_merge_results(results))

    result, report = _validate_schema(result)
    if not result.get("candidate", {}).get("name"):
        logger.warning(f"[解析结果] 候选人姓名为空，可能解析质量较低，建议人工复核")

    return result


# ── 日志辅助 ──────────────────────────────────────────────────────

def _log_result(result: dict, filename: str, elapsed: float):
    c     = result.get("candidate", {})
    edus  = result.get("educations", [])
    works = result.get("works", [])
    projs = result.get("projects", [])

    logger.info(
        f"[解析完成] {filename} | 耗时={elapsed:.2f}s | "
        f"姓名={c.get('name', '-')} 教育={len(edus)}条 工作={len(works)}条 项目={len(projs)}条"
    )
    for line in json.dumps(result, ensure_ascii=False, indent=2).splitlines():
        logger.info(line)


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
