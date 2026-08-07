"""定义正向标签提取使用的输出 Schema 和动态 Prompt 模板。

运行时由 `tag.tagger` 按已启用维度注入 `allowedValues`。本模块只维护模型
需要理解的任务步骤、固定维度规则和自定义维度输出契约，不读取运行时配置。
"""


POSITIVE_TAG_SCHEMA = {
    "positiveTags": [
        {
            "dimension": "education",
            "educationValueArray": [],
            "source": "rule",
        },
        {
            "dimension": "companyBg",
            "companyBgValueArray": ["行业龙头", "上市公司"],
            "source": "llm",
            "confidence": 0.90,
        },
        {
            "dimension": "workYears",
            "workYearsValueArray": [],
            "source": "rule",
        },
        {
            "dimension": "skills",
            "skillsValueArray": ["Python", "机器学习", "Kubernetes"],
            "source": "llm",
        },
        {
            "dimension": "industry",
            "industryValueArray": [
                "互联网/电商",
                "企业服务/SaaS",
                "人工智能/数据智能",
            ],
            "source": "llm",
            "confidence": 0.85,
        },
    ],
    "confidence": 0.88,
}


POSITIVE_PROMPT_HEADER = """# 正向标签提取任务

## 1. 输入

### 1.1 候选人简历

{resume_json}

### 1.2 目标岗位参考

1. 职位：{job_title}
2. 必要技能：{required_skills}
3. 岗位描述：{job_description}

## 2. 判断边界

1. 候选人标签只能来自简历中的明确事实或可解释证据。
2. 岗位信息只用于评估匹配背景，不得写成候选人已经具备的经历或技能。
3. 每个维度只能输出其 `allowedValues` 中的标准值。
4. 没有可靠证据的维度输出空数组，不得猜测。

## 3. 执行步骤

1. 阅读完整结构化简历，分别收集教育、公司、工作年限、技能和行业证据。
2. 按下方维度规则将证据归一到候选值。
3. 对数组去重，并按证据强度填写允许使用的 `confidence`。
4. 检查输出字段名必须符合 `{{dimension}}ValueArray` 规则。

## 4. 输出格式

{schema_example}

### 4.1 输出硬约束

1. 直接输出一个 JSON 对象，不得添加说明、注释或 Markdown 代码块。
2. 所有维度值必须使用字符串数组。
3. 字段名必须使用 `{{dimension}}ValueArray`。
4. 没有匹配项时对应数组填写 `[]`。

## 5. 各维度打标规则

"""


# `{values}` 和 `{value_guidance}` 在每次请求前由运行时配置注入。
DIM_PROMPT: dict[str, str] = {
    "education": """### 5.1 `education`：教育背景

1. 输出字段：`educationValueArray`，类型为字符串数组，不带 `confidence`。
2. 候选值：{values}
3. 模型固定返回空数组 `[]`。
4. 代码根据全部 `educations[].school` 和最高学历执行确定性分类并覆盖结果。
5. 学历无法识别时，代码不会默认“本科”。

#### 5.1.1 逐值判定说明

{value_guidance}""",
    "companyBg": """### 5.2 `companyBg`：公司背景

1. 输出字段：`companyBgValueArray`，类型为字符串数组，携带整体 `confidence`。
2. 候选值：{values}
3. 逐一检查 `works[].company` 的真实公司背景。
4. 规模声誉、所有制、资本状态和官方资质可以同时命中多个值。
5. 模型值最终必须通过版本化公司事实库或公司自身明确证据复核。
6. 客户、供应商、合作方的资质不能归到候选人任职公司。
7. 多个匹配项只保留去重后的候选值，`confidence` 使用最高置信度。

#### 5.2.1 逐值判定说明

{value_guidance}""",
    "workYears": """### 5.3 `workYears`：工作年限

1. 输出字段：`workYearsValueArray`，类型为单元素字符串数组，不带 `confidence`。
2. 候选值：{values}
3. 模型固定返回空数组 `[]`。
4. 代码会根据正式工作区间并集和毕业时间，覆盖为“应届”“往届无经验”
   “1年以下”“1-3年（满1年）”“3-5年（满3年）”
   “5-10年（满5年）”或“10年及以上”。
5. 模型不得自行估算或猜测工作年限。

#### 5.3.1 逐值判定说明

{value_guidance}""",
    "skills": """### 5.4 `skills`：技能标签

1. 输出字段：`skillsValueArray`，类型为字符串数组，不带 `confidence`。
2. 候选值：{values}
3. 候选值已由规则层根据结构化简历和内部抽取原文预选，不代表最终全部输出。
4. 结合 `works[].description` 和 `projects[].description` 对候选值排序和筛选。
5. 只输出简历明确体现且属于候选值的技能。
6. 统一大小写和标准名称，例如“python”归一为“Python”。
7. 去重后最多输出 12 个技能；候选值为空时必须输出 `[]`。
8. 岗位要求中的技能不得写入候选人技能。

#### 5.4.1 逐值判定说明

{value_guidance}""",
    "industry": """### 5.5 `industry`：行业背景

1. 输出字段：`industryValueArray`，类型为字符串数组，携带整体 `confidence`。
2. 候选值：{values}
3. 依据公司主营业务和项目服务对象判断行业。
4. 只输出有公司或项目强证据的候选值，按证据强度最多输出 3 个。
5. `confidence` 使用全部命中行业中的最高置信度。

#### 5.5.1 逐值判定说明

{value_guidance}""",
}


GENERIC_DIM_PROMPT = """### 自定义维度 `{dimension}`：{display_name}

1. 输出字段：`{dimension}ValueArray`，类型为字符串数组，携带整体 `confidence`。
2. 候选值：{values}
3. 根据候选人完整简历，从候选值中选择所有有明确证据的匹配项。
4. `allowedValues` 为空时，才允许自由提取与该维度直接相关的标签。
5. 必须在 `positiveTags` 中输出该维度；没有匹配项时填写空数组 `[]`。
6. `confidence` 取值范围为 0.70 至 1.00。

#### 逐值判定说明

{value_guidance}
"""
