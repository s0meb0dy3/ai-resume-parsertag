#!/usr/bin/env python3
"""将 Excel 技能标签库同步到运行时配置和独立 ID 映射。

本脚本只读取 `skill_300_new.xlsx` 的 `Skill标签库` 工作表，不依赖 Excel 客户端或
第三方解析库。同步过程保留现有标签维度和对外 JSON 结构，仅替换 `skills`
维度的允许值及其内部映射记录。
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = ROOT / "localmaterial" / "简历解析与打标" / "skill_300_new.xlsx"
RUNTIME_CONFIG_FILE = ROOT / "schemas" / "tag_config_runtime.json"
MAPPING_FILE = ROOT / "schemas" / "tag_value_id_mapping.json"
PYTHON_LIBRARY_FILE = ROOT / "tag" / "skill_library.py"
WORKSHEET_NAME = "Skill标签库"
EXPECTED_SKILL_COUNT = 300
MAPPING_VERSION = "2026.08.06-work-years-v2"
DIMENSION_MAPPING_ID_PREFIX = {
    "education": "1001",
    "companyBg": "1002",
    "workYears": "1003",
    "skills": "1004",
    "industry": "1005",
}
_CELL_REFERENCE_RE = re.compile(r"[A-Z]+")
_SKILL_ID_RE = re.compile(r"SK\d{6}")
_XML_NAMESPACE = {
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}


@dataclass(frozen=True)
class SkillRecord:
    """表示技能库中一条通过完整性校验的标准技能。

    Attributes:
        skill_id (str): Excel 源表 ID，格式为 `SK` 加六位数字，仅用于同步校验。
        value (str): 用于 `skills.allowedValues` 和对外标签值的标准名称。
        category (str): Excel 中用于技能治理和人工维护的分类名称。
        aliases (tuple[str, ...]): 可归一到标准名称的候选别名。
    """

    skill_id: str
    value: str
    category: str
    aliases: tuple[str, ...]


def _cell_text(cell: ElementTree.Element) -> str:
    """读取内联字符串或普通值单元格的文本。

    Args:
        cell (ElementTree.Element): OOXML 工作表中的 `c` 单元格节点。

    Returns:
        str: 合并富文本片段后的单元格文本；空单元格返回空字符串。
    """
    inline_parts = [
        item.text or "" for item in cell.findall(".//x:t", _XML_NAMESPACE)
    ]
    if inline_parts:
        return "".join(inline_parts)
    value = cell.find("x:v", _XML_NAMESPACE)
    return value.text if value is not None and value.text is not None else ""


def _worksheet_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    """根据工作表名称解析其在 OOXML 压缩包中的真实路径。

    Args:
        archive (zipfile.ZipFile): 已打开的 XLSX 压缩包。
        sheet_name (str): 需要读取的工作表显示名称。

    Returns:
        str: 可直接传给 `ZipFile.read` 的工作表 XML 路径。

    Raises:
        ValueError: 工作表不存在或工作簿关系缺失时抛出。
    """
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(
        archive.read("xl/_rels/workbook.xml.rels")
    )
    relationship_targets = {
        item.attrib["Id"]: item.attrib["Target"]
        for item in relationships.findall("pr:Relationship", _XML_NAMESPACE)
    }
    for sheet in workbook.findall(".//x:sheet", _XML_NAMESPACE):
        if sheet.attrib.get("name") != sheet_name:
            continue
        relationship_id = sheet.attrib.get(
            f"{{{_XML_NAMESPACE['r']}}}id", ""
        )
        target = relationship_targets.get(relationship_id)
        if not target:
            break
        return target.lstrip("/")
    raise ValueError(f"工作簿缺少工作表或关系: {sheet_name}")


def load_skill_records(path: Path = SOURCE_FILE) -> list[SkillRecord]:
    """从 Excel 主表加载并校验 300 条技能记录。

    Args:
        path (Path): 技能标签库 XLSX 文件路径。

    Returns:
        list[SkillRecord]: 按 Excel 行顺序排列的标准技能记录。

    Raises:
        ValueError: 表头、数量、ID、标准名称、分类或别名不满足契约时抛出。
        zipfile.BadZipFile: 输入文件不是合法 XLSX 容器时抛出。
    """
    with zipfile.ZipFile(path) as archive:
        worksheet = ElementTree.fromstring(
            archive.read(_worksheet_path(archive, WORKSHEET_NAME))
        )

    rows: list[dict[str, str]] = []
    for row in worksheet.findall(".//x:sheetData/x:row", _XML_NAMESPACE):
        values: dict[str, str] = {}
        for cell in row.findall("x:c", _XML_NAMESPACE):
            reference = cell.attrib.get("r", "")
            match = _CELL_REFERENCE_RE.match(reference)
            if match:
                values[match.group()] = _cell_text(cell).strip()
        rows.append(values)

    expected_header = {
        "A": "skill_id",
        "B": "skill_name",
        "C": "category",
        "D": "aliases",
    }
    if not rows or rows[0] != expected_header:
        raise ValueError(f"技能库表头不符合约定: {rows[0] if rows else None!r}")

    records: list[SkillRecord] = []
    for row_number, row in enumerate(rows[1:], start=2):
        aliases = tuple(
            alias.strip() for alias in row.get("D", "").split(";") if alias.strip()
        )
        record = SkillRecord(
            skill_id=row.get("A", ""),
            value=row.get("B", ""),
            category=row.get("C", ""),
            aliases=aliases,
        )
        if not _SKILL_ID_RE.fullmatch(record.skill_id):
            raise ValueError(f"第 {row_number} 行技能 ID 无效: {record.skill_id!r}")
        if not record.value or not record.category or not record.aliases:
            raise ValueError(f"第 {row_number} 行存在空名称、分类或别名")
        records.append(record)

    if len(records) != EXPECTED_SKILL_COUNT:
        raise ValueError(
            f"技能数量必须为 {EXPECTED_SKILL_COUNT}，实际为 {len(records)}"
        )
    if len({item.skill_id for item in records}) != len(records):
        raise ValueError("技能 ID 不允许重复")
    expected_skill_ids = {
        f"SK{sequence:06d}"
        for sequence in range(1, EXPECTED_SKILL_COUNT + 1)
    }
    actual_skill_ids = {item.skill_id for item in records}
    if actual_skill_ids != expected_skill_ids:
        missing = sorted(expected_skill_ids - actual_skill_ids)
        unexpected = sorted(actual_skill_ids - expected_skill_ids)
        raise ValueError(
            f"技能 ID 必须完整覆盖 1-{EXPECTED_SKILL_COUNT}: "
            f"missing={missing} unexpected={unexpected}"
        )
    if len({item.value for item in records}) != len(records):
        raise ValueError("技能标准名称不允许重复")
    return records


def _find_skill_dimension(config: dict) -> dict:
    """在运行时配置中定位唯一的 `skills` 正向维度。

    Args:
        config (dict): 完整运行时标签配置。

    Returns:
        dict: 可原位更新的 `skills` 维度配置。

    Raises:
        ValueError: 配置中不存在或存在多个 `skills` 维度时抛出。
    """
    dimensions = [
        item
        for item in config.get("positiveDimensions", [])
        if item.get("dimension") == "skills"
    ]
    if len(dimensions) != 1:
        raise ValueError("运行时配置必须且只能包含一个 skills 维度")
    return dimensions[0]


def _mapping_id(dimension: str, sequence: int) -> str:
    """按维度前缀和允许值位置生成 8 位映射 ID。

    Args:
        dimension (str): 运行时正向标签维度。
        sequence (int): 标签在该维度 ``allowedValues`` 中的位置，从 1 开始。

    Returns:
        str: 四位维度前缀加四位组内序号，例如 ``10040001``。

    Raises:
        ValueError: 维度没有固定前缀，或序号不在四位 ID 范围内时抛出。
    """
    prefix = DIMENSION_MAPPING_ID_PREFIX.get(dimension)
    if prefix is None:
        raise ValueError(f"正向标签维度缺少 ID 前缀: {dimension}")
    if not 1 <= sequence <= 9999:
        raise ValueError(f"标签组内序号超出四位 ID 范围: {sequence}")
    return f"{prefix}{sequence:04d}"


def update_runtime_config(records: list[SkillRecord]) -> None:
    """只替换运行时配置中的技能允许值。

    Args:
        records (list[SkillRecord]): 已通过校验的 Excel 技能记录。

    Note:
        本函数不新增、删除或重排维度，也不改变任何响应字段。
    """
    config = json.loads(RUNTIME_CONFIG_FILE.read_text(encoding="utf-8"))
    _find_skill_dimension(config)["allowedValues"] = [item.value for item in records]
    RUNTIME_CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _order_mapping_records(
    mapping_records: list[dict],
    runtime_config: dict,
) -> list[dict]:
    """按正向维度及其允许值顺序排列映射记录。

    Args:
        mapping_records (list[dict]): 待排序的完整正向标签映射。
        runtime_config (dict): 提供维度和 ``allowedValues`` 顺序的运行时配置。

    Returns:
        list[dict]: 维度前缀分组、组内 ID 递增且值顺序与配置一致的映射。

    Raises:
        ValueError: 配置或映射存在重复项、重复 ID，或两者不能一一对应时抛出。
    """
    expected_keys = [
        (dimension["dimension"], value)
        for dimension in runtime_config.get("positiveDimensions", [])
        for value in dimension.get("allowedValues", [])
    ]
    if len(expected_keys) != len(set(expected_keys)):
        raise ValueError("运行时正向标签配置存在重复的 dimension + value")

    mapping_by_key: dict[tuple[str, str], dict] = {}
    mapping_ids: set[str] = set()
    for record in mapping_records:
        key = (record.get("dimension", ""), record.get("value", ""))
        if key in mapping_by_key:
            raise ValueError(f"ID 映射存在重复的 dimension + value: {key!r}")
        mapping_id = str(record.get("id", ""))
        if mapping_id in mapping_ids:
            raise ValueError(f"ID 映射存在重复 ID: {mapping_id}")
        mapping_by_key[key] = record
        mapping_ids.add(mapping_id)

    expected_set = set(expected_keys)
    actual_set = set(mapping_by_key)
    if expected_set != actual_set:
        missing = sorted(expected_set - actual_set)
        unexpected = sorted(actual_set - expected_set)
        raise ValueError(
            f"运行时允许值与 ID 映射不能一一对应: "
            f"missing={missing} unexpected={unexpected}"
        )
    ordered_records: list[dict] = []
    for dimension in runtime_config.get("positiveDimensions", []):
        dimension_name = dimension["dimension"]
        for sequence, value in enumerate(
            dimension.get("allowedValues", []),
            start=1,
        ):
            record = dict(mapping_by_key[(dimension_name, value)])
            record["id"] = _mapping_id(dimension_name, sequence)
            ordered_records.append(record)
    return ordered_records


def update_mapping(records: list[SkillRecord]) -> None:
    """用 Excel 数据替换技能记录，并按运行时允许值顺序输出映射。

    Args:
        records (list[SkillRecord]): 已通过校验的 Excel 技能记录。

    Note:
        非技能维度映射保持内容不变。输出先按运行时正向维度排列，对应
        ``1001`` 至 ``1005`` 前缀，再按各维度 ``allowedValues`` 排列。
    """
    payload = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
    non_skill_records = [
        item for item in payload["mappings"] if item.get("dimension") != "skills"
    ]
    skill_records = [
        {
            "id": _mapping_id("skills", sequence),
            "dimension": "skills",
            "value": item.value,
            "category": item.category,
            "aliases": list(item.aliases),
        }
        for sequence, item in enumerate(records, start=1)
    ]
    runtime_config = json.loads(
        RUNTIME_CONFIG_FILE.read_text(encoding="utf-8")
    )
    payload["mappings"] = _order_mapping_records(
        [*non_skill_records, *skill_records],
        runtime_config,
    )
    payload["version"] = MAPPING_VERSION
    MAPPING_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def update_python_fallback(records: list[SkillRecord]) -> None:
    """生成静态配置使用的技能允许值常量模块。

    Args:
        records (list[SkillRecord]): 已通过校验的 Excel 技能记录。

    Note:
        生成文件只保存标准名称，详细 ID、分类和别名仍以独立映射文件为准。
    """
    lines = [
        '"""提供由 `skill_300_new.xlsx` 生成的技能标签允许值。"""',
        "",
        "# 此文件由 `scripts/sync_skill_library.py` 生成，请勿手工维护列表内容。",
        "SKILL_ALLOWED_VALUES = [",
    ]
    lines.extend(
        f"    {json.dumps(item.value, ensure_ascii=False)}," for item in records
    )
    lines.extend(["]", ""])
    PYTHON_LIBRARY_FILE.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    """执行技能库同步并输出便于发布核对的摘要。

    Returns:
        int: 同步成功时返回进程退出码 0。
    """
    records = load_skill_records()
    update_runtime_config(records)
    update_mapping(records)
    update_python_fallback(records)
    categories = len({item.category for item in records})
    print(
        f"技能库同步完成: skills={len(records)} categories={categories} "
        f"first={_mapping_id('skills', 1)}/{records[0].value} "
        f"last={_mapping_id('skills', len(records))}/{records[-1].value}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
