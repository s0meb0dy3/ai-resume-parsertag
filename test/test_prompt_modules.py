"""验证细分 Prompt 模块的职责边界、格式和旧入口兼容性。"""

from datetime import date
import json
from pathlib import Path

from core import prompts as compatibility_prompts
from core.prompting.common import TAGGER_SYSTEM
from core.prompting.negative_tags import (
    NEGATIVE_PROMPT_HEADER,
    NEGATIVE_TAG_SCHEMA,
)
from core.prompting.positive_tags import (
    POSITIVE_PROMPT_HEADER,
    POSITIVE_TAG_SCHEMA,
)
from core.prompting.positive_value_guidance import (
    VALUE_GUIDANCE_BY_DIMENSION,
    build_positive_value_guidance,
)
from core.prompting.resume_parsing import (
    BATCH_SYSTEM,
    PROMPT_HR,
    get_resume_prompt_bundle,
)
from tag.tag_value_id_mapper import get_tag_value_id_mapper
from tag.tagger import _build_negative_prompt, _build_positive_prompt


ROOT = Path(__file__).resolve().parents[1]


def test_compatibility_module_reexports_split_prompt_constants():
    """验证旧导入路径继续返回细分模块中的同一组常量对象。"""
    assert compatibility_prompts.PROMPT_HR is PROMPT_HR
    assert compatibility_prompts.POSITIVE_PROMPT_HEADER is POSITIVE_PROMPT_HEADER
    assert compatibility_prompts.POSITIVE_TAG_SCHEMA is POSITIVE_TAG_SCHEMA
    assert compatibility_prompts.NEGATIVE_PROMPT_HEADER is NEGATIVE_PROMPT_HEADER
    assert compatibility_prompts.NEGATIVE_TAG_SCHEMA is NEGATIVE_TAG_SCHEMA
    assert compatibility_prompts.TAGGER_SYSTEM is TAGGER_SYSTEM


def test_resume_prompt_uses_numbered_sections_and_explicit_output_contract():
    """验证精简后的解析提示词按读取顺序呈现任务、边界和输出约束。"""
    headings = [
        "## 1. 任务目标",
        "## 2. 职位与项目角色提取规则",
        "## 3. JSON 字段结构",
        "## 4. 输出硬约束",
    ]
    positions = [PROMPT_HR.index(heading) for heading in headings]

    assert positions == sorted(positions)
    assert all(f'"{key}"' in PROMPT_HR for key in ("candidate", "educations", "works", "projects"))
    assert '"gender": "性别编码：男=1，女=2，未知=0"' in PROMPT_HR
    assert "原文明确写至今、在读、目前等仍在读状态时统一填至今" in PROMPT_HR
    assert "原文未提供结束日期时留空，不得根据学制推测" in PROMPT_HR
    assert "原文明确写至今、目前、现在等仍在职状态时统一填至今" in PROMPT_HR
    assert "原文明确写至今、目前、现在等仍在进行状态时统一填至今" in PROMPT_HR
    assert PROMPT_HR.count("原文未提供时留空，不得推断") == 4
    assert "candidate.workYears" in PROMPT_HR
    assert "你是专业的简历解析助手" not in PROMPT_HR
    assert "候选人与家庭成员边界" not in PROMPT_HR
    assert "## 6. 执行步骤" not in PROMPT_HR
    assert "根据职责中的明确证据选择最匹配的职位关键词" not in PROMPT_HR
    assert "仅在职责存在明确证据时补充最匹配的通用名称" in PROMPT_HR
    assert "除第 2 节允许的职位或项目角色补全外" in PROMPT_HR
    assert "Python 确定性候选只用于提醒复核" not in PROMPT_HR
    assert BATCH_SYSTEM.startswith(PROMPT_HR)
    assert "## 5. 部分页面处理" in BATCH_SYSTEM


def test_resume_prompt_bundle_refreshes_month_and_cache_key():
    """验证常驻进程跨月后使用新月份及其独立缓存键。"""
    august_prompt, august_batch, august_key = get_resume_prompt_bundle(
        date(2026, 8, 31)
    )
    september_prompt, september_batch, september_key = get_resume_prompt_bundle(
        date(2026, 9, 1)
    )

    assert "2026-08" in august_prompt
    assert "2026-08" in august_batch
    assert "2026-09" in september_prompt
    assert "2026-09" in september_batch
    assert august_key != september_key


