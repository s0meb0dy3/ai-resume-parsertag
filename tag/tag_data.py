"""提供教育、企业和行业标签使用的静态分类数据与确定性规则。"""

import re

from .tag_config import MAX_INDUSTRY_TAGS
from .reference_data import (
    load_company_reference_data,
    load_education_reference_data,
)


_EDUCATION_REFERENCE = load_education_reference_data()
C9_SCHOOLS = set(_EDUCATION_REFERENCE.categories["C9"])
SCHOOLS_985 = set(_EDUCATION_REFERENCE.categories["985"])
SCHOOLS_211 = set(_EDUCATION_REFERENCE.categories["211"])
SCHOOLS_DOUBLE_FIRST = set(_EDUCATION_REFERENCE.categories["双一流"])
QS_TOP50 = set(_EDUCATION_REFERENCE.categories["海外名校QS50"])
QS_TOP100 = set(_EDUCATION_REFERENCE.categories["海外名校QS100"])

# 英文院校名称中的机构关键词只用于识别非 QS 名单内的海外学校。
OVERSEAS_KEYWORDS = {
    "university", "université", "universität", "universidad",
    "college", "institute of technology", "school of",
}

_SCHOOL_RANK_SUFFIX_RE = re.compile(
    r"\s*[（(]\s*(?:C9|985|211|双一流)"
    r"(?:\s*[,，、/+&]\s*(?:C9|985|211|双一流))*\s*[）)]\s*$",
    flags=re.IGNORECASE,
)


# 学历层级展开表：命中高级别时同时携带下级标签
EDUCATION_HIERARCHY: dict[str, list[str]] = {
    "C9":              ["C9", "985", "211", "双一流"],
    "985":             ["985", "211", "双一流"],
    "211":             ["211", "双一流"],
    "双一流":          ["双一流"],
    "海外名校QS50":  ["海外名校QS50", "海外名校QS100"],
    "海外名校QS100": ["海外名校QS100"],
    "海外硕士":        ["海外硕士"],
    "博士":            ["博士"],
    "硕士":            ["硕士"],
    "本科":            ["本科"],
    "专科及以下":      ["专科及以下"],
}


def classify_education(schools: list[str], degrees: list[str]) -> list[str]:
    """根据学校和学历生成去重且保序的学历标签。

    Args:
        schools (list[str]): 教育经历中的学校名称列表。
        degrees (list[str]): 教育经历中的学历名称列表。

    Returns:
        list[str]: 所有适用的学历标签。院校等级会按 ``EDUCATION_HIERARCHY``
        同时展开下级标签，例如 985 会包含 211 和双一流。

    Note:
        学校层级和最高学历独立判定；无法识别学历时不生成默认本科标签。
        学校名称末尾的 `（985）` 等等级注记先移除，不能作为海外院校证据。
    """
    tags: list[str] = []
    seen: set[str] = set()

    def add_tags(new_tags: list[str]) -> None:
        """将新标签按首次出现顺序加入结果。

        Args:
            new_tags: 待合并的学历标签。
        """
        for t in new_tags:
            if t not in seen:
                seen.add(t)
                tags.append(t)

    raw_schools = [
        _SCHOOL_RANK_SUFFIX_RE.sub("", str(school or "").strip())
        for school in schools
        if str(school or "").strip()
    ]
    normalized_schools = [
        _EDUCATION_REFERENCE.resolve(school) or school
        for school in raw_schools
    ]
    normalized_degrees = [str(degree or "").strip() for degree in degrees]
    has_doctor = any(d in ("博士研究生", "博士") for d in normalized_degrees)
    has_master = any(d in ("硕士研究生", "硕士") for d in normalized_degrees)
    has_master_or_above = has_doctor or has_master

    def is_overseas_school(school: str) -> bool:
        """判断学校名称是否有明确海外证据。

        Args:
            school (str): 已移除国内院校等级注记的学校名称。

        Returns:
            bool: 学校属于非内地 QS 名单，或名称含明确海外机构标记时返回
            `True`。
        """
        if school in SCHOOLS_DOUBLE_FIRST:
            return False
        known_overseas_qs = school in QS_TOP100
        return (
            known_overseas_qs
            or any(keyword in school.casefold() for keyword in OVERSEAS_KEYWORDS)
            or any(marker in school for marker in ("（海外）", "(海外)", "海外"))
        )

    for school in normalized_schools:
        if school in C9_SCHOOLS:
            add_tags(EDUCATION_HIERARCHY["C9"])
        elif school in SCHOOLS_985:
            add_tags(EDUCATION_HIERARCHY["985"])
        elif school in SCHOOLS_211:
            add_tags(EDUCATION_HIERARCHY["211"])
        elif school in SCHOOLS_DOUBLE_FIRST:
            add_tags(EDUCATION_HIERARCHY["双一流"])

    overseas_schools = [
        school for school in normalized_schools if is_overseas_school(school)
    ]
    if overseas_schools:
        for school in overseas_schools:
            if school in QS_TOP50:
                add_tags(EDUCATION_HIERARCHY["海外名校QS50"])
            elif school in QS_TOP100:
                add_tags(EDUCATION_HIERARCHY["海外名校QS100"])
        if not any(tag.startswith("海外名校") for tag in tags) and has_master_or_above:
            add_tags(EDUCATION_HIERARCHY["海外硕士"])

    # 学校层级与最高学历独立判定；未知学历不再默认本科。
    if has_doctor:
        add_tags(EDUCATION_HIERARCHY["博士"])
    elif has_master:
        add_tags(EDUCATION_HIERARCHY["硕士"])
    elif any(d in ("本科", "学士") for d in normalized_degrees):
        add_tags(EDUCATION_HIERARCHY["本科"])
    elif any(d in ("专科", "高职", "中专", "高中", "初中") for d in normalized_degrees):
        add_tags(EDUCATION_HIERARCHY["专科及以下"])

    return tags


