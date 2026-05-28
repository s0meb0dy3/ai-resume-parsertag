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
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from config import RUNTIME_CFG_FILE, TAGS_ADMIN_TOKEN
from tasks import _cfg_lock

router = APIRouter()


# ── 鉴权依赖 ──────────────────────────────────────────────────────

async def _require_admin(authorization: str = Header(default="")) -> None:
    """写接口鉴权：配置了 TAGS_ADMIN_TOKEN 时强制校验 Bearer Token。"""
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
    from tag import tag_config as _tc
    importlib.reload(_tc)
    return {
        "positiveDimensions": copy.deepcopy(_tc.POSITIVE_DIMENSIONS),
        "negativeDimensions": copy.deepcopy(_tc.NEGATIVE_DIMENSIONS),
        "severityDeduction":  dict(_tc.SEVERITY_DEDUCTION),
    }


def _cfg_save(cfg: dict):
    tmp = RUNTIME_CFG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    tmp.replace(RUNTIME_CFG_FILE)


def _cfg_response(cfg: dict) -> JSONResponse:
    return JSONResponse(content={
        "code": 0, "msg": "success",
        "data": {**cfg, "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    })


# ── 路由 ──────────────────────────────────────────────────────────

@router.get("/api/tags/config")
async def get_tags_config():
    async with _cfg_lock:
        cfg = _cfg_load()
    return _cfg_response(cfg)


@router.post("/api/tags/config/positive", dependencies=[Depends(_require_admin)])
async def add_positive_dimension(body: dict):
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


@router.put("/api/tags/config/positive/{dimension}", dependencies=[Depends(_require_admin)])
async def update_positive_dimension(dimension: str, body: dict):
    async with _cfg_lock:
        cfg    = _cfg_load()
        target = next((d for d in cfg["positiveDimensions"] if d["dimension"] == dimension), None)
        if target is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        if "displayName"   in body: target["displayName"]   = body["displayName"]
        if "enabled"       in body: target["enabled"]       = bool(body["enabled"])
        if "allowedValues" in body: target["allowedValues"] = list(body["allowedValues"])
        _cfg_save(cfg)
    return _cfg_response(cfg)


@router.delete("/api/tags/config/positive/{dimension}", dependencies=[Depends(_require_admin)])
async def delete_positive_dimension(dimension: str):
    async with _cfg_lock:
        cfg    = _cfg_load()
        before = len(cfg["positiveDimensions"])
        cfg["positiveDimensions"] = [d for d in cfg["positiveDimensions"] if d["dimension"] != dimension]
        if len(cfg["positiveDimensions"]) == before:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        _cfg_save(cfg)
    return _cfg_response(cfg)


@router.put("/api/tags/config/negative/{dimension}", dependencies=[Depends(_require_admin)])
async def update_negative_dimension(dimension: str, body: dict):
    async with _cfg_lock:
        cfg    = _cfg_load()
        target = next((d for d in cfg["negativeDimensions"] if d["dimension"] == dimension), None)
        if target is None:
            return JSONResponse(content={"code": 1004, "msg": f"dimension '{dimension}' 不存在"})
        if "displayName" in body: target["displayName"] = body["displayName"]
        if "enabled"     in body: target["enabled"]     = bool(body["enabled"])
        _cfg_save(cfg)
    return _cfg_response(cfg)


@router.post("/api/tags/config/negative/{dimension}/rules", dependencies=[Depends(_require_admin)])
async def add_negative_rule(dimension: str, body: dict):
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
        rule.setdefault("tagName", rule_key)
        rule.setdefault("enabled", True)
        rule.setdefault("severity", "medium")
        rules[rule_key] = rule
        _cfg_save(cfg)
    return _cfg_response(cfg)


@router.put("/api/tags/config/negative/{dimension}/rules/{rule_key}", dependencies=[Depends(_require_admin)])
async def update_negative_rule(dimension: str, rule_key: str, body: dict):
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
    return _cfg_response(cfg)


@router.put("/api/tags/config/severity", dependencies=[Depends(_require_admin)])
async def update_severity(body: dict):
    allowed = {"low", "medium", "high"}
    invalid = [k for k in body if k not in allowed]
    if invalid:
        return JSONResponse(content={"code": 1001, "msg": f"无效字段: {invalid}，仅支持 low/medium/high"})
    async with _cfg_lock:
        cfg = _cfg_load()
        cfg["severityDeduction"].update({k: int(v) for k, v in body.items()})
        _cfg_save(cfg)
    return _cfg_response(cfg)


@router.post("/api/tags/config/reset", dependencies=[Depends(_require_admin)])
async def reset_tags_config():
    async with _cfg_lock:
        if RUNTIME_CFG_FILE.exists():
            RUNTIME_CFG_FILE.unlink()
        cfg = _cfg_load()
    return _cfg_response(cfg)
