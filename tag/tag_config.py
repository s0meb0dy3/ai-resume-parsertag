"""
标签系统动态配置文件
修改维度或规则参数后无需改动打标逻辑代码。
"""

# ── 正向标签维度配置（顺序即输出顺序，共5个维度）────────────────────
POSITIVE_DIMENSIONS = [
    {
        "dimension":     "education",
        "displayName":   "学历背景",
        "enabled":       True,
        "allowedValues": [
            "C9", "985", "211", "双一流",
            "海外名校(QS50)", "海外名校(QS100)", "海外硕士",
            "博士", "硕士", "本科", "专科及以下",
        ],
    },
    {
        "dimension":     "companyBg",
        "displayName":   "公司背景",
        "enabled":       True,
        "allowedValues": [
            "头部大厂", "腰部中厂", "外企500强", "一线外企",
            "独角兽", "明星创业", "国企/央企", "传统企业转型",
        ],
    },
    {
        "dimension":     "workYears",
        "displayName":   "工作年限",
        "enabled":       True,
        "allowedValues": ["应届", "2-3年", "3-5年", "5-10年", "10-15年", "15年+"],
    },
    {
        "dimension":     "skills",
        "displayName":   "技能标签",
        "enabled":       True,
        "allowedValues": [],  # 不限枚举，动态提取
    },
    {
        "dimension":     "industry",
        "displayName":   "行业背景",
        "enabled":       True,
        "allowedValues": [
            "互联网/电商", "金融科技", "企业服务/SaaS", "游戏",
            "在线教育", "医疗健康", "人工智能", "汽车/出行",
            "物联网/硬件", "物流/供应链",
        ],
    },
]

# ── 负面/敏感词标签维度配置（4个维度）────────────────────────────────
NEGATIVE_DIMENSIONS = [
    {
        "dimension":   "jobstable",
        "displayName": "职业稳定性风险",
        "enabled":     True,
        "rules": {
            "frequent_job_change": {
                "tagName":      "频繁跳槽",
                "enabled":      True,
                "window_years": 3,    # 检测窗口（年）
                "threshold":    4,    # 近N年内段数阈值
                "severity":     "medium",
            },
            "short_tenure": {
                "tagName":          "最近任职时长偏短",
                "enabled":          True,
                "threshold_months": 3,    # 最近一段低于此时长触发
                "severity":         "medium",
            },
            "gap_period": {
                "tagName":          "工作空窗期过长",
                "enabled":          True,
                "threshold_months": 6,    # 空窗期超过此时长触发
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
                "threshold_months": 2,    # 重叠超过此时长触发
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
                "severity": "high",
            },
        },
    },
]

# ── 严重程度扣分权重 ──────────────────────────────────────────────
SEVERITY_DEDUCTION = {
    "low":    -5,
    "medium": -10,
    "high":   -20,
}
