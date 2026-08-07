"""验证标签配置结构、Excel 技能库和独立 ID 映射契约。"""

import copy
import json
from pathlib import Path

import pytest

from core.postprocess import _conform_to_template
from scripts.sync_skill_library import _mapping_id, load_skill_records
from tag.tag_config_guard import TagConfigStructureError, validate_runtime_tag_config
from tag.tag_data import classify_education
from tag.tag_value_id_mapper import (
    TagValueIdMapper,
    TagValueMappingError,
    get_tag_value_id_mapper,
)
from tag.tagger import (
    _normalize_positive_tags,
    _tagger_load_cfg,
    _extract_skills_rule,
    detect_sensitive_tags,
    extract_positive_tags_llm,
    extract_positive_tags_rule,
)


ROOT = Path(__file__).resolve().parents[1]


def _runtime_config() -> dict:
    """读取测试使用的完整运行时标签配置。

    Returns:
        dict: 从仓库 JSON 文件反序列化的运行时配置。
    """
    return json.loads(
        (ROOT / "schemas/tag_config_runtime.json").read_text(encoding="utf-8")
    )


def _id_mapping() -> dict:
    """读取完整标签 ID 映射文件。

    Returns:
        dict: 从仓库 JSON 文件反序列化的标签映射。
    """
    return json.loads(
        (ROOT / "schemas/tag_value_id_mapping.json").read_text(encoding="utf-8")
    )


def test_mapping_records_follow_dimension_and_allowed_value_order():
    """验证映射按维度分组，组内 ID 递增且与允许值完全对应。"""
    config = _runtime_config()
    mappings = [
        record for record in _id_mapping()["mappings"]
        if record["id"].startswith("1")
    ]
    expected_keys = [
        (dimension["dimension"], value)
        for dimension in config["positiveDimensions"]
        for value in dimension["allowedValues"]
    ]
    actual_keys = [
        (record["dimension"], record["value"])
        for record in mappings
    ]
    expected_ids = [
        _mapping_id(dimension["dimension"], sequence)
        for dimension in config["positiveDimensions"]
        for sequence, _value in enumerate(
            dimension["allowedValues"],
            start=1,
        )
    ]

    assert actual_keys == expected_keys
    assert [record["id"] for record in mappings] == expected_ids


def test_every_allowed_value_has_one_independent_id_mapping():
    """验证所有正向允许值均有且仅有一个独立 ID 映射。"""
    cfg = _runtime_config()
    mapper = get_tag_value_id_mapper()
    ids = set()
    for dimension in cfg["positiveDimensions"]:
        for value in dimension["allowedValues"]:
            record = mapper.resolve(dimension["dimension"], value)
            assert record is not None
            assert record["id"] not in ids
            ids.add(record["id"])
    assert mapper.resolve("skills", "Python3")["value"] == "Python"
    assert mapper.resolve("skills", "FPGA开发")["value"] == "FPGA"
    assert mapper.resolve("education", "海外名校（QS50）")["value"] == "海外名校QS50"


def test_all_dimensions_have_stable_ids_and_match_tag_id_prefixes():
    """验证正负向维度 ID、顺序以及值级 ID 前缀保持一致。"""
    mapper = get_tag_value_id_mapper()
    assert (
        mapper.version
        == "ai-resume-parsertag:ID-mapping-tag:V1.0.1-20260806"
    )
    expected = [
        {"id": "1001", "dimension": "education", "displayName": "教育背景"},
        {"id": "1002", "dimension": "companyBg", "displayName": "公司背景"},
        {"id": "1003", "dimension": "workYears", "displayName": "工作年限"},
        {"id": "1004", "dimension": "skills", "displayName": "技能标签"},
        {"id": "1005", "dimension": "industry", "displayName": "行业背景"},
        {"id": "2001", "dimension": "jobstable", "displayName": "职业稳定性风险"},
        {"id": "2002", "dimension": "integrity", "displayName": "诚信与合规风险"},
        {"id": "2003", "dimension": "matchRisk", "displayName": "匹配度硬伤"},
        {"id": "2004", "dimension": "location", "displayName": "地域相关"},
    ]

    assert mapper.dimension_records() == expected
    for dimension in expected:
        assert mapper.resolve_dimension(dimension["id"]) == dimension
        assert mapper.resolve_dimension(dimension["dimension"]) == dimension
        assert mapper.resolve_dimension(dimension["displayName"]) == dimension
        assert mapper.records(dimension["id"]) == mapper.records(dimension["dimension"])
        assert all(
            record["dimensionId"] == dimension["id"]
            and record["id"].startswith(dimension["id"])
            for record in mapper.records(dimension["dimension"])
        )
    assert mapper.resolve("1004", "Python3")["id"] == "10040001"


