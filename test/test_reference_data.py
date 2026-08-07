"""验证院校、公司、行业和技能上下文的确定性规则。"""

import json

import pytest

from tag.reference_data import (
    DEFAULT_EDUCATION_DATA_FILE,
    ReferenceDataError,
    load_company_reference_data,
    load_education_reference_data,
)
from tag.tag_data import (
    classify_companies,
    classify_education,
    classify_industry,
)
from tag.tagger import (
    _contains_skill_term,
    _extract_skills_rule,
    _normalize_positive_tags,
    _tagger_load_cfg,
    extract_positive_tags_rule,
)


def test_education_reference_has_complete_domestic_counts():
    """验证国内四类院校名单数量和集合关系完整。"""
    reference = load_education_reference_data()

    assert len(reference.categories["C9"]) == 9
    assert len(reference.categories["985"]) == 39
    assert len(reference.categories["211"]) == 115
    assert len(reference.categories["双一流"]) == 147
    assert reference.categories["C9"] <= reference.categories["985"]
    assert reference.categories["985"] <= reference.categories["211"]
    assert reference.categories["211"] <= reference.categories["双一流"]


def test_education_reference_has_complete_qs_2026_snapshot():
    """验证 QS 2026 前 50、前 100 集合数量、边界和集合关系。"""
    reference = load_education_reference_data()
    top50 = reference.categories["海外名校QS50"]
    top100 = reference.categories["海外名校QS100"]

    assert len(top50) == 50
    assert len(top100) == 100
    assert top50 <= top100
    assert "延世大学" in top50
    assert "苏黎世大学" in top100
    assert "苏黎世大学" not in top50
    assert {"卡尔斯鲁厄理工学院", "米兰理工大学"} <= top100


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("Yonsei University", "延世大学"),
        ("University of Zurich", "苏黎世大学"),
        ("Politecnico di Milano", "米兰理工大学"),
        ("Karlsruhe Institute of Technology", "卡尔斯鲁厄理工学院"),
        ("Institut Polytechnique de Paris", "巴黎理工学院"),
        ("University of Illinois Urbana-Champaign", "伊利诺伊大学厄巴纳-香槟分校"),
    ],
)
def test_education_reference_resolves_qs_2026_english_aliases(
    alias: str,
    canonical: str,
):
    """验证 QS 2026 院校英文名称能够解析为统一中文名称。

    Args:
        alias (str): QS 院校英文名称或常用英文名。
        canonical (str): 预期中文标准名称。
    """
    reference = load_education_reference_data()

    assert reference.resolve(alias) == canonical


def test_education_reference_rejects_incomplete_qs_snapshot(tmp_path):
    """验证不完整 QS 快照不能通过参考数据加载契约。

    Args:
        tmp_path (Path): Pytest 提供的临时目录。
    """
    payload = json.loads(DEFAULT_EDUCATION_DATA_FILE.read_text(encoding="utf-8"))
    payload["overseasRankingSnapshot"]["complete"] = False
    snapshot = tmp_path / "incomplete_education_data.json"
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ReferenceDataError, match="必须标记为完整"):
        load_education_reference_data(snapshot)


def test_education_reference_rejects_incomplete_qs_count(tmp_path):
    """验证标记完整但院校数量不足的 QS 快照仍会被拒绝。

    Args:
        tmp_path (Path): Pytest 提供的临时目录。
    """
    payload = json.loads(DEFAULT_EDUCATION_DATA_FILE.read_text(encoding="utf-8"))
    payload["overseasRankingSnapshot"]["top50"].pop()
    snapshot = tmp_path / "short_education_data.json"
    snapshot.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ReferenceDataError, match="QS Top50 应为 50 所"):
        load_education_reference_data(snapshot)


@pytest.mark.parametrize(
    ("school", "expected", "unexpected"),
    [
        (
            "Yonsei University",
            {"海外名校QS50", "海外名校QS100", "本科"},
            set(),
        ),
        (
            "University of Zurich",
            {"海外名校QS100", "硕士"},
            {"海外名校QS50"},
        ),
        (
            "Peking University",
            {"C9", "985", "211", "双一流", "本科"},
            {"海外名校QS50", "海外名校QS100"},
        ),
    ],
)
def test_education_classification_uses_qs_snapshot_without_mainland_false_positive(
    school: str,
    expected: set[str],
    unexpected: set[str],
):
    """验证 QS 边界院校命中且内地院校不会误打海外标签。

    Args:
        school (str): 学校英文名称或标准名称。
        expected (set[str]): 必须生成的教育标签。
        unexpected (set[str]): 不得生成的教育标签。
    """
    degree = "硕士" if school == "University of Zurich" else "本科"
    values = set(classify_education([school], [degree]))

    assert expected <= values
    assert not unexpected & values


