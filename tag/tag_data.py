"""
静态标签数据：院校名单 / 大厂名单 / 行业词典
支持热更新：修改此文件后无需重启服务，tagger.py 按需加载。
"""

# ── 院校名单 ─────────────────────────────────────────────────────

C9_SCHOOLS = {
    "北京大学", "清华大学", "复旦大学", "上海交通大学",
    "浙江大学", "南京大学", "中国科学技术大学", "哈尔滨工业大学", "西安交通大学"
}

SCHOOLS_985 = C9_SCHOOLS | {
    "北京航空航天大学", "北京理工大学", "中国农业大学", "北京师范大学",
    "中央民族大学", "南开大学", "天津大学", "大连理工大学", "吉林大学",
    "同济大学", "华东师范大学", "厦门大学", "山东大学", "中国海洋大学",
    "武汉大学", "华中科技大学", "中南大学", "湖南大学", "中山大学",
    "华南理工大学", "四川大学", "重庆大学", "电子科技大学", "云南大学",
    "兰州大学", "东北大学", "东南大学", "国防科学技术大学"
}

SCHOOLS_211 = SCHOOLS_985 | {
    "北京交通大学", "北京工业大学", "北京科技大学", "北京化工大学",
    "北京邮电大学", "对外经济贸易大学", "中央财经大学", "北京外国语大学",
    "中国传媒大学", "中央音乐学院", "中国地质大学", "中国矿业大学",
    "中国石油大学", "华北电力大学", "河海大学", "江南大学",
    "南京航空航天大学", "南京理工大学", "苏州大学", "安徽大学",
    "合肥工业大学", "华中师范大学", "中南财经政法大学", "华中农业大学",
    "武汉理工大学", "中国地质大学（武汉）", "湖南师范大学",
    "暨南大学", "华南师范大学", "广西大学", "海南大学",
    "贵州大学", "西南大学", "西南交通大学", "西南财经大学",
    "四川农业大学", "陕西师范大学", "西北工业大学", "西北农林科技大学",
    "新疆大学", "内蒙古大学", "黑龙江大学", "辽宁大学",
    "延边大学", "东北师范大学", "郑州大学", "湖南大学",
    "南昌大学", "太原理工大学", "河北工业大学", "长安大学",
    "宁夏大学", "青海大学", "西藏大学", "石河子大学",
    "中央戏剧学院", "华东理工大学", "上海外国语大学", "上海财经大学",
    "华东政法大学",
}

SCHOOLS_DOUBLE_FIRST = SCHOOLS_211  # 双一流基本包含 211，简化处理

# QS前50（部分代表性院校）
QS_TOP50 = {
    "麻省理工学院", "MIT", "剑桥大学", "牛津大学", "斯坦福大学",
    "哈佛大学", "加州理工学院", "帝国理工学院", "ETH Zurich", "苏黎世联邦理工学院",
    "芝加哥大学", "普林斯顿大学", "哥伦比亚大学", "耶鲁大学",
    "宾夕法尼亚大学", "康奈尔大学", "新加坡国立大学", "南洋理工大学",
    "UCL", "伦敦大学学院", "爱丁堡大学", "多伦多大学",
    "慕尼黑工业大学", "墨尔本大学", "悉尼大学",
    "东京大学", "京都大学", "首尔国立大学",
    "香港大学", "香港科技大学", "香港中文大学",
    "洛桑联邦理工学院", "EPFL",
}

# QS 51-100（部分）
QS_TOP100 = QS_TOP50 | {
    "布里斯托大学", "曼彻斯特大学", "格拉斯哥大学", "圣安德鲁斯大学",
    "阿姆斯特丹大学", "慕尼黑大学", "海德堡大学",
    "纽约大学", "卡内基梅隆大学", "杜克大学", "约翰霍普金斯大学",
    "加州大学伯克利分校", "加州大学洛杉矶分校", "密歇根大学", "威斯康星大学",
    "大阪大学", "东北大学（日本）", "首尔大学",
    "新南威尔士大学", "澳大利亚国立大学", "昆士兰大学",
    "北京大学", "清华大学",  # 已在 QS 前 50 附近
}

# 海外高校关键词（非 QS 榜但属于海外院校）
OVERSEAS_KEYWORDS = {
    "university", "université", "universität", "universidad",
    "college", "institute of technology", "school of",
    "大学（", "大学）"
}


# 学历层级展开表：命中高级别时同时携带下级标签
EDUCATION_HIERARCHY: dict[str, list[str]] = {
    "C9":              ["C9", "985", "211", "双一流"],
    "985":             ["985", "211", "双一流"],
    "211":             ["211", "双一流"],
    "双一流":          ["双一流"],
    "海外名校(QS50)":  ["海外名校(QS50)", "海外名校(QS100)"],
    "海外名校(QS100)": ["海外名校(QS100)"],
    "海外硕士":        ["海外硕士"],
    "博士":            ["博士"],
    "硕士":            ["硕士"],
    "本科":            ["本科"],
    "专科及以下":      ["专科及以下"],
}