def test_positive_prompt_injects_runtime_values_into_structured_sections():
    """验证正向提示词将运行时候选值写入对应维度的分点规则。"""
    prompt = _build_positive_prompt(
        [{
            "dimension": "skills",
            "displayName": "技能标签",
            "enabled": True,
            "allowedValues": ["Python", "SQL"],
        }],
        '{"candidate":{"name":"张三"}}',
        "后端工程师",
        "Python",
        "负责 API 开发",
    )

    assert "## 1. 输入" in prompt
    assert "## 3. 执行步骤" in prompt
    assert "### 5.4 `skills`：技能标签" in prompt
    assert "候选值：Python|SQL" in prompt
    assert "#### 5.4.1 逐值判定说明" in prompt
    assert "`Python`：分类为“编程语言与开发框架”" in prompt
    assert "可识别唯一别名：Python3、Python开发、Python编程" in prompt
    assert "`SQL`：分类为“编程语言与开发框架”" in prompt
    assert "岗位要求中的技能不得写入候选人技能" in prompt


def test_all_runtime_positive_values_have_individual_prompt_guidance():
    """验证每个运行时正向允许值均生成独立且可审查的判定说明。"""
    config = json.loads(
        (ROOT / "schemas/tag_config_runtime.json").read_text(encoding="utf-8")
    )
    mapper = get_tag_value_id_mapper()
    skill_records = mapper.records("skills")

    for dimension in config["positiveDimensions"]:
        name = dimension["dimension"]
        values = dimension["allowedValues"]
        guidance = build_positive_value_guidance(
            name,
            values,
            skill_records if name == "skills" else None,
        )
        assert len(guidance.splitlines()) == len(values)
        for value in values:
            assert f"`{value}`：" in guidance
            assert f"标准输出必须原样使用 `{value}`" in guidance


def test_authored_guidance_covers_every_fixed_dimension_value():
    """验证固定维度当前允许值不会回退到无业务语义的通用说明。"""
    config = json.loads(
        (ROOT / "schemas/tag_config_runtime.json").read_text(encoding="utf-8")
    )
    for dimension in config["positiveDimensions"]:
        name = dimension["dimension"]
        if name == "skills":
            continue
        assert set(dimension["allowedValues"]) == set(
            VALUE_GUIDANCE_BY_DIMENSION[name]
        )


def test_skill_prompt_excludes_broad_alias_and_keeps_qualified_aliases():
    """验证技能提示词排除宽泛别名并保留带标准技能名的限定别名。"""
    prompt = _build_positive_prompt(
        [{
            "dimension": "skills",
            "displayName": "技能标签",
            "enabled": True,
            "allowedValues": ["PyTorch", "TensorFlow"],
        }],
        '{"candidate":{"name":"张三"}}',
        "算法工程师",
        "深度学习框架",
        "负责模型训练",
    )
    guidance = prompt.split("#### 5.4.1 逐值判定说明", 1)[1]

    assert "`PyTorch`：" in guidance
    assert "`TensorFlow`：" in guidance
    assert "可识别唯一别名：深度学习框架" not in guidance
    assert "PyTorch深度学习框架" in guidance
    assert "TensorFlow深度学习框架" in guidance


def test_short_language_skills_have_explicit_false_positive_boundaries():
    """验证 C 和 C++ 的逐值说明排除协议后缀及普通字符误命中。"""
    prompt = _build_positive_prompt(
        [{
            "dimension": "skills",
            "displayName": "技能标签",
            "enabled": True,
            "allowedValues": ["C", "C++"],
        }],
        '{"candidate":{"name":"张三"}}',
        "嵌入式工程师",
        "C/C++",
        "负责底层开发",
    )

    assert "`C`：" in prompt
    assert "`biss-c`、CDR、PC、型号后缀和普通字母 C 均不是技能证据" in prompt
    assert "`C++`：" in prompt
    assert "单独的 C、加号和型号字符不判定" in prompt


def test_negative_prompt_lists_enabled_rules_with_recheckable_evidence_contract():
    """验证负向提示词只注入启用规则并要求输出可复核证据。"""
    prompt = _build_negative_prompt(
        [{
            "dimension": "customRisk",
            "displayName": "自定义风险",
            "enabled": True,
            "rules": {
                "enabled_rule": {
                    "tagName": "经历描述矛盾",
                    "severity": "high",
                    "description": "同一经历的时间或机构描述互相矛盾",
                    "enabled": True,
                },
                "disabled_rule": {
                    "tagName": "停用规则",
                    "severity": "low",
                    "enabled": False,
                },
            },
        }],
        '{"candidate":{"name":"张三"}}',
    )

    assert "## 2. 判断边界" in prompt
    assert "### 风险维度 `customRisk`：自定义风险" in prompt
    assert "1. 经历描述矛盾（严重程度：high）" in prompt
    assert "停用规则" not in prompt
    assert "`matchedContent` 必须简洁说明可复核证据" in prompt
