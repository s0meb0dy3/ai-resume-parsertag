"""
算法内部接口：将打标各阶段独立暴露，供内部服务/调试使用。
可跳过解析直接对已有 resume JSON 重跑打标；生产环境对外请通过 /api/resume/async/* 接口。
"""
import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from parser import _llm_call
from tasks import executor

router = APIRouter()


@router.post("/algorithm/positive-tag/extract")
async def algorithm_positive_tag(request: dict):
    """仅执行正向标签提取，输入已结构化的 resume JSON；跳过解析阶段，用于打标逻辑调试。"""
    from tag.tagger import extract_positive_tags_llm
    resume   = request.get("resume", {})
    job_info = request.get("jobInfo", {})
    if not resume:
        return JSONResponse(status_code=400, content={"error": "缺少 resume 字段"})
    result = await asyncio.get_running_loop().run_in_executor(
        executor, lambda: extract_positive_tags_llm(resume, job_info, _llm_call)
    )
    return JSONResponse(content=result)


@router.post("/algorithm/sensitive-tag/detect")
async def algorithm_sensitive_tag(request: dict):
    """仅执行风险标签检测（纯规则，无 LLM 调用），输入已结构化的 resume JSON。"""
    from tag.tagger import detect_sensitive_tags
    resume   = request.get("resume", {})
    job_info = request.get("jobInfo", {})
    if not resume:
        return JSONResponse(status_code=400, content={"error": "缺少 resume 字段"})
    return JSONResponse(content=detect_sensitive_tags(resume, job_info))