def classify_education(schools: list[str], degrees: list[str]) -> list[str]:
    """
    给定学校列表和学历列表，返回所有适用的学历标签列表（并行输出）。
    例：985 学校 → ["985", "211", "双一流"]；C9 → ["C9", "985", "211", "双一流"]。
    遍历所有 schools，合并去重后返回。
    """
    tags: list[str] = []
    seen: set[str] = set()

    def add_tags(new_tags: list[str]) -> None:
        for t in new_tags:
            if t not in seen:
                seen.add(t)
                tags.append(t)

    has_doctor = any(d in ("博士研究生", "博士") for d in degrees)
    has_master_or_above = has_doctor or any(d in ("硕士研究生", "硕士") for d in degrees)
    is_overseas = any(
        any(kw in s.lower() for kw in OVERSEAS_KEYWORDS) or
        any(kw in s for kw in ("（海外）", "海外", "国际"))
        for s in schools
    )

    for s in schools:
        if s in C9_SCHOOLS:
            add_tags(EDUCATION_HIERARCHY["C9"])
        elif s in SCHOOLS_985:
            add_tags(EDUCATION_HIERARCHY["985"])
        elif s in SCHOOLS_211:
            add_tags(EDUCATION_HIERARCHY["211"])

    if is_overseas:
        for s in schools:
            if s in QS_TOP50:
                add_tags(EDUCATION_HIERARCHY["海外名校(QS50)"])
            elif s in QS_TOP100:
                add_tags(EDUCATION_HIERARCHY["海外名校(QS100)"])
        if not tags and has_master_or_above:
            add_tags(EDUCATION_HIERARCHY["海外硕士"])

    if not tags:
        if has_doctor:
            add_tags(EDUCATION_HIERARCHY["博士"])
        elif has_master_or_above:
            add_tags(EDUCATION_HIERARCHY["硕士"])
        else:
            found = False
            for d in degrees:
                if d in ("本科", "学士"):
                    add_tags(EDUCATION_HIERARCHY["本科"])
                    found = True
                    break
                if d in ("专科", "高职", "中专", "高中", "初中"):
                    add_tags(EDUCATION_HIERARCHY["专科及以下"])
                    found = True
                    break
            if not found:
                add_tags(EDUCATION_HIERARCHY["本科"])

    return tags


# ── 企业背景 ──────────────────────────────────────────────────────

TOP_TIER_COMPANIES = {
    "阿里巴巴", "阿里", "淘宝", "天猫", "支付宝", "蚂蚁集团", "蚂蚁金服",
    "腾讯", "微信", "QQ",
    "字节跳动", "字节", "抖音", "TikTok", "今日头条",
    "百度",
    "华为",
    "美团",
    "京东",
    "拼多多", "多多",
    "网易",
    "快手",
    "小红书",
}

MID_TIER_COMPANIES = {
    "哔哩哔哩", "B站", "bilibili",
    "得物", "毒App",
    "携程", "去哪儿",
    "微博", "新浪",
    "知乎",
    "SHEIN", "希音",
    "米哈游", "原神",
    "vivo", "OPPO", "荣耀", "小米",
    "滴滴", "滴滴出行",
    "Boss直聘", "BOSS",
    "猎聘", "58同城", "安居客",
    "饿了么", "盒马",
    "贝壳", "链家",
    "理想汽车", "蔚来", "小鹏",
    "陌陌", "探探",
    "虎牙", "斗鱼", "YY",
    "Soul", "Keep",
    "钉钉",
}

FORTUNE500_COMPANIES = {
    "微软", "Microsoft",
    "谷歌", "Google", "Alphabet",
    "亚马逊", "Amazon", "AWS",
    "苹果", "Apple",
    "Meta", "Facebook", "Instagram",
    "英特尔", "Intel",
    "英伟达", "NVIDIA",
    "IBM",
    "甲骨文", "Oracle",
    "SAP",
    "Salesforce",
    "Adobe",
    "思科", "Cisco",
    "高通", "Qualcomm",
    "德勤", "Deloitte",
    "麦肯锡", "McKinsey",
    "波士顿咨询", "BCG",
    "埃森哲", "Accenture",
}

FIRST_TIER_FOREIGN = {
    "SAP", "IBM", "Oracle", "Salesforce",
    "Siemens", "西门子", "博世", "Bosch",
    "飞利浦", "Philips", "松下", "Panasonic",
    "三星", "Samsung", "LG",
    "索尼", "Sony", "东芝", "Toshiba",
    "ABB", "霍尼韦尔", "Honeywell",
    "毕马威", "KPMG", "普华永道", "PwC", "安永", "EY",
}