def test_negative_rules_have_stable_ids_and_match_runtime_tag_names():
    """验证每条负向规则都能按 dimension/ruleKey 绑定唯一标签 ID。"""
    mapper = get_tag_value_id_mapper()
    expected_ids = {
        ("jobstable", "frequent_job_change"): "20010001",
        ("jobstable", "short_tenure"): "20010002",
        ("jobstable", "gap_period"): "20010003",
        ("integrity", "timeline_overlap"): "20020001",
        ("matchRisk", "training_institution"): "20030001",
        ("location", "sensitive_words"): "20040001",
    }

    for dimension in _runtime_config()["negativeDimensions"]:
        for rule_key, rule in dimension["rules"].items():
            record = mapper.resolve_rule(dimension["dimension"], rule_key)
            assert record is not None
            assert record["id"] == expected_ids[(dimension["dimension"], rule_key)]
            assert record["value"] == rule["tagName"]


def test_tag_mapping_rejects_value_id_with_wrong_dimension_prefix():
    """验证值级 ID 不能挂到其他维度编号下。"""
    payload = _id_mapping()
    payload["mappings"][0]["id"] = "10020001"

    with pytest.raises(TagValueMappingError, match="前缀与维度 ID 不一致"):
        TagValueIdMapper(payload)


def test_company_and_industry_taxonomies_each_have_sixteen_values():
    """验证公司、行业标准值数量及旧名称兼容映射符合当前契约。"""
    config = _runtime_config()
    dimensions = {
        item["dimension"]: item["allowedValues"]
        for item in config["positiveDimensions"]
    }
    mapper = get_tag_value_id_mapper()

    assert len(dimensions["companyBg"]) == 16
    assert len(dimensions["industry"]) == 16
    assert mapper.resolve("companyBg", "头部大厂")["value"] == "行业龙头"
    assert mapper.resolve("companyBg", "外企500强")["value"] == "世界500强"
    assert mapper.resolve("industry", "在线教育")["value"] == "教育/培训"
    assert mapper.resolve("industry", "物联网/硬件")["value"] == "通信/物联网/智能硬件"


def test_work_year_values_and_ids_follow_the_approved_boundaries():
    """验证工作年限值、顺序、ID 和旧值别名符合新版契约。"""
    expected_values = [
        "应届",
        "往届无经验",
        "1年以下",
        "1-3年（满1年）",
        "3-5年（满3年）",
        "5-10年（满5年）",
        "10年及以上",
    ]
    config = _runtime_config()
    allowed_values = next(
        item["allowedValues"]
        for item in config["positiveDimensions"]
        if item["dimension"] == "workYears"
    )
    records = get_tag_value_id_mapper().records("workYears")

    assert allowed_values == expected_values
    assert [item["value"] for item in records] == expected_values
    assert [item["id"] for item in records] == [
        f"1003000{index}" for index in range(1, 8)
    ]
    assert get_tag_value_id_mapper().resolve("workYears", "0-1年")["value"] == "1年以下"
    assert get_tag_value_id_mapper().resolve("workYears", "15年+")["value"] == "10年及以上"


