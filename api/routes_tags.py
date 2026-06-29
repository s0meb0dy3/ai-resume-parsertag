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

router = APIRouter()


# ── 鉴权依赖 ──────────────────────────────────────────────────────

async def _require_admin(authorization: str = Header(default="")) -> None:
    """写接口鉴权：配置了 TAGS_ADMIN_TOKEN 时强制校验 Bearer Token。"""
    # TAGS_ADMIN_TOKEN 未配置时跳过校验，兼容内网隔离部署（无需鉴权的私有环境）
    if TAGS_ADMIN_TOKEN and authorization != f"Bearer {TAGS_ADMIN_TOKEN}":
        raise HTTPException(
            status_code=401,
            detail="需要管理员 Token，请在请求头中添加: Authorization: Bearer <TAGS_ADMIN_TOKEN>",
        )


# ── 配置辅助函数 ──────────────────────────────────────────────────

def _cfg_load() -> dict:
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


def _cfg_response(cfg: dict, action: str = "GET") -> JSONResponse:
    body = {"code": 0, "msg": "success",
            "data": {**cfg, "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}
    pos  = len(cfg.get("positiveDimensions", []))
    neg  = len(cfg.get("negativeDimensions", []))
    logger.info(f"[tags/config] {action} 正向维度={pos} 负向维度={neg}")
    # 将完整配置逐行写入日志，提供审计链路（方便排查"谁改了什么配置"）
    for line in json.dumps(body, ensure_ascii=False, indent=2).splitlines():
        logger.info(line)
    return JSONResponse(content=body)


# ── 路由 ──────────────────────────────────────────────────────────

@router.get("/api/tags/config")
async def get_tags_config():
    # 读操作也加锁：防止在写操作原子替换文件的瞬间读到空/半截内容
    async with _cfg_lock:
        cfg = _cfg_load()
    return _cfg_response(cfg)


@router.post("/api/tags/config/positive", dependencies=[Depends(_require_admin)])
async def add_positive_dimension(body: AddPositiveDimensionBody):
    dim = body.dimension.strip()
    async with _cfg_lock:
        cfg = _cfg_load()
        # 在锁内校验唯一性，防止并发请求同时插入相同 dimension
        if any(d["dimension"] == dim for d in cfg["positiveDimensions"]):
            return JSONResponse(content={"code": 1002, "msg": f"dimension '{dim}' 已存在"})
        cfg["positiveDimensions"].append({
            "dimension":     dim,
            "displayName":   body.displayName or dim,  # displayName 缺省时回退为 dimension 本身
            "enabled":       body.enabled,
            "allowedValues": body.allowedValues,
        })
        _cfg_save(cfg)
    return _cfg_response(cfg, f"ADD positive/{dim}")


@router.put("/api/tags/config/positive/{dimension}", dependencies=[Depends(_require_admin)])
async def update_positive_dimension(dimension: str, body: UpdatePositiveDimensionBody):
    async with _cfg_lock:
        cfg    = _cfg_load()
        target = next((d for d in cfg["positiveDimensions"] if d["dimension"] == dimension), None)
        if target is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        # 仅更新请求体中显式传入的字段（非 None），支持只改 displayName 而不影响 allowedValues 等
        if body.displayName   is not None: target["displayName"]   = body.displayName
        if body.enabled       is not None: target["enabled"]       = body.enabled
        if body.allowedValues is not None: target["allowedValues"] = body.allowedValues
        _cfg_save(cfg)
    return _cfg_response(cfg, f"UPDATE positive/{dimension}")


@router.delete("/api/tags/config/positive/{dimension}", dependencies=[Depends(_require_admin)])
async def delete_positive_dimension(dimension: str):
    async with _cfg_lock:
        cfg    = _cfg_load()
        before = len(cfg["positiveDimensions"])
        cfg["positiveDimensions"] = [d for d in cfg["positiveDimensions"] if d["dimension"] != dimension]
        # 通过数量变化判断是否真正删除，避免多一次 next() 查找
        if len(cfg["positiveDimensions"]) == before:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        _cfg_save(cfg)
    return _cfg_response(cfg, f"DELETE positive/{dimension}")


@router.post("/api/tags/config/negative", dependencies=[Depends(_require_admin)])
async def add_negative_dimension(body: AddNegativeDimensionBody):
    dim = body.dimension.strip()
    async with _cfg_lock:
        cfg = _cfg_load()
        if any(d["dimension"] == dim for d in cfg["negativeDimensions"]):
            return JSONResponse(content={"code": 1002, "msg": f"dimension '{dim}' 已存在"})
        cfg["negativeDimensions"].append({
            "dimension":     dim,
            "displayName":   body.displayName or dim,
            "enabled":       body.enabled,
            "detectionType": body.detectionType,
            "rules":         body.rules or {},
        })
        _cfg_save(cfg)
    return _cfg_response(cfg, f"ADD negative/{dim}")


@router.put("/api/tags/config/negative/{dimension}", dependencies=[Depends(_require_admin)])
async def update_negative_dimension(dimension: str, body: UpdateNegativeDimensionBody):
    # 负向维度只允许改 displayName 和 enabled，rules 通过专属路由管理，职责分离
    async with _cfg_lock:
        cfg    = _cfg_load()
        target = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if target is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        if body.displayName is not None: target["displayName"] = body.displayName
        if body.enabled     is not None: target["enabled"]     = body.enabled
        _cfg_save(cfg)
    return _cfg_response(cfg, f"UPDATE negative/{dimension}")


@router.delete("/api/tags/config/negative/{dimension}", dependencies=[Depends(_require_admin)])
async def delete_negative_dimension(dimension: str):
    async with _cfg_lock:
        cfg    = _cfg_load()
        before = len(cfg["negativeDimensions"])
        cfg["negativeDimensions"] = [d for d in cfg["negativeDimensions"] if d["dimension"] != dimension]
        if len(cfg["negativeDimensions"]) == before:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        _cfg_save(cfg)
    return _cfg_response(cfg, f"DELETE negative/{dimension}")


@router.post("/api/tags/config/negative/{dimension}/rules", dependencies=[Depends(_require_admin)])
async def add_negative_rule(dimension: str, body: AddNegativeRuleBody):
    rule_key = body.ruleKey.strip()
    async with _cfg_lock:
        cfg = _cfg_load()
        dim = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if dim is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        # setdefault：维度首次添加规则时自动初始化 rules 字典，不要求调用方预先创建
        rules = dim.setdefault("rules", {})
        if rule_key in rules:
            return JSONResponse(content={"code": 1002, "msg": f"ruleKey '{rule_key}' 已存在"})
        # exclude ruleKey 本身（它是路由标识符，不作为规则内容存储）
        rule = body.model_dump(exclude={"ruleKey"}, exclude_none=True)
        rule.setdefault("tagName", rule_key)  # tagName 缺省时与 ruleKey 保持一致
        rules[rule_key] = rule
        _cfg_save(cfg)
    return _cfg_response(cfg, f"ADD negative/{dimension}/rules/{rule_key}")


@router.put("/api/tags/config/negative/{dimension}/rules/{rule_key}", dependencies=[Depends(_require_admin)])
async def update_negative_rule(dimension: str, rule_key: str, body: UpdateNegativeRuleBody):
    async with _cfg_lock:
        cfg = _cfg_load()
        dim = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if dim is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        rules = dim.get("rules", {})
        if rule_key not in rules:
            return JSONResponse(content={"code": 1004, "msg": f"ruleKey '{rule_key}' 不存在"})
        # dict.update 合并更新：调用方只需传要改的字段，其余字段原样保留
        rules[rule_key].update({k: v for k, v in body.model_dump().items() if k != "ruleKey"})
        _cfg_save(cfg)
    return _cfg_response(cfg, f"UPDATE negative/{dimension}/rules/{rule_key}")


@router.delete("/api/tags/config/negative/{dimension}/rules/{rule_key}", dependencies=[Depends(_require_admin)])
async def delete_negative_rule(dimension: str, rule_key: str):
    async with _cfg_lock:
        cfg = _cfg_load()
        dim = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if dim is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        if rule_key not in dim.get("rules", {}):
            return JSONResponse(content={"code": 1004, "msg": f"ruleKey '{rule_key}' 不存在"})
        del dim["rules"][rule_key]
        _cfg_save(cfg)
    return _cfg_response(cfg, f"DELETE negative/{dimension}/rules/{rule_key}")


@router.put("/api/tags/config/severity", dependencies=[Depends(_require_admin)])
async def update_severity(body: UpdateSeverityBody):
    # exclude_none=True：允许只传部分等级（如只调整 high），不传的等级保持原值
    updates = body.model_dump(exclude_none=True)
    if not updates:
        return JSONResponse(content={"code": 1001, "msg": "至少提供一个字段 (low / medium / high)"})
    async with _cfg_lock:
        cfg = _cfg_load()
        cfg["severityDeduction"].update(updates)
        _cfg_save(cfg)
    return _cfg_response(cfg, f"UPDATE severity {list(updates.keys())}")


@router.post("/api/tags/config/reset", dependencies=[Depends(_require_admin)])
async def reset_tags_config():
    async with _cfg_lock:
        # 删除运行时文件后 _cfg_load() 自动回退到 tag_config.py 的代码默认值，
        # 无需手动重写文件内容，避免默认值与代码不同步
        if RUNTIME_CFG_FILE.exists():
            RUNTIME_CFG_FILE.unlink()
        cfg = _cfg_load()
    return _cfg_response(cfg, "RESET to default")
