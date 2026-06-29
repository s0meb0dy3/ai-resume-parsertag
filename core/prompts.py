"""
LLM Prompt 常量集中管理模块。

所有发往 LLM 的 system / user 消息模板统一在此定义，其他模块只做 import。
Schema 结构体使用 Python dict + json.dumps() 定义，与文本模板分离，便于独立维护。

模块结构
────────
1. 简历解析（PDF → JSON）
   - POSITION_RULES     职位字段提取规则（嵌入 PROMPT_HR）
   - COMPANY_SCHEMA     输出 JSON 结构模板
   - PROMPT_HR          单批次解析 system prompt
   - BATCH_SYSTEM       多批次解析 system prompt

2. Schema 校验字段集合
   - SCHEMA_KEYS / CANDIDATE_KEYS / EDU_KEYS / WORK_KEYS / PROJ_KEYS

3. 正向打标（JSON → positiveTags）
   - TAGGER_SYSTEM      打标 LLM system prompt
   - POSITIVE_TAG_SCHEMA    输出结构示例
   - POSITIVE_PROMPT_HEADER 动态 prompt 头部模板
   - DIM_PROMPT             5 个内置维度的详细提取指令
   - GENERIC_DIM_PROMPT     API 新增自定义维度的通用指令模板

4. 负向打标 · LLM 语义检测（detectionType="llm"）
   - NEGATIVE_TAG_SCHEMA    输出结构示例
   - NEGATIVE_PROMPT_HEADER 动态 prompt 头部模板
   - GENERIC_NEG_DIM_PROMPT 自定义负向维度的通用指令模板
"""
import json


# ════════════════════════════════════════════════════════════════════
# 1. 简历解析
# ════════════════════════════════════════════════════════════════════

POSITION_RULES = """
## position（works）和 role（projects）字段提取规则

**规则一：有明确职位名称——保留关键词，不截断**
- 复合职位完整保留：如 "算法工程师/AI系统负责人" → "算法工程师/AI系统负责人"
- 带方向说明完整保留：如 "Java开发工程师（自研系统方向）" → "Java开发工程师（自研系统方向）"
- 带级别完整保留：如 "高级软件工程师" / "P7研发工程师"
- 去除猎聘平台冗余标签（"所在部门：" "汇报对象：" "月薪：" 等）

**规则二：无明确职位——根据职责描述推断职位关键词**
| 描述关键词                          | 推断职位               |
|------------------------------------|----------------------|
| 开发/研发/实现/代码/编程              | 软件工程师/开发工程师   |
| 算法/模型/训练/推理/LLM/NLP/CV       | 算法工程师             |
| 架构/系统设计/微服务/分布式            | 架构师/系统工程师       |
| 测试/质量/QA/自动化测试               | 测试工程师             |
| 产品/需求/PRD/原型                   | 产品经理               |
| 数据/分析/BI/报表/指标                | 数据分析师             |
| 运营/增长/用户/活动策划               | 运营专员/运营经理       |
| 销售/客户/商务/BD                    | 销售/客户经理           |
| 研究/论文/实验/课题/科研              | 研究员/研究生           |
| 管理/带领团队/绩效/招聘               | 团队负责人/工程经理     |
| 前端/UI/页面/组件/React/Vue          | 前端工程师             |
| 后端/服务/API/数据库                  | 后端工程师             |
| 运维/DevOps/部署/CI/CD              | 运维工程师             |

**规则三：实习经历** → 职位后加"（实习）"，如 "软件工程师（实习）"

**规则四：科研/学术经历（无明确职位）**
→ 根据学历推断：本科→"本科"，硕士→"硕士研究生"，博士→"博士研究生/研究员"
→ 可加方向：如 "博士研究生（光学方向）"

**输出要求：position/role 只填职位名称，禁止填写职责句子或"职位/职责"等占位文字**
"""