def test_excel_skill_library_matches_runtime_values_and_mapping():
    """验证 Excel 技能内容及映射顺序与运行时允许值一致。"""
    source_records = load_skill_records()
    config = _runtime_config()
    allowed_values = next(
        item["allowedValues"]
        for item in config["positiveDimensions"]
        if item["dimension"] == "skills"
    )
    mapping_records = get_tag_value_id_mapper().records("skills")

    assert len(source_records) == len(allowed_values) == len(mapping_records) == 300
    assert [item.value for item in source_records] == allowed_values
    assert [item["value"] for item in mapping_records] == allowed_values
    assert [
        _mapping_id("skills", sequence)
        for sequence in range(1, len(source_records) + 1)
    ] == [
        item["id"] for item in mapping_records
    ]
    assert mapping_records[0] == {
        "id": "10040001",
        "dimensionId": "1004",
        "dimension": "skills",
        "value": "Python",
        "aliases": ["Python3", "Python开发", "Python编程"],
        "category": "编程语言与开发框架",
    }
    mapping_by_id = {item["id"]: item for item in mapping_records}
    for sequence, source_record in enumerate(source_records, start=1):
        mapping_record = mapping_by_id[_mapping_id("skills", sequence)]
        assert mapping_record["value"] == source_record.value
        assert mapping_record["category"] == source_record.category
        assert mapping_record["aliases"] == list(source_record.aliases)
    assert get_tag_value_id_mapper().resolve("skills", "成本控制")["id"] == "10040281"
    assert mapping_records[-1]["id"] == "10040300"
    assert mapping_records[-1]["value"] == "环境保洁管理"
    assert get_tag_value_id_mapper().resolve("skills", "Agent")["value"] == "AI Agent"
    assert get_tag_value_id_mapper().resolve("skills", "SpringBoot")["id"] == "10040023"


def test_ambiguous_excel_alias_is_not_mapped_to_an_arbitrary_skill():
    """验证跨技能复用的宽泛别名不会被静默映射到任一技能。"""
    mapper = get_tag_value_id_mapper()

    assert mapper.resolve("skills", "深度学习框架") is None
    assert mapper.resolve("skills", "机器人离线编程") is None


def test_rule_fallback_extracts_standard_skills_from_resume_evidence():
    """验证 LLM 失败时仍从简历证据提取技能库内的标准技能。"""
    resume = {
        "candidate": {"selfEvaluation": "多年 FPGA 开发经验"},
        "educations": [],
        "works": [{
            "company": "测试公司",
            "position": "高级 FPGA 工程师",
            "description": (
                "使用 MATLAB 完成算法仿真，以 FPGA 和 Vivado 实现 "
                "EtherCAT 通信并进行 EMC 测试。"
            ),
            "startDate": "2020-01",
            "endDate": "2024-01",
        }],
        "projects": [],
    }

    result = extract_positive_tags_rule(resume)
    by_dimension = {
        item["dimension"]: item
        for item in result["positiveTags"]
    }

    assert by_dimension["skills"]["skillsValueArray"] == [
        "MATLAB",
        "FPGA",
        "Vivado",
        "EMC",
        "EtherCAT",
    ]
    assert by_dimension["skills"]["source"] == "rule"


def test_rule_fallback_extracts_new_skill_300_values_and_aliases():
    """验证新增技能和别名按新表顺序归一到标准值。"""
    resume = {
        "candidate": {
            "selfEvaluation": (
                "使用 SpringBoot、Vue 3、CUDA、PostgreSQL、Abaqus，"
                "并通过 8D 报告闭环质量问题。"
            )
        },
        "educations": [],
        "works": [],
        "projects": [],
    }

    assert _extract_skills_rule(resume) == [
        "Spring Boot",
        "Vue.js",
        "CUDA",
        "PostgreSQL",
        "Abaqus",
        "8D问题解决",
    ]


def test_internal_document_text_restores_skills_omitted_from_resume_json():
    """验证统一 JSON 省略技能时仍可使用内部原文恢复真实标准技能。"""
    resume = {
        "candidate": {"name": "杨强华", "selfEvaluation": "多年硬件开发经验"},
        "educations": [],
        "works": [],
        "projects": [],
    }

    result = extract_positive_tags_rule(
        resume,
        skill_evidence_text="熟悉 Linux、FPGA、DSP，并使用 Vivado 完成开发。",
    )
    skills = next(
        item for item in result["positiveTags"]
        if item["dimension"] == "skills"
    )

    assert skills["skillsValueArray"] == ["Linux", "FPGA", "DSP", "Vivado"]
    assert skills["source"] == "rule"
    assert "skill_evidence_text" not in json.dumps(result, ensure_ascii=False)


