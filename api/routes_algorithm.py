"""
算法内部接口：将打标各阶段独立暴露，供内部服务/调试使用。
可跳过解析直接对已有 resume JSON 重跑打标；生产环境对外请通过 /api/resume/async/* 接口。
"""
import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.response_schemas import (
    HttpErrorResponse,
    PositiveAlgorithmResponse,
    SensitiveAlgorithmResponse,
)
from core.parser import _call_llm_with_fallback
from core.tasks import executor
from logger.logger_manager import wrap_for_executor

router = APIRouter(tags=["算法调试"])


@router.post(
    "/algorithm/positive-tag/extract",
    response_model=PositiveAlgorithmResponse,
    responses={400: {"model": HttpErrorResponse, "description": "缺少简历数据"}},
)
async def algorithm_positive_tag(request: dict):
    """对已结构化简历执行正向 LLM 标签提取。

    Args:
        request (dict): 包含必填 `resume` 和可选 `jobInfo` 的调试请求。

    Returns:
        JSONResponse: 正向标签结果；`resume` 缺失或为空时返回 HTTP 400。

    Note:
        该接口跳过 PDF 解析，仅用于算法阶段调试。
    """
    from tag.tagger import extract_positive_tags_llm
    resume   = request.get("resume", {})
    job_info = request.get("jobInfo", {})
    if not resume:
        return JSONResponse(status_code=400, content={"error": "缺少 resume 字段"})
    result = await asyncio.get_running_loop().run_in_executor(
        executor,
        wrap_for_executor(
            lambda: extract_positive_tags_llm(
                resume, job_info, _call_llm_with_fallback
            )
        ),
    )
    return JSONResponse(content=result)


@router.post(
    "/algorithm/sensitive-tag/detect",
    response_model=SensitiveAlgorithmResponse,
    responses={400: {"model": HttpErrorResponse, "description": "缺少简历数据"}},
)
async def algorithm_sensitive_tag(request: dict):
    """对已结构化简历执行纯规则风险标签检测。

    Args:
        request (dict): 包含必填 `resume` 和可选 `jobInfo` 的调试请求。

    Returns:
        JSONResponse: 风险命中分组、命中数和扣分；`resume` 缺失或
            为空时返回 HTTP 400。

    Note:
        该入口不执行 `detectionType=llm` 的负向维度。
    """
    from tag.tagger import detect_sensitive_tags
    resume   = request.get("resume", {})
    job_info = request.get("jobInfo", {})
    if not resume:
        return JSONResponse(status_code=400, content={"error": "缺少 resume 字段"})
    return JSONResponse(content=detect_sensitive_tags(resume, job_info))
