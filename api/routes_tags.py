"""
打标配置 CRUD 路由。

运行时配置优先读 schemas/tag_config_runtime.json；不存在时回退到 tag_config.py 静态默认值。
所有写操作先写临时文件再原子替换，防止并发写入导致 JSON 损坏。

鉴权：写接口（POST/PUT/DELETE）要求 Authorization: Bearer <TAGS_ADMIN_TOKEN>；
      未配置 TAGS_ADMIN_TOKEN 时跳过鉴权（适合内网隔离部署）。
"""
import copy
import importlib
import json
import shutil
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from config import RUNTIME_CFG_FILE, TAGS_ADMIN_TOKEN, logger
from core.tasks import _cfg_lock
from api.schemas import (
    AddPositiveDimensionBody,
    UpdatePositiveDimensionBody,
    AddNegativeDimensionBody,
    UpdateNegativeDimensionBody,
    AddNegativeRuleBody,
    UpdateNegativeRuleBody,
    UpdateSeverityBody,
)
from api.response_schemas import (
    BusinessErrorResponse,
    HttpDetailErrorResponse,
    TagConfigResponse,
    TagMappingResponse,
)
from logger.operation import log_async_operation, log_operation
from tag.tag_config_guard import TagConfigStructureError, validate_runtime_tag_config
from tag.tag_value_id_mapper import get_tag_value_id_mapper

router = APIRouter(tags=["标签配置"])


# 管理接口鉴权依赖

async def _require_admin(authorization: str = Header(default="")) -> None:
    """在已配置管理密钥时校验标签配置写请求。

    Args:
        authorization (str): HTTP `Authorization` 请求头原值。

    Raises:
        HTTPException: Bearer Token 与 `TAGS_ADMIN_TOKEN` 不匹配时抛出 HTTP 401。

    Note:
        `TAGS_ADMIN_TOKEN` 未配置时为兼容内网部署而跳过校验。
    """
    # TAGS_ADMIN_TOKEN 未配置时跳过校验，兼容内网隔离部署（无需鉴权的私有环境）
    if TAGS_ADMIN_TOKEN and authorization != f"Bearer {TAGS_ADMIN_TOKEN}":
        raise HTTPException(
            status_code=401,
            detail="需要管理员 Token，请在请求头中添加: Authorization: Bearer <TAGS_ADMIN_TOKEN>",
        )


# 标签配置辅助函数

def _cfg_load() -> dict:
    """加载运行时标签配置，文件不存在时回退到代码默认值。

    Returns:
        dict: 包含正向维度、负向维度和风险扣分映射的可修改配置。

    Raises:
        OSError: 运行时配置文件无法读取时抛出。
        json.JSONDecodeError: 运行时配置不是合法 JSON 时抛出。
    """
    if RUNTIME_CFG_FILE.exists():
        with open(RUNTIME_CFG_FILE, encoding="utf-8") as f:
            return json.load(f)
    # 运行时文件不存在时回退到代码默认值；importlib.reload 确保读到模块当前状态，
    # 防止 Python 缓存旧的模块级变量（热部署场景）
    from tag import tag_config as _tc
    importlib.reload(_tc)
    return {
        "positiveDimensions": copy.deepcopy(_tc.POSITIVE_DIMENSIONS),
        "negativeDimensions": copy.deepcopy(_tc.NEGATIVE_DIMENSIONS),
        "severityDeduction":  dict(_tc.SEVERITY_DEDUCTION),
    }


def _cfg_save(cfg: dict):
    """将标签配置先写临时文件，再替换运行时配置。

    Args:
        cfg (dict): 已在路由层合并的完整标签配置。

    Raises:
        OSError: 临时文件和目标文件均无法写入或替换时抛出。

    Note:
        Docker Desktop 单文件挂载可拒绝原子 `replace`，此时回退到锁保护的
        复制和删除流程。
    """
    validate_runtime_tag_config(cfg)
    # 先写 .tmp 再原子 rename：避免写入中途崩溃导致 JSON 半截损坏，
    # rename 在同一文件系统内是原子操作（POSIX 保证）
    tmp = RUNTIME_CFG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    try:
        tmp.replace(RUNTIME_CFG_FILE)
    except OSError:
        # Docker Desktop (Mac) 单文件 bind mount 时 .tmp 与目标跨 filesystem，rename 报 EBUSY；
        # 回退到 copy+delete，_cfg_lock 已保证单写，无并发风险
        shutil.copy2(str(tmp), str(RUNTIME_CFG_FILE))
        tmp.unlink(missing_ok=True)