@pytest.mark.parametrize(
    ("school", "expected", "unexpected"),
    [
        ("云南大学", {"211", "双一流", "本科"}, {"985"}),
        ("西北工业大学", {"985", "211", "双一流", "本科"}, set()),
        ("黑龙江大学", {"本科"}, {"211", "双一流"}),
        ("华东政法大学", {"本科"}, {"211", "双一流"}),
        ("南方科技大学", {"双一流", "本科"}, {"211", "985"}),
        ("电子科大", {"985", "211", "双一流", "本科"}, set()),
    ],
)
def test_education_classification_uses_corrected_lists(
    school: str,
    expected: set[str],
    unexpected: set[str],
):
    """验证历史错项、双一流新增院校和常见简称。

    Args:
        school (str): 测试学校标准名称或简称。
        expected (set[str]): 必须出现的教育标签。
        unexpected (set[str]): 不得出现的教育标签。
    """
    values = set(classify_education([school], ["本科"]))

    assert expected <= values
    assert not unexpected & values


def test_company_reference_supports_multiple_qualifications():
    """验证同一公司可以命中多个经过校验的资质。"""
    reference = load_company_reference_data()
    assert reference.version == "2026.08.05"

    values = {
        item["value"]
        for item in classify_companies(["深圳市雷赛智能控制股份有限公司"])
    }

    assert values == {"上市公司", "高新技术企业", "专精特新企业"}


def test_company_qualification_rejects_customer_context():
    """验证客户资质描述不会被错误归到任职公司。"""
    result = classify_companies(
        ["某软件有限公司"],
        evidence_text="负责服务上市公司客户，并对接高新技术企业供应商。",
    )

    assert result == []


def test_company_qualification_accepts_explicit_self_evidence():
    """验证未知公司仍可使用明确且非客户语境的资质证据。"""
    values = {
        item["value"]
        for item in classify_companies(
            ["某制造有限公司"],
            evidence_text="公司是国家高新技术企业和专精特新企业。",
        )
    }

    assert values == {"高新技术企业", "专精特新企业"}


@pytest.mark.parametrize(
    "text",
    [
        "负责网络运维和在线状态监控。",
        "教育经历：计算机专业，完成机器学习课程。",
        "计划学习机器学习和深度学习。",
        "个人爱好是参加游戏比赛。",
    ],
)
def test_industry_rules_reject_broad_or_excluded_context(text: str):
    """验证宽泛词和学习、爱好语境不能独立触发行业标签。

    Args:
        text (str): 不应形成行业经历证据的文本。
    """
    assert classify_industry(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("负责电商平台的交易和订单系统。", "互联网/电商"),
        ("负责银行核心系统、支付清算和金融风控。", "金融/银行/证券/保险"),
        ("交付多租户 SaaS、CRM 和 ERP 企业软件。", "企业服务/SaaS"),
        ("负责手游服务端、游戏引擎和电竞业务。", "游戏/传媒/文娱"),
        ("负责 K12 在线题库和课程平台。", "教育/培训"),
        ("建设医院诊疗系统并支持医疗器械研发。", "医疗健康/生物医药"),
        ("训练大模型并部署推理服务。", "人工智能/数据智能"),
        ("开发 ADAS、车联网和自动驾驶系统。", "汽车/出行"),
        ("开发 MES 和自动化产线系统。", "制造业/工业自动化/新材料"),
        ("负责晶圆制造和芯片封装测试。", "半导体/集成电路"),
        ("研发 5G 基站和无线通信设备。", "通信/物联网/智能硬件"),
        ("建设仓储配送、TMS 和供应链履约平台。", "物流/供应链"),
        ("研发卫星雷达和无人机电子对抗系统。", "航空航天/国防科技"),
        ("建设光伏电站和储能系统。", "能源/电力/新能源"),
        ("负责连锁门店的新零售会员系统。", "消费品/零售"),
        ("负责地产项目的建筑工程和物业管理。", "房地产/建筑/物业"),
    ],
)
def test_industry_rules_require_strong_business_evidence(text: str, expected: str):
    """验证明确业务或技术证据能够触发对应行业。

    Args:
        text (str): 包含强行业证据的工作描述。
        expected (str): 预期行业标准值。
    """
    assert expected in {item["value"] for item in classify_industry(text)}


def test_industry_rules_return_at_most_three_strongest_values():
    """验证跨行业履历按证据强度最多输出三个行业。"""
    result = classify_industry(
        "负责半导体芯片设计、5G通信设备和智能硬件开发，并建设新能源储能系统，"
        "同时交付连锁门店零售平台。"
    )

    assert len(result) == 3
    assert {item["value"] for item in result} == {
        "半导体/集成电路",
        "通信/物联网/智能硬件",
        "能源/电力/新能源",
    }


