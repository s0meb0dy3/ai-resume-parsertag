"""提供标签标准值、别名和内部 ID 的确定性映射。

运行时标签 JSON 同时输出业务值、维度 ID、标签 ID 和映射版本。映射文件独立
维护正负向维度、允许值 ID 和别名；无法唯一归属的冲突别名不会自动映射。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

DEFAULT_MAPPING_FILE = (
    Path(__file__).resolve().parent.parent / "schemas" / "tag_value_id_mapping.json"
)
_SPACE_RE = re.compile(r"\s+")
_DIMENSION_ID_RE = re.compile(r"\d{4}")
_TAG_ID_RE = re.compile(r"\d{8}")


class TagValueMappingError(ValueError):
    """表示独立标签映射文件违反唯一性或字段契约。"""


def normalize_tag_term(value: str) -> str:
    """生成供维度内标签和别名匹配使用的标准键。

    Args:
        value (str): 标签标准值或别名原文。

    Returns:
        str: 已统一大小写、括号、区间符号并移除空白的匹配键。
    """
    text = str(value or "").strip().casefold()
    text = text.replace("（", "(").replace("）", ")").replace("～", "-")
    return _SPACE_RE.sub("", text)


class TagValueIdMapper:
    """加载、校验并查询独立的标签 ID 与值映射。

    Attributes:
        version (str): 映射数据的内容版本。

    Note:
        同一维度内的标准值和 ID 必须唯一，值级 ID 的前四位必须等于维度 ID。
        多个技能共享的宽泛别名会被标记为歧义，避免静默映射到错误技能。
    """

    def __init__(self, payload: dict) -> None:
        """校验映射负载并建立 ID、标准值和别名索引。

        Args:
            payload (dict): 包含 `version`、`dimensions` 和 `mappings` 的映射数据。

        Raises:
            TagValueMappingError: 顶层字段或任一映射记录不满足契约时抛出。
        """
        self.version = str(payload.get("version", ""))
        dimensions = payload.get("dimensions")
        mappings = payload.get("mappings")
        if (
            not self.version
            or not isinstance(dimensions, list)
            or not dimensions
            or not isinstance(mappings, list)
        ):
            raise TagValueMappingError(
                "映射文件必须包含 version、非空 dimensions 和 mappings 数组"
            )
        self._dimensions_by_id: dict[str, dict] = {}
        self._dimensions_by_code: dict[str, dict] = {}
        self._dimensions_by_display_name: dict[str, dict] = {}
        for item in dimensions:
            self._add_dimension(item)
        self._by_id: dict[str, dict] = {}
        self._by_dimension_value: dict[tuple[str, str], dict] = {}
        self._by_dimension_rule_key: dict[tuple[str, str], dict] = {}
        self._aliases: dict[tuple[str, str], dict] = {}
        self._ambiguous_aliases: set[tuple[str, str]] = set()
        for item in mappings:
            self._add(item)

    def _add_dimension(self, item: dict) -> None:
        """校验并注册一条维度 ID、稳定代码和展示名称映射。"""
        if not isinstance(item, dict) or set(item) != {"id", "dimension", "displayName"}:
            raise TagValueMappingError(f"无效维度映射记录: {item!r}")
        dimension_id = str(item.get("id", ""))
        dimension = str(item.get("dimension", "")).strip()
        display_name = str(item.get("displayName", "")).strip()
        if (
            not _DIMENSION_ID_RE.fullmatch(dimension_id)
            or not dimension
            or not display_name
        ):
            raise TagValueMappingError(f"无效维度映射记录: {item!r}")
        if dimension_id in self._dimensions_by_id:
            raise TagValueMappingError(f"维度 ID 重复: {dimension_id}")
        if dimension in self._dimensions_by_code:
            raise TagValueMappingError(f"维度代码重复: {dimension}")
        if display_name in self._dimensions_by_display_name:
            raise TagValueMappingError(f"维度展示名重复: {display_name}")
        record = {
            "id": dimension_id,
            "dimension": dimension,
            "displayName": display_name,
        }
        self._dimensions_by_id[dimension_id] = record
        self._dimensions_by_code[dimension] = record
        self._dimensions_by_display_name[display_name] = record

    @classmethod
    def load(cls, path: str | Path = DEFAULT_MAPPING_FILE) -> "TagValueIdMapper":
        """从 JSON 文件加载标签映射。

        Args:
            path (str | Path): 独立标签映射文件路径。

        Returns:
            TagValueIdMapper: 已完成字段和唯一性校验的映射器。

        Raises:
            OSError: 映射文件无法读取时抛出。
            json.JSONDecodeError: 映射文件不是合法 JSON 时抛出。
            TagValueMappingError: 映射内容不满足契约时抛出。
        """
        with Path(path).open(encoding="utf-8") as file:
            return cls(json.load(file))

    def _add(self, item: dict) -> None:
        """校验一条映射记录并加入查询索引。

        Args:
            item (dict): 单条 ID、维度、标准值、分类和别名记录。

        Raises:
            TagValueMappingError: 字段无效，或 ID、标准值重复时抛出。
        """
        if not isinstance(item, dict):
            raise TagValueMappingError("mappings 元素必须是 JSON object")
        tag_id = str(item.get("id", ""))
        dimension = str(item.get("dimension", ""))
        value = str(item.get("value", "")).strip()
        category = str(item.get("category", "")).strip()
        rule_key_value = item.get("ruleKey")
        rule_key = str(rule_key_value).strip() if rule_key_value is not None else ""
        aliases = item.get("aliases", [])
        if (
            not _TAG_ID_RE.fullmatch(tag_id)
            or not dimension
            or not value
            or not isinstance(aliases, list)
            or (rule_key_value is not None and not rule_key)
            or any(not isinstance(alias, str) or not alias.strip() for alias in aliases)
        ):
            raise TagValueMappingError(f"无效映射记录: {item!r}")
        dimension_record = self._dimensions_by_code.get(dimension)
        if dimension_record is None:
            raise TagValueMappingError(f"标签引用未知维度: {dimension}")
        if not tag_id.startswith(dimension_record["id"]):
            raise TagValueMappingError(
                f"标签 ID 前缀与维度 ID 不一致: {tag_id}/{dimension_record['id']}"
            )
        if tag_id in self._by_id:
            raise TagValueMappingError(f"ID 重复: {tag_id}")
        key = (dimension, value)
        if key in self._by_dimension_value:
            raise TagValueMappingError(f"维度内标签值重复: {dimension}/{value}")
        rule_key_index = (dimension, rule_key)
        if rule_key and rule_key_index in self._by_dimension_rule_key:
            raise TagValueMappingError(f"维度内规则键重复: {dimension}/{rule_key}")
        record = {
            "id": tag_id,
            "dimensionId": dimension_record["id"],
            "dimension": dimension,
            "value": value,
            "aliases": list(aliases),
        }
        if category:
            record["category"] = category
        if rule_key:
            record["ruleKey"] = rule_key
        self._by_id[tag_id] = record
        self._by_dimension_value[key] = record
        if rule_key:
            self._by_dimension_rule_key[rule_key_index] = record
        for term in [value, *aliases]:
            alias_key = (dimension, normalize_tag_term(term))
            if alias_key in self._ambiguous_aliases:
                continue
            existing = self._aliases.get(alias_key)
            if existing and existing["id"] != tag_id:
                # Excel 允许宽泛别名服务多个技能，冲突项必须交给上层语义判断。
                self._aliases.pop(alias_key, None)
                self._ambiguous_aliases.add(alias_key)
            else:
                self._aliases[alias_key] = record

    def resolve_dimension(self, value: str) -> dict | None:
        """按维度 ID、稳定代码或展示名称解析维度记录。"""
        key = str(value or "").strip()
        item = (
            self._dimensions_by_id.get(key)
            or self._dimensions_by_code.get(key)
            or self._dimensions_by_display_name.get(key)
        )
        return dict(item) if item else None

    def dimension_records(self) -> list[dict]:
        """按映射文件顺序返回全部维度记录副本。"""
        return [dict(item) for item in self._dimensions_by_id.values()]

    def resolve_rule(self, dimension: str, rule_key: str) -> dict | None:
        """按维度代码或 ID 和稳定规则键解析负向标签记录。"""
        dimension_record = self.resolve_dimension(dimension)
        dimension_code = (
            dimension_record["dimension"] if dimension_record else dimension
        )
        item = self._by_dimension_rule_key.get((dimension_code, str(rule_key).strip()))
        return dict(item) if item else None

    def resolve(self, dimension: str, value: str) -> dict | None:
        """在指定维度内把标准值或唯一别名解析为映射记录。

        Args:
            dimension (str): 固定标签维度标识。
            value (str): 待归一的标准值或别名。

        Returns:
            dict | None: 独立映射记录副本；未知或歧义别名返回 `None`。
        """
        dimension_record = self.resolve_dimension(dimension)
        dimension_code = (
            dimension_record["dimension"] if dimension_record else dimension
        )
        item = self._aliases.get((dimension_code, normalize_tag_term(value)))
        return dict(item) if item else None

    def resolve_many(self, dimension: str, values: Iterable[str]) -> tuple[list[dict], list[str]]:
        """批量解析标签并按 ID 去重，保留未识别原词。

        Args:
            dimension (str): 固定标签维度标识。
            values (Iterable[str]): 待归一的标签标准值或别名序列。

        Returns:
            tuple[list[dict], list[str]]: 已解析映射记录和未识别原词列表。
        """
        resolved: list[dict] = []
        unknown: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = self.resolve(dimension, value)
            if item and item["id"] not in seen:
                seen.add(item["id"])
                resolved.append(item)
            elif not item:
                unknown.append(str(value))
        return resolved, unknown

    def records(self, dimension: str | None = None) -> list[dict]:
        """返回全部或指定维度的映射记录。

        Args:
            dimension (str | None): 需要筛选的维度；为空时返回全部维度。

        Returns:
            list[dict]: 按映射文件顺序排列的记录副本。
        """
        dimension_record = self.resolve_dimension(dimension) if dimension else None
        dimension_code = (
            dimension_record["dimension"] if dimension_record else dimension
        )
        values = self._by_id.values()
        return [
            dict(item)
            for item in values
            if dimension_code is None or item["dimension"] == dimension_code
        ]


_default_mapper: TagValueIdMapper | None = None


def get_tag_value_id_mapper() -> TagValueIdMapper:
    """返回进程内复用的默认标签映射器。

    Returns:
        TagValueIdMapper: 首次调用时从默认 JSON 文件加载的映射器。

    Raises:
        OSError: 默认映射文件无法读取时抛出。
        json.JSONDecodeError: 默认映射文件不是合法 JSON 时抛出。
        TagValueMappingError: 默认映射内容不满足契约时抛出。
    """
    global _default_mapper
    if _default_mapper is None:
        _default_mapper = TagValueIdMapper.load()
    return _default_mapper
