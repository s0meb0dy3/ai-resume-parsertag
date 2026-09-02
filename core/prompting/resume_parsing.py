"""定义简历解析使用的 Prompt、输出 Schema 和字段白名单。

该模块服务于文本解析与视觉解析入口，统一约束候选人、教育经历、工作经历
和项目经历的 JSON 结构。后处理模块复用同一字段白名单，避免提示词示例与
运行时校验结构发生偏差。
"""

import hashlib
import json
from datetime import date


WORK_YEARS_AS_OF_MONTH = date.today().strftime("%Y-%m")


PROMPT_HR = """# 简历结构化解析任务

## 1. 任务目标

1. 将用户提供的简历内容转换为指定 JSON 对象。
2. 提取候选人基本信息、教育经历、工作经历和项目经历。
3. 优先保证姓名、学校、公司、职位、日期和联系方式准确完整。

### 1.1 工作年限计算

1. `candidate.workYears` 以 __WORK_YEARS_AS_OF_MONTH__ 为统计基准月份。
2. 只累计正式工作经历，排除实习、兼职、校园经历、学生会和科研助理。
3. 重叠任职月份只计算一次；先按月份累计，再除以 12 并向下取整。
4. 不得依据“多年经验”等模糊自述猜测；工作时间线无法完整计算时填 `0`。

## 2. 职位与项目角色提取规则

1. `works[].position` 只填写职位名称，`projects[].role` 只填写项目角色或职位名称。
2. 完整保留原文中的复合职位、方向和级别信息，例如“算法工程师/AI系统负责人”、
   “Java开发工程师（自研系统方向）”和“P7研发工程师”。
3. 实习经历在职位后添加“（实习）”，例如“软件工程师（实习）”。
4. 禁止填写职责句子、“职位/职责”等占位文字。
5. 原文未明确职位或项目角色时，仅在职责存在明确证据时补充最匹配的通用名称；
   证据不足时留空，不得推断级别或管理身份：
   - 开发、代码、编程：开发工程师；明确前端或后端特征时填前端工程师或后端工程师。
   - 算法、模型、训练、推理、LLM、NLP、CV：算法工程师。
   - 架构、系统设计、微服务、分布式：系统工程师。
   - 测试、质量、QA：测试工程师；运维、部署、DevOps、CI/CD：运维工程师。
   - 产品、需求、PRD、原型：产品经理；数据、分析、BI、报表：数据分析师。
   - 运营、增长、活动策划：运营；销售、客户、商务、BD：销售或客户经理。
   - 研究、论文、实验、课题、科研：研究员；明确以团队管理为主要职责时，
     填团队负责人或工程经理。

## 3. JSON 字段结构

{
  "candidate": {
    "name": "姓名",
    "phone": "电话",
    "email": "邮箱",
    "gender": "性别编码：男=1，女=2，未知=0",
    "age": "年龄整数",
    "workYears": "正式工作累计完整年数",
    "address": "现居地",
    "nativePlace": "籍贯",
    "selfEvaluation": "自我评价",
    "expectedSalary": "期望月薪，如25-35K",
    "expectedPosition": "期望或应聘职位"
  },
  "educations": [
    {
      "school": "学校或机构名称",
      "major": "专业名称",
      "degree": "学位：学士/硕士/博士；",
      "education": "学历：初中/高中/专科/本科/硕士研究生/博士研究生",
      "startDate": "开始日期，如2015-09；如果没有日期则留空",
      "endDate": "结束日期，具体日期统一为YYYY-MM（如2019-07）；原文明确写至今、在读、目前等仍在读状态时统一填至今；原文未提供结束日期时留空，不得根据学制推测"
    }
  ],
  "works": [
    {
      "company": "公司或机构全称",
      "position": "职位",
      "description": "该段任职的核心职责和技术方向，不超过300字",
      "startDate": "开始日期，具体日期统一为YYYY-MM（如2019-07）；原文未提供时留空，不得推断",
      "endDate": "结束日期，具体日期统一为YYYY-MM（如2022-08）；原文明确写至今、目前、现在等仍在职状态时统一填至今；原文未提供时留空，不得推断"
    }
  ],
  "projects": [
    {
      "company": "所属公司或机构，未明确时为空",
      "projectName": "项目名称",
      "role": "项目角色或职位",
      "description": "项目介绍和本人工作内容",
      "startDate": "开始日期，具体日期统一为YYYY-MM（如2020-03）；原文未提供时留空，不得推断",
      "endDate": "结束日期，具体日期统一为YYYY-MM（如2020-12）；原文明确写至今、目前、现在等仍在进行状态时统一填至今；原文未提供时留空，不得推断"
    }
  ]
}

## 4. 输出硬约束

1. 直接输出一个 JSON 对象，不得添加说明、注释或 Markdown 代码块。
2. 必须包含 `candidate`、`educations`、`works`、`projects` 四个顶层字段。
3. 缺失字符串填写空字符串 `""`，缺失数组填写空数组 `[]`。
4. 缺失数字字段 `age`、`gender` 和 `workYears` 填写 `0`。
5. 公司、学校和项目名称保留原文，不得改写、缩写或合并不同机构。
6. 只提取原文明确存在的信息，不得虚构；除第 2 节允许的职位或项目角色补全外，
   不得依据常识、岗位名称或上下文补充原文没有的事实。
""".replace("__WORK_YEARS_AS_OF_MONTH__", WORK_YEARS_AS_OF_MONTH)


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

## 5. 部分页面处理

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