def test_positive_prompt_only_injects_evidence_backed_skill_candidates():
    """验证正向模型只接收本次简历命中的技能候选及逐值说明。"""
    resume = {
        "candidate": {"name": "杨强华", "selfEvaluation": "多年 FPGA 开发经验"},
        "educations": [],
        "works": [{
            "company": "测试公司",
            "position": "FPGA 工程师",
            "description": "使用 MATLAB 仿真并通过 Vivado 实现 FPGA 逻辑。",
            "startDate": "2020-01",
            "endDate": "2024-01",
        }],
        "projects": [],
    }
    captured: dict[str, str] = {}

    def fake_llm(messages):
        """记录本次用户 Prompt 并返回候选技能。

        Args:
            messages (list): 正向标签模型消息列表。

        Returns:
            str: 包含三个证据技能的正向标签 JSON。
        """
        captured["prompt"] = messages[1]["content"]
        return json.dumps({
            "positiveTags": [{
                "dimension": "skills",
                "skillsValueArray": ["FPGA", "Vivado", "MATLAB"],
            }],
            "confidence": 0.9,
        }, ensure_ascii=False)

    result = extract_positive_tags_llm(
        resume,
        {},
        fake_llm,
        skill_evidence_text="原始文档还明确记录 Linux 调试经验。",
    )
    prompt = captured["prompt"]
    skills = next(
        item for item in result["positiveTags"] if item["dimension"] == "skills"
    )

    assert "候选值：MATLAB|Linux|FPGA|Vivado" in prompt
    assert "`MATLAB`：" in prompt
    assert "`Linux`：" in prompt
    assert "`FPGA`：" in prompt
    assert "`Vivado`：" in prompt
    assert "`Python`：" not in prompt
    assert "`Spring Boot`：" not in prompt
    assert len(prompt) < 20_000
    assert skills["skillsValueArray"] == ["FPGA", "Vivado", "MATLAB", "Linux"]
    assert len(next(
        dimension["allowedValues"]
        for dimension in _tagger_load_cfg()["positiveDimensions"]
        if dimension["dimension"] == "skills"
    )) == 300


def test_positive_prompt_requires_empty_skills_when_no_evidence_exists():
    """验证无技能证据时 Prompt 要求空数组且不会开放自由提取。"""
    resume = {
        "candidate": {"name": "测试候选人", "selfEvaluation": "沟通认真负责"},
        "educations": [],
        "works": [],
        "projects": [],
    }
    captured: dict[str, str] = {}

    def fake_llm(messages):
        """记录无候选技能的 Prompt 并返回空正向结果。

        Args:
            messages (list): 正向标签模型消息列表。

        Returns:
            str: 不包含任何技能的正向标签 JSON。
        """
        captured["prompt"] = messages[1]["content"]
        return json.dumps({"positiveTags": [], "confidence": 0.7})

    extract_positive_tags_llm(resume, {}, fake_llm)
    prompt = captured["prompt"]

    assert "候选值：（当前简历无技能证据候选）" in prompt
    assert "`skillsValueArray=[]`" in prompt
    assert "不得从岗位要求、常识或技能库自由补充" in prompt
    assert '"skillsValueArray": []' in prompt


def test_raw_only_skill_keeps_a_slot_when_model_reaches_limit():
    """验证模型技能已满 12 个时仍为仅原文可见的技能保留位置。"""
    model_skills = [
        "FPGA", "MATLAB", "Vivado", "EtherCAT", "数字信号处理", "Linux",
        "EMC", "C", "C++", "Shell", "机器学习", "OFDM",
    ]
    resume = {
        "candidate": {"selfEvaluation": " ".join(model_skills)},
        "educations": [],
        "works": [],
        "projects": [],
    }

    def fake_llm(_messages):
        """返回达到技能展示上限的模型标签。

        Args:
            _messages: 测试不使用的模型消息列表。

        Returns:
            str: 包含 12 个已有结构化证据技能的正向标签 JSON。
        """
        return json.dumps({
            "positiveTags": [{
                "dimension": "skills",
                "skillsValueArray": model_skills,
            }],
            "confidence": 0.9,
        }, ensure_ascii=False)

    result = extract_positive_tags_llm(
        resume,
        {},
        fake_llm,
        skill_evidence_text="原始 PDF 明确写有 DSP 开发经验。",
    )
    skills = next(
        item for item in result["positiveTags"]
        if item["dimension"] == "skills"
    )["skillsValueArray"]

    assert len(skills) == 12
    assert "DSP" in skills
    assert "OFDM" not in skills


