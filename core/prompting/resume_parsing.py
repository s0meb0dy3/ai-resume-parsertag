"""定义简历解析使用的 Prompt、输出 Schema 和字段白名单。

该模块服务于文本解析与视觉解析入口，统一约束候选人、教育经历、工作经历
和项目经历的 JSON 结构。后处理模块复用同一字段白名单，避免提示词示例与
运行时校验结构发生偏差。
"""

import hashlib
import json
from datetime import date


POSITION_RULES = """## 4. 职位与项目角色提取规则

### 4.1 原文存在明确职位

1. 完整保留复合职位，例如“算法工程师/AI系统负责人”。
2. 完整保留方向说明，例如“Java开发工程师（自研系统方向）”。
3. 完整保留级别信息，例如“高级软件工程师”或“P7研发工程师”。
4. 删除“所在部门”“汇报对象”“月薪”等招聘平台冗余标签。

### 4.2 原文没有明确职位

根据职责中的明确证据选择最匹配的职位关键词：

1. 开发、研发、实现、代码、编程：软件工程师或开发工程师。
2. 算法、模型、训练、推理、LLM、NLP、CV：算法工程师。
3. 架构、系统设计、微服务、分布式：架构师或系统工程师。
4. 测试、质量、QA、自动化测试：测试工程师。
5. 产品、需求、PRD、原型：产品经理。
6. 数据、分析、BI、报表、指标：数据分析师。
7. 运营、增长、用户、活动策划：运营专员或运营经理。
8. 销售、客户、商务、BD：销售或客户经理。
9. 研究、论文、实验、课题、科研：研究员或研究生。
10. 管理、带领团队、绩效、招聘：团队负责人或工程经理。
11. 前端、UI、页面、组件、React、Vue：前端工程师。
12. 后端、服务、API、数据库：后端工程师。
13. 运维、DevOps、部署、CI/CD：运维工程师。

### 4.3 特殊经历

1. 实习经历：在职位后添加“（实习）”，例如“软件工程师（实习）”。
2. 科研或学术经历：没有明确职位时，可依据学历填写“本科”“硕士研究生”
   或“博士研究生/研究员”，并可保留明确方向。

### 4.4 字段边界

1. `works[].position` 只填写职位名称。
2. `projects[].role` 只填写项目角色或职位名称。
3. 禁止填写职责句子、“职位/职责”等占位文字。
"""


# 解析提示词和后处理共同依赖此结构，修改字段时必须同步更新字段白名单。
WORK_YEARS_AS_OF_MONTH = date.today().strftime("%Y-%m")

COMPANY_SCHEMA = {
    "candidate": {
        "name": "姓名，必要信息",
        "phone": "电话，必要信息，格式1xxxxxxxxxx",
        "email": "邮箱",
        "gender": "性别，男/女；不存在时不填",
        "age": "年龄整数；不存在时填0",
        "workYears": "正式工作累计完整年数，非负整数；无法判断时填0",
        "address": "居住地信息，如北京市朝阳区；不存在时不填",
        "nativePlace": "籍贯信息，重要信息，需要结合原文多次确认",
        "selfEvaluation": "自我评价；不存在时不填",
        "expectedSalary": "期望月薪，如25-35K",
        "expectedPosition": "应聘职位；不存在时不填",
    },
    "educations": [
        {
            "school": "学校或机构名称",
            "major": "专业名称",
            "degree": "学位：学士/硕士/博士；注意区分学位与学历",
            "education": "学历：初中/高中/专科/本科/硕士研究生/博士研究生",
            "startDate": "开始时间，如2015-09",
            "endDate": "结束时间，如2019-07",
        }
    ],
    "works": [
        {
            "company": "公司或机构全称；保留原文，不得改写或缩写",
            "position": "职位，必须遵守职位提取规则",
            "description": "汇总该公司阶段的核心职责与技术方向，不超过300字；无证据时不填",
            "startDate": "开始时间，如2019-07",
            "endDate": "结束时间，如2022-08；当前任职统一填至今",
        }
    ],
    "projects": [
        {
            "company": "所属公司或机构全称；保留原文，不得改写或缩写；不存在时不填",
            "projectName": "项目完整名称；保留原文，不得改写或缩写",
            "role": "项目角色或职位，必须遵守职位提取规则",
            "description": "保留项目介绍和工作内容；原文冗长或混乱时可精炼；不存在时不填",
            "startDate": "开始时间，如2020-03",
            "endDate": "结束时间，如2020-12",
        }
    ],
}


