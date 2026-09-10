"""
简历解析核心模块。

职责：
  - 维护全局 OpenAI 兼容客户端（单例）
  - _call_llm_with_fallback：统一 LLM 调用入口，自动处理模型回退
  - parse_pdf_with_visual_llm：PDF 页面视觉解析 → 结构化 JSON
  - parse_docx_images_with_visual_llm：DOCX 页面视觉解析 → 结构化 JSON
  - parse_image_with_visual_llm：JPEG/PNG 视觉解析 → 结构化 JSON
  - parse_sync_resume_document：PDF/DOCX 文本优先，独立图片直接视觉解析
  - 扫描 PDF/图片型 DOCX：无文本时抽取页面或正文图片并执行视觉降级
      ≤ PDF_MAX_PAGES_ONCE 页：单批一次性调用
      > PDF_MAX_PAGES_ONCE 页：分批并发推理 + 智能合并

推理策略：
  - 图像渲染采用 144 DPI，平衡清晰度与 token 消耗
  - 批量路径使用 ThreadPoolExecutor，并发数受 MAX_WORKERS 限制

思考模式：
  - extra_body 传入 _THINKING_DISABLE_PARAMS，关闭 CoT 推理链
  - 简历解析请求使用稳定 prompt_cache_key，网关不支持时自动无缓存重试
  - thinking block 签名校验失败时自动去掉 extra_body 重试
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import json_repair
from openai import OpenAI, APIStatusError, APIConnectionError, APITimeoutError

from logger.langfuse_client import observe, update_current_observation
from config import (
    MODEL_SERVICE_URL, API_KEY, MODEL_NAME, TEMPERATURE, MAX_TOKENS,
    API_TIMEOUT, MAX_RETRIES, MAX_WORKERS, PDF_MAX_PAGES_ONCE,
    UPLOAD_DIR, RESULT_DIR, FALLBACK_MODELS, LLM_PREFIX_CACHE_ENABLED,
    get_current_model_name, get_thinking_disable_params,
    reset_current_model_name, set_current_model_name, wrap_for_executor,
)
from logger.logger_manager import logger
from logger.operation import log_operation, record_llm_attempt, record_llm_usage
from core.prompting.resume_parsing import (
    BATCH_SYSTEM,
    PROMPT_HR,
    RESUME_PROMPT_CACHE_KEY,
    build_text_resume_user_prompt,
    get_resume_prompt_bundle,
)
from core.document_text import (
    DocumentTextUnavailableError,
    detect_document_type,
    extract_document_text,
    extract_standalone_image,
    is_low_entropy_cjk_text,
    render_docx_pages,
)
from core.resume_contract import normalize_resume_payload
from core.resume_hints import extract_deterministic_resume_hints
from utils.pdf_utils import pdf_to_images, smart_batch_size, pil_to_base64
from core.postprocess import _post_process, _validate_schema, _merge_results
from utils.file_utils import get_document_source_from_content, normalize_filename

# LLM 客户端

client = OpenAI(
    base_url=MODEL_SERVICE_URL,
    api_key=API_KEY,
    timeout=API_TIMEOUT,
    max_retries=MAX_RETRIES,
)

# 默认模型保留为兜底，具体请求可通过 config 的模型上下文覆盖。


@dataclass(frozen=True)
class ParsedResumeDocument:
    """保存解析结果及仅供后续打标使用的内部证据。

    Attributes:
        resume (dict): 符合公开契约的结构化简历，不包含内部辅助字段。
        skill_evidence_text (str): Python 抽取的完整文档文本，仅用于技能证据校验。

    Note:
        该对象只在同步解析与打标流水线内部传递。API 响应、结果文件和完整结果
        日志只使用 ``resume``，不得序列化整个对象。
    """

    resume: dict
    skill_evidence_text: str = ""


def _is_prompt_cache_unsupported(exc: APIStatusError) -> bool:
    """判断模型网关是否因不支持前缀缓存字段而拒绝请求。

    Args:
        exc (APIStatusError): OpenAI SDK 返回的 HTTP 状态异常。

    Returns:
        bool: 400/422 响应明确提及未知缓存字段时返回 ``True``。
    """
    if exc.status_code not in {400, 422}:
        return False
    body = getattr(exc, "body", None)
    detail = f"{exc} {body}".lower()
    cache_field_mentioned = (
        "prompt_cache_key" in detail
        or "prompt cache key" in detail
    )
    unsupported_marker = any(marker in detail for marker in (
        "unknown",
        "unsupported",
        "unrecognized",
        "unexpected",
        "extra_forbidden",
        "extra inputs are not permitted",
    ))
    return cache_field_mentioned and unsupported_marker


def _cached_prompt_tokens(usage) -> int | None:
    """读取兼容 OpenAI usage 结构的缓存 Prompt token 数。

    Args:
        usage: OpenAI SDK 返回的 token 用量对象。

    Returns:
        int | None: 服务端返回的缓存 token 数；未提供该指标时返回 ``None``。
    """
    details = getattr(usage, "prompt_tokens_details", None)
    if isinstance(details, dict):
        value = details.get("cached_tokens")
    else:
        value = getattr(details, "cached_tokens", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _call_llm_model_once(
    messages: list,
    model_name: str,
    prompt_cache_key: str | None = None,
    *,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """调用单个模型，并兼容 thinking block 签名失败重试。

    Args:
        messages (list): OpenAI Chat Completions 消息列表。
        model_name (str): 本次尝试的确切模型名。
        prompt_cache_key (str | None): 简历解析固定前缀的稳定缓存键；为空时不请求
            服务端前缀缓存路由。
        timeout (float | None): 管理探测的单次超时；指定时关闭 SDK 自动重试。
        max_tokens (int | None): 管理探测的输出上限；缺省保持业务配置。

    Returns:
        str: 模型第一个 choice 的文本内容。

    Raises:
        APITimeoutError: 单模型调用超时时抛出，由上层决定回退。
        APIConnectionError: 模型服务整体无法连接时抛出。
        APIStatusError: 模型服务返回非成功 HTTP 状态时抛出。
        Exception: thinking 参数重试仍失败或其他 SDK 异常时抛出。
    """
    # 管理页探测沿用真实业务参数构造，但限制超时和输出、不执行 SDK 自动重试。
    active_client = client.with_options(timeout=timeout, max_retries=0) if timeout else client
    cache_key = (
        prompt_cache_key
        if LLM_PREFIX_CACHE_ENABLED and prompt_cache_key
        else None
    )
    with log_operation(
        "LLM单模型调用",
        model=model_name,
        prefixCacheRequested=bool(cache_key),
        level="DEBUG",
        failure_level="DEBUG",
        emit_start=False,
    ) as operation:
        kwargs = dict(
            model=model_name,
            messages=messages,
            temperature=TEMPERATURE,
            max_tokens=max_tokens if max_tokens is not None else MAX_TOKENS,
            extra_body=get_thinking_disable_params(model_name),
        )
        if cache_key:
            kwargs["prompt_cache_key"] = cache_key
        while True:
            try:
                record_llm_attempt(model_name)
                resp = active_client.chat.completions.create(**kwargs)
                break
            except APITimeoutError:
                raise
            except APIConnectionError as exc:
                # 单模型连接失败仍可能由模型回退或规则打标恢复。这里只记录
                # WARNING，最终无法恢复时由外层任务边界输出唯一 ERROR 与堆栈。
                logger.warning(
                    f"[LLM] model={model_name} 无法连接 {MODEL_SERVICE_URL}: {exc}"
                )
                raise
            except APIStatusError as exc:
                if "prompt_cache_key" in kwargs and _is_prompt_cache_unsupported(exc):
                    logger.warning(
                        f"[LLM] model={model_name} 模型网关不支持 prompt_cache_key，"
                        "已关闭本次前缀缓存参数并重试"
                    )
                    kwargs.pop("prompt_cache_key", None)
                    operation.add(prefixCacheFallback=True)
                    continue
                if exc.status_code in (401, 403):
                    hint = (
                        "认证失败，请检查 API_KEY 配置"
                        if exc.status_code == 401
                        else "无权限，请检查 API_KEY 或账户权限"
                    )
                    logger.error(f"[LLM] model={model_name} {hint}: {exc.message}")
                raise
            except Exception as exc:
                error_text = str(exc)
                thinking_params_enabled = bool(kwargs.get("extra_body"))
                if thinking_params_enabled and (
                    "thinking block" in error_text
                    or "Invalid signature" in error_text
                ):
                    logger.warning(
                        f"[LLM] model={model_name} thinking block 错误，"
                        "去掉 thinking 参数重试"
                    )
                    kwargs["extra_body"] = {}
                    operation.add(thinkingRetry=True)
                    continue
                raise
        usage = resp.usage
        input_tokens = output_tokens = total_tokens = cached_tokens = 0
        if usage:
            input_tokens = usage.prompt_tokens or 0
            output_tokens = usage.completion_tokens or 0
            total_tokens = usage.total_tokens or 0
            usage_fields = dict(
                prompt=input_tokens,
                completion=output_tokens,
                total=total_tokens,
            )
            cached_tokens = _cached_prompt_tokens(usage) or 0
            if cached_tokens:
                usage_fields["cachedPrompt"] = cached_tokens
            operation.add(**usage_fields)
        record_llm_usage(
            model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=cached_tokens,
        )
        return resp.choices[0].message.content


def _llm_try_model(messages: list, model_name: str) -> str:
    """通过旧私有函数名执行指定模型的单次调用。

    Args:
        messages (list): OpenAI Chat Completions 消息列表。
        model_name (str): 本次调用指定的模型名称。

    Returns:
        str: 指定模型返回的文本内容。

    Raises:
        Exception: 模型请求失败时保留原异常抛出。
    """
    return _call_llm_model_once(messages, model_name)

def _sanitize_llm_messages(messages: list) -> list:
    """观测用副本：图片消息替换为占位符，base64 不上传 Langfuse。"""
    out = []
    for msg in messages:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    parts.append({"type": "text", "text": "[image:redacted]"})
                else:
                    parts.append(part)
            content = parts
        out.append({**msg, "content": content})
    return out


@observe()
def _call_llm_with_fallback(
    messages: list,
    prompt_cache_key: str | None = None,
) -> str:
    """按当前模型和备用模型顺序执行 LLM 调用。

    主模型（当前 ContextVar）失败时依次尝试 FALLBACK_MODELS；
    服务器整体不通（APIConnectionError）或鉴权失败（401/403）时不回退。

    Args:
        messages (list): OpenAI Chat Completions 消息列表。
        prompt_cache_key (str | None): 可选的固定 Prompt 前缀缓存键。

    Returns:
        str: 第一个成功模型的文本响应。

    Raises:
        APIConnectionError: 模型服务网络不可达时终止回退链并抛出。
        APIStatusError: 鉴权失败或所有模型均返回状态错误时抛出。
        Exception: 所有可用模型均失败时抛出最后一个异常。
    """
    update_current_observation(input=_sanitize_llm_messages(messages))
    from core.model_config import request_model_snapshot
    snapshot = request_model_snapshot.get()
    primary = get_current_model_name()
    fallbacks = list(snapshot.fallbackModels) if snapshot else FALLBACK_MODELS
    all_models = list(dict.fromkeys([primary] + fallbacks))
    if snapshot:
        logger.info(f"[模型选择] configVersion={snapshot.version} model={primary} fallbacks={fallbacks}")
    last_error: Exception | None = None

    for i, model_name in enumerate(all_models):
        if i == 0:
            logger.debug(f"[LLM] model={model_name} max_tokens={MAX_TOKENS}")
        else:
            logger.warning(f"[LLM] 回退到备用模型 [{i}/{len(all_models) - 1}]: {model_name}")
        _has_next = i < len(all_models) - 1
        _next_tip = "尝试下一个备用模型" if _has_next else "已无可用模型"
        try:
            result = _call_llm_model_once(
                messages,
                model_name,
                prompt_cache_key=prompt_cache_key,
            )
            update_current_observation(output=result)
            return result
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


def _llm_call(messages: list) -> str:
    """通过旧私有函数名执行带备用模型的 LLM 调用。

    Args:
        messages (list): OpenAI Chat Completions 消息列表。

    Returns:
        str: 首个成功模型返回的文本内容。

    Raises:
        Exception: 主模型和允许回退的备用模型均失败时抛出。
    """
    return _call_llm_with_fallback(messages)


# 简历解析核心

def _decode_and_validate_resume_json(raw: str, source_name: str) -> dict:
    """将模型文本转换为通过公开契约校验的简历对象并记录分段耗时。

    Args:
        raw (str): 模型返回的 JSON 或可由 `json_repair` 修复的文本。
        source_name (str): 日志中标识当前文档或视觉来源的名称。

    Returns:
        dict: 已完成字段后处理、Schema 补全和 Pydantic 契约归一的简历。

    Raises:
        ValueError: 模型文本为空，或修复后的顶层不是非空对象时抛出。
        ResumeContractError: 归一后的字段类型仍不符合公开契约时抛出。

    Note:
        `jsonDecodeSeconds` 只计算文本到 Python 对象的时间；
        `postProcessSeconds` 包含字段后处理、Schema 补全和契约校验。
    """
    if not raw or not raw.strip():
        raise ValueError("LLM 返回空响应")
    with log_operation(
        "本地JSON解码与契约校验",
        source=source_name,
        responseCharacters=len(raw),
        level="DEBUG",
        failure_level="DEBUG",
        emit_start=False,
    ) as operation:
        conversion_started = time.perf_counter()
        decode_started = time.perf_counter()
        parsed = json_repair.loads(raw)
        decode_seconds = time.perf_counter() - decode_started
        if not isinstance(parsed, dict) or not parsed:
            raise ValueError("LLM 返回的 Resume JSON 无效")

        post_process_started = time.perf_counter()
        result = _post_process(parsed)
        result, schema_report = _validate_schema(result)
        result = normalize_resume_payload(result)
        post_process_seconds = time.perf_counter() - post_process_started
        operation.add(
            jsonDecodeDurationMs=round(decode_seconds * 1000, 3),
            postProcessDurationMs=round(post_process_seconds * 1000, 3),
            conversionDurationMs=round(
                (time.perf_counter() - conversion_started) * 1000,
                3,
            ),
            schemaFixes=len(schema_report.get("fixes", [])),
            educations=len(result["educations"]),
            works=len(result["works"]),
            projects=len(result["projects"]),
        )
        return result


# 保留旧私有名称，避免现有内部测试或扩展代码在升级时立即中断。
_convert_llm_json_to_resume = _decode_and_validate_resume_json


def _parse_visual_images_with_llm(
    images: list,
    source_name: str,
    verbose: bool = False,
) -> dict:
    """通过统一视觉模型流程解析按阅读顺序排列的简历图片。

    Args:
        images (list[Image.Image]): PDF 页面或 DOCX 正文中按顺序提取的图片。
        source_name (str): 日志中标识图片来源的文件名或类型。
        verbose (bool): 是否记录图片数量和分批调试信息。

    Returns:
        dict: 已归一的标准简历对象。

    Raises:
        ValueError: 图片为空、模型响应为空或 JSON 无效时抛出。

    Note:
        图片数量超过单次上限时按原顺序分批并发推理，最后复用 PDF 的确定性
        合并和 Schema 校验逻辑。
    """
    total_pages = len(images)
    if total_pages == 0:
        raise ValueError("视觉解析没有可用图片")
    if verbose:
        logger.debug(f"视觉解析 {source_name} images={total_pages}")

    prompt_hr, batch_system, prompt_cache_key = get_resume_prompt_bundle()
    indexed_images = list(enumerate(images))
    if total_pages <= PDF_MAX_PAGES_ONCE:
        content = []
        for i, img in indexed_images:
            if total_pages > 1:
                content.append({"type": "text", "text": f"【第 {i+1}/{total_pages} 页】"})
            content.append({"type": "image_url", "image_url": {"url": pil_to_base64(img), "detail": "high"}})
        content.append({"type": "text", "text": "请将以上简历内容严格按指定JSON格式输出，直接输出JSON。"})
        raw = _call_llm_with_fallback(
            [
                {"role": "system", "content": prompt_hr},
                {"role": "user", "content": content},
            ],
            prompt_cache_key=prompt_cache_key,
        )
        if not raw or not raw.strip():
            logger.error(f"[解析失败] 模型返回空内容，pages={total_pages}，请检查模型服务或 prompt 配置")
            raise ValueError("LLM 返回空响应")
        result = _decode_and_validate_resume_json(raw, source_name)

    else:
        batch_sz = smart_batch_size(total_pages)
        batches = [
            indexed_images[index:index + batch_sz]
            for index in range(0, total_pages, batch_sz)
        ]
        if verbose:
            logger.debug(
                f"{source_name} images={total_pages} batches={len(batches)} "
                f"batchSize={batch_sz}"
            )

        model_name = get_current_model_name()

        def _infer_batch(batch: list) -> dict:
            """推理长 PDF 的单个页面批次，失败时返回空结果。

            Args:
                batch (list): `(page_index, PIL.Image)` 页面元组列表。

            Returns:
                dict: 模型提取的局部简历对象；该批次失败时返回空字典。
            """
            content = []
            for page_idx, img in batch:
                content.append({"type": "text", "text": f"【第 {page_idx+1}/{total_pages} 页】"})
                content.append({"type": "image_url", "image_url": {"url": pil_to_base64(img), "detail": "high"}})
            content.append({"type": "text", "text": "请按指定格式提取以上页面的简历信息，直接输出JSON。"})
            # ContextVar 是线程局部的；线程池中每个线程必须独立设置当前模型名，不能依赖外层协程的值
            token = set_current_model_name(model_name)
            try:
                raw = _call_llm_with_fallback(
                    [
                        {"role": "system", "content": batch_system},
                        {"role": "user", "content": content},
                    ],
                    prompt_cache_key=prompt_cache_key,
                )
                if not raw or not raw.strip():
                    logger.warning(f"[解析失败] 分批推理返回空内容，页码={[idx+1 for idx, _ in batch]}")
                    return {}
                parsed = json_repair.loads(raw)
                if not parsed:
                    logger.warning(
                        f"[解析失败] 分批 JSON 解析为空，"
                        f"页码={[idx+1 for idx, _ in batch]} responseCharacters={len(raw)}"
                    )
                    return {}
                return parsed
            except Exception as e:
                logger.error(f"[解析失败] 分批推理异常，页码={[idx+1 for idx, _ in batch]}: {e}")
                return {}
            finally:
                reset_current_model_name(token)

        workers = min(MAX_WORKERS, len(batches))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # 每个批次必须捕获独立 Context；同一个 Context 不能被并发线程重复进入。
            futures = [
                pool.submit(wrap_for_executor(_infer_batch), batch)
                for batch in batches
            ]
            results = [future.result() for future in futures]

        result = _post_process(_merge_results(results))

    if total_pages > PDF_MAX_PAGES_ONCE:
        result, report = _validate_schema(result)
        result = normalize_resume_payload(result)
    if not result.get("candidate", {}).get("name"):
        logger.warning(f"[解析结果] 候选人姓名为空，可能解析质量较低，建议人工复核")

    return result


@observe()
def parse_pdf_with_visual_llm(source, verbose: bool = False) -> dict:
    """将 PDF 渲染为图像，通过 LLM 提取并归一为结构化简历。

    Args:
        source (bytes | str | Path): PDF 文件字节或本地路径。
        verbose (bool): 是否记录页数和分批调试信息。

    Returns:
        dict: 包含 `candidate`、`educations`、`works` 和 `projects` 的
            结构化简历。

    Raises:
        ValueError: PDF 加密、LLM 返回空内容或 JSON 修复后仍为空时抛出。
        Exception: PDF 渲染、模型调用或结果解析失败时向上传递。
    """
    result = _parse_visual_images_with_llm(
        pdf_to_images(source),
        source_name="PDF",
        verbose=verbose,
    )
    update_current_observation(
        input={"sourceType": "pdf"},
        output={"resume": result},
    )
    return result


@observe()
def parse_docx_images_with_visual_llm(
    source,
    filename: str = "resume.docx",
    verbose: bool = False,
) -> dict:
    """把 DOCX 正文渲染为页面图片并通过视觉模型生成简历 JSON。

    Args:
        source: DOCX 文件路径、字节数据或文件对象。
        filename (str): 日志和安全校验使用的文件名。
        verbose (bool): 是否记录图片数量和分批调试信息。

    Returns:
        dict: 已归一的标准简历对象。

    Raises:
        DocumentExtractionError: DOCX 页面渲染或资源预算无效时抛出。
        DocumentTextUnavailableError: DOCX 没有可用于视觉解析的页面时抛出。
        ValueError: 模型响应为空或 JSON 无效时抛出。
    """
    images = render_docx_pages(source, filename)
    result = _parse_visual_images_with_llm(
        images,
        source_name="DOCX",
        verbose=verbose,
    )
    update_current_observation(
        input={"sourceType": "docx", "filename": filename},
        output={"resume": result},
    )
    return result


@observe()
def parse_image_with_visual_llm(
    source,
    filename: str = "resume.png",
    verbose: bool = False,
) -> dict:
    """校验独立 JPEG/PNG 简历图片并通过视觉模型生成 Resume JSON。

    Args:
        source: 图片文件路径、字节数据或文件对象。
        filename (str): 用于真实格式校验和日志展示的文件名。
        verbose (bool): 是否记录图片数量和视觉解析调试信息。

    Returns:
        dict: 已归一的标准简历对象。

    Raises:
        DocumentExtractionError: 图片损坏、伪装或超过资源预算时抛出。
        ValueError: 模型响应为空或 JSON 无效时抛出。
    """
    image = extract_standalone_image(source, filename)
    result = _parse_visual_images_with_llm(
        [image],
        source_name="IMAGE",
        verbose=verbose,
    )
    update_current_observation(
        input={"sourceType": "image", "filename": filename},
        output={"resume": result},
    )
    return result


# 保留旧通用名称，避免现有异步视觉解析调用方中断。
run = parse_pdf_with_visual_llm


@observe()
def parse_sync_resume_document(
    source,
    filename: str = "resume.pdf",
    include_skill_evidence: bool = False,
    operation_name: str = "同步解析",
) -> dict | ParsedResumeDocument:
    """按文本优先和视觉降级策略解析文档或图片简历。

    Args:
        source: PDF、DOCX、JPEG 或 PNG 文件路径、字节数据或文件对象。
        filename (str): 用于类型校验和日志展示的文件名。
        include_skill_evidence (bool): 是否返回含原文证据的内部解析对象。
        operation_name (str): 方法边界日志使用的同步或异步业务名称。

    Returns:
        dict | ParsedResumeDocument: 默认返回完整结构化简历；内部解析与打标入口
            请求证据时返回 ``ParsedResumeDocument``。

    Raises:
        DocumentExtractionError: 文档损坏、加密、类型不符或超过预算时抛出。
        DocumentTextUnavailableError: DOCX 没有可读文本时抛出。
        ValueError: LLM 返回空内容或无效简历 JSON 时抛出。

    Note:
        普通文档先由 Python 库抽取文本，再由单次 LLM 统一生成 JSON。有效 PDF
        没有文本层时渲染页面；DOCX 没有文本时提取正文图片；独立图片通过安全
        校验后直接进入视觉解析。
    """
    display_filename = normalize_filename(filename)
    with log_operation(
        operation_name,
        file=display_filename,
        emit_start=False,
        failure_level="DEBUG",
    ) as operation:
        try:
            document = extract_document_text(source, display_filename)
            if document.document_type in {"pdf", "image"} and not document.text:
                raise DocumentTextUnavailableError(
                    f"{document.document_type.upper()} 没有可用文本，执行视觉解析"
                )
            # 大量汉字被错误映射成少数字符时，文本虽然非空但不可用于语义解析。
            if is_low_entropy_cjk_text(document.text):
                raise DocumentTextUnavailableError(
                    f"{document.document_type.upper()} 抽取文本汉字信息熵过低，执行视觉解析"
                )
        except DocumentTextUnavailableError as exc:
            suffix = Path(display_filename).suffix.lower()
            if suffix == ".pdf":
                document_type = "pdf"
                fallback_name = "扫描PDF视觉降级"
                fallback_parser = lambda: parse_pdf_with_visual_llm(
                    source,
                    verbose=True,
                )
            elif suffix == ".docx":
                document_type = "docx"
                fallback_name = "图片DOCX视觉降级"
                fallback_parser = lambda: parse_docx_images_with_visual_llm(
                    source,
                    display_filename,
                    verbose=True,
                )
            elif suffix in {".jpg", ".jpeg", ".png"}:
                document_type = "image"
                fallback_name = "简历图片视觉解析"
                fallback_parser = lambda: parse_image_with_visual_llm(
                    source,
                    display_filename,
                    verbose=True,
                )
            else:
                raise
            with log_operation(
                fallback_name,
                file=display_filename,
                reason=str(exc),
                level="WARNING",
                failure_level="DEBUG",
                emit_start=False,
            ) as fallback_operation:
                result = fallback_parser()
                fallback_operation.add(
                    educations=len(result["educations"]),
                    works=len(result["works"]),
                    projects=len(result["projects"]),
                )
            operation.add(
                documentType=document_type,
                parserMode="visualFallback",
                fallbackReason=str(exc),
                educations=len(result["educations"]),
                works=len(result["works"]),
                projects=len(result["projects"]),
            )
            if include_skill_evidence:
                return ParsedResumeDocument(resume=result)
            return result
        deterministic_hints = extract_deterministic_resume_hints(
            document.text,
            display_filename,
        )
        hints_payload = deterministic_hints.to_prompt_payload()
        user_content = build_text_resume_user_prompt(
            document.text,
            hints_payload,
        )
        prompt_hr, _batch_system, prompt_cache_key = get_resume_prompt_bundle()
        with log_operation(
            "LLM简历语义结构化",
            file=display_filename,
            documentType=document.document_type,
            requestedModel=get_current_model_name(),
            prefixCacheEnabled=LLM_PREFIX_CACHE_ENABLED,
            promptCacheKey=(
                prompt_cache_key
                if LLM_PREFIX_CACHE_ENABLED
                else None
            ),
            emit_start=False,
            failure_level="DEBUG",
        ) as llm_operation:
            raw = _call_llm_with_fallback(
                [
                    {"role": "system", "content": prompt_hr},
                    {"role": "user", "content": user_content},
                ],
                prompt_cache_key=prompt_cache_key,
            )
            if not raw or not raw.strip():
                raise ValueError("LLM 返回空响应")
            llm_operation.add(responseCharacters=len(raw))

        result = _decode_and_validate_resume_json(raw, display_filename)
        operation.add(
            documentType=document.document_type,
            parserMode="textFirst",
            extractor=document.extractor,
            characters=document.character_count,
            llmCallCount=1,
            deterministicCandidates=sum(
                len(value)
                for value in hints_payload.values()
            ),
            educations=len(result["educations"]),
            works=len(result["works"]),
            projects=len(result["projects"]),
        )
        if include_skill_evidence:
            update_current_observation(
                input={"filename": display_filename},
                output={"resume": result, "skillEvidenceText": document.text},
            )
            return ParsedResumeDocument(
                resume=result,
                skill_evidence_text=document.text,
            )
        update_current_observation(
            input={"filename": display_filename},
            output={"resume": result},
        )
        return result


# 解析结果日志

def _log_resume_parse_result(result: dict, filename: str, elapsed: float):
    """记录不含候选人正文和身份字段的解析摘要。

    Args:
        result (dict): 已归一的结构化简历。
        filename (str): 日志中的简历文件名。
        elapsed (float): 解析耗时，单位为秒。

    Note:
        完整简历由结果存储和接口响应承载，不进入普通日志。
    """
    with log_operation(
        "解析结果日志",
        file=filename,
        level="DEBUG",
        failure_level="DEBUG",
        emit_start=False,
    ) as operation:
        edus = result.get("educations", [])
        works = result.get("works", [])
        projs = result.get("projects", [])
        operation.add(
            parseDurationMs=round(elapsed * 1000),
            educations=len(edus),
            works=len(works),
            projects=len(projs),
        )


def _log_result(result: dict, filename: str, elapsed: float):
    """通过旧私有函数名记录解析摘要和完整结果。

    Args:
        result (dict): 已归一的结构化简历。
        filename (str): 日志中的简历文件名。
        elapsed (float): 解析耗时，单位为秒。
    """
    return _log_resume_parse_result(result, filename, elapsed)


def parse_async_visual_resume_with_logging(source, filename: str = "unknown.pdf") -> dict:
    """按真实文档类型执行异步视觉解析并记录耗时与结果。

    Args:
        source (bytes | str | Path): PDF、DOCX、JPEG 或 PNG 来源。
        filename (str): 日志和结果关联使用的文件名。

    Returns:
        dict: 已归一的结构化简历。
    """
    display_filename = normalize_filename(filename)
    with log_operation(
        "异步视觉解析",
        file=display_filename,
        emit_start=False,
        failure_level="DEBUG",
    ) as operation:
        t0 = time.monotonic()
        document_type = detect_document_type(source, display_filename)
        if document_type == "pdf":
            result = parse_pdf_with_visual_llm(source, verbose=True)
            renderer = "PyMuPDF"
        elif document_type == "docx":
            result = parse_docx_images_with_visual_llm(
                source,
                display_filename,
                verbose=True,
            )
            renderer = "Mammoth+PyMuPDF"
        else:
            result = parse_image_with_visual_llm(
                source,
                display_filename,
                verbose=True,
            )
            renderer = "Pillow"
        elapsed = time.monotonic() - t0
        _log_resume_parse_result(result, display_filename, elapsed)
        operation.add(
            documentType=document_type,
            renderer=renderer,
            educations=len(result["educations"]),
            works=len(result["works"]),
            projects=len(result["projects"]),
        )
        return result


def parse_async_resume_with_logging(source, filename: str = "resume.pdf") -> dict:
    """执行异步任务使用的多格式视觉解析并记录结果。

    Args:
        source: PDF、DOCX、JPEG 或 PNG 文件路径、字节数据或文件对象。
        filename (str): 用于日志、类型校验和结果关联的文件名。

    Returns:
        dict: 视觉模型解析并经统一契约归一的结构化简历。

    Raises:
        Exception: 文档渲染或视觉 LLM 解析失败时保留原异常抛出。
    """
    return parse_async_visual_resume_with_logging(source, filename)


def parse_async_resume_for_tagging_with_logging(
    source,
    filename: str = "resume.pdf",
) -> ParsedResumeDocument:
    """兼容旧入口执行异步多格式视觉解析，不生成 Python 原文证据。

    Args:
        source: PDF、DOCX、JPEG 或 PNG 文件路径、字节数据或文件对象。
        filename (str): 用于日志、类型校验和结果关联的文件名。

    Returns:
        ParsedResumeDocument: 公开简历对象；`skill_evidence_text` 固定为空。

    Raises:
        Exception: 文档渲染或视觉 LLM 解析失败时保留原异常抛出。

    Note:
        异步链不调用 Python 文本抽取，因此内部证据始终为空。
    """
    return ParsedResumeDocument(
        resume=parse_async_visual_resume_with_logging(source, filename),
    )


def parse_sync_resume_with_logging(source, filename: str = "resume.pdf") -> dict:
    """执行同步文本优先解析并记录完整结果和方法摘要。

    Args:
        source: PDF、DOCX、JPEG 或 PNG 文件路径、字节数据或文件对象。
        filename (str): 用于类型校验、日志和结果关联的文件名。

    Returns:
        dict: 已通过统一契约归一的完整结构化简历。

    Raises:
        Exception: 文档抽取、视觉降级或 LLM 统一 JSON 失败时保留原异常抛出。
    """
    display_filename = normalize_filename(filename)
    with log_operation(
        "同步解析调用",
        file=display_filename,
        level="DEBUG",
        failure_level="DEBUG",
        emit_start=False,
    ) as operation:
        t0 = time.monotonic()
        result = parse_sync_resume_document(source, display_filename)
        elapsed = time.monotonic() - t0
        _log_resume_parse_result(result, display_filename, elapsed)
        operation.add(
            educations=len(result["educations"]),
            works=len(result["works"]),
            projects=len(result["projects"]),
        )
        return result


def parse_sync_resume_for_tagging_with_logging(
    source,
    filename: str = "resume.pdf",
) -> ParsedResumeDocument:
    """执行同步解析并保留后续技能校验所需的内部原文证据。

    Args:
        source: PDF、DOCX、JPEG 或 PNG 文件路径、字节数据或文件对象。
        filename (str): 用于类型校验、日志和结果关联的文件名。

    Returns:
        ParsedResumeDocument: 公开简历对象和仅供打标使用的原文证据。

    Raises:
        Exception: 文档抽取、视觉降级或 LLM 统一 JSON 失败时保留原异常抛出。

    Note:
        扫描 PDF 和图片型 DOCX 的视觉降级路径没有 Python 原文，内部证据为空
        字符串。完整结果日志仍只输出公开 ``resume``，不会暴露内部证据字段。
    """
    display_filename = normalize_filename(filename)
    with log_operation(
        "同步解析并保留打标证据",
        file=display_filename,
        level="DEBUG",
        failure_level="DEBUG",
        emit_start=False,
    ) as operation:
        t0 = time.monotonic()
        parsed_document = parse_sync_resume_document(
            source,
            display_filename,
            include_skill_evidence=True,
        )
        elapsed = time.monotonic() - t0
        result = parsed_document.resume
        _log_resume_parse_result(result, display_filename, elapsed)
        operation.add(
            educations=len(result["educations"]),
            works=len(result["works"]),
            projects=len(result["projects"]),
            skillEvidenceCharacters=len(parsed_document.skill_evidence_text),
        )
        return parsed_document


# 保留旧私有名称，避免现有路由和测试调用方中断。
run_sync_document = parse_sync_resume_document
_parse_with_logging = parse_async_visual_resume_with_logging
_parse_sync_with_logging = parse_sync_resume_with_logging


# 结果文件维护

def _cleanup_uploads(max_age_hours: int = 24 * 15):
    """删除上传目录中超过保留期的普通文件。

    Args:
        max_age_hours (int): 文件最大保留小时数，默认为 15 天。

    Note:
        单个文件删除失败只记录 warning，不中断其他文件清理。
    """
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


def _save_resume_result(task_id: str, filename: str, result: dict) -> str | None:
    """将完整解析打标结果保存为本地 JSON 文件。

    Args:
        task_id (str): 用于生成唯一文件名的任务 UUID。
        filename (str): 原始简历文件名，仅安全字符保留在结果文件名中。
        result (dict): 要持久化的完整结果。

    Note:
        写入失败只记录 warning，不改变主任务的成功状态。
    """
    display_filename = normalize_filename(filename)
    with log_operation(
        "保存处理结果",
        taskId=task_id,
        file=display_filename,
        level="DEBUG",
        emit_start=False,
    ) as operation:
        stem = Path(display_filename).stem[:40]
        safe_stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
        safe_stem = re.sub(r"_+", "_", safe_stem).strip("_.") or "resume"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = RESULT_DIR / f"{ts}_{safe_stem}_{task_id[:8]}.json"
        try:
            with open(out_path, "w", encoding="utf-8") as file:
                json.dump(result, file, ensure_ascii=False, indent=2)
        except Exception as exc:
            operation.mark_failed(exc)
            logger.warning(f"结果保存失败: {exc}")
            return None
        logger.debug(f"结果已保存: {out_path.name}")
        operation.add(resultFile=out_path.name)
        return out_path.name


# 保留旧私有名称，避免现有结果保存调用方中断。
_save_result = _save_resume_result


# 批量处理

def _process_one(item: dict) -> dict:
    """执行单个批量工作项并将异常转为按项结果。

    Args:
        item (dict): 包含 `_source`、`index` 和 `filename` 的内部工作项。

    Returns:
        dict: 输入索引、文件名、成功标识、耗时以及结果或错误。
    """
    source = item.get("_source")
    t0     = time.time()
    try:
        result = parse_pdf_with_visual_llm(source, verbose=False)
        return {"index": item.get("index", 0), "filename": item.get("filename", ""),
                "success": True, "result": result, "elapsed_s": round(time.time() - t0, 2), "error": None}
    except Exception as e:
        return {"index": item.get("index", 0), "filename": item.get("filename", ""),
                "success": False, "result": None, "elapsed_s": round(time.time() - t0, 2), "error": str(e)}


def _batch_process(resume_items: list, max_concurrency: int) -> list:
    """并发获取并解析多个简历条目，保持输入顺序。

    Args:
        resume_items (list): OpenAI content 格式的简历来源列表。
        max_concurrency (int): 本批次使用的最大线程数。

    Returns:
        list: 与输入顺序一致的按项解析结果。
    """
    work_items = []
    for i, item in enumerate(resume_items):
        source, filename = get_document_source_from_content([item])
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