def test_rule_skill_extraction_rejects_understanding_and_ambiguous_aliases():
    """验证“了解”语境和共享宽泛别名都不会形成技能标签。"""
    skills = _extract_skills_rule({
        "candidate": {
            "selfEvaluation": "了解深度学习框架和机器人离线编程。",
        },
        "educations": [],
        "works": [],
        "projects": [],
    })

    assert "深度学习" not in skills
    assert "PyTorch" not in skills
    assert "TensorFlow" not in skills
    assert "RobotStudio" not in skills
    assert "RoboGuide" not in skills


def test_rule_skill_extraction_rejects_short_or_broad_false_matches():
    """验证技术连接符和宽泛中文短别名不会造成跨领域技能误报。"""
    skills = _extract_skills_rule({
        "candidate": {"selfEvaluation": "多年 FPGA 开发经验。"},
        "educations": [],
        "works": [{
            "company": "测试公司",
            "position": "FPGA 工程师",
            "description": (
                "实现 biss-c 编码器协议、SFP+接口开发，并完成信号源定位。"
            ),
            "startDate": "2020-01",
            "endDate": "2024-01",
        }],
        "projects": [],
    })

    assert "FPGA" in skills
    assert "C" not in skills
    assert "RESTful API" not in skills
    assert "信号发生器" not in skills


def test_domestic_rank_annotation_is_not_treated_as_overseas_education():
    """验证国内院校等级注记不会触发“海外硕士”规则。"""
    values = classify_education(
        ["电子科技大学（985）", "电子科技大学（985）"],
        ["硕士研究生", "本科"],
    )

    assert values == ["985", "211", "双一流", "硕士"]
    assert "海外硕士" not in values


def test_education_rule_overrides_conflicting_model_values_everywhere():
    """验证模型教育分类与简历事实冲突时统一采用确定性规则结果。"""
    resume = {
        "candidate": {"name": "杨强华"},
        "educations": [{
            "school": "电子科技大学（985）",
            "major": "通信与信息工程学院",
            "degree": "硕士",
            "education": "硕士研究生",
            "startDate": "2013-09",
            "endDate": "2016-07",
        }],
        "works": [],
        "projects": [],
    }
    normalized = _normalize_positive_tags(
        [{
            "dimension": "education",
            "educationValueArray": ["海外硕士"],
            "source": "ai",
        }],
        resume,
        _tagger_load_cfg()["positiveDimensions"],
    )
    education = next(
        item for item in normalized if item["dimension"] == "education"
    )

    assert education == {
        "dimension": "education",
        "educationValueArray": ["985", "211", "双一流", "硕士"],
        "source": "rule",
    }

    result = _conform_to_template(
        "task-id",
        resume,
        {
            "positiveTags": [{
                "dimension": "education",
                "educationValueArray": ["海外硕士"],
                "source": "ai",
            }],
            "confidence": 0.9,
            "hitTags": [],
            "hitCount": 0,
            "deductionScore": 0,
            "tagParseTime": "2026-08-03 16:50:00",
        },
        "2026-08-03 16:49:00",
    )
    final_education = result["positiveTags"]["tagsArray"][0]
    assert [item["tag"] for item in final_education["value"]] == [
        "985", "211", "双一流", "硕士",
    ]
    assert {item["source"] for item in final_education["value"]} == {"rule"}