_COMPANY_REFERENCE = load_company_reference_data()
_COMPANY_CONFIDENCE = {
    "行业龙头": 0.95,
    "知名中大型企业": 0.90,
    "世界500强": 0.96,
    "中国500强": 0.94,
    "中国民营500强": 0.94,
    "知名跨国企业": 0.92,
    "独角兽企业": 0.90,
    "高成长创业公司": 0.86,
    "国企/央企": 0.96,
    "上市公司": 0.96,
    "高新技术企业": 0.92,
    "专精特新企业": 0.92,
    "专精特新小巨人": 0.95,
    "制造业单项冠军": 0.95,
    "国家级企业技术中心": 0.95,
    "科研院所/事业单位": 0.94,
}
_CUSTOMER_QUALIFICATION_RE = re.compile(
    r"(?:服务|面向|对接|客户|合作方|供应商).{0,16}"
    r"(?:500强|行业龙头|跨国企业|上市公司|独角兽|高成长创业|国企|央企|"
    r"高新技术企业|专精特新|单项冠军|企业技术中心|事业单位)",
    flags=re.IGNORECASE,
)
_EXPLICIT_QUALIFICATION_PATTERNS = {
    "世界500强": [
        re.compile(r"(?:公司|集团|企业).{0,12}(?:世界|财富|Fortune)\s*500\s*强", re.IGNORECASE),
    ],
    "中国500强": [
        re.compile(r"(?:公司|集团|企业).{0,12}中国(?:企业)?\s*500\s*强"),
    ],
    "中国民营500强": [
        re.compile(r"(?:公司|集团|企业).{0,12}(?:中国)?民营(?:企业)?\s*500\s*强"),
    ],
    "行业龙头": [
        re.compile(r"(?:公司|集团|企业).{0,12}(?:细分)?行业龙头(?:企业)?"),
    ],
    "知名跨国企业": [
        re.compile(r"(?:公司|集团|企业).{0,12}(?:知名|大型|全球性)?跨国(?:公司|集团|企业)"),
    ],
    "上市公司": [
        re.compile(r"(?:公司|集团|企业).{0,12}(?:A股|港股|美股)?上市公司"),
        re.compile(r"(?:公司|集团|企业).{0,12}(?:上交所|深交所|北交所|港交所|纳斯达克|纽交所).{0,8}上市"),
    ],
    "高新技术企业": [
        re.compile(r"(?:国家级?|省级?)?高新技术企业"),
    ],
    "专精特新企业": [
        re.compile(r"(?:国家级?|省级?)?专精特新(?:小巨人)?企业"),
    ],
    "专精特新小巨人": [
        re.compile(r"(?:国家级?)?专精特新[‘'\"“”]?小巨人[’'\"“”]?(?:企业)?"),
    ],
    "制造业单项冠军": [
        re.compile(r"(?:国家级?)?制造业单项冠军(?:企业|产品)?"),
    ],
    "国家级企业技术中心": [
        re.compile(r"国家级企业技术中心"),
    ],
    "国企/央企": [
        re.compile(r"(?:公司|集团|企业).{0,10}(?:中央企业|国有企业|央企|国企)"),
    ],
    "科研院所/事业单位": [
        re.compile(r"(?:公司|单位|机构).{0,10}(?:科研院所|事业单位)"),
    ],
}
_RESEARCH_ORGANIZATION_RE = re.compile(
    r"(?:科学院|科学研究院|科学研究所|技术研究院|技术研究所|国家实验室)$"
)