# 输出 JSON 结构模板，嵌入 PROMPT_HR 告知 LLM 期望的字段格式
COMPANY_SCHEMA = {
    "candidate": {
        "name":             "姓名，必要信息",
        "phone":            "电话，必要信息，格式1xxxxxxxxxx",
        "email":            "邮箱",
        "gender":           "性别 男/女 若不存在则不填",
        "age":              "年龄整数，若不存在则填0",
        "address":          "居住地信息，如 北京市朝阳区，若不存在则不填",
        "nativePlace":      "籍贯信息，重要信息，可多次确认",
        "selfEvaluation":   "自我评价，若不存在则不填",
        "expectedSalary":   "期望薪资（月薪）如 25-35K",
        "expectedPosition": "应聘职位，若不存在则不填",
    },
    "educations": [
        {
            "school":    "学校/机构名称",
            "major":     "专业名称",
            "degree":    "学位：学士/硕士/博士，注意区分学位与学历",
            "education": "学历：初中/高中/专科/本科/硕士研究生/博士研究生",
            "startDate": "开始时间，如 2015-09",
            "endDate":   "结束时间，如 2019-07",
        }
    ],
    "works": [
        {
            "company":     "公司/机构全称，保留原文，不得改写或缩写，如公司1",
            "position":    "职位（见下方规则）",
            "description": "根据该公司下的所有项目经历，总结该阶段的核心工作职责与技术方向，不超过300字，若无任何项目或工作描述则不填",
            "startDate":   "开始时间，如 2019-07",
            "endDate":     "结束时间，如 2022-08",
        },
        {
            "company":     "公司/机构全称，保留原文，不得改写或缩写，如公司2",
            "position":    "职位（见下方规则）",
            "description": "根据该公司下的所有项目经历，总结该阶段的核心工作职责与技术方向，不超过300字，若无任何项目或工作描述则不填",
            "startDate":   "开始时间，如 2019-07",
            "endDate":     "结束时间，如 2022-08",
        },
    ],
    "projects": [
        {
            "company":     "所属公司/机构全称，保留原文，不得改写或缩写，若不存在则不填",
            "projectName": "项目完整名称，保留原文，不得改写或缩写，如项目1",
            "role":        "项目角色/职位（见下方规则）",
            "description": "项目描述，包括项目介绍、工作内容等所有相关信息，优先使用简历中的完整原始描述，若原文过于冗长或结构混乱则进行精炼总结，若不存在则不填",
            "startDate":   "开始时间，如 2020-03",
            "endDate":     "结束时间，如 2020-12",
        },
        {
            "company":     "所属公司/机构全称，保留原文，不得改写或缩写，若不存在则不填",
            "projectName": "项目完整名称，保留原文，不得改写或缩写，如项目2",
            "role":        "项目角色/职位（见下方规则）",
            "description": "项目描述，包括项目介绍、工作内容等所有相关信息，优先使用简历中的完整原始描述，若原文过于冗长或结构混乱则进行精炼总结，若不存在则不填",
            "startDate":   "开始时间，如 2020-03",
            "endDate":     "结束时间，如 2020-12",
        },
    ],
}

PROMPT_HR = f"""你是专业的简历解析助手。将给定的简历内容转换为以下 JSON 格式输出。
包含基本信息、教育经历、工作经历、项目经历，确保信息准确完整，不虚构、不推测。

## 重要规则：家庭成员处理

文档中可能包含"家庭成员"或"主要社会关系"等表格，其中按行列出配偶、父母、子女等关系人。
这些人均不是候选人本人，其姓名、工作单位、联系方式等信息一律忽略，不得作为候选人信息提取。

候选人（即填表人本人）识别方法：
- 候选人信息位于文档最开头的"基本信息"表格，在"姓名"字段对应的单元格中
- 家庭成员表格中的"姓名"列出的是家属姓名（如配偶、父母），不是候选人姓名，绝对不能提取为 candidate.name
- 判断依据：若某姓名旁边标有"夫妻/配偶/父母/子女/兄弟/姐妹"等关系字样，则该姓名是家属，忽略

{POSITION_RULES}

## 输出格式

works[].position 和 projects[].role 严格遵守上方职位提取规则：
{json.dumps(COMPANY_SCHEMA, ensure_ascii=False, indent=2)}

## 输出要求（必须严格遵守）

- 直接输出 JSON 对象，禁止添加任何说明文字、注释或 markdown 代码块标记（```json 等）
- 必须包含所有顶层字段：candidate、educations、works、projects，一个都不能省略
- 字段值若无信息：字符串填 ""，数组填 []，数字字段（age/gender）填 0
- 禁止虚构或推测原文中不存在的信息
"""