def test_runtime_config_structure_is_locked_but_allowed_values_are_content():
    """验证允许值可治理，但固定维度、字段和类型不能改变。"""
    cfg = _runtime_config()
    validate_runtime_tag_config(cfg)

    wrong_type = copy.deepcopy(cfg)
    wrong_type["positiveDimensions"][0]["enabled"] = "true"
    with pytest.raises(TagConfigStructureError, match="enabled"):
        validate_runtime_tag_config(wrong_type)

    changed_dimension = copy.deepcopy(cfg)
    changed_dimension["positiveDimensions"][0]["dimension"] = "school"
    with pytest.raises(TagConfigStructureError, match="维度"):
        validate_runtime_tag_config(changed_dimension)

    unmapped_negative_name = copy.deepcopy(cfg)
    unmapped_negative_name["negativeDimensions"][3]["rules"]["sensitive_words"][
        "tagName"
    ] = "未知地域风险"
    with pytest.raises(TagConfigStructureError, match="负向规则缺少独立 ID 映射"):
        validate_runtime_tag_config(unmapped_negative_name)


def test_public_tag_json_exposes_stable_ids_and_mapping_version():
    """验证公开标签 JSON 同时返回维度 ID、标签 ID 和映射版本。"""
    resume = {
        "candidate": {"name": "张三"},
        "educations": [{"school": "北京大学", "education": "硕士"}],
        "works": [],
        "projects": [],
    }
    tag_info = {
        "positiveTags": [{
            "dimension": "education",
            "educationValueArray": ["985", "硕士"],
            "source": "ai",
            "confidence": 0.9,
        }],
        "confidence": 0.9,
        "hitTags": [],
        "hitCount": 0,
        "deductionScore": 0,
        "tagParseTime": "2026-08-03 12:00:00",
    }
    result = _conform_to_template("task", resume, tag_info, "2026-08-03 11:00:00")
    education = result["positiveTags"]["tagsArray"][0]
    assert result["mappingVersion"] == get_tag_value_id_mapper().version
    assert set(education) == {"dimensionId", "dimension", "displayName", "value"}
    assert education["dimensionId"] == "1001"
    assert education["displayName"] == "教育背景"
    assert all(set(value) == {"tagId", "tag", "source"} for value in education["value"])
    assert [(value["tagId"], value["tag"]) for value in education["value"]] == [
        ("10010001", "C9"),
        ("10010002", "985"),
        ("10010003", "211"),
        ("10010004", "双一流"),
        ("10010009", "硕士"),
    ]


def test_current_negative_rule_hits_are_exposed_as_rule_source():
    """验证当前内置负向检测不再被错误标记为 AI 来源。"""
    result = _conform_to_template(
        "task",
        {"candidate": {}, "educations": [], "works": [], "projects": []},
        {
            "positiveTags": [],
            "confidence": 0.8,
            "hitTags": [{
                "category": "匹配度硬伤",
                "hits": [{
                    "tag": "疑似培训机构背景",
                    "severity": "medium",
                    "matchedContent": "教育经历中包含：某培训学院",
                    "source": "rule",
                }],
            }],
            "hitCount": 1,
            "deductionScore": -10,
            "tagParseTime": "2026-08-06 12:00:00",
        },
        "2026-08-06 11:59:00",
    )

    dimension = result["negativeTags"]["tagsArray"][0]
    assert dimension["dimensionId"] == "2003"
    assert dimension["value"][0]["tagId"] == "20030001"
    assert dimension["value"][0]["source"] == "rule"


def test_guangxi_is_a_default_sensitive_region_with_stable_tag_mapping():
    """验证无需请求参数也会检测广西，并输出可映射的固定风险标签。"""
    result = detect_sensitive_tags(
        {
            "candidate": {"nativePlace": "广西南宁"},
            "educations": [],
            "works": [],
            "projects": [],
        },
        {},
    )

    assert result["hitCount"] == 1
    assert result["deductionScore"] == -20
    location = result["hitTags"][0]
    assert location["dimension"] == "location"
    assert location["hits"] == [{
        "tag": "敏感地域",
        "ruleKey": "sensitive_words",
        "severity": "high",
        "matchedContent": "命中“广西”：...广西南宁...",
        "source": "rule",
    }]
    mapping = get_tag_value_id_mapper().resolve_rule("location", "sensitive_words")
    assert mapping["id"] == "20040001"
    assert mapping["value"] == location["hits"][0]["tag"]


