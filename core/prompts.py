"""兼容旧调用方的 Prompt 常量导出入口。

提示词实现已按简历解析、正向标签、负向标签和公共打标职责迁移到
`core.prompting` 子模块。新代码应从具体子模块导入；本模块保留原有常量名，
避免外部调用方在升级时发生导入错误。
"""

from core.prompting.common import TAGGER_SYSTEM
from core.prompting.negative_tags import (
    GENERIC_NEG_DIM_PROMPT,
    NEGATIVE_PROMPT_HEADER,
    NEGATIVE_TAG_SCHEMA,
)
from core.prompting.positive_tags import (
    DIM_PROMPT,
    GENERIC_DIM_PROMPT,
    POSITIVE_PROMPT_HEADER,
    POSITIVE_TAG_SCHEMA,
)
from core.prompting.resume_parsing import (
    BATCH_SYSTEM,
    CANDIDATE_KEYS,
    EDU_KEYS,
    PROJ_KEYS,
    PROMPT_HR,
    SCHEMA_KEYS,
    WORK_KEYS,
)


__all__ = [
    "BATCH_SYSTEM",
    "CANDIDATE_KEYS",
    "DIM_PROMPT",
    "EDU_KEYS",
    "GENERIC_DIM_PROMPT",
    "GENERIC_NEG_DIM_PROMPT",
    "NEGATIVE_PROMPT_HEADER",
    "NEGATIVE_TAG_SCHEMA",
    "POSITIVE_PROMPT_HEADER",
    "POSITIVE_TAG_SCHEMA",
    "PROJ_KEYS",
    "PROMPT_HR",
    "SCHEMA_KEYS",
    "TAGGER_SYSTEM",
    "WORK_KEYS",
]