# 多批次解析时追加说明：当前传入的只是简历的部分页面
BATCH_SYSTEM = PROMPT_HR + "\n注意：以下是简历的部分页面，提取其中包含的所有信息，不存在的字段填空。"


# ════════════════════════════════════════════════════════════════════
# 2. Schema 校验字段集合
# ════════════════════════════════════════════════════════════════════

SCHEMA_KEYS    = {"candidate", "educations", "works", "projects"}
CANDIDATE_KEYS = {
    "name", "phone", "email", "gender", "age",
    "address", "nativePlace", "selfEvaluation", "expectedSalary", "expectedPosition",
}
EDU_KEYS  = {"school", "major", "degree", "education", "startDate", "endDate"}
WORK_KEYS = {"company", "position", "description", "startDate", "endDate"}
PROJ_KEYS = {"company", "projectName", "role", "description", "startDate", "endDate"}


# ════════════════════════════════════════════════════════════════════
# 3. 正向打标
# ════════════════════════════════════════════════════════════════════

TAGGER_SYSTEM = "你是专业的 HR 招聘评估助手，只输出 JSON。"

# ── 3-1 输出结构示例 ─────────────────────────────────────────────
# 字段命名规则：{dimension}ValueArray
# education / workYears 无 confidence（规则层确定性计算）
# companyBg / industry / 自定义维度 携带 confidence
POSITIVE_TAG_SCHEMA = {
    "positiveTags": [
        {
            "dimension":           "education",
            "educationValueArray": ["985", "211", "双一流"],
            "source":              "ai",
        },
        {
            "dimension":           "companyBg",
            "companyBgValueArray": ["互联网大厂"],
            "source":              "ai",
            "confidence":          0.90,
        },
        {
            "dimension":           "workYears",
            "workYearsValueArray": ["5-10年"],
            "source":              "ai",
        },
        {
            "dimension":          "skills",
            "skillsValueArray":   ["Python", "机器学习", "Kubernetes"],
            "source":             "ai",
        },
        {
            "dimension":          "industry",
            "industryValueArray": ["互联网", "金融科技"],
            "source":             "ai",
            "confidence":         0.85,
        },
    ],
    "confidence": 0.88,
}

# ── 3-2 动态 prompt 头部 ─────────────────────────────────────────
# tagger._build_positive_prompt() 按运行时 positiveDimensions 逐维度拼装，
# 配置 API 新增/删除/禁用维度后，下一次打标 prompt 自动反映变更。
# 自定义维度的 schema 示例也会动态追加（见 _build_positive_prompt 实现）。
POSITIVE_PROMPT_HEADER = """根据候选人简历，提取结构化正向标签。

## 输入

### 候选人简历（已结构化）
{resume_json}

### 目标岗位参考
- 职位：{job_title}
- 必要技能：{required_skills}
- 岗位描述：{job_description}

## 输出格式

直接输出 JSON，禁止添加说明文字或 markdown 标记。
所有维度使用数组字段，命名规则：{{dimension}}ValueArray。

{schema_example}

## 各维度打标规则

"""

