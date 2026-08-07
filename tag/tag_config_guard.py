"""保护运行时标签配置的固定维度和 JSON 结构。

管理接口可以修改显示名、开关、允许值和规则内容，但不能新增、删除或重排维度，
也不能改变既有字段类型。所有正向允许值还必须存在独立 ID 映射。
"""
from __future__ import annotations

from typing import Any

from .tag_config import NEGATIVE_DIMENSIONS, POSITIVE_DIMENSIONS
from .tag_value_id_mapper import get_tag_value_id_mapper


class TagConfigStructureError(ValueError):
    """表示配置写入改变了锁定维度、字段类型或映射完整性。"""


_TOP_LEVEL_KEYS = {"positiveDimensions", "negativeDimensions", "severityDeduction"}
_SEVERITY_LEVELS = {"low", "medium", "high"}


def _shape(value: Any, ignore_list_values: bool = False):
    """生成用于比较配置字段类型和嵌套层级的结构摘要。

    Args:
        value (Any): 需要提取结构的配置节点。
        ignore_list_values (bool): 是否忽略列表具体内容，仅保留标量占位。

    Returns:
        dict | list | str: 保留字典键、列表层级和标量类型的结构摘要。
    """
    if isinstance(value, dict):
        return {key: _shape(item, key in {"allowedValues", "keywords"}) for key, item in value.items()}
    if isinstance(value, list):
        if ignore_list_values:
            return ["scalar"]
        return [_shape(item) for item in value]
    return type(value).__name__


def validate_runtime_tag_config(cfg: dict) -> None:
    """校验运行时配置结构、字段类型和允许值映射完整性。

    Args:
        cfg (dict): 管理接口准备持久化的完整运行时标签配置。

    Raises:
        TagConfigStructureError: 维度、结构、类型、风险扣分或 ID 映射无效时抛出。

    Note:
        `allowedValues` 属于可治理内容，因此允许替换，但每个值必须非空、唯一，
        并能通过独立映射文件解析。
    """
    if not isinstance(cfg, dict) or set(cfg) != _TOP_LEVEL_KEYS:
        raise TagConfigStructureError("tag_config_runtime.json 顶层字段不可改变")
    positive = cfg.get("positiveDimensions")
    negative = cfg.get("negativeDimensions")
    if not isinstance(positive, list) or not isinstance(negative, list):
        raise TagConfigStructureError("positiveDimensions/negativeDimensions 必须是数组")
    if [item.get("dimension") for item in positive] != [item["dimension"] for item in POSITIVE_DIMENSIONS]:
        raise TagConfigStructureError("正向维度、顺序和 dimension 值不可改变")
    if [item.get("dimension") for item in negative] != [item["dimension"] for item in NEGATIVE_DIMENSIONS]:
        raise TagConfigStructureError("负向维度、顺序和 dimension 值不可改变")

    for actual, baseline in zip(positive, POSITIVE_DIMENSIONS):
        if not isinstance(actual, dict):
            raise TagConfigStructureError("positiveDimensions 元素必须是 object")
        if set(actual) != set(baseline):
            raise TagConfigStructureError(f"正向维度 {baseline['dimension']} 字段集合不可改变")
        if not isinstance(actual.get("dimension"), str):
            raise TagConfigStructureError(f"{baseline['dimension']}.dimension 必须是字符串")
        if not isinstance(actual.get("displayName"), str):
            raise TagConfigStructureError(f"{baseline['dimension']}.displayName 必须是字符串")
        if not isinstance(actual.get("enabled"), bool):
            raise TagConfigStructureError(f"{baseline['dimension']}.enabled 必须是布尔值")
        allowed = actual.get("allowedValues")
        if not isinstance(allowed, list) or any(not isinstance(value, str) or not value.strip() for value in allowed):
            raise TagConfigStructureError(f"{baseline['dimension']}.allowedValues 必须是非空字符串数组")
        if len(allowed) != len(set(allowed)):
            raise TagConfigStructureError(f"{baseline['dimension']}.allowedValues 不允许重复值")

    for actual, baseline in zip(negative, NEGATIVE_DIMENSIONS):
        if _shape(actual) != _shape(baseline):
            raise TagConfigStructureError(f"负向维度 {baseline['dimension']} 的字段和规则结构不可改变")

    severity = cfg.get("severityDeduction")
    if not isinstance(severity, dict) or set(severity) != _SEVERITY_LEVELS:
        raise TagConfigStructureError(f"severityDeduction 只允许字段: {sorted(_SEVERITY_LEVELS)}")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in severity.values()):
        raise TagConfigStructureError("severityDeduction 的值必须是整数")
    if any(value > 0 for value in severity.values()):
        raise TagConfigStructureError("severityDeduction 必须为 0 或负数，禁止风险加分")

    mapper = get_tag_value_id_mapper()
    for dimension in positive:
        if mapper.resolve_dimension(dimension["dimension"]) is None:
            raise TagConfigStructureError(
                f"正向维度缺少独立 ID 映射: {dimension['dimension']}"
            )
        for value in dimension["allowedValues"]:
            if mapper.resolve(dimension["dimension"], value) is None:
                raise TagConfigStructureError(
                    f"allowedValues 缺少独立 ID 映射: {dimension['dimension']}/{value}"
                )

    for dimension in negative:
        dimension_code = dimension["dimension"]
        if mapper.resolve_dimension(dimension_code) is None:
            raise TagConfigStructureError(
                f"负向维度缺少独立 ID 映射: {dimension_code}"
            )
        for rule_key, rule in dimension.get("rules", {}).items():
            tag_name = rule.get("tagName", "")
            mapping = mapper.resolve_rule(dimension_code, rule_key)
            if mapping is None or mapping["value"] != tag_name:
                raise TagConfigStructureError(
                    f"负向规则缺少独立 ID 映射: "
                    f"{dimension_code}/{rule_key}/{tag_name}"
                )