def test_company_official_qualification_hierarchy_and_new_values():
    """验证新增官方资质值及小巨人父级标签能够同时输出。"""
    result = classify_companies(
        ["某制造有限公司"],
        evidence_text=(
            "公司是国家级专精特新小巨人企业和制造业单项冠军企业，"
            "并拥有国家级企业技术中心。"
        ),
    )

    assert {item["value"] for item in result} == {
        "专精特新企业",
        "专精特新小巨人",
        "制造业单项冠军",
        "国家级企业技术中心",
    }


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("不熟悉 Python", "Python"),
        ("未使用 Kubernetes", "Kubernetes"),
        ("计划学习 PyTorch", "PyTorch"),
        ("仅了解 Java", "Java"),
        ("了解机器学习", "机器学习"),
        ("岗位要求：熟悉 Docker", "Docker"),
        ("no experience with TensorFlow", "TensorFlow"),
        ("Python 经验不足", "Python"),
        ("所学课程包括 C++、数据结构和操作系统", "C++"),
    ],
)
def test_skill_match_rejects_negative_context(text: str, term: str):
    """验证否定、计划和岗位要求语境不构成技能证据。

    Args:
        text (str): 包含技能词但不代表实际能力的文本。
        term (str): 待判断技能标准值。
    """
    assert _contains_skill_term(text, term) is False


def test_skill_match_keeps_independent_positive_evidence():
    """验证否定命中之后出现真实项目证据时仍保留技能。"""
    text = "不熟悉 Python，但在项目中使用 Python 开发数据服务。"

    assert _contains_skill_term(text, "Python") is True


def test_rule_skill_extraction_filters_requirement_only_terms():
    """验证技能规则不会把简历中的岗位要求当作候选人技能。"""
    resume = {
        "candidate": {
            "selfEvaluation": "岗位要求熟悉 Docker；本人使用 Python 开发服务。",
            "expectedPosition": "",
        },
        "educations": [],
        "works": [],
        "projects": [],
    }

    values = _extract_skills_rule(resume)

    assert "Python" in values
    assert "Docker" not in values


def test_rule_fallback_marks_company_and_industry_source_as_rule():
    """验证确定性公司和行业兜底不会伪装为 AI 来源。"""
    resume = {
        "candidate": {"selfEvaluation": "", "expectedPosition": ""},
        "educations": [],
        "works": [{
            "company": "华为技术有限公司",
            "position": "算法工程师",
            "description": "负责训练大模型并部署推理服务。",
            "startDate": "2024-01",
            "endDate": "2025-01",
        }],
        "projects": [],
    }

    result = extract_positive_tags_rule(resume)
    entries = {
        item["dimension"]: item
        for item in result["positiveTags"]
    }

    assert entries["companyBg"]["source"] == "rule"
    assert entries["industry"]["source"] == "rule"


def test_normalization_merges_verified_company_qualifications():
    """验证模型成功时仍补全版本化数据中的公司多项资质。"""
    resume = {
        "candidate": {},
        "educations": [],
        "works": [{
            "company": "深圳市雷赛智能控制股份有限公司",
            "position": "FPGA工程师",
            "description": "负责伺服控制板卡研发。",
            "startDate": "2020-01",
            "endDate": "2024-01",
        }],
        "projects": [],
    }
    tags = [{
        "dimension": "companyBg",
        "companyBgValueArray": ["上市公司", "行业龙头"],
        "source": "llm",
    }]

    normalized = _normalize_positive_tags(
        tags,
        resume,
        _tagger_load_cfg()["positiveDimensions"],
        llm_assisted=True,
    )
    company = next(item for item in normalized if item["dimension"] == "companyBg")

    assert set(company["companyBgValueArray"]) == {
        "上市公司",
        "高新技术企业",
        "专精特新企业",
    }
    assert company["source"] == "hybrid"


def test_normalization_rejects_model_industry_without_strong_evidence():
    """验证模型行业值没有强业务证据时不会进入最终标签。"""
    resume = {
        "candidate": {},
        "educations": [],
        "works": [{
            "company": "某运维公司",
            "position": "网络运维工程师",
            "description": "负责网络运维和在线状态监控。",
            "startDate": "2020-01",
            "endDate": "2024-01",
        }],
        "projects": [],
    }
    tags = [{
        "dimension": "industry",
        "industryValueArray": ["互联网/电商"],
        "source": "llm",
    }]

    normalized = _normalize_positive_tags(
        tags,
        resume,
        _tagger_load_cfg()["positiveDimensions"],
        llm_assisted=True,
    )
    industry = next(item for item in normalized if item["dimension"] == "industry")

    assert industry["industryValueArray"] == []