# ── 3-3 内置维度详细指令 ─────────────────────────────────────────
# {values} 由运行时 allowedValues 动态注入（"|" 分隔）。
# skills 无候选值约束，模板中不含 {values} 槽位。
DIM_PROMPT: dict[str, str] = {

    "education": """**education**（educationValueArray 为字符串数组，无 confidence，并行输出所有适用等级）
- 候选值：{values}
- 步骤一：遍历所有 educations[].school，对每所院校匹配已知院校层级
  - C9             → ["C9", "985", "211", "双一流"]（C9 ⊂ 985 ⊂ 211 ⊂ 双一流，高层级包含所有下级）
  - 985（非 C9）   → ["985", "211", "双一流"]
  - 211（非 985）  → ["211", "双一流"]
  - 海外 QS 前 50  → ["海外名校(QS50)", "海外名校(QS100)"]
  - 海外 QS 51-100 → ["海外名校(QS100)"]
- 步骤二：合并所有学校命中结果，去重后形成最终数组
  - 示例（985 院校硕士）："educationValueArray": ["985", "211", "双一流", "硕士"]
- 步骤三：若步骤一中所有院校均未命中任何层级，按最高学历补充兜底值
  - 博士研究生→"博士" / 硕士研究生→"硕士" / 本科→"本科" / 专科→"专科及以下"
  - 注意：兜底值仅在无院校层级命中时使用，不与层级标签混用
- 禁止输出候选值以外的值（如"博士研究生"应映射为"博士"，不得直接输出）""",

    "companyBg": """**companyBg**（companyBgValueArray 为字符串数组，携带整体 confidence，单条记录）
- 候选值：{values}
- 头部大厂参考：阿里/腾讯/字节/百度/华为/美团/京东/拼多多/网易/快手/小红书
- confidence 取所有匹配项的最高值""",

    "workYears": """**workYears**（workYearsValueArray 为单元素字符串数组，无 confidence）
- 候选值：{values}
- 从 works[] 计算累计工作年限后按以下规则映射：
  - 0-1年 → 应届 / 1-3年 → 2-3年 / 3-5年 → 3-5年
  - 5-10年 → 5-10年 / 10-15年 → 10-15年 / 15年+ → 15年+""",

    "skills": """**skills**（skillsValueArray 为字符串数组，无 confidence）
- 合并 works[].description + projects[].description，提取核心技术栈
- 去重、归一化（如 "python" → "Python"）
- 按职能大类归集：前端 / 后端 / 数据 / AI / 移动端 / 运维 / 产品 / 管理""",

    "industry": """**industry**（industryValueArray 为字符串数组，携带整体 confidence，单条记录）
- 候选值：{values}
- 从公司业务和项目描述推断行业，最多输出 2 个
- confidence 取所有命中行业的最高值""",
}

# ── 3-4 自定义维度通用指令 ───────────────────────────────────────
# 通过 POST /api/tags/config/positive 新增的维度自动使用此模板，无需改代码。
# _build_positive_prompt() 同时会向 schema 示例追加该维度的占位条目，
# 确保 LLM 知道需要在输出中包含此维度。
GENERIC_DIM_PROMPT = """**{dimension}**（{dimension}ValueArray 为字符串数组，携带整体 confidence）
- 显示名称：{display_name}
- 候选值：{values}
- 根据候选人完整简历，从候选值中选出所有匹配项（allowedValues 为空则自由提取相关标签）
- 必须在 positiveTags 数组中输出此维度条目；有匹配项填入对应标签，无匹配则 {dimension}ValueArray 填空数组 []
- confidence 取 0.70~1.00"""


# ════════════════════════════════════════════════════════════════════
# 4. 负向打标 · LLM 语义检测
# ════════════════════════════════════════════════════════════════════
# 仅处理 detectionType="llm" 的自定义负向维度。
# 4 个内置维度（jobstable / integrity / location / matchRisk）及
# detectionType="rule" 的自定义维度均由规则层处理，不进入此路径。

# ── 4-1 输出结构示例 ─────────────────────────────────────────────
NEGATIVE_TAG_SCHEMA = {
    "negativeTags": [
        {
            "dimension": "customRisk",
            "hits": [
                {
                    "tag":            "负面标签名",
                    "severity":       "medium",
                    "matchedContent": "简历中支撑该判断的原文片段或推理依据",
                }
            ],
        }
    ]
}

# ── 4-2 动态 prompt 头部 ─────────────────────────────────────────
# tagger._build_negative_prompt() 按 detectionType="llm" 的维度动态拼装。
NEGATIVE_PROMPT_HEADER = """根据候选人简历，识别以下各风险维度的潜在问题。

## 输入

### 候选人简历（已结构化）
{resume_json}

## 输出格式

直接输出 JSON，禁止添加说明文字或 markdown 标记。
仅输出确实命中的维度，无风险的维度不输出，hits 为空时省略整个维度对象。

{schema_example}

## 各风险维度检测规则

"""

# ── 4-3 自定义负向维度指令模板 ──────────────────────────────────
# {rules_text} 由每条 rule 的 tagName / severity / description 逐行拼接而成。
GENERIC_NEG_DIM_PROMPT = """**{dimension}**（{display_name}）
{rules_text}"""