def _build_tag_config_response(cfg: dict, action: str = "GET") -> JSONResponse:
    """构造统一配置响应并记录本次操作摘要。

    Args:
        cfg (dict): 返回给调用方的完整标签配置。
        action (str): 写入日志的配置操作名。

    Returns:
        JSONResponse: 包含配置、更新时间和成功码的响应。
    """
    with log_operation("获取标签配置信息", action=action) as operation:
        body = {"code": 0, "msg": "success",
                "data": {
                    **cfg,
                    "mappingVersion": get_tag_value_id_mapper().version,
                    "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }}
        pos = len(cfg.get("positiveDimensions", []))
        neg = len(cfg.get("negativeDimensions", []))
        operation.add(positiveDimensions=pos, negativeDimensions=neg)
        return JSONResponse(content=body)


def _cfg_response(cfg: dict, action: str = "GET") -> JSONResponse:
    """通过旧私有函数名调用统一标签配置响应构造器。

    Args:
        cfg (dict): 返回给调用方的完整标签配置。
        action (str): 写入日志的配置操作名。

    Returns:
        JSONResponse: 包含配置、更新时间和成功码的响应。
    """
    return _build_tag_config_response(cfg, action)


# 标签配置路由

_TAG_CONFIG_HTTP_ERRORS = {
    400: {"model": BusinessErrorResponse, "description": "标签配置内容无效"},
    401: {"model": HttpDetailErrorResponse, "description": "管理员 Token 无效"},
    409: {"model": BusinessErrorResponse, "description": "标签结构已锁定"},
}


@router.get("/api/tags/config", response_model=TagConfigResponse)
@log_async_operation("获取标签配置信息")
async def get_tags_config():
    """返回当前生效的标签维度、规则和扣分配置。

    Returns:
        JSONResponse: 完整运行时配置或代码默认配置。
    """
    # 读操作也加锁：防止在写操作原子替换文件的瞬间读到空/半截内容
    async with _cfg_lock:
        cfg = _cfg_load()
    return _build_tag_config_response(cfg)


@router.get("/api/tags/mapping", response_model=TagMappingResponse)
@log_async_operation("获取标签ID映射")
async def get_tags_mapping():
    """返回当前标签维度和值的完整、带版本 ID 映射快照。

    Returns:
        dict: 当前映射版本、全部维度记录和全部标签值记录。
    """
    mapper = get_tag_value_id_mapper()
    return {
        "code": 0,
        "msg": "success",
        "data": {
            "mappingVersion": mapper.version,
            "dimensions": mapper.dimension_records(),
            "mappings": mapper.records(),
        },
    }


@router.post(
    "/api/tags/config/positive",
    response_model=BusinessErrorResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def add_positive_dimension(body: AddPositiveDimensionBody):
    """新增全局唯一的正向标签维度。

    Args:
        body (AddPositiveDimensionBody): 维度标识、展示名、启用状态和候选值。

    Returns:
        JSONResponse: 更新后配置，或维度已存在的业务错误。
    """
    return JSONResponse(
        status_code=409,
        content={"code": 1003, "msg": "标签配置维度结构已锁定，不允许新增正向维度"},
    )


@router.put(
    "/api/tags/config/positive/{dimension}",
    response_model=TagConfigResponse | BusinessErrorResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def update_positive_dimension(dimension: str, body: UpdatePositiveDimensionBody):
    """部分更新指定正向标签维度。

    Args:
        dimension (str): 稳定维度标识。
        body (UpdatePositiveDimensionBody): 需要更新的可选字段。

    Returns:
        JSONResponse: 更新后配置，或维度不存在的业务错误。
    """
    async with _cfg_lock:
        cfg    = _cfg_load()
        target = next((d for d in cfg["positiveDimensions"] if d["dimension"] == dimension), None)
        if target is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        # 仅更新请求体中显式传入的字段（非 None），支持只改 displayName 而不影响 allowedValues 等
        if body.displayName   is not None: target["displayName"]   = body.displayName
        if body.enabled       is not None: target["enabled"]       = body.enabled
        if body.allowedValues is not None: target["allowedValues"] = body.allowedValues
        try:
            _cfg_save(cfg)
        except TagConfigStructureError as exc:
            return JSONResponse(status_code=400, content={"code": 1005, "msg": str(exc)})
    return _build_tag_config_response(cfg, f"UPDATE positive/{dimension}")


@router.delete(
    "/api/tags/config/positive/{dimension}",
    response_model=BusinessErrorResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def delete_positive_dimension(dimension: str):
    """从运行时配置删除指定正向标签维度。

    Args:
        dimension (str): 待删除的维度标识。

    Returns:
        JSONResponse: 删除后配置，或维度不存在的业务错误。
    """
    return JSONResponse(
        status_code=409,
        content={"code": 1003, "msg": "标签配置维度结构已锁定，不允许删除正向维度"},
    )


@router.post(
    "/api/tags/config/negative",
    response_model=BusinessErrorResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def add_negative_dimension(body: AddNegativeDimensionBody):
    """新增规则型或 LLM 型负向标签维度。

    Args:
        body (AddNegativeDimensionBody): 维度元数据、检测类型和初始规则。

    Returns:
        JSONResponse: 更新后配置，或维度已存在的业务错误。
    """
    return JSONResponse(
        status_code=409,
        content={"code": 1003, "msg": "标签配置维度结构已锁定，不允许新增负向维度"},
    )


@router.put(
    "/api/tags/config/negative/{dimension}",
    response_model=TagConfigResponse | BusinessErrorResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def update_negative_dimension(dimension: str, body: UpdateNegativeDimensionBody):
    """更新负向维度的展示名或启用状态。

    Args:
        dimension (str): 稳定负向维度标识。
        body (UpdateNegativeDimensionBody): 需要更新的可选字段。

    Returns:
        JSONResponse: 更新后配置，或维度不存在的业务错误。
    """
    # 负向维度只允许改 displayName 和 enabled，rules 通过专属路由管理，职责分离
    async with _cfg_lock:
        cfg    = _cfg_load()
        target = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if target is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        if body.displayName is not None: target["displayName"] = body.displayName
        if body.enabled     is not None: target["enabled"]     = body.enabled
        try:
            _cfg_save(cfg)
        except TagConfigStructureError as exc:
            return JSONResponse(status_code=400, content={"code": 1005, "msg": str(exc)})
    return _build_tag_config_response(cfg, f"UPDATE negative/{dimension}")


@router.delete(
    "/api/tags/config/negative/{dimension}",
    response_model=BusinessErrorResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def delete_negative_dimension(dimension: str):
    """从运行时配置删除指定负向标签维度。

    Args:
        dimension (str): 待删除的负向维度标识。

    Returns:
        JSONResponse: 删除后配置，或维度不存在的业务错误。
    """
    return JSONResponse(
        status_code=409,
        content={"code": 1003, "msg": "标签配置维度结构已锁定，不允许删除负向维度"},
    )


@router.post(
    "/api/tags/config/negative/{dimension}/rules",
    response_model=BusinessErrorResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def add_negative_rule(dimension: str, body: AddNegativeRuleBody):
    """在指定负向维度下新增唯一规则。

    Args:
        dimension (str): 目标负向维度标识。
        body (AddNegativeRuleBody): 规则标识、标签名、等级和透传参数。

    Returns:
        JSONResponse: 更新后配置，或维度/规则冲突的业务错误。
    """
    return JSONResponse(
        status_code=409,
        content={"code": 1003, "msg": "标签配置规则结构已锁定，不允许新增规则"},
    )


@router.put(
    "/api/tags/config/negative/{dimension}/rules/{rule_key}",
    response_model=TagConfigResponse | BusinessErrorResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def update_negative_rule(dimension: str, rule_key: str, body: UpdateNegativeRuleBody):
    """合并更新指定负向规则的任意允许字段。

    Args:
        dimension (str): 目标负向维度标识。
        rule_key (str): 目标规则标识。
        body (UpdateNegativeRuleBody): 要合并进现有规则的字段。

    Returns:
        JSONResponse: 更新后配置，或维度/规则不存在的业务错误。
    """
    async with _cfg_lock:
        cfg = _cfg_load()
        dim = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if dim is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        rules = dim.get("rules", {})
        if rule_key not in rules:
            return JSONResponse(content={"code": 1004, "msg": f"ruleKey '{rule_key}' 不存在"})
        updates = body.model_dump(exclude_unset=True)
        unknown_fields = set(updates) - set(rules[rule_key])
        if unknown_fields:
            return JSONResponse(
                status_code=400,
                content={"code": 1005, "msg": f"规则字段结构已锁定，禁止新增: {sorted(unknown_fields)}"},
            )
        rules[rule_key].update(updates)
        try:
            _cfg_save(cfg)
        except TagConfigStructureError as exc:
            return JSONResponse(status_code=400, content={"code": 1005, "msg": str(exc)})
    return _build_tag_config_response(cfg, f"UPDATE negative/{dimension}/rules/{rule_key}")


@router.delete(
    "/api/tags/config/negative/{dimension}/rules/{rule_key}",
    response_model=BusinessErrorResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def delete_negative_rule(dimension: str, rule_key: str):
    """删除指定负向维度下的单条规则。

    Args:
        dimension (str): 目标负向维度标识。
        rule_key (str): 待删除的规则标识。

    Returns:
        JSONResponse: 删除后配置，或维度/规则不存在的业务错误。
    """
    return JSONResponse(
        status_code=409,
        content={"code": 1003, "msg": "标签配置规则结构已锁定，不允许删除规则"},
    )


@router.put(
    "/api/tags/config/severity",
    response_model=TagConfigResponse | BusinessErrorResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def update_severity(body: UpdateSeverityBody):
    """部分更新风险等级的扣分值。

    Args:
        body (UpdateSeverityBody): low、medium 或 high 的可选更新值。

    Returns:
        JSONResponse: 更新后配置，或请求未提供任何值的业务错误。
    """
    # exclude_none=True：允许只传部分等级（如只调整 high），不传的等级保持原值
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return JSONResponse(content={"code": 1001, "msg": "至少提供一个字段 (low / medium / high)"})
    async with _cfg_lock:
        cfg = _cfg_load()
        cfg["severityDeduction"].update({key: -abs(value) for key, value in updates.items()})
        _cfg_save(cfg)
    return _build_tag_config_response(cfg, f"UPDATE severity {list(updates.keys())}")


@router.post(
    "/api/tags/config/reset",
    response_model=TagConfigResponse,
    responses=_TAG_CONFIG_HTTP_ERRORS,
    dependencies=[Depends(_require_admin)],
)
async def reset_tags_config():
    """删除运行时覆盖并恢复代码中的默认标签配置。

    Returns:
        JSONResponse: 重置后的默认配置。

    Note:
        该操作会删除 `RUNTIME_CFG_FILE`，且不保留文件级历史版本。
    """
    async with _cfg_lock:
        # 删除运行时文件后 _cfg_load() 自动回退到 tag_config.py 的代码默认值，
        # 无需手动重写文件内容，避免默认值与代码不同步
        if RUNTIME_CFG_FILE.exists():
            RUNTIME_CFG_FILE.unlink()
        cfg = _cfg_load()
    return _build_tag_config_response(cfg, "RESET to default")