def _has_explicit_qualification(text: str, patterns: list[re.Pattern]) -> bool:
    """判断文本是否包含公司自身资质且不是客户描述。

    Args:
        text (str): 工作和项目描述证据。
        patterns (list[re.Pattern]): 当前资质的正向表达式。

    Returns:
        bool: 至少存在一条非客户语境的明确资质陈述时返回 `True`。
    """
    for pattern in patterns:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 24)
            end = min(len(text), match.end() + 12)
            if not _CUSTOMER_QUALIFICATION_RE.search(text[start:end]):
                return True
    return False


def classify_companies(
    companies: list[str],
    evidence_text: str = "",
) -> list[dict]:
    """根据版本化公司资质和明确文本证据生成公司背景标签。

    Args:
        companies (list[str]): 工作经历中的公司名称列表。
        evidence_text (str): 可选的工作和项目描述证据。

    Returns:
        list[dict]: 去重后的公司背景标签及规则置信度。

    Note:
        同一公司可以具有多个资质。未知企业只接受“公司自身是某资质”的明确
        语句；“服务上市公司客户”等客户语境不会触发。置信度是规则分值，不是
        统计概率。
    """
    tags: list[dict] = []
    seen: set[str] = set()

    def add(value: str, confidence: float) -> None:
        """追加尚未出现的企业背景标签。

        Args:
            value (str): 企业背景标签值。
            confidence (float): 该规则对应的固定置信度。

        Note:
            国家级专精特新小巨人是专精特新企业的子集，命中时同时输出父级值。
        """
        if value == "专精特新小巨人" and "专精特新企业" not in seen:
            seen.add("专精特新企业")
            tags.append({
                "value": "专精特新企业",
                "confidence": min(confidence, _COMPANY_CONFIDENCE["专精特新企业"]),
            })
        if value not in seen:
            seen.add(value)
            tags.append({"value": value, "confidence": confidence})

    for company in companies:
        if not company:
            continue
        matched_records = _COMPANY_REFERENCE.match(company)
        for record in matched_records:
            for qualification in record["qualifications"]:
                add(qualification, _COMPANY_CONFIDENCE[qualification])
        if not matched_records and _RESEARCH_ORGANIZATION_RE.search(company.strip()):
            add("科研院所/事业单位", 0.78)

    for qualification, patterns in _EXPLICIT_QUALIFICATION_PATTERNS.items():
        if _has_explicit_qualification(evidence_text, patterns):
            add(qualification, 0.84)

    return tags


