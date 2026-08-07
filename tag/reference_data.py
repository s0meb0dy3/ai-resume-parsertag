"""加载并校验院校与公司资质参考数据。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
DEFAULT_EDUCATION_DATA_FILE = SCHEMA_DIR / "education_institution_data.json"
DEFAULT_COMPANY_DATA_FILE = SCHEMA_DIR / "company_qualification_data.json"

_SPACE_RE = re.compile(r"\s+")
_COMPANY_SUFFIX_RE = re.compile(
    r"(?:股份)?有限公司$|有限责任公司$|集团有限公司$|集团$|公司$"
)


class ReferenceDataError(ValueError):
    """表示参考数据缺少字段、数量不完整或存在冲突。"""


def normalize_reference_name(value: str) -> str:
    """生成院校和公司名称的大小写无关匹配键。

    Args:
        value (str): 标准名称或别名。

    Returns:
        str: 已统一括号、大小写和空白的匹配键。
    """
    text = str(value or "").strip().casefold()
    text = text.replace("（", "(").replace("）", ")")
    return _SPACE_RE.sub("", text)


def _load_json(path: Path) -> dict:
    """读取 JSON 对象并校验基础元数据。

    Args:
        path (Path): 参考数据文件路径。

    Returns:
        dict: 完整参考数据对象。

    Raises:
        ReferenceDataError: 顶层不是对象，或版本和来源字段无效时抛出。
    """
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ReferenceDataError(f"参考数据顶层必须是对象: {path}")
    if not str(payload.get("version", "")).strip():
        raise ReferenceDataError(f"参考数据缺少 version: {path}")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ReferenceDataError(f"参考数据缺少 sources: {path}")
    return payload


def _validate_name_list(values: object, field: str) -> tuple[str, ...]:
    """校验名称数组为非空且没有标准化重复项。

    Args:
        values (object): 待校验的 JSON 字段值。
        field (str): 用于错误信息的字段路径。

    Returns:
        tuple[str, ...]: 保留原始顺序的名称元组。

    Raises:
        ReferenceDataError: 字段不是字符串数组或存在重复名称时抛出。
    """
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ReferenceDataError(f"{field} 必须是非空字符串数组")
    normalized = [normalize_reference_name(value) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ReferenceDataError(f"{field} 存在重复名称")
    return tuple(value.strip() for value in values)


@dataclass(frozen=True)
class EducationReferenceData:
    """保存版本化院校分类、别名和海外排名快照。"""

    version: str
    categories: dict[str, frozenset[str]]
    alias_to_name: dict[str, str]

    def resolve(self, school: str) -> str | None:
        """把标准名称或常见简称解析为院校标准名称。

        Args:
            school (str): 已完成业务注记清理的学校名称。

        Returns:
            str | None: 命中时返回标准名称，否则返回 `None`。
        """
        return self.alias_to_name.get(normalize_reference_name(school))


@dataclass(frozen=True)
class CompanyReferenceData:
    """保存版本化公司标准名称、别名和资质标签。"""

    version: str
    records: tuple[dict, ...]

    def match(self, company: str) -> list[dict]:
        """查找公司名称对应的全部资质记录。

        Args:
            company (str): 工作经历中的公司名称。

        Returns:
            list[dict]: 匹配记录副本；未知公司返回空数组。

        Note:
            标准名称采用去公司后缀精确匹配，品牌简称允许作为名称片段匹配。
            长度小于两个字符的别名不会参与片段匹配。
        """
        normalized = normalize_reference_name(company)
        without_suffix = _COMPANY_SUFFIX_RE.sub("", normalized)
        matched: list[dict] = []
        for record in self.records:
            canonical_key = normalize_reference_name(record["name"])
            canonical_without_suffix = _COMPANY_SUFFIX_RE.sub("", canonical_key)
            if without_suffix == canonical_without_suffix:
                matched.append(dict(record))
                continue
            for alias in record["aliases"]:
                alias_key = normalize_reference_name(alias)
                if len(alias_key) >= 2 and alias_key in normalized:
                    matched.append(dict(record))
                    break
        return matched


@lru_cache(maxsize=1)
def load_education_reference_data(
    path: Path = DEFAULT_EDUCATION_DATA_FILE,
) -> EducationReferenceData:
    """加载完整国内院校分类与版本化海外排名快照。

    Args:
        path (Path): 院校参考数据文件路径。

    Returns:
        EducationReferenceData: 已校验且可直接查询的数据对象。

    Raises:
        ReferenceDataError: 名单数量、集合关系或别名映射无效时抛出。
    """
    payload = _load_json(path)
    classifications = payload.get("classifications")
    if not isinstance(classifications, dict):
        raise ReferenceDataError("classifications 必须是对象")

    required_counts = {
        "c9": 9,
        "project985": 39,
        "project211": 115,
        "doubleFirstClassAdditional2022": 32,
    }
    raw_categories: dict[str, tuple[str, ...]] = {}
    for key, expected_count in required_counts.items():
        values = _validate_name_list(classifications.get(key), f"classifications.{key}")
        if len(values) != expected_count:
            raise ReferenceDataError(
                f"classifications.{key} 应为 {expected_count} 所，实际 {len(values)} 所"
            )
        raw_categories[key] = values

    project211 = frozenset(raw_categories["project211"])
    project985 = frozenset(raw_categories["project985"])
    c9 = frozenset(raw_categories["c9"])
    additions = frozenset(raw_categories["doubleFirstClassAdditional2022"])
    if not c9 <= project985 or not project985 <= project211:
        raise ReferenceDataError("C9、985、211 名单集合关系无效")
    if additions & project211:
        raise ReferenceDataError("双一流新增院校不得与 211 名单重复")
    double_first = project211 | additions
    if len(double_first) != 147:
        raise ReferenceDataError("第二轮双一流建设高校应为 147 所")

    ranking = payload.get("overseasRankingSnapshot")
    if not isinstance(ranking, dict):
        raise ReferenceDataError("overseasRankingSnapshot 必须是对象")
    if ranking.get("complete") is not True:
        raise ReferenceDataError("QS 排名快照必须标记为完整")
    qs_top50_names = _validate_name_list(ranking.get("top50"), "ranking.top50")
    qs_top100_names = _validate_name_list(ranking.get("top100"), "ranking.top100")
    if len(qs_top50_names) != 50:
        raise ReferenceDataError(
            f"QS Top50 应为 50 所，实际 {len(qs_top50_names)} 所"
        )
    if len(qs_top100_names) != 100:
        raise ReferenceDataError(
            f"QS Top100 应为 100 所，实际 {len(qs_top100_names)} 所"
        )
    qs_top50 = frozenset(qs_top50_names)
    qs_top100 = frozenset(qs_top100_names)
    if not qs_top50 <= qs_top100:
        raise ReferenceDataError("QS50 必须是 QS100 的子集")

    all_names = project211 | additions | qs_top100
    aliases = payload.get("aliases", {})
    if not isinstance(aliases, dict):
        raise ReferenceDataError("aliases 必须是对象")
    alias_to_name = {normalize_reference_name(name): name for name in all_names}
    for canonical, values in aliases.items():
        if canonical not in all_names:
            raise ReferenceDataError(f"别名对应未知院校: {canonical}")
        for alias in _validate_name_list(values, f"aliases.{canonical}"):
            key = normalize_reference_name(alias)
            existing = alias_to_name.get(key)
            if existing and existing != canonical:
                raise ReferenceDataError(f"院校别名冲突: {alias}")
            alias_to_name[key] = canonical

    return EducationReferenceData(
        version=str(payload["version"]),
        categories={
            "C9": c9,
            "985": project985,
            "211": project211,
            "双一流": frozenset(double_first),
            "海外名校QS50": qs_top50,
            "海外名校QS100": qs_top100,
        },
        alias_to_name=alias_to_name,
    )


@lru_cache(maxsize=1)
def load_company_reference_data(
    path: Path = DEFAULT_COMPANY_DATA_FILE,
) -> CompanyReferenceData:
    """加载公司资质数据并校验名称、别名和标签值。

    Args:
        path (Path): 公司资质参考数据文件路径。

    Returns:
        CompanyReferenceData: 已校验的公司资质查询对象。

    Raises:
        ReferenceDataError: 公司记录、别名或资质字段无效时抛出。
    """
    payload = _load_json(path)
    source_ids = {
        str(source.get("id", "")).strip()
        for source in payload["sources"]
        if isinstance(source, dict)
    }
    if len(source_ids) != len(payload["sources"]) or "" in source_ids:
        raise ReferenceDataError("公司资质 sources.id 必须非空且唯一")
    allowed = set(_validate_name_list(payload.get("allowedQualifications"), "allowedQualifications"))
    raw_records = payload.get("companies")
    if not isinstance(raw_records, list) or not raw_records:
        raise ReferenceDataError("companies 必须是非空数组")

    records: list[dict] = []
    names: set[str] = set()
    aliases: dict[str, str] = {}
    for index, item in enumerate(raw_records):
        if not isinstance(item, dict):
            raise ReferenceDataError(f"companies[{index}] 必须是对象")
        name = str(item.get("name", "")).strip()
        qualifications = _validate_name_list(
            item.get("qualifications"), f"companies[{index}].qualifications"
        )
        record_aliases = _validate_name_list(
            item.get("aliases", []), f"companies[{index}].aliases"
        )
        source_refs = _validate_name_list(
            item.get("sourceRefs"), f"companies[{index}].sourceRefs"
        )
        name_key = normalize_reference_name(name)
        if not name or name_key in names:
            raise ReferenceDataError(f"公司标准名称无效或重复: {name}")
        if not set(qualifications) <= allowed:
            raise ReferenceDataError(f"公司包含未知资质: {name}")
        if not set(source_refs) <= source_ids:
            raise ReferenceDataError(f"公司包含未知来源引用: {name}")
        names.add(name_key)
        for alias in record_aliases:
            alias_key = normalize_reference_name(alias)
            existing = aliases.get(alias_key)
            if existing and existing != name:
                raise ReferenceDataError(f"公司别名冲突: {alias}")
            aliases[alias_key] = name
        records.append({
            "name": name,
            "aliases": list(record_aliases),
            "qualifications": list(qualifications),
            "sourceRefs": list(source_refs),
        })
    return CompanyReferenceData(
        version=str(payload["version"]),
        records=tuple(records),
    )
