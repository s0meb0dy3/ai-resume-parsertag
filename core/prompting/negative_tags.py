"""定义负向语义标签检测使用的输出 Schema 和动态 Prompt 模板。

本模块只处理 `detectionType="llm"` 的自定义负向维度。内置负向维度和
`detectionType="rule"` 的自定义维度仍由确定性规则执行，不进入模型提示词。
"""


NEGATIVE_TAG_SCHEMA = {
    "negativeTags": [
        {
            "dimension": "customRisk",
            "hits": [
                {
                    "tag": "负面标签名",
                    "severity": "medium",
                    "matchedContent": "简历中支撑判断的原文片段或推理依据",
                }
            ],
        }
    ]
}


NEGATIVE_PROMPT_HEADER = """# 负向标签检测任务

## 1. 输入

### 1.1 候选人简历

{resume_json}

## 2. 判断边界

1. 只检测下方明确列出的风险维度和规则。
2. 每个风险命中都必须有简历原文或可复核事实作为依据。
3. 信息缺失不等于风险，不得因字段为空而推断负面结论。
4. 不得输出确定性规则层负责的其他风险。

## 3. 执行步骤

1. 逐个阅读风险维度及其规则。
2. 在简历中定位满足规则的直接证据。
3. 记录标签名、规则指定的严重程度和最短充分证据。
4. 删除无证据、重复或相互矛盾的命中。

## 4. 输出格式

{schema_example}

### 4.1 输出硬约束

1. 直接输出一个 JSON 对象，不得添加说明、注释或 Markdown 代码块。
2. 只输出确实命中的维度。
3. 某维度没有风险时省略整个维度对象，不得输出空 `hits`。
4. `severity` 必须使用对应规则指定的值。
5. `matchedContent` 必须简洁说明可复核证据。

## 5. 各风险维度检测规则

"""


GENERIC_NEG_DIM_PROMPT = """### 风险维度 `{dimension}`：{display_name}

按以下规则逐条判断：

{rules_text}
"""
