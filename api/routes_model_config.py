"""独立模型管理 API。共享管理凭证必须配置；不返回网关密钥或上游错误正文。"""
import base64
import io
import secrets
import time
from pathlib import Path
from threading import BoundedSemaphore
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from config import MODEL_ADMIN_TOKEN, logger
from core.model_config import ConfigConflict, ModelConfigState, ModelSelection, get_model_store
from core.parser import _call_llm_model_once, client

router = APIRouter(tags=["模型配置"])
security = HTTPBearer(auto_error=False)
_probe_slot = BoundedSemaphore(1)


def require_admin(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
    if not MODEL_ADMIN_TOKEN:
        raise HTTPException(503, "模型管理未启用，请配置 MODEL_ADMIN_TOKEN")
    if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), MODEL_ADMIN_TOKEN.encode()):
        raise HTTPException(401, "管理员凭证无效", headers={"WWW-Authenticate": "Bearer"})


class ApplyConfig(ModelSelection):
    expectedVersion: int = Field(ge=0)


class RollbackConfig(BaseModel):
    expectedVersion: int = Field(ge=0)


class ProbeRequest(ModelSelection):
    kind: Literal["text", "vision"]


class ProbeItem(BaseModel):
    model: str
    passed: bool
    seconds: float
    message: str


class ProbeResult(BaseModel):
    kind: str
    results: list[ProbeItem]


@router.get("/admin/models", include_in_schema=False)
def model_page():
    """公开静态页面壳不含配置，所有数据请求必须鉴权。"""
    return FileResponse(Path(__file__).parents[1] / "web" / "model-config.html",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})


@router.get("/admin/model-assets/{filename}", include_in_schema=False)
def model_page_asset(filename: str):
    """仅公开构建后的 CSS/JS，不暴露源码、依赖或任意磁盘文件。"""
    if filename not in {"model-config.css", "model-config.js"}:
        raise HTTPException(404, "资源不存在")
    return FileResponse(Path(__file__).parents[1] / "web" / "assets" / filename,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/api/admin/models/config", response_model=ModelConfigState, dependencies=[Depends(require_admin)])
def read_config():
    """读取生效配置与上一版；不包含网关 URL 或密钥。"""
    return get_model_store().state()


def commit_config(selection, version):
    store = get_model_store()
    try:
        state = store.apply(selection, version)
    except ConfigConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "配置持久化失败，生效配置未更改，请检查挂载目录权限和空间") from exc
    logger.info(f"[模型配置应用] actor=shared-admin version={state.current.version} "
                f"previous={state.previous.model_dump()} current={state.current.model_dump()}")
    return state


@router.put("/api/admin/models/config", response_model=ModelConfigState, dependencies=[Depends(require_admin)])
def apply_config(body: ApplyConfig):
    """保存并整体应用配置，仅影响后续请求；版本不匹配返回 409。"""
    selection = ModelSelection(**body.model_dump(exclude={"expectedVersion"}))
    return commit_config(selection, body.expectedVersion)


@router.post("/api/admin/models/rollback", response_model=ModelConfigState, dependencies=[Depends(require_admin)])
def rollback_config(body: RollbackConfig):
    """恢复上一版内容并生成新版本号，不重跑历史任务。"""
    return commit_config(None, body.expectedVersion)


@router.get("/api/admin/models/catalog", response_model=list[str], dependencies=[Depends(require_admin)])
def model_catalog():
    """从现有网关获取可选模型；不支持列表的网关仍可手动填写 ID。"""
    try:
        page = client.with_options(timeout=15, max_retries=0).models.list()
        return sorted({m.id for m in page.data})
    except Exception as exc:
        raise HTTPException(502, "无法加载网关模型列表，请手动输入模型 ID") from exc


@router.post("/api/admin/models/test", response_model=ProbeResult, dependencies=[Depends(require_admin)])
def test_config(body: ProbeRequest):
    """逐个直接测试主备模型，不回退、不修改配置；通过不代表简历业务质量已验证。"""
    if not _probe_slot.acquire(blocking=False):
        raise HTTPException(429, "已有模型测试正在进行，请稍后再试")
    try:
        # 无个人信息的随机样例；视觉验证码只存在图片中，避免靠文本提示猜中。
        expected = str(secrets.randbelow(9000) + 1000)
        if body.kind == "vision":
            image = Image.new("RGB", (240, 100), "white")
            ImageDraw.Draw(image).text((30, 25), expected, fill="black", font_size=40)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            content = [{"type": "text", "text": "只回复图片中的四位数字，不要解释。"},
                       {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()}}]
        else:
            content = f"只回复 {expected}，不要解释。"
        results = []
        for model in dict.fromkeys((body.syncModel, body.asyncModel, *body.fallbackModels)):
            started = time.monotonic()
            try:
                answer = _call_llm_model_once([{ "role": "user", "content": content}], model,
                                              timeout=45, max_tokens=1024)
                passed = answer.strip() == expected
                message = "样例验证通过（不代表业务质量）" if passed else "返回内容未通过样例校验"
            except Exception:
                passed, message = False, "调用失败，请检查模型权限、能力、参数兼容性或网关状态"
            results.append(ProbeItem(model=model, passed=passed,
                                     seconds=round(time.monotonic() - started, 2), message=message))
        return ProbeResult(kind=body.kind, results=results)
    finally:
        _probe_slot.release()