STATE_OWNED_KEYWORDS = {
    "中国", "国家", "央企", "国企", "中石油", "中石化", "中国电信",
    "中国移动", "中国联通", "国家电网", "中国银行", "工商银行",
    "建设银行", "农业银行", "交通银行", "招商银行",
    "国防", "航天", "航空", "核工业", "兵器",
}

UNICORN_COMPANIES = {
    "商汤科技", "商汤",
    "旷视科技", "旷视",
    "依图科技",
    "寒武纪",
    "地平线",
    "云从科技",
    "字节跳动",  # 已在大厂，优先归大厂
    "陆金所",
    "微众银行",
    "平安科技",
    "京东数科", "京东科技",
    "货拉拉",
    "Keep",
    "Boss直聘",
    "猫眼娱乐",
    "众安保险",
    "Manbang", "满帮集团",
    "快看漫画",
    "虎扑",
}


def classify_companies(companies: list[str]) -> list[dict]:
    """
    给定公司名称列表，返回企业背景标签列表（可多个）。
    每项: {"value": "头部大厂", "confidence": 0.95}
    """
    tags = []
    seen = set()

    def add(value: str, conf: float):
        if value not in seen:
            seen.add(value)
            tags.append({"value": value, "confidence": conf})

    for company in companies:
        if not company:
            continue
        c_lower = company.lower()

        # 头部大厂（精确匹配关键词）
        if any(kw in company for kw in TOP_TIER_COMPANIES):
            add("头部大厂", 0.95)
            continue

        # 外企500强（精确/包含匹配）
        if any(kw.lower() in c_lower for kw in FORTUNE500_COMPANIES):
            add("外企500强", 0.90)
            continue

        # 一线外企
        if any(kw.lower() in c_lower for kw in FIRST_TIER_FOREIGN):
            add("一线外企", 0.88)
            continue

        # 腰部中厂
        if any(kw in company for kw in MID_TIER_COMPANIES):
            add("腰部中厂", 0.85)
            continue

        # 独角兽
        if any(kw in company for kw in UNICORN_COMPANIES):
            add("独角兽", 0.80)
            continue

        # 国企/央企（关键词匹配）
        if any(kw in company for kw in STATE_OWNED_KEYWORDS):
            add("国企/央企", 0.75)
            continue

    return tags


# ── 行业词典 ──────────────────────────────────────────────────────

INDUSTRY_KEYWORDS = {
    "互联网/电商": [
        "电商", "平台", "社交", "内容", "本地生活", "外卖", "团购",
        "直播", "短视频", "社区", "论坛", "游戏", "娱乐",
        "互联网", "在线", "网络",
    ],
    "金融科技": [
        "支付", "理财", "保险", "银行", "证券", "基金", "信贷",
        "风控", "金融", "fintech", "区块链", "数字货币",
    ],
    "企业服务/SaaS": [
        "CRM", "ERP", "OA", "协同办公", "云服务", "SaaS", "PaaS", "IaaS",
        "企业软件", "B2B", "供应链管理", "HR系统", "财务系统",
    ],
    "游戏": [
        "游戏", "手游", "端游", "网游", "游戏引擎", "Unity", "Unreal",
        "游戏开发", "电竞",
    ],
    "在线教育": [
        "教育", "在线教育", "K12", "职业教育", "知识付费", "课程",
        "培训", "学习", "题库", "作业",
    ],
    "医疗健康": [
        "医疗", "健康", "医院", "药品", "医药", "互联网医疗",
        "健康管理", "医疗器械", "基因", "生物",
    ],
    "人工智能": [
        "人工智能", "AI", "大模型", "LLM", "机器学习", "深度学习",
        "自动驾驶", "计算机视觉", "NLP", "语音识别", "AI平台",
    ],
    "汽车/出行": [
        "汽车", "新能源", "电动车", "出行", "网约车", "自动驾驶",
        "车联网", "ADAS", "造车",
    ],
    "物联网/硬件": [
        "IoT", "物联网", "智能硬件", "嵌入式", "芯片", "半导体",
        "传感器", "智能家居", "工业互联网",
    ],
    "物流/供应链": [
        "物流", "快递", "仓储", "供应链", "配送", "运输",
        "货运", "仓配", "最后一公里",
    ],
}


def classify_industry(text: str) -> list[dict]:
    """从文本中推断行业，返回 [{"value": "...", "confidence": 0.xx}]"""
    text_lower = text.lower()
    hits = []
    for industry, keywords in INDUSTRY_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw.lower() in text_lower)
        if count >= 1:
            conf = min(0.95, 0.60 + count * 0.08)
            hits.append({"value": industry, "confidence": round(conf, 2)})
    hits.sort(key=lambda x: x["confidence"], reverse=True)
    return hits[:2]  # 最多返回2个行业


# SEVERITY_DEDUCTION 已移至 tag_config.py