def test_non_sensitive_region_does_not_trigger_default_location_risk():
    """验证默认只预设广西，不会将其他地域自动判为敏感。"""
    result = detect_sensitive_tags(
        {
            "candidate": {"nativePlace": "广东深圳"},
            "educations": [],
            "works": [],
            "projects": [],
        },
        {},
    )

    assert result["hitCount"] == 0
    assert result["hitTags"] == []


def test_llm_tags_are_canonicalized_limited_and_work_years_are_overridden():
    """验证模型别名受允许值约束且工作年限由确定性规则覆盖。"""
    resume = {
        "candidate": {},
        "educations": [],
        "works": [{
            "company": "A", "position": "FPGA工程师",
            "startDate": "2026-01", "endDate": "2026-07",
        }],
        "projects": [],
    }

    def fake_llm(_messages):
        """返回包含标准技能、唯一别名和未知值的模型结果。

        Args:
            _messages: 测试不使用的模型消息列表。

        Returns:
            str: 待归一的正向标签 JSON 文本。
        """
        return json.dumps({
            "positiveTags": [
                {"dimension": "workYears", "workYearsValueArray": ["15年+"]},
                {
                    "dimension": "skills",
                    "skillsValueArray": ["FPGA", "FPGA开发", "未知技能"],
                },
                {"dimension": "industry", "industryValueArray": [
                    "航空航天/国防科技", "通信/物联网/智能硬件",
                    "人工智能/数据智能",
                ]},
            ],
            "confidence": 0.9,
        }, ensure_ascii=False)

    result = extract_positive_tags_llm(resume, {}, fake_llm)
    by_dimension = {tag["dimension"]: tag for tag in result["positiveTags"]}
    assert by_dimension["workYears"]["workYearsValueArray"] == ["1年以下"]
    assert by_dimension["skills"]["skillsValueArray"] == ["FPGA"]
    assert by_dimension["industry"]["industryValueArray"] == []


def test_llm_skills_without_resume_evidence_are_rejected():
    """验证模型输出的短词技能必须通过简历证据边界校验。"""
    resume = {
        "candidate": {"selfEvaluation": "多年 FPGA 开发经验"},
        "educations": [],
        "works": [{
            "company": "测试公司",
            "position": "FPGA 工程师",
            "description": "实现 biss-c 编码器协议并完成 FPGA 逻辑开发。",
            "startDate": "2020-01",
            "endDate": "2024-01",
        }],
        "projects": [],
    }

    def fake_llm(_messages):
        """返回包含无证据短词技能的模型结果。

        Args:
            _messages: 测试不使用的模型消息列表。

        Returns:
            包含 C、C++ 和 FPGA 的正向标签 JSON。
        """
        return json.dumps({
            "positiveTags": [{
                "dimension": "skills",
                "skillsValueArray": ["C", "C++", "FPGA"],
            }],
            "confidence": 0.9,
        }, ensure_ascii=False)

    result = extract_positive_tags_llm(resume, {}, fake_llm)
    skills = next(
        item for item in result["positiveTags"] if item["dimension"] == "skills"
    )

    assert skills["skillsValueArray"] == ["FPGA"]
    assert skills["source"] == "hybrid"


def test_missing_llm_skill_dimension_is_filled_from_resume_evidence():
    """验证模型漏掉技能维度时仍保留简历中的标准技能证据。"""
    resume = {
        "candidate": {"selfEvaluation": "多年 FPGA 开发经验"},
        "educations": [],
        "works": [],
        "projects": [],
    }

    def fake_llm(_messages):
        """返回不包含技能维度的有效正向标签 JSON。

        Args:
            _messages: 测试不使用的模型消息列表。

        Returns:
            仅包含行业维度的正向标签 JSON。
        """
        return json.dumps({
            "positiveTags": [{
                "dimension": "industry",
                "industryValueArray": [],
            }],
            "confidence": 0.9,
        }, ensure_ascii=False)

    result = extract_positive_tags_llm(resume, {}, fake_llm)
    skills = next(
        item for item in result["positiveTags"] if item["dimension"] == "skills"
    )

    assert skills == {
        "dimension": "skills",
        "skillsValueArray": ["FPGA"],
        "source": "rule",
    }