PROMPT_HR = f"""# 简历结构化解析任务

## 1. 角色

你是专业的简历解析助手。

## 2. 任务目标

1. 将用户提供的简历内容转换为指定 JSON 对象。
2. 提取候选人基本信息、教育经历、工作经历和项目经历。
3. 优先保证姓名、学校、公司、职位、日期和联系方式准确完整。
4. 只提取原文存在的信息，不得虚构或补充无证据内容。
5. Python 确定性候选只用于提醒复核，必须回到原文确认后才能写入结果。

### 2.1 工作年限计算

1. `candidate.workYears` 以 {WORK_YEARS_AS_OF_MONTH} 为统计基准月份。
2. 只累计正式工作经历，排除实习、兼职、校园经历、学生会和科研助理。
3. 重叠任职月份只计算一次；先按月份累计，再除以 12 并向下取整。
4. 不得依据“多年经验”等模糊自述猜测；工作时间线无法完整计算时填 `0`。

## 3. 候选人与家庭成员边界

### 3.1 候选人识别

1. 候选人是简历或登记表的填表人本人。
2. 候选人姓名通常位于文档开头的基本信息区域或“姓名”字段对应位置。
3. 文件名中的姓名只可作为核对线索，不能覆盖文档中的明确姓名。

### 3.2 家庭成员排除

1. 忽略“家庭成员”或“主要社会关系”表格中的姓名、单位和联系方式。
2. 姓名附近出现“夫妻”“配偶”“父母”“子女”“兄弟”“姐妹”等关系词时，
   该姓名属于家属，不得写入 `candidate.name`。
3. 不得把家属的工作单位、电话或邮箱写入候选人字段。

{POSITION_RULES}

## 5. JSON 字段结构

{json.dumps(COMPANY_SCHEMA, ensure_ascii=False, indent=2)}

## 6. 执行步骤

1. 先阅读完整原文，再使用 Python 候选复核可能遗漏的姓名、联系方式、学校、
   日期和章节边界；候选与原文冲突时以原文为准。
2. 定位候选人基本信息，排除家庭成员和表格错位内容。
3. 按时间和机构归集全部教育经历。
4. 按公司归集工作经历，并从对应项目总结该阶段职责。
5. 独立提取每个项目，保留项目名称、所属公司、角色、描述和日期。
6. 检查所有顶层字段和子字段是否符合 JSON 字段结构。

## 7. 输出硬约束

1. 直接输出一个 JSON 对象，不得添加说明、注释或 Markdown 代码块。
2. 必须包含 `candidate`、`educations`、`works`、`projects` 四个顶层字段。
3. 缺失字符串填写空字符串 `""`，缺失数组填写空数组 `[]`。
4. 缺失数字字段 `age`、`gender` 和 `workYears` 填写 `0`。
5. 公司、学校和项目名称保留原文，不得改写、缩写或合并不同机构。
6. 禁止根据常识、岗位名称或上下文猜测原文没有的信息。
7. 禁止把 Python 候选对象或章节边界写入最终 JSON。
"""


# 缓存键随固定系统 Prompt 内容变化，避免新旧解析规则共享服务端前缀缓存。
RESUME_PROMPT_CACHE_KEY = (
    "resume-parse-"
    + hashlib.sha256(PROMPT_HR.encode("utf-8")).hexdigest()[:16]
)
TEXT_RESUME_USER_PREFIX = """# 输入材料

## 1. Python 抽取原文

"""
TEXT_RESUME_HINTS_HEADER = """

## 2. Python 确定性候选

以下候选由正则和章节规则从同一份原文提取，只用于防遗漏核对，不是最终事实：

"""
TEXT_RESUME_USER_SUFFIX = """

## 3. 输出要求

1. 必须先依据原文判断，候选错误或无原文支持时忽略。
2. 候选存在但原文归属不明确时保持对应字段为空，不得猜测。
3. 严格按系统消息指定的 JSON 结构输出，直接输出 JSON。
"""


def build_text_resume_user_prompt(document_text: str, hints: dict) -> str:
    """构造固定前缀在前、动态原文和候选在后的文本解析消息。

    Args:
        document_text (str): Python 从 PDF 或 DOCX 抽取的完整原文。
        hints (dict): 确定性字段候选和章节边界，不得进入公开结果。

    Returns:
        str: 供单次 LLM 语义结构化调用使用的用户消息。

    Note:
        固定系统 Prompt 和 ``TEXT_RESUME_USER_PREFIX`` 始终位于动态内容之前，
        便于支持 prompt cache 的模型服务复用预填充结果。
    """
    return (
        TEXT_RESUME_USER_PREFIX
        + document_text.strip()
        + TEXT_RESUME_HINTS_HEADER
        + json.dumps(hints, ensure_ascii=False, separators=(",", ":"))
        + TEXT_RESUME_USER_SUFFIX
    )


BATCH_SYSTEM = PROMPT_HR + """

## 8. 部分页面处理

1. 当前输入仅包含简历的部分页面。
2. 提取当前页面中出现的全部有效信息。
3. 当前页面没有的信息按输出硬约束填写空值，禁止参考未提供页面进行猜测。
"""


def get_resume_prompt_bundle(as_of_date: date | None = None) -> tuple[str, str, str]:
    """按请求统计月份构造解析 Prompt、分批 Prompt 和缓存键。

    Args:
        as_of_date (date | None): 工作年限统计日期；为空时读取当前日期。

    Returns:
        tuple[str, str, str]: 完整解析 Prompt、部分页面 Prompt 和对应缓存键。

    Note:
        模块级常量保留给兼容调用方；解析主链应调用本函数，避免常驻进程跨月后
        继续使用启动月份。一次文档解析只调用一次，以保证所有页面批次口径一致。
    """
    as_of_month = (as_of_date or date.today()).strftime("%Y-%m")
    prompt_hr = PROMPT_HR.replace(WORK_YEARS_AS_OF_MONTH, as_of_month)
    batch_system = BATCH_SYSTEM.replace(WORK_YEARS_AS_OF_MONTH, as_of_month)
    cache_key = "resume-parse-" + hashlib.sha256(
        prompt_hr.encode("utf-8")
    ).hexdigest()[:16]
    return prompt_hr, batch_system, cache_key


# 字段白名单供后处理执行确定性补全与裁剪。
SCHEMA_KEYS = {"candidate", "educations", "works", "projects"}
CANDIDATE_KEYS = {
    "name",
    "phone",
    "email",
    "gender",
    "age",
    "workYears",
    "address",
    "nativePlace",
    "selfEvaluation",
    "expectedSalary",
    "expectedPosition",
}
EDU_KEYS = {"school", "major", "degree", "education", "startDate", "endDate"}
WORK_KEYS = {"company", "position", "description", "startDate", "endDate"}
PROJ_KEYS = {"company", "projectName", "role", "description", "startDate", "endDate"}