INDUSTRY_RULES = {
    "互联网/电商": {
        "strongKeywords": [
            "电商", "电子商务", "互联网产品", "本地生活", "外卖", "团购",
            "直播带货", "短视频平台", "内容平台", "社交平台",
        ],
        "supportKeywords": ["平台", "在线", "网络", "社区", "论坛", "直播", "内容", "社交"],
        "exclusionPatterns": [r"网络(?:运维|安全|故障|协议|设备)", r"(?:在线|平台)(?:状态|工具|模块)"],
    },
    "金融/银行/证券/保险": {
        "strongKeywords": [
            "金融科技", "fintech", "银行", "证券", "基金", "保险", "信托",
            "支付清算", "信贷", "金融风控", "核心银行", "财富管理",
        ],
        "supportKeywords": ["理财", "支付", "清算", "授信", "反欺诈", "征信", "券商"],
        "exclusionPatterns": [r"银行(?:卡|账户).{0,8}(?:支付|缴费)"],
    },
    "企业服务/SaaS": {
        "strongKeywords": [
            "SaaS", "PaaS", "IaaS", "企业软件", "企业服务", "CRM", "ERP",
            "协同办公", "供应链管理", "HR系统", "财务系统",
        ],
        "supportKeywords": ["OA", "B2B", "云服务", "多租户", "企业数字化"],
        "exclusionPatterns": [r"个人使用.{0,8}(?:OA|ERP|CRM)"],
    },
    "游戏/传媒/文娱": {
        "strongKeywords": [
            "游戏开发", "手游", "端游", "网游", "游戏引擎", "电竞", "数字传媒",
            "广告投放", "影视制作", "网络文学", "音乐平台", "文娱内容",
        ],
        "supportKeywords": ["游戏", "Unity", "Unreal", "关卡", "战斗系统", "传媒", "广告", "影视"],
        "exclusionPatterns": [r"(?:参加|喜欢|爱好).{0,8}游戏"],
    },
    "教育/培训": {
        "strongKeywords": [
            "在线教育", "K12", "职业教育", "知识付费", "题库", "慕课", "教培",
            "教育培训", "教学平台", "课程平台", "在线课程", "教务系统",
        ],
        "supportKeywords": ["课程", "培训", "教学", "作业", "招生", "教务"],
        "exclusionPatterns": [r"教育经历", r"(?:参加|完成).{0,8}(?:课程|培训)"],
    },
    "医疗健康/生物医药": {
        "strongKeywords": [
            "医疗", "医院", "医药", "药品", "医疗器械", "互联网医疗",
            "健康管理", "基因检测", "生物制药", "创新药", "临床试验", "生命科学",
        ],
        "supportKeywords": ["健康", "临床", "诊疗", "患者", "医生"],
        "exclusionPatterns": [r"个人健康", r"入职体检"],
    },
    "人工智能/数据智能": {
        "strongKeywords": [
            "人工智能", "AI平台", "大模型", "LLM", "机器学习", "深度学习",
            "计算机视觉", "NLP", "语音识别", "模型训练", "智能体", "大数据平台",
            "数据中台", "商业智能",
        ],
        "supportKeywords": ["算法模型", "推理服务", "特征工程", "神经网络", "RAG", "数据仓库", "BI"],
        "exclusionPatterns": [
            r"(?:计划|准备|希望|正在)学习.{0,12}(?:人工智能|机器学习|深度学习|大模型)",
            r"(?:人工智能|机器学习|深度学习|大模型).{0,8}(?:课程|培训|作业)",
        ],
    },
    "汽车/出行": {
        "strongKeywords": [
            "汽车", "新能源汽车", "电动车", "网约车", "自动驾驶", "车联网",
            "ADAS", "整车", "汽车零部件",
        ],
        "supportKeywords": ["出行", "座舱", "三电系统", "充电桩", "造车"],
        "exclusionPatterns": [r"(?:乘坐|驾驶|购买).{0,8}(?:汽车|电动车)"],
    },
    "半导体/集成电路": {
        "strongKeywords": [
            "半导体", "集成电路", "芯片设计", "芯片验证", "芯片制造", "晶圆",
            "封装测试", "IC设计", "ASIC", "EDA", "光刻",
        ],
        "supportKeywords": ["芯片", "流片", "SoC", "FPGA", "模拟电路", "数字电路"],
        "exclusionPatterns": [r"采购.{0,8}(?:芯片|晶圆)", r"芯片卡"],
    },
    "通信/物联网/智能硬件": {
        "strongKeywords": [
            "通信设备", "无线通信", "移动通信", "电信运营", "5G", "基站",
            "IoT", "物联网", "智能硬件", "嵌入式", "传感器", "智能家居",
        ],
        "supportKeywords": ["硬件开发", "板卡", "固件", "MCU", "通信协议", "射频", "工业互联网"],
        "exclusionPatterns": [r"采购.{0,8}(?:传感器|硬件)", r"网络通信(?:故障|运维)"],
    },
    "物流/供应链": {
        "strongKeywords": [
            "物流", "快递", "仓储", "供应链", "配送", "货运", "仓配",
            "最后一公里",
        ],
        "supportKeywords": ["运输", "库存", "WMS", "TMS", "干线"],
        "exclusionPatterns": [r"物流管理专业", r"快递收发"],
    },
    "航空航天/国防科技": {
        "strongKeywords": [
            "航空", "航天", "军工", "国防", "雷达", "导弹", "卫星", "无人机",
            "电子对抗", "军用通信",
        ],
        "supportKeywords": ["微波通信", "飞行器", "机载", "弹载", "航电"],
        "exclusionPatterns": [r"(?:乘坐|购买).{0,8}(?:飞机|无人机)"],
    },
    "制造业/工业自动化/新材料": {
        "strongKeywords": [
            "先进制造", "智能制造", "高端装备", "自动化产线", "数控", "新材料",
            "超材料", "复合材料", "金属材料", "MES", "工业自动化", "工业机器人",
        ],
        "supportKeywords": ["工艺制造", "生产制造", "CNC", "材料研发", "PLC", "生产线"],
        "exclusionPatterns": [r"材料准备", r"办公自动化"],
    },
    "能源/电力/新能源": {
        "strongKeywords": [
            "新能源", "电力系统", "电网", "发电", "输配电", "光伏", "风电",
            "储能", "锂电池", "氢能", "石油化工", "油气勘探",
        ],
        "supportKeywords": ["能源", "电力", "电站", "充换电", "电池", "炼化"],
        "exclusionPatterns": [r"手机电池", r"节约能源"],
    },
    "消费品/零售": {
        "strongKeywords": [
            "零售", "新零售", "商超", "连锁门店", "快消品", "消费品", "食品饮料",
            "服装品牌", "美妆", "家电零售",
        ],
        "supportKeywords": ["门店", "会员营销", "商品管理", "POS", "品牌营销", "经销商"],
        "exclusionPatterns": [r"零售银行", r"个人消费"],
    },
    "房地产/建筑/物业": {
        "strongKeywords": [
            "房地产", "地产开发", "建筑工程", "工程建设", "建筑设计", "物业管理",
            "智慧楼宇", "工程造价", "施工管理",
        ],
        "supportKeywords": ["楼盘", "土建", "施工", "BIM", "物业", "园区运营"],
        "exclusionPatterns": [r"软件架构设计", r"数据仓库分层建设"],
    },
}

