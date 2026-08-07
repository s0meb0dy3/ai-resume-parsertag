"""验证 Swagger/OpenAPI 自动文档与当前接口代码保持一致。"""
import asyncio
import json
from pathlib import Path

from app import app
from api.response_schemas import TagConfigResponse, TaggedResumeResult
from api.routes_async import get_async_result
from api.routes_tags import _cfg_load
from tag.tag_value_id_mapper import get_tag_value_id_mapper
from config import TASK_TTL_MINUTES
from scripts.export_openapi import DEFAULT_OUTPUT, check_openapi_document


def test_swagger_and_openapi_endpoints_are_enabled():
    """验证部署环境固定提供 Swagger、ReDoc 和 OpenAPI JSON 入口。"""
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"


def test_openapi_operations_have_unique_ids_and_descriptions():
    """验证所有业务操作都具有唯一标识和可读说明。"""
    document = app.openapi()
    operation_ids = []
    for path_item in document["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            operation_ids.append(operation["operationId"])
            assert operation.get("summary")
            assert operation.get("description")

    assert len(operation_ids) == len(set(operation_ids))


def test_openapi_success_responses_have_concrete_schemas():
    """验证所有 JSON 成功响应都可用于生成有类型的客户端。"""
    document = app.openapi()
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            success_response = operation["responses"].get("200")
            if not success_response:
                continue
            json_content = success_response.get("content", {}).get("application/json")
            if not json_content:
                continue
            assert json_content.get("schema"), f"{method.upper()} {path} 200 schema 为空"


def test_openapi_declares_business_and_http_error_responses():
    """验证兼容接口和配置接口声明实际使用的 HTTP 错误状态。"""
    paths = app.openapi()["paths"]
    chat_responses = paths["/v1/chat/completions"]["post"]["responses"]
    config_responses = paths["/api/tags/config/positive"]["post"]["responses"]

    assert "400" in chat_responses
    assert "401" in config_responses
    assert "409" in config_responses


def test_response_models_accept_current_result_and_config_payloads():
    """验证公开结果模板和运行时标签配置符合 Swagger 响应模型。"""
    result_path = Path(__file__).parents[1] / "schemas" / "resume_result_template.json"
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    result_payload.pop("_comment", None)
    config_payload = {
        **_cfg_load(),
        "mappingVersion": get_tag_value_id_mapper().version,
        "updatedAt": "2026-08-04 10:00:00",
    }

    TaggedResumeResult.model_validate(result_payload)
    TagConfigResponse.model_validate({
        "code": 0,
        "msg": "success",
        "data": config_payload,
    })


def test_mapping_query_is_documented_with_a_concrete_response_schema():
    """验证后端可通过公开接口读取与结果一致的版本化 ID 映射。"""
    operation = app.openapi()["paths"]["/api/tags/mapping"]["get"]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/TagMappingResponse")


def test_committed_openapi_document_matches_application():
    """验证仓库 OpenAPI 文件未落后于 FastAPI 路由和 Pydantic Schema。"""
    assert Path(DEFAULT_OUTPUT).is_file()
    assert check_openapi_document(DEFAULT_OUTPUT)


def test_missing_task_message_uses_configured_ttl():
    """验证轮询过期提示读取当前 TTL 配置而不是硬编码旧值。"""
    response = asyncio.run(get_async_result("missing-openapi-contract-task"))
    body = json.loads(response.body)

    assert body["code"] == 2001
    assert f"TTL {TASK_TTL_MINUTES}分钟" in body["msg"]
