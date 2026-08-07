"""构造标签值到独立 ID 的受约束语义归一 Prompt。

本模块只负责提供候选映射上下文。模型结果必须由调用方再次通过
`TagValueIdMapper` 校验，不能直接写入正式标签数据。
"""
from __future__ import annotations

import json

from tag.tag_value_id_mapper import TagValueIdMapper, get_tag_value_id_mapper


def build_tag_id_mapping_prompt(
    dimension: str,
    values: list[str],
    mapper: TagValueIdMapper | None = None,
) -> str:
    """构造限定模型只能选择现有 ID 与标准值的归一提示词。

    Args:
        dimension (str): 需要归一的固定标签维度。
        values (list[str]): 模型或外部系统提供的原始标签值。
        mapper (TagValueIdMapper | None): 可选映射器，测试或隔离配置时注入。

    Returns:
        str: 包含输入、候选表和固定输出格式的完整提示词。

    Note:
        技能候选包含 Excel 分类，便于模型区分同名或近义技能。调用方仍需用
        确定性映射器校验模型返回的每组 `id` 和 `value`。
    """
    active_mapper = mapper or get_tag_value_id_mapper()
    candidates = []
    for item in active_mapper.records(dimension):
        candidate = {
            "id": item["id"],
            "value": item["value"],
            "aliases": item["aliases"],
        }
        if item.get("category"):
            candidate["category"] = item["category"]
        candidates.append(candidate)
    input_json = json.dumps(values, ensure_ascii=False, indent=2)
    candidates_json = json.dumps(candidates, ensure_ascii=False, indent=2)
    return f"""# 标签值与 ID 归一任务

## 1. 输入

1. 标签维度：`{dimension}`
2. 待归一标签：

{input_json}

## 2. 候选映射表

{candidates_json}

## 3. 执行步骤

1. 逐个读取待归一标签。
2. 对照候选映射表中的标准值、唯一别名和分类判断匹配项。
3. 唯一匹配时返回候选表中原样存在的 `id` 和 `value`。
4. 无法匹配或存在多个可能匹配项时，将原输入放入 `unknown`。

## 4. 输出格式

{{
  "resolved": [
    {{"input": "原输入", "id": "候选ID", "value": "候选标准值"}}
  ],
  "unknown": ["无法唯一匹配的原输入"]
}}

## 5. 输出硬约束

1. 直接输出一个 JSON 对象，不得添加说明、注释或 Markdown 代码块。
2. 只能返回候选映射表中同时存在且互相对应的 `id` 和 `value`。
3. 禁止创建新 ID、修改标准值或将歧义输入强制绑定到任一候选项。
"""
