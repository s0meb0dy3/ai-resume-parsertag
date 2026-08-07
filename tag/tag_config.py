"""提供标签系统未加载运行时 JSON 时使用的静态默认配置。

`tagger` 优先读取 `schemas/tag_config_runtime.json`，文件不存在时才回退到
本模块的正向维度、负向维度和扣分权重。技能允许值由 `skill_300_new.xlsx` 同步生成，
修改运行时配置不会覆盖本模块内容。
"""

from .skill_library import SKILL_ALLOWED_VALUES


POSITIVE_DIMENSION_ORDER = (
    "education",
    "companyBg",
    "workYears",
    "skills",
    "industry",
)

POSITIVE_DIMENSION_DISPLAY_NAMES = {
    "education": "教育背景",
    "companyBg": "公司背景",
    "workYears": "工作年限",
    "skills": "技能标签",
    "industry": "行业背景",
}

MAX_SKILL_TAGS = 12
MAX_INDUSTRY_TAGS = 3


def sort_positive_tag_entries(tags: list[dict]) -> list[dict]:
    """按产品约定的正向维度顺序返回标签列表副本。

    Args:
        tags (list[dict]): 包含 `dimension` 字段的正向标签列表。

    Returns:
        list[dict]: 教育背景、公司背景、工作年限、技能标签、行业背景依次排列
            的新列表；未知扩展维度稳定保留在内置维度之后。

    Note:
        该函数不修改输入列表，也不改变单个标签对象的 JSON 结构。
    """
    order = {
        dimension: index
        for index, dimension in enumerate(POSITIVE_DIMENSION_ORDER)
    }
    return sorted(
        tags,
        key=lambda item: order.get(item.get("dimension", ""), len(order)),
    )


# 正向标签维度配置
POSITIVE_DIMENSIONS = [
    {
        "dimension":     "education",
        "displayName":   "教育背景",
        "enabled":       True,
        "allowedValues": [
            "C9", "985", "211", "双一流",
            "海外名校QS50", "海外名校QS100", "海外硕士",
            "博士", "硕士", "本科", "专科及以下",
        ],
    },
    {
        "dimension":     "companyBg",
        "displayName":   "公司背景",
        "enabled":       True,
        "allowedValues": [
            "行业龙头", "知名中大型企业", "世界500强", "中国500强",
            "中国民营500强", "知名跨国企业", "独角兽企业", "高成长创业公司",
            "国企/央企", "上市公司", "高新技术企业", "专精特新企业",
            "专精特新小巨人", "制造业单项冠军", "国家级企业技术中心",
            "科研院所/事业单位",
        ],
    },
    {
        "dimension":     "workYears",
        "displayName":   "工作年限",
        "enabled":       True,
        "allowedValues": [
            "应届", "往届无经验", "1年以下", "1-3年（满1年）",
            "3-5年（满3年）", "5-10年（满5年）", "10年及以上",
        ],
    },
    {
        "dimension":     "skills",
        "displayName":   "技能标签",
        "enabled":       True,
        "allowedValues": list(SKILL_ALLOWED_VALUES),
    },
    {
        "dimension":     "industry",
        "displayName":   "行业背景",
        "enabled":       True,
        "allowedValues": [
            "互联网/电商", "金融/银行/证券/保险", "企业服务/SaaS",
            "游戏/传媒/文娱", "教育/培训", "医疗健康/生物医药",
            "人工智能/数据智能", "汽车/出行", "半导体/集成电路",
            "通信/物联网/智能硬件", "物流/供应链", "航空航天/国防科技",
            "制造业/工业自动化/新材料", "能源/电力/新能源",
            "消费品/零售", "房地产/建筑/物业",
        ],
    },
]

# 负面和敏感词标签维度配置
NEGATIVE_DIMENSIONS = [
    {
        "dimension":   "jobstable",
        "displayName": "职业稳定性风险",
        "enabled":     True,
        "rules": {
            "frequent_job_change": {
                "tagName":      "频繁跳槽",
                "enabled":      True,
                "window_years": 3,    # 单位：年
                "threshold":    4,    # 检测窗口内触发风险的最少任职段数
                "severity":     "medium",
            },
            "short_tenure": {
                "tagName":          "最近任职时长偏短",
                "enabled":          True,
                "threshold_months": 3,    # 单位：月，最近一段低于该值时触发
                "severity":         "medium",
            },
            "gap_period": {
                "tagName":          "工作空窗期过长",
                "enabled":          True,
                "threshold_months": 6,    # 单位：月，空窗期超过该值时触发
                "severity":         "low",
            },
        },
    },
    {
        "dimension":   "integrity",
        "displayName": "诚信与合规风险",
        "enabled":     True,
        "rules": {
            "timeline_overlap": {
                "tagName":          "工作时间线重叠",
                "enabled":          True,
                "threshold_months": 2,    # 单位：月，时间线重叠超过该值时触发
                "severity":         "high",
            },
        },
    },
    {
        "dimension":   "matchRisk",
        "displayName": "匹配度硬伤",
        "enabled":     True,
        "rules": {
            "training_institution": {
                "tagName":  "疑似培训机构背景",
                "enabled":  True,
                "keywords": ["培训", "学院（培训）", "训练营", "bootcamp", "在线课程", "慕课"],
                "severity": "medium",
            },
        },
    },
    {
        "dimension":   "location",
        "displayName": "地域相关",
        "enabled":     True,
        "rules": {
            "sensitive_words": {
                "tagName":  "敏感地域",
                "enabled":  True,
                "keywords": ["广西"],
                "severity": "high",
            },
        },
    },
]

# 严重程度扣分权重
SEVERITY_DEDUCTION = {
    "low":    -5,
    "medium": -10,
    "high":   -20,
}