# 兼容只读取关键词的内部工具；新分类逻辑使用强证据、辅助证据和排除项。
INDUSTRY_KEYWORDS = {
    name: [*rule["strongKeywords"], *rule["supportKeywords"]]
    for name, rule in INDUSTRY_RULES.items()
}
_INDUSTRY_EXECUTION_RE = re.compile(
    r"(?:负责|主导|参与|开发|设计|实现|训练|部署|运营|交付|维护|项目|产品)",
    flags=re.IGNORECASE,
)


def _contains_industry_term(text: str, term: str) -> bool:
    """判断行业术语是否在文本中以合理边界出现。

    Args:
        text (str): 已统一为小写的工作和项目文本。
        term (str): 行业强证据或辅助证据词。

    Returns:
        bool: 中文直接命中，英文数字术语满足边界时返回 `True`。
    """
    value = term.casefold()
    pattern = re.escape(value)
    if re.search(r"[a-z0-9]", value):
        pattern = rf"(?<![a-z0-9]){pattern}(?![a-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def classify_industry(text: str) -> list[dict]:
    """根据关键词命中数量推断候选人的行业背景。

    Args:
        text (str): 工作和项目描述拼接后的待匹配文本。

    Returns:
        list[dict]: 按规则置信度降序排列的行业标签，最多返回三个。

    Note:
        每个行业至少命中一个强证据词。辅助词不能独立触发；排除语境会降低
        规则分数。置信度是启发式分数，不应解释为统计概率。
    """
    text_lower = text.lower()
    hits: list[dict] = []
    for industry, rule in INDUSTRY_RULES.items():
        strong_count = sum(
            1 for keyword in rule["strongKeywords"]
            if _contains_industry_term(text_lower, keyword)
        )
        if strong_count == 0:
            continue
        support_count = sum(
            1 for keyword in rule["supportKeywords"]
            if _contains_industry_term(text_lower, keyword)
        )
        exclusion_count = sum(
            1 for pattern in rule["exclusionPatterns"]
            if re.search(pattern, text_lower, flags=re.IGNORECASE)
        )
        if exclusion_count and not _INDUSTRY_EXECUTION_RE.search(text_lower):
            continue
        score = strong_count * 3 + support_count - exclusion_count * 3
        if score < 3:
            continue
        confidence = min(
            0.94,
            0.62 + strong_count * 0.08 + support_count * 0.025,
        )
        hits.append({
            "value": industry,
            "confidence": round(confidence, 2),
            "_score": score,
        })
    hits.sort(key=lambda item: (item["_score"], item["confidence"]), reverse=True)
    return [
        {"value": item["value"], "confidence": item["confidence"]}
        for item in hits[:MAX_INDUSTRY_TAGS]
    ]


# SEVERITY_DEDUCTION 已移至 tag_config.py
