# AI 简历解析 + 智能打标 技术方案

> 版本：v3.3
> 日期：2026-05-27
> 作者：AIHR 技术团队

---

## 目录

1. [方案定位与背景](#1-方案定位与背景)
2. [正向标签体系](#2-正向标签体系)
3. [负面标签检测](#3-负面标签检测)
4. [整体架构设计](#4-整体架构设计)
5. [异步任务架构](#5-异步任务架构)
6. [API 接口设计](#6-api-接口设计)
7. [统一输出 JSON 规范（v3.1）](#7-统一输出-json-规范v31)
8. [Prompt 设计](#8-prompt-设计)
9. [数据模型](#9-数据模型)
10. [标签配置热更新](#10-标签配置热更新)
11. [第三方对接指南](#11-第三方对接指南)
12. [性能与风险](#12-性能与风险)

---

## 1. 方案定位与背景

### 1.1 方案定位

| 属性 | 说明 |
|------|------|
| **服务属性** | 纯无状态中台服务，零数据库、零本地持久化 |
| **核心能力** | 简历结构化解析 + 正向 5 维度 AI 打标 + 负面/风险检测 |
| **对接模式** | 调用方上传简历文件 → 服务异步处理 → 调用方轮询结果 → 调用方自行存储 |
| **服务架构** | Python FastAPI 单服务，asyncio 事件循环 + ThreadPoolExecutor 线程池 |
| **输出标准** | 统一结构化 JSON（v3.1），简历数据 + 正向标签 + 负面标签平铺返回 |
| **兼容接口** | OpenAI `/v1/chat/completions` 兼容，支持渠道直接对接 |

### 1.2 核心目标

| 目标 | 说明 |
|------|------|
| 双阶段流水线 | 阶段一 OCR/多模态解析 → 阶段二 AI 打标（正向 + 负面），串行执行 |
| 异步执行 | 提交即返回 `taskId`，避免调用方超时（全流程 45~90s） |
| 状态可见 | 轮询接口暴露双阶段状态（0 待处理 / 1 解析中 / 2 打标中 / 3 完成 / 4 失败） |
| 配置热更新 | 标签维度/规则参数修改后无需重启服务即可生效 |
| 格式统一 | 简历字段 + 标签 v3.1 一体返回，调用方自行拆分持久化 |

---

## 2. 正向标签体系

### 2.1 总览

- 共 **5 个固定维度**（`rankLevel` 维度已废弃），全部由 AI 自动提取
- **一个维度可输出多个标签**（education 输出所有适用层级，companyBg/skills/industry 可多条）
- **置信度**：推断型维度（companyBg、skills、industry）携带；确定性维度（education、workYears）不输出 confidence
- LLM 提取失败时自动降级到规则层（院校名单 + 大厂名单 + 行业词典匹配）

### 2.2 五大维度详细规范

---

**维度一：学历层次（education）**

| 输出标签 | 判断依据 |
|---------|---------|
| C9 | 学校匹配 C9 联盟名单（9所） |
| 985 | 学校匹配 985 名单（39所） |
| 211 | 学校匹配 211 名单（112所） |
| 双一流 | 学校匹配双一流名单（基本与 211 重合） |
| 海外名校(QS50) | 学校匹配 QS 前 50（MIT/哈佛/清华/北大/NUS 等 50所） |
| 海外名校(QS100) | 学校匹配 QS 51-100（UCLA/UNSW/布里斯托 等） |
| 海外硕士 | 海外高校且学历为硕士及以上，但不在 QS 榜单内 |
| 博士 | 国内博士研究生（未命中名校） |
| 硕士 | 国内硕士研究生（未命中名校） |
| 本科 | 本科学历 |
| 专科及以下 | 专科、高职、中专等 |

> **层级展开规则（并行输出所有适用层级）**：
> - 命中 C9 → 输出 `["C9", "985", "211", "双一流"]`
> - 命中 985（非C9） → 输出 `["985", "211", "双一流"]`
> - 命中 211（非985） → 输出 `["211", "双一流"]`
> - 命中 QS50 → 输出 `["海外名校(QS50)", "海外名校(QS100)"]`
> - 遍历所有教育经历中的学校，去重合并后输出

---

**维度二：企业背景（companyBg）**

| 输出标签 | 判断依据 |
|---------|---------|
| 头部大厂 | 匹配大厂名单：阿里/腾讯/字节/百度/华为/美团/京东/拼多多/网易/快手/小红书（及其子品牌） |
| 腰部中厂 | 匹配中型互联网：B站/得物/携程/微博/知乎/SHEIN/米哈游/vivo/OPPO/小米/滴滴等 |
| 外企500强 | 匹配跨国科技：微软/谷歌/亚马逊/苹果/Meta/英伟达/IBM/SAP/麦肯锡/波士顿咨询等 |
| 一线外企 | 匹配一线外资：SAP/IBM/Oracle/Salesforce/西门子/博世/三星/索尼/毕马威/普华永道等 |
| 独角兽 | 匹配估值 $1B+ 未上市：商汤/旷视/寒武纪/地平线/陆金所/货拉拉/众安保险等 |
| 明星创业 | 知名 VC 背书的早期高成长公司（LLM 推断） |
| 国企/央企 | 关键词匹配：中国/国家/央企/国企/中石油/国家电网/工商银行/航天/航空等 |
| 传统企业转型 | 传统行业数字化转型企业（LLM 推断） |

> 候选人多段工作经历可命中**多个**企业背景标签，全部输出；confidence 取所有命中项中最高值

---

**维度三：工作年限（workYears）**

| 输出标签 | 累计工作年限 |
|---------|------------|
| 应届 | 0~1 年 |
| 2-3年 | 1~3 年 |
| 3-5年 | 3~5 年 |
| 5-10年 | 5~10 年 |
| 10-15年 | 10~15 年 |
| 15年+ | 15 年以上 |

> 从 `works[].startDate / endDate` 计算月数总和（支持"至今"/"present"），映射到上述区间；单元素数组输出

---

**维度四：核心技能（skills）**

从 `works[].description` + `projects[].description` 提取技术栈，按职能大类归集：

| 职能大类 | 关键词示例 |
|---------|---------|
| 前端 | Vue / React / Angular / TypeScript / Next.js / 小程序 / Flutter / Electron |
| 后端 | Java / Spring Boot / Go / Python / C++ / Rust / .NET / PHP |
| 数据 | SQL / Spark / Flink / Hadoop / Hive / ClickHouse / ETL / 数据治理 |
| AI/算法 | 机器学习 / 深度学习 / NLP / CV / 推荐系统 / LLM / RAG / Agent / 多模态 |
| 移动端 | Android / iOS / React Native / Kotlin / Swift |
| 运维/DevOps | Docker / Kubernetes / CI/CD / Terraform / AWS / 阿里云 / 腾讯云 |
| 产品/设计 | Figma / 用户研究 / A/B 测试 / 交互设计 |
| 管理 | 团队管理 / PMP / Scrum / OKR / 跨部门协作 |

> 归一化处理（"python" → "Python"）；去重；携带整体 confidence

---

**维度五：行业背景（industry）**

| 输出标签 | 关键词匹配（公司业务 + 项目描述） |
|---------|--------------------------------|
| 互联网/电商 | 电商、平台、社交、内容、本地生活、外卖、直播、短视频 |
| 金融科技 | 支付、理财、保险、银行、证券、基金、风控、fintech |
| 企业服务/SaaS | CRM、ERP、OA、协同办公、云服务、SaaS、B2B |
| 游戏 | 游戏、手游、端游、Unity、Unreal、电竞 |
| 在线教育 | 教育、K12、职业教育、知识付费、课程、培训 |
| 医疗健康 | 医疗、健康、医院、药品、互联网医疗、基因 |
| 人工智能 | AI、大模型、LLM、机器学习、自动驾驶、NLP、语音识别 |
| 汽车/出行 | 汽车、新能源、电动车、出行、网约车、ADAS |
| 物联网/硬件 | IoT、物联网、智能硬件、嵌入式、芯片、半导体 |
| 物流/供应链 | 物流、快递、仓储、供应链、配送、货运 |

> 最多输出 2 个行业，按关键词命中数量排序；携带整体 confidence

---

### 2.3 输出格式（正向标签 v3.1）

```json
"positiveTags": {
  "tagsArray": [
    {
      "dimension": "education",
      "displayName": "学历背景",
      "value": [
        {"tag": "985",   "source": "ai"},
        {"tag": "211",   "source": "ai"},
        {"tag": "双一流","source": "ai"},
        {"tag": "硕士",  "source": "rule"}
      ]
    },
    {
      "dimension": "companyBg",
      "displayName": "公司背景",
      "confidence": 0.95,
      "value": [
        {"tag": "头部大厂", "source": "ai"},
        {"tag": "腰部中厂", "source": "ai"}
      ]
    },
    {
      "dimension": "workYears",
      "displayName": "工作年限",
      "value": [
        {"tag": "5-10年", "source": "ai"}
      ]
    },
    {
      "dimension": "skills",
      "displayName": "技能标签",
      "confidence": 0.92,
      "value": [
        {"tag": "Java",         "source": "ai"},
        {"tag": "Spring Boot",  "source": "ai"},
        {"tag": "MySQL",        "source": "ai"},
        {"tag": "Redis",        "source": "ai"},
        {"tag": "Kafka",        "source": "ai"}
      ]
    },
    {
      "dimension": "industry",
      "displayName": "行业背景",
      "confidence": 0.88,
      "value": [
        {"tag": "互联网/电商", "source": "ai"},
        {"tag": "人工智能",   "source": "ai"}
      ]
    }
  ]
}
```

**字段说明**

| 字段 | 类型 | 说明 |
|------|------|------|
| `dimension` | string | 维度标识：education / companyBg / workYears / skills / industry |
| `displayName` | string | 维度中文名（来自 tag_config.py） |
| `confidence` | float | 推断型维度（companyBg / skills / industry）携带；education / workYears 不含 |
| `value[].tag` | string | 标签值，枚举受 `allowedValues` 约束（skills 维度除外） |
| `value[].source` | string | `"ai"`（LLM 提取）或 `"rule"`（规则层补充） |

---

## 3. 负面标签检测

### 3.1 检测维度（4类）

| dimension | displayName | 检测规则 | severity |
|-----------|-------------|---------|---------|
| `jobstable` | 职业稳定性风险 | 频繁跳槽（3年≥4段工作）/ 最近任职≤3个月 / 空窗期≥6个月 | medium / medium / low |
| `integrity` | 诚信与合规风险 | 工作时间线重叠（两段工作重叠超过2个月） | high |
| `matchRisk` | 匹配度硬伤 | 疑似培训机构背景（教育经历含"培训"/"训练营"/"bootcamp"等） | medium |
| `location` | 地域相关 | 自定义敏感词全文匹配（由调用方在 jobSensitiveWords 传入） | high |

### 3.2 规则配置参数（tag_config.py）

```python
NEGATIVE_DIMENSIONS = [
    {
        "dimension": "jobstable", "displayName": "职业稳定性风险", "enabled": True,
        "rules": {
            "frequent_job_change": {"window_years": 3, "threshold": 4, "severity": "medium"},
            "short_tenure":        {"threshold_months": 3, "severity": "medium"},
            "gap_period":          {"threshold_months": 6, "severity": "low"},
        }
    },
    {
        "dimension": "integrity", "displayName": "诚信与合规风险", "enabled": True,
        "rules": {
            "timeline_overlap": {"threshold_months": 2, "severity": "high"},
        }
    },
    {
        "dimension": "matchRisk", "displayName": "匹配度硬伤", "enabled": True,
        "rules": {
            "training_institution": {
                "keywords": ["培训", "学院（培训）", "训练营", "bootcamp", "在线课程", "慕课"],
                "severity": "medium"
            }
        }
    },
    {
        "dimension": "location", "displayName": "地域相关", "enabled": True,
        "rules": {
            "sensitive_words": {"severity": "high"}  # 敏感词由 jobSensitiveWords 传入
        }
    }
]

SEVERITY_DEDUCTION = {"low": -5, "medium": -10, "high": -20}
```

### 3.3 输出格式（负面标签 v3.1）

```json
"negativeTags": {
  "hitCount": 3,
  "deductionScore": -40,
  "tagsArray": [
    {
      "dimension": "jobstable",
      "displayName": "职业稳定性风险",
      "value": [
        {
          "tag": "频繁跳槽",
          "severity": "medium",
          "matchedContent": "近3年内4段工作经历：A公司, B公司, C公司, D公司",
          "source": "ai"
        },
        {
          "tag": "工作空窗期过长",
          "severity": "low",
          "matchedContent": "A公司 离职后至 B公司 入职，空窗约8个月",
          "source": "ai"
        }
      ]
    },
    {
      "dimension": "integrity",
      "displayName": "诚信与合规风险",
      "value": [
        {
          "tag": "工作时间线重叠",
          "severity": "high",
          "matchedContent": "A公司(2020-01~2022-06) 与 B公司(2022-03~2023-12) 时间重叠3个月",
          "source": "ai"
        }
      ]
    }
  ]
}
```

**风险等级与扣分**

| severity | 扣分 | 前端展示建议 |
|---------|------|------------|
| `low` | -5 | 蓝色提示 |
| `medium` | -10 | 黄色警告，建议面试核实 |
| `high` | -20 | 红色风险，建议 HR 人工审核 |

---

## 4. 整体架构设计

### 4.1 服务架构概览

```
调用方（HR 系统 / 第三方）
    │
    ├─ POST /api/resume/async/parse            ← 单文件异步上传（multipart）
    ├─ POST /api/resume/async/batch            ← 批量异步（JSON，type=url，≤20份）
    ├─ POST /api/resume/async/batch/multiform  ← 批量异步（files + urls 混合，≤20份）
    ├─ GET  /api/resume/async/result           ← 轮询结果
    └─ POST /api/resume/sync/batch             ← 同步批量（url/file/base64，≤50份，阻塞等待）

AIHR Python 服务（FastAPI v3.1.0，端口 21804）
    │
    ├─ uvicorn asyncio 事件循环（接收/响应请求）
    │
    ├─ ThreadPoolExecutor（max_workers=24）← 隔离阻塞 IO
    │       ├─ PDF → fitz 渲染图像
    │       ├─ Qwen3.5-397B-A17B LLM 调用（OCR 解析 + 正向打标）
    │       └─ 规则层计算（敏感词检测）
    │
    ├─ 内存任务缓存（asyncio.Lock + dict，TTL 10min）
    │
    └─ OpenAI 兼容层（/v1/chat/completions，供渠道对接）

后端存储层（算法侧不持久化，由调用方自行入库）
    ├─ logs/aihr_<date>_<pid>.log    ← 结构化运营日志（滚动）
    └─ results/<ts>_<name>_<id>.json ← 完整 JSON 文件（离线审计）
```

### 4.2 解析与打标执行时序

```
0s      提交请求（POST /api/resume/async/parse 或 batch）
        └─ 生成 taskId，status=0，立即返回（< 100ms）
           asyncio.create_task 启动后台流水线

0.1s  ┌─ 阶段一：简历解析（status → 1）─────────────────────────────┐
      │  run_in_executor → 线程池                                   │
      │  1. PDF → fitz 逐页渲染为 PNG（DPI=144）                     │
      │  2. ≤8页：一次调用 LLM；>8页：分批并发 + 结果合并             │
      │  3. Qwen3.5-397B-A17B 多模态 LLM → 结构化 JSON              │
      │  4. json_repair 解析 + _validate_schema 字段校验             │
      │  输出：candidate / educations / works / projects            │
      └───────────────────────────────────── 耗时 20~40s ───────────┘

20~40s ┌─ 阶段二：打标（status → 2）──────────────────────────────────┐
       │  run_in_executor → 线程池（串行，依赖阶段一结果）             │
       │  ① LLM 正向标签提取（耗时 5~20s）                            │
       │     - 结构化简历 + 岗位信息注入 Prompt                       │
       │     - 调用 Qwen3.5 → 5 维度 positiveTags（{dim}ValueArray） │
       │     - LLM 失败 → 自动降级规则层                              │
       │  ② 规则层负面检测（耗时 < 1s）                               │
       │     - 稳定性：频繁跳槽 / 任职偏短 / 空窗期                    │
       │     - 诚信：时间线重叠                                        │
       │     - 地域：自定义敏感词全文匹配                               │
       │     - 硬伤：疑似培训机构检测                                   │
       └────────────────────────────────────── 耗时 5~20s ───────────┘

45~60s  _conform_to_template 序列化 v3.1 JSON
        → 写入结构化日志（3段日志块）
        → 持久化到 results/ 目录
        → status=3，内存缓存更新

        调用方轮询 GET /api/resume/async/result → 获取完整结果
```

### 4.3 LLM 降级机制

```
LLM 正向打标失败（超时 / 网络 / JSON 修复失败）
    │
    └─→ 规则层降级（extract_positive_tags_rule）
        ├─ education  ← 院校名单精确匹配（C9/985/211/QS 榜）
        ├─ workYears  ← works[].startDate/endDate 计算月数
        ├─ companyBg  ← 大厂/外企/国企名单关键词匹配
        └─ industry   ← 行业关键词词典匹配
        （skills 仅 LLM 可提取，降级时不输出）
```

### 4.4 日志系统

#### 4.4.1 文件存储结构

日志按功能级别和月份分目录归档，`backupCount=0` 永久保留，防止 inode 耗尽：

```
logs/                                     ← LOG_DIR 环境变量控制（默认 ./logs）
├── debug/
│   └── YYYY-MM/
│       └── debug_YYYY-MM-DD_<pid>.log   ← 全量日志（DEBUG 级别及以上）
└── error/
    └── YYYY-MM/
        └── error_YYYY-MM-DD_<pid>.log   ← 仅 ERROR 级别
```

**轮转规则**（`_SizeTimedRotatingFileHandler`，标准 `TimedRotatingFileHandler` 扩展）：
- 时间轮转：每天零点（`when="midnight"`）创建新文件
- 大小轮转：单文件超过 `max_bytes`（默认 50MB）时立即触发，防止单文件无限增长
- 两者叠加：大小触发优先，时间轮转每天保底执行一次

文件名格式：`debug_2026-06-02_<pid>.log`，`<pid>` 默认为进程 PID，生产环境可通过 `log_pid: aihr` 改为固定服务名，便于多实例日志归并。

#### 4.4.2 日志格式

**文本格式（默认，`json_format: false`）**

```
2026-06-02 10:30:45 | INFO  | [a1b2c3d4] [简历解析] [张三.pdf] [-] | [parser.py:128 _log_result] | 解析结果 文件: 张三.pdf 耗时: 32s
```

| 字段 | 示例 | 说明 |
|------|------|------|
| 时间 | `2026-06-02 10:30:45` | 北京时间（CST=UTC+8），`Formatter.converter` 强制注入，不依赖容器 TZ 环境变量 |
| 级别 | `INFO` | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| request_id | `a1b2c3d4` | 8 位十六进制，每个 HTTP 请求唯一，中间件 `_reset_log_context` 自动生成注入 |
| business_type | `简历解析` | 业务类型标识（简历解析 / 打标 / batch 等） |
| filename_ctx | `张三.pdf` | 当前处理的文件名 |
| 扩展字段 | `-` | `set_extra_context()` 注入的自定义 key=value；无时显示 `-` |
| 代码位置 | `parser.py:128 _log_result` | 文件名:行号 函数名 |
| 消息 | 日志正文 | 业务内容 |

控制台附带 ANSI 颜色（`coloredlogs`），文件输出纯文本，两者格式字符串相同。

**JSON 格式（`json_format: true`，适配 K8s/ELK/Loki/Fluentd）**

```json
{"ts": "2026-06-02T10:30:45", "level": "INFO", "request_id": "a1b2c3d4", "business_type": "简历解析", "filename_ctx": "张三.pdf", "caller": "parser.py:128:_log_result", "msg": "解析结果 文件: 张三.pdf"}
```

每行一条单行 JSON，无需正则即可被 ELK Logstash / Fluentd / Loki 直接解析字段；`set_extra_context()` 注入的 key-value 作为顶层字段展开，便于按字段过滤。

#### 4.4.3 请求上下文传播

每个 HTTP 请求进入时，`_reset_log_context` 中间件自动设置三个上下文字段，整条调用链（包括线程池）均自动携带，无需手动传参：

| 字段 | 说明 | 设置时机 |
|------|------|---------|
| `request_id` | 8 位十六进制，每请求自动生成 | 中间件入口 |
| `business_type` | 业务类型标识 | 请求入口（解析 / 打标 / batch） |
| `filename` | 当前处理文件名 | 任务启动时 |

**双轨传播机制**（`_ContextFilter` 实现）：
- asyncio 协程：`ContextVar` 天然隔离，不同协程的请求上下文互不干扰
- ThreadPoolExecutor 线程：`wrap_for_executor(fn)` 在「调用时」捕获 ContextVar 快照注入线程，线程池线程复用时不串台

优先级：`ContextVar`（asyncio）> `threading.local`（线程兜底）> 默认值 `-`

`_ContextFilter` 挂在 logger 层（而非 handler 层），每条 LogRecord 只执行一次上下文注入，不随 handler 数量线性增加。

#### 4.4.4 每次任务的三段日志块

每个异步任务完成后，`debug/` 日志中连续输出三段结构化内容（`tasks.py` 流水线尾部触发）：

| 序号 | 日志块标题 | 内容 |
|------|-----------|------|
| ① | `解析结果 文件: xxx 耗时: Ns` | 候选人基础信息（姓名/手机/邮箱）/ 教育经历摘要 / 工作经历摘要 / 项目经历摘要 + 完整解析 JSON |
| ② | `打标结果 文件: xxx taskId: xxx` | 5 维正向标签（含置信度）/ 风险标签列表 / 整体置信度 / 扣分合计 |
| ③ | `完整输出 JSON 文件: xxx taskId: xxx` | v3.1 格式完整 JSON（与接口返回完全一致） |

三段日志均写入 `debug/` 目录；同时 `parser._save_result()` 将完整 JSON 落盘到 `results/` 目录，服务重启后内存缓存清空时可人工追溯。

#### 4.4.5 results/ 目录

每个完成的任务同步写入磁盘文件，格式为：

```
results/
└── 20260602_103045_张三_e39d009b.json   ← <日期时间>_<姓名>_<taskId 前8位>.json
```

文件内容与接口返回的 `data.result` 完全一致（v3.1 JSON）。用途：审计追溯 / 服务重启后补查历史结果 / 对比接口返回与磁盘文件一致性。

> 注意：`results/` 目录不做自动清理，需运维手动或定时任务维护磁盘占用。`uploads/` 目录超过 15 天的文件由服务启动时的 `_cleanup_uploads()` 自动清理。

#### 4.4.6 噪音过滤

`_NoiseFilter` 过滤以下 uvicorn.access 日志，避免健康检查探针污染业务日志：

- `GET /health`
- `HEAD /health`
- `GET /favicon.ico`

过滤器绑定到 `uvicorn.access` logger，不影响业务 logger 的其他日志。

#### 4.4.7 配置与热更新

**配置文件**：`log_config.yml`（服务根目录），watchdog 监听文件变化，200ms 防抖后自动重新加载，无需重启即可调整日志级别或切换格式。

```yaml
log_config:
  name: aihr                   # logger 名称（logging.getLogger(name) 全局访问）
  log_level_console: INFO      # 控制台级别；INFO 过滤 DEBUG 噪音（健康检查等）
  log_level_file: DEBUG        # 文件输出级别；通常保持 DEBUG
  log_folder: logs             # 日志根目录（相对路径基于服务根目录）
  log_pid: ""                  # 文件名标识；空 = 进程 PID；生产建议填 aihr
  max_bytes: 52428800          # 单文件大小上限（50MB）；0 = 不限
  use_queue: false             # Gunicorn 多 worker 时设为 true
  json_format: false           # true = 单行 JSON（ELK/Loki 场景）；false = 带颜色文本
```

**环境变量覆盖**（优先级高于配置文件）

| 变量 | 说明 |
|------|------|
| `LOG_LEVEL` | 同时覆盖控制台和文件日志级别（K8s ConfigMap 临时调整场景常用） |
| `LOG_DIR` | 日志根目录（覆盖 `log_folder`） |
| `LOG_KEEP_DAYS` | 日志保留天数（供外部清理脚本使用，服务本身不做自动清理） |
| `LOG_MAX_MB` | 单文件大小上限（MB），覆盖 `max_bytes` |
| `LOG_CONFIG_PATH` | 指定配置文件路径（K8s ConfigMap 挂载路径场景） |

**Gunicorn 多 worker 模式**（`use_queue: true`）：启用 `QueueHandler → QueueListener` 异步写文件，单一队列消费者在后台线程分流到 debug/error handler，消除多 worker 同时写同一文件的竞争条件；原版双 QueueHandler 双写问题已修复。

**第三方 logger 接管**：uvicorn / httpx / sqlalchemy 等第三方 logger 统一接管到同一套 handler，控制台与文件输出格式一致；列表可通过 `log_config.third_party_loggers` 覆盖。

#### 4.4.8 logger_manager 在业务中的集成方式

##### 导入层级

`logger_manager.py` 不直接被业务模块导入，而是由 `config.py` 统一转发：

```
logger_manager.py
   └── config.py（re-export）
         ├── logger              ← 全局 Logger 单例（懒加载）
         ├── set_log_context     ← 注入 request_id / business_type / filename
         ├── wrap_for_executor   ← 线程池 ContextVar 快照包装
         ├── get_logger_manager  ← 获取 LoggerManager 单例（lifespan 使用）
         └── set_extra_context   ← 注入自定义扩展字段
```

业务模块统一写 `from config import logger, set_log_context`，`parser.py` 例外（直接 `from logger_manager import logger`，原因：parser 不依赖 config 中的环境变量转发即可工作）。

##### 生命周期管理（app.py lifespan）

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    lm = get_logger_manager()
    lm.start_config_watcher()   # ① 启动 watchdog，监听 log_config.yml 热更新
    _cleanup_uploads()
    cleanup_task = asyncio.create_task(_periodic_cleanup())
    yield
    cleanup_task.cancel()
    executor.shutdown(wait=False)
    lm.stop_config_watcher()    # ② 停止 watchdog（5s 超时保护）
    lm.stop_queue_listener()    # ③ 停止 QueueListener，确保缓冲日志落盘
```

服务启动时启动配置监听器，关闭时按顺序停止监听器和队列写入器，保证日志无丢失。

##### 请求上下文注入（app.py 中间件）

```python
@app.middleware("http")
async def _reset_log_context(request: Request, call_next):
    # 每个请求入口重置 thread-local，防止前一请求的上下文泄漏到探针/系统日志
    _thread_local.request_id    = "-"
    _thread_local.business_type = "-"
    _thread_local.filename      = "-"
    return await call_next(request)
```

中间件只做重置（兜底）；实际上下文字段由业务代码在流水线入口显式注入。

##### 流水线上下文注入与线程传播（tasks.py）

`_run_parse_and_tag` 在每个阶段开始前调用 `set_log_context`，再用 `wrap_for_executor` 将快照传入线程池：

```python
# 阶段一：简历解析
set_log_context(request_id=task_id[:8], business_type="简历解析", filename=filename)
# ↑ 必须在 wrap_for_executor 之前调用，copy_context() 才能捕获正确快照
async with _parse_semaphore:
    await _set_task_field(task_id, status=1)
    resume = await loop.run_in_executor(
        executor, wrap_for_executor(_parse_with_logging), source, filename
    )

# 阶段二：标签打标（上下文切换为"标签打标"）
set_log_context(request_id=task_id[:8], business_type="标签打标", filename=filename)
await _set_task_field(task_id, status=2)
tag_info = await loop.run_in_executor(
    executor, wrap_for_executor(lambda: run_tagging(resume, job_info, llm_call_fn=_llm_call))
)
```

**关键约束**：`set_log_context` 必须在 `wrap_for_executor` 之前调用，`copy_context()` 才能捕获正确的 ContextVar 快照；线程池中的子线程通过 `ctx.run(fn)` 继承该快照，整条链路日志均携带正确的 `request_id / business_type / filename`。

##### 三段日志块的具体写入点

| 序号 | 写入位置 | 函数 | 日志级别 |
|------|---------|------|---------|
| ① 解析结果 | `parser.py` | `_log_result()` ← 由 `_parse_with_logging()` 调用 | `INFO`（摘要）/ `DEBUG`（完整 JSON） |
| ② 打标结果 | `tasks.py` | `_log_tag_info()` ← 由 `_run_parse_and_tag()` 调用 | `INFO` |
| ③ 完整响应 | `tasks.py` | `_run_parse_and_tag()` 内联 `logger.debug` | `DEBUG` |

**第①段**（`parser._log_result`）用 `├─` 盒子格式输出：

```
┌────────────────────────────────────────────────────────────
│ 解析结果  文件: 张三.pdf  耗时: 32.15s
├──── 基本信息 ────────────────────────
│  姓名: 张三  性别: 男  年龄: 28  电话: 138xxxxxxxx
│  邮箱: ...  地址: 广东省深圳市南山区  ...
├──── 教育经历(1) ──────────────────────
│  上海交通大学  计算机科学  硕士研究生 / 硕士  2017-09 ~ 2020-07
├──── 工作经历(2) ──────────────────────
│  [高级后端工程师]  字节跳动  2022-03 ~ 至今
│    负责推荐系统...
├──── 项目经历(1) ──────────────────────
│  [技术负责人]  实时推荐引擎重构  ...
├──── 完整简历 JSON ──────────────────── （DEBUG 级别）
│  { "candidate": {...}, ... }
└────────────────────────────────────────────────────────────
```

**第②段**（`tasks._log_tag_info`）：

```
┌────────────────────────────────────────────────────────────
│ 打标结果  文件: 张三.pdf  taskId: e39d009b-...
│ 简历解析: 2026-06-02 10:30:45  打标耗时: 8.3s  全程: 40.5s
├──── 正向标签(5) ────────────────────────
│  [education    ] 985, 211, 双一流, 硕士
│  [companyBg    ] 头部大厂  置信度=0.95
│  [workYears    ] 3-5年
│  [skills       ] Java, Spring Boot, MySQL, Redis  置信度=0.92
│  [industry     ] 互联网/电商, 人工智能  置信度=0.88
│  整体置信度: 0.92
├──── 风险标签(0) ────────────────────────
│  无风险标签命中
│  标签打标时间: 2026-06-02 10:31:08
└────────────────────────────────────────────────────────────
```

**第③段**（`tasks` 内联 `logger.debug`）：完整 v3.1 JSON，格式同接口返回，每行前缀 `│  `。

##### 磁盘持久化（parser._save_result）

流水线末尾、`status→3` 之前，`_save_result` 将完整 JSON 写入 `results/` 目录：

```python
def _save_result(task_id: str, filename: str, result: dict):
    stem      = Path(filename).stem[:40]
    safe_stem = "".join(c if c.isalnum() or c in "-_." else "_" for c in stem)
    ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path  = RESULT_DIR / f"{ts}_{safe_stem}_{task_id[:8]}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"结果已保存: {out_path.name}")
```

文件名示例：`20260602_103045_张三_e39d009b.json`，`RESULT_DIR` 由环境变量 `RESULT_DIR` 控制（Dockerfile 默认 `/app/results`）。

##### 其他业务日志点

| 位置 | 触发条件 | 级别 | 内容 |
|------|---------|------|------|
| `parser._llm_call` | thinking block 签名错误 | `WARNING` | 去掉 thinking 参数重试提示 |
| `parser.run` | 大页面分批模式 | `DEBUG` | 分批数量 / 批大小 / schema 修正明细 |
| `parser._cleanup_uploads` | uploads 清理完成 / 失败 | `INFO` / `WARNING` | 删除文件数 / 失败文件名 |
| `tasks._cleanup_task_store` | TTL 到期清理 | `INFO` | 清理过期任务数量 |
| `tasks._run_parse_and_tag` | 阶段一完成 | `INFO` | `[taskId] 阶段一完成，耗时 Ns` |
| `tasks._run_parse_and_tag` | 任务失败 | `ERROR` | `[taskId] 任务失败: <异常信息>` |

#### 4.4.9 日志采集

服务同时维护两条日志输出路径，部署模式不同，采集方式也不同。

**两条输出路径**

| 路径 | 输出目标 | 控制开关 |
|------|---------|---------|
| 文件 | `logs/debug/` + `logs/error/`（分级归档） | 始终写入 |
| stdout | uvicorn 进程标准输出（app logger + uvicorn.access） | `to_console: true`（默认开启） |

---

**Docker Compose 部署 — 文件采集**

`docker-compose.yml` 将 `./logs:/app/logs` 挂载到宿主机，日志文件在宿主机可直接读写：

```
宿主机 ./logs/
├── debug/YYYY-MM/debug_<date>_<pid>.log   ← 全量，Filebeat / Fluentd 采集
└── error/YYYY-MM/error_<date>_<pid>.log   ← 仅 ERROR，告警场景
```

**Filebeat 配置示例（文本格式）**

```yaml
filebeat.inputs:
  - type: log
    paths:
      - /path/to/aihr/logs/debug/*/*.log
    multiline:
      pattern: '^\d{4}-\d{2}-\d{2}'   # 日期开头为新行
      negate: true
      match: after
```

**Filebeat 配置示例（JSON 格式，`json_format: true`）**

```yaml
filebeat.inputs:
  - type: log
    paths:
      - /path/to/aihr/logs/debug/*/*.log
    json.keys_under_root: true         # 字段提升至根，无需正则解析
    json.overwrite_keys: true
```

---

**Kubernetes 部署 — stdout 采集**

当前 CI/CD 的 K8s Deployment（`.gitlab-ci.yml`）未挂载 PVC，`/app/logs` 写入容器临时存储，Pod 重启后丢失。K8s 自动采集 stdout，是 K8s 环境的**主要采集通道**：

```
容器 stdout（uvicorn + app logger）
   ↓
K8s Node 上的容器运行时（containerd / Docker）
   ↓ /var/log/containers/*.log
DaemonSet（Fluent Bit / Filebeat） → Elasticsearch / Loki / Splunk
```

**开启 JSON 格式**后，Loki / ES 无需 Grok 正则，直接按字段过滤：

```
{app="aihr-ocr"} | json | business_type="简历解析" | level="ERROR"
```

**生产 K8s 推荐补充配置**

```yaml
# Deployment 补充：挂载 PVC 持久化日志文件（人工追溯 / 审计）
volumes:
  - name: logs
    persistentVolumeClaim:
      claimName: aihr-logs-pvc
containers:
  - name: aihr-ocr
    volumeMounts:
      - name: logs
        mountPath: /app/logs
    env:
      - name: json_format          # 让 stdout 输出结构化 JSON
        value: "true"
      - name: LOG_LEVEL            # K8s ConfigMap 动态调整级别，无需重启
        value: "INFO"
```

> PVC 容量建议：按 `50MB × 天数 × 副本数` 预估；`error/` 目录体积远小于 `debug/`。

---

**本地调试快速命令**

```bash
# 实时跟踪当天最新 debug 日志
tail -f logs/debug/$(date +%Y-%m)/debug_$(date +%Y-%m-%d)_*.log

# 按 taskId 过滤（文件中唯一标识）
grep "e39d009b" logs/debug/2026-06/debug_2026-06-02_*.log

# K8s Pod 实时日志（最近 200 行）
kubectl logs -n demo -l app=aihr-ocr --tail=200 -f

# K8s JSON 格式日志 jq 过滤（仅 ERROR）
kubectl logs -n demo -l app=aihr-ocr | jq 'select(.level=="ERROR")'
```

---

## 5. 异步任务架构

### 5.1 任务状态机

| 状态码 | 含义 | 说明 |
|--------|------|------|
| `0` | 待处理 | 任务已入队，尚未开始 |
| `1` | 简历解析中 | 阶段一：OCR/多模态解析进行中 |
| `2` | 标签打标中 | 阶段二：正向打标 + 负面检测进行中 |
| `3` | 任务完成 | 完整 JSON 已就绪，可获取结果 |
| `4` | 任务失败 | 处理异常，`errorMsg` 字段说明原因 |

```
0 → 1 → 2 → 3（完成）
               ↘ 4（失败）
```

### 5.2 任务存储结构

```python
@dataclass
class TaskRecord:
    task_id:           str
    status:            int          # 0/1/2/3/4
    created_at:        float
    completed_at:      float | None
    result:            dict | None  # v3.1 完整 JSON
    error_msg:         str | None
    estimated_seconds: int = 90     # 预估完成时间
    extra:             str | None   # 调用方透传字段（batch接口用）
```

### 5.3 内存缓存规格

| 属性 | 说明 |
|------|------|
| 容器 | `asyncio.Lock + dict`（协程安全） |
| Key | UUID taskId |
| Value | TaskRecord（含完整 result） |
| TTL | 10 分钟（每 60s 定时清理过期记录） |
| 预警阈值 | 2000 条（超出说明轮询不及时，需排查） |

### 5.4 文件输入支持

| 来源 | 说明 |
|------|------|
| multipart file upload | 二进制字节流，支持 PDF（当前主链路） |
| `type=url` | MinIO / HTTP URL，服务自动下载并校验 PDF 魔数（`%PDF`） |
| `type=file` | 本地文件路径（内部测试用） |
| `type=base64` | base64 编码字节（/v1/chat 接口） |

### 5.5 详细函数调用链路

#### 5.5.1 文件依赖关系

```
app.py（入口）
  ├── api/routes_async.py      → 异步解析+打标接口
  │     └── tasks.py           → 任务状态机 + 流水线编排
  │           ├── parser.py    → PDF 解析核心（LLM 调用 + 分批并发）
  │           │     ├── pdf_utils.py    → PDF 渲染（fitz + PIL，DPI=144）
  │           │     └── postprocess.py → 后处理 + Schema 校验 + 合并 + 序列化
  │           ├── file_utils.py → 文件下载与 PDF 来源识别
  │           └── tag/tagger.py → 正向打标 + 风险检测
  │                 ├── tag_data.py    → 院校/公司/行业数据（启动时加载）
  │                 │     └── (读取) schemas/tag_static_data.json（人工维护）
  │                 └── tag_config.py  → 打标维度默认配置（兜底值）
  ├── api/routes_openai.py     → OpenAI 兼容接口
  ├── api/routes_tags.py       → 打标配置 CRUD
  │     └── (读写) schemas/tag_config_runtime.json
  └── api/routes_algorithm.py  → 算法内部接口（仅打标，无解析）
```

#### 5.5.2 主流程：单文件异步解析+打标

```
POST /api/resume/async/parse
  api/routes_async.py  async_parse_resume()
    │  校验文件格式 / 大小 / 幂等 requestId
    │  生成 task_id，写入 _task_store[task_id] = TaskRecord(status=0)
    └─ asyncio.create_task(
         tasks.py  _run_parse_and_tag(task_id, file_bytes, filename, job_info)
       )
       ├─ [阶段一] status → 1
       │   loop.run_in_executor(executor,
       │     parser.py  _parse_with_logging(source, filename)
       │       parser.py  run(source, verbose=True)
       │         pdf_utils.py  pdf_to_images(source)
       │           pdf_utils.py  _open_pdf(source)         ← fitz.open + 加密检测
       │           fitz.Document.get_pixmap()              ← 每页渲染 PNG（DPI=144）
       │           PIL.Image.open()
       │         ┌─ [≤8页] 一次调用 ──────────────────────────────────┐
       │         │  parser.py  _llm_call(messages)                    │
       │         │    openai.OpenAI.chat.completions.create()         │
       │         │    → Qwen3.5-397B-A17B（多模态，images + prompt）  │
       │         └───────────────────────────────────────────────────┘
       │         ┌─ [>8页] 分批并发 ──────────────────────────────────┐
       │         │  pdf_utils.py  pdf_to_batches(source, batch_size)  │
       │         │  pdf_utils.py  smart_batch_size(page_count)        │
       │         │  ThreadPoolExecutor.map(                           │
       │         │    parser.py  _infer_batch(batch)                  │
       │         │      parser.py  _llm_call(messages)                │
       │         │  )                                                 │
       │         │  postprocess.py  _merge_results(batch_results)     │
       │         │    postprocess.py  _merge_candidate(a, b)          │
       │         │    postprocess.py  _dedup(items, keys)             │
       │         └───────────────────────────────────────────────────┘
       │         json_repair.loads(raw)
       │         postprocess.py  _post_process(data)       ← gender/age 规范化
       │         postprocess.py  _validate_schema(data)    ← 字段补全/类型校验
       │       parser.py  _log_result(result, filename, elapsed)
       │   )
       │
       ├─ [阶段二] status → 2
       │   loop.run_in_executor(executor,
       │     tag/tagger.py  run_tagging(resume, job_info, llm_call_fn=_llm_call)
       │       tagger.py  extract_positive_tags_llm(resume, job_info, llm_call_fn)
       │         POSITIVE_TAG_PROMPT.format(resume_json, job_title, ...)
       │         parser.py  _llm_call(messages)            ← 纯文本，5维度打标
       │         json_repair.loads(raw)
       │         tagger.py  _normalize_positive_tags(tags, resume)
       │           tagger.py  _expand_edu_values(raw_values)
       │             tag_data.py  EDUCATION_HIERARCHY.get(v)
       │           tagger.py  _normalize_work_years_str(v)
       │           tag_data.py  classify_education()       ← 补充缺失 education
       │           tagger.py  _calc_work_years()           ← 补充缺失 workYears
       │         ─ LLM 失败时降级 ──────────────────────────────────────
       │         tagger.py  extract_positive_tags_rule(resume)
       │           tag_data.py  classify_education(schools, degrees)
       │             tag_data.py  C9_SCHOOLS（set 精确匹配）
       │             tag_data.py  SCHOOLS_985
       │             tag_data.py  SCHOOLS_211
       │             tag_data.py  QS_TOP50
       │             tag_data.py  QS_TOP100
       │             tag_data.py  EDUCATION_HIERARCHY（层级展开）
       │           tagger.py  _calc_work_years(works)
       │             tagger.py  _parse_date(date_str)      ← 支持"至今"/"present"
       │             dateutil.relativedelta
       │           tagger.py  _map_work_years(years)
       │           tag_data.py  classify_companies(companies)
       │             tag_data.py  TOP_TIER_COMPANIES
       │             tag_data.py  FORTUNE500_COMPANIES
       │             tag_data.py  FIRST_TIER_FOREIGN
       │             tag_data.py  MID_TIER_COMPANIES
       │             tag_data.py  UNICORN_COMPANIES
       │             tag_data.py  STATE_OWNED_KEYWORDS
       │           tag_data.py  classify_industry(all_text)
       │             tag_data.py  INDUSTRY_KEYWORDS（10个行业，词频打分）
       │
       │       tagger.py  detect_sensitive_tags(resume, job_info)
       │         tagger.py  _neg_cfg_map()
       │           tag_config.py  NEGATIVE_DIMENSIONS（4个维度配置）
       │         tagger.py  _detect_stability(works, hit_tags, rules)
       │           tagger.py  _parse_date()
       │           rules["frequent_job_change"]  ← window_years=3, threshold=4
       │           rules["short_tenure"]          ← threshold_months=3
       │           rules["gap_period"]            ← threshold_months=6
       │         tagger.py  _detect_timeline_overlap(works, hit_tags, rules)
       │           tagger.py  _parse_date()
       │           rules["timeline_overlap"]      ← threshold_months=2
       │         tagger.py  _parse_sensitive_words(jobSensitiveWords)
       │         tagger.py  _detect_custom_words(resume, candidate, words, ...)
       │         tagger.py  _detect_education_risk(educations, hit_tags, rules)
       │           rules["training_institution"]  ← keywords 列表匹配
       │         tag_config.py  SEVERITY_DEDUCTION（扣分权重）
       │   )
       │
       ├─ tasks.py   _log_tag_info(task_id, filename, tag_info, ...)
       ├─ postprocess.py  _conform_to_template(task_id, resume, tag_info, ...)
       │     load_tag_cfg()              ← mtime 缓存，每次打标读最新运行时配置
       │     _build_display_maps(cfg)   ← 动态构建 displayName 查找表
       │     tag_data.py  classify_education()  ← education 规则层补全
       ├─ parser.py  _save_result(task_id, filename, result)  → results/*.json
       └─ _set_task_field(task_id, status=3, result=result)
```

#### 5.5.3 OpenAI 兼容接口链路

```
POST /v1/chat/completions  (model=DeepSeek-OCR-JSON 或 AIHR-Resume)
  api/routes_openai.py  create_chat_completion(request)
    api/routes_openai.py  _is_probe(contents)    ← 健康探测短路返回
    file_utils.py  get_pdf_source_from_content(contents)
      ├─ type=url    → file_utils.py  download_file(url, filename)
      │                  requests.get() + %PDF 魔数校验
      ├─ type=file   → 直接使用本地路径
      └─ type=base64 → base64.b64decode()
    ─ async_mode=false（同步，默认） ────────────────────────────────────
    await tasks.py  _run_parse_and_tag(...)       ← 等待完整流水线
    ─ async_mode=true（异步） ──────────────────────────────────────────
    asyncio.create_task(_run_parse_and_tag(...))  ← 立即返回 taskId

POST /v1/chat/completions  (model=DeepSeek-OCR)
  api/routes_openai.py  create_chat_completion(request)
    file_utils.py  get_pdf_source_from_content(contents)
    loop.run_in_executor(executor,
      parser.py  _parse_with_logging(source, filename)  ← 仅解析，不打标
    )
    返回结构化简历 JSON 字符串（OpenAI message.content）
```

#### 5.5.4 批量异步接口链路

##### POST /api/resume/async/batch（JSON body，type=url，≤20份）

```text
POST /api/resume/async/batch
  api/routes_async.py  async_batch_parse(request: dict)
  │
  ├─ [入参校验]
  │   resumes_raw = request.get("resumes", [])
  │   ① resumes_raw 为空          → 400  {"code":1001, "msg":"resumes 数组不能为空"}
  │   ② resumes_raw 非 list       → 400  {"code":1001, "msg":"resumes 必须是数组"}
  │   ③ 任意 item.type != "url"   → 400  {"code":1001, "msg":"resumes 仅支持 type=url"}
  │   ④ len(resumes_raw) > 20     → 400  {"code":1003, "msg":"单次最多提交 20 份"}
  │
  ├─ [构建 job_info]
  │   job_info = {
  │     jobTitle:          request.get("jobTitle", ""),
  │     requiredSkills:    request.get("requiredSkills", ""),
  │     jobDescription:    request.get("jobDescription", ""),
  │     jobSensitiveWords: request.get("jobSensitiveWords", ""),
  │   }
  │
  ├─ [逐条处理，顺序执行，下载串行但任务并发]
  │   loop = asyncio.get_running_loop()
  │   for item in resumes_raw:                      ← 逐条迭代
  │     filename = item["file"].get("name", "resume.pdf")
  │     extra    = item["file"].get("extra") or item.get("extra")  ← 透传业务字段
  │     │
  │     ├─ [文件下载，阻塞 IO 卸载到线程池]
  │     │   source, fn = await loop.run_in_executor(
  │     │     executor,
  │     │     file_utils.py  get_pdf_source_from_content([item])
  │     │       │  遍历 content_items，找到第一个 type != "text" 的项
  │     │       │  ctype = item["type"]  → "url"
  │     │       │  file_data = item["file"]
  │     │       │  filename  = file_data["name"]
  │     │       └─ file_utils.py  download_file(file_data["path"], filename)
  │     │             requests.get(url, timeout=60)   ← HTTP/HTTPS 下载
  │     │             response.raise_for_status()     ← 非 2xx 抛异常
  │     │             content = response.content
  │     │             [魔数校验] content.startswith(b"%PDF")
  │     │               失败 → raise ValueError("下载内容非有效 PDF ...")
  │     │             save_path = UPLOAD_DIR / filename
  │     │             open(save_path, "wb").write(content)  ← 落盘到 uploads/
  │     │             return str(save_path)           ← source = 本地路径字符串
  │     │   )
  │     │
  │     ├─ [下载异常处理]
  │     │   except Exception as e:
  │     │     tasks.append({"code":1001, "msg":f"文件下载失败: {e}", "taskId":None})
  │     │     continue
  │     │   if source is None:
  │     │     tasks.append({"code":1001, "msg":"无法下载 PDF ...", "taskId":None})
  │     │     continue
  │     │
  │     ├─ [创建任务记录]
  │     │   task_id = str(uuid.uuid4())
  │     │   rec = TaskRecord(task_id=task_id, extra=extra)  ← extra 绑定到记录
  │     │   async with _task_lock:
  │     │     _task_store[task_id] = rec              ← 写入内存缓存（status=0）
  │     │
  │     ├─ [提交后台流水线（非阻塞）]
  │     │   asyncio.create_task(
  │     │     tasks.py  _run_parse_and_tag(task_id, source, fn or filename, job_info)
  │     │   )                                         ← 立即返回，不等待完成
  │     │   注：source 此时为 uploads/ 目录下的本地路径
  │     │
  │     └─ tasks.append({"code":0, "taskId":task_id, "status":0, "extra":extra, ...})
  │
  └─ [返回汇总结果]
      submitted = sum(1 for t in tasks if t["code"] == 0)
      return JSONResponse({
        "code": 0, "msg": "success",
        "total":    len(resumes_raw),
        "submitted": submitted,
        "failed":   len(resumes_raw) - submitted,
        "estimatedSeconds": 90,
        "tasks": tasks,      ← 每份文件的 taskId / 状态 / extra
      })
```

**关键时序说明**

| 步骤 | 是否阻塞事件循环 | 说明 |
|------|----------------|------|
| 文件下载（`download_file`） | 否（`run_in_executor`） | 每份文件**顺序** await，下载完一份再下一份 |
| `_run_parse_and_tag` | 否（`create_task`） | 所有任务**并发**在后台执行，互不阻塞 |
| 接口响应 | < 100ms × N（下载耗时除外） | 下载完所有文件后立即返回 |

> `extra` 字段来源：`item["file"]["extra"]` 或 `item["extra"]`，原样存入 `TaskRecord.extra`，轮询 `GET /api/resume/async/result` 或 `GET /api/tasks` 时随结果透传回来。

---

##### POST /api/resume/async/batch/multiform（multipart，files + urls 混用，≤20份）

```text
POST /api/resume/async/batch/multiform
  api/routes_async.py  async_batch_multiform_parse(
                         files, resumes, jobTitle, requiredSkills,
                         jobDescription, jobSensitiveWords
                       )
  │
  ├─ [入参校验]
  │   ① files 和 resumes 均为空  → 400  {"code":1001, "msg":"files 或 resumes 至少提供一个"}
  │   ② resumes 非合法 JSON      → 400  {"code":1001, "msg":"resumes 不是合法的 JSON 字符串"}
  │   ③ resumes 中有非 type=url  → 400  {"code":1001, "msg":"resumes 仅支持 type=url"}
  │   ④ total_count > 20         → 400  {"code":1003, "msg":"合并后共N份，单次最多20份"}
  │
  ├─ [处理 files 字段（二进制上传）]
  │   for upload in (files or []):
  │     filename = upload.filename or "resume.pdf"
  │     suffix   = Path(filename).suffix.lower()
  │     ① suffix 不支持或非 .pdf → tasks.append({"code":1001, "msg":"格式不支持"})
  │     file_bytes = await upload.read()             ← 读入内存
  │     ② len(file_bytes) > MAX_FILE_SIZE_MB*1024*1024
  │            → tasks.append({"code":1002, "msg":"文件超过NMB"})
  │     task_id = str(uuid.uuid4())
  │     async with _task_lock:
  │       _task_store[task_id] = TaskRecord(task_id=task_id)
  │     asyncio.create_task(
  │       tasks.py  _run_parse_and_tag(task_id, file_bytes, filename, job_info)
  │     )                                            ← source = bytes（不落盘）
  │     tasks.append({"code":0, "taskId":task_id, ...})
  │
  └─ [处理 resumes 字段（URL JSON 字符串）]
      resume_items = json.loads(resumes)
      for item in resume_items:
        filename = item["file"].get("name", "resume.pdf")
        extra    = item["file"].get("extra") or item.get("extra")
        source, fn = await loop.run_in_executor(
          executor,
          file_utils.py  get_pdf_source_from_content([item])
            └─ file_utils.py  download_file(url, filename)  ← 下载并落盘 uploads/
        )
        task_id = str(uuid.uuid4())
        rec = TaskRecord(task_id=task_id, extra=extra)
        async with _task_lock:
          _task_store[task_id] = rec
        asyncio.create_task(
          tasks.py  _run_parse_and_tag(task_id, source, fn or filename, job_info)
        )
        tasks.append({"code":0, "taskId":task_id, "extra":extra, ...})
```

**两种来源的 source 类型差异**

| 来源 | source 类型 | 说明 |
|------|------------|------|
| `files` 字段（二进制上传） | `bytes` | 直接传内存字节流，不写磁盘 |
| `resumes` 字段（type=url） | `str`（本地路径） | 下载后落盘到 `uploads/`，传路径字符串 |

> `_run_parse_and_tag` → `_parse_with_logging` → `run(source)` → `pdf_to_images(source)` → `_open_pdf(source)`：`fitz.open` 同时接受 `bytes`（`stream=source`）和路径字符串，两种来源均兼容。

#### 5.5.5 内部原子接口链路

```
POST /algorithm/positive-tag/extract
  api/routes_algorithm.py  algorithm_positive_tag(request)
    tag/tagger.py  extract_positive_tags_llm(resume, job_info, _llm_call)
    → 返回 {positiveTags, confidence}

POST /algorithm/sensitive-tag/detect
  api/routes_algorithm.py  algorithm_sensitive_tag(request)
    tag/tagger.py  detect_sensitive_tags(resume, job_info)
    → 返回 {hitTags, hitCount, deductionScore}
```

#### 5.5.6 标签配置 CRUD 链路

```
# ── 辅助函数（api/routes_tags.py）─────────────────────────────
_cfg_load()                           ← 优先读 schemas/tag_config_runtime.json
  ├─ 文件存在 → json.load()
  └─ 文件不存在 → importlib.reload(tag_config)
                    copy.deepcopy(POSITIVE/NEGATIVE_DIMENSIONS, SEVERITY_DEDUCTION)

_cfg_save(cfg)                        ← 原子写入（tmp → rename）
  tmp = tag_config_runtime.tmp
  json.dump(cfg) → tmp.replace(schemas/tag_config_runtime.json)

# ── 查询 ────────────────────────────────────────────────────
GET /api/tags/config
  api/routes_tags.py  get_tags_config()
    async with _cfg_lock:              ← tasks.py 中的 asyncio.Lock
      _cfg_load()                     ← 读 JSON 或 .py 默认值
    → _cfg_response(cfg)              ← {"code":0,"msg":"success","data":{...,"updatedAt":...}}

# ── 鉴权（写操作） ────────────────────────────────────────────
所有写接口依赖 _require_admin(authorization: Header)
  → TAGS_ADMIN_TOKEN 非空时：校验 Authorization: Bearer <token>
  → TAGS_ADMIN_TOKEN 为空时：跳过鉴权（适合内网部署）

# ── 正向维度写操作 ────────────────────────────────────────────
POST   /api/tags/config/positive
PUT    /api/tags/config/positive/{dimension}
DELETE /api/tags/config/positive/{dimension}
  async with _cfg_lock:
    cfg = _cfg_load()
    [增/改/删 cfg["positiveDimensions"] 中的对应项]
    _cfg_save(cfg)
  → _cfg_response(cfg)

# ── 负向维度写操作 ────────────────────────────────────────────
PUT    /api/tags/config/negative/{dimension}
POST   /api/tags/config/negative/{dimension}/rules
PUT    /api/tags/config/negative/{dimension}/rules/{ruleKey}
DELETE /api/tags/config/negative/{dimension}/rules/{ruleKey}
  async with _cfg_lock:
    cfg = _cfg_load()
    [增/改/删 cfg["negativeDimensions"][dim]["rules"][ruleKey]]
    _cfg_save(cfg)
  → _cfg_response(cfg)

# ── 扣分权重 & 重置 ──────────────────────────────────────────
PUT  /api/tags/config/severity
  async with _cfg_lock:
    cfg["severityDeduction"].update(body)
    _cfg_save(cfg)

POST /api/tags/config/reset
  async with _cfg_lock:
    tag_config_runtime.json.unlink()  ← 删除运行时覆盖文件
    cfg = _cfg_load()                 ← 重新从 tag_config.py 读默认值
  → _cfg_response(cfg)

# ── 并发安全 ─────────────────────────────────────────────────
_cfg_lock = asyncio.Lock()            ← 定义在 tasks.py，所有读写操作共享

# ── 热更新生效路径 ────────────────────────────────────────────
写入 schemas/tag_config_runtime.json 后：
  _tagger_load_cfg()（tag/tagger.py）   ← mtime 缓存：下次打标时自动重新加载
  _conform_to_template()（postprocess.py）← 每次调用时动态构建 displayName 映射

# ── 注意事项 ─────────────────────────────────────────────────
schemas/tag_config_runtime.json 容器内路径 /app/schemas/tag_config_runtime.json
  → 若需跨容器重启持久化，需将 /app/schemas 目录挂载为 volume
tag_data.py（院校/大厂名单）修改后需重启服务才能生效（不在此链路内）
```

#### 5.5.7 异步基础设施

```
app.py lifespan()
  parser.py  _cleanup_uploads()           ← 清理 uploads/ 目录过期文件（>15天）
  asyncio.create_task(
    tasks.py  _periodic_cleanup()         ← 每60s清理过期 TaskRecord（TTL 10min）
  )

tasks.py  _set_task_field(task_id, **kwargs)
  async with _task_lock:                  ← asyncio.Lock 协程安全
    setattr(rec, k, v)

tasks.py  _cleanup_task_store()
  cutoff = time.time() - TASK_TTL_MINUTES * 60
  [删除 created_at < cutoff 的记录]

parser.py  _llm_call(messages)
  client.chat.completions.create(
    model=MODEL_NAME,
    temperature=TEMPERATURE,             ← 0.01（低温保稳定性）
    max_tokens=MAX_TOKENS,               ← 4096
    extra_body=_DISABLE_THINKING,        ← 禁用 thinking block
  )
  [异常处理] "thinking block" / "Invalid signature"
    → 去掉 extra_body 重试一次
```

---

## 6. API 接口设计

### 6.1 健康与模型检查

#### `GET /health`

```json
{"status": "healthy", "timestamp": "2026-05-25T10:30:00"}
```

#### `GET /v1/models`（OpenAI 兼容）

返回支持的模型列表，供渠道平台健康检测：

| model id | 说明 |
|---------|------|
| `DeepSeek-OCR` | 仅解析，返回结构化简历 JSON |
| `DeepSeek-OCR-JSON` | 解析 + 打标，返回 v3.1 完整 JSON（同步或异步模式） |
| `AIHR-Resume` | 同 DeepSeek-OCR-JSON，新名称 |

---

### 6.2 异步单文件接口

#### `POST /api/resume/async/parse`

上传简历，触发异步解析+打标，立即返回 taskId。

**请求（multipart/form-data）**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | file | 是 | 简历文件（PDF，≤20MB） |
| `requestId` | string | 否 | 幂等标识，相同 requestId 直接返回已有 taskId |
| `jobTitle` | string | 否 | 目标岗位名称 |
| `requiredSkills` | string | 否 | 必要技能，逗号分隔 |
| `jobDescription` | string | 否 | JD 全文描述 |
| `jobSensitiveWords` | string | 否 | 自定义敏感词，逗号/分号分隔 |

**成功响应**

```json
{"code": 0, "msg": "success", "data": {"taskId": "uuid", "status": 0, "estimatedSeconds": 90}}
```

**错误码**

| code | 原因 |
|------|------|
| 1001 | 文件格式不支持（当前仅支持 PDF） |
| 1002 | 文件超过 20MB |

---

#### `GET /api/resume/async/result`

查询任务状态，完成后返回完整 v3.1 JSON。

**请求参数**：`?taskId=<uuid>`

**轮询建议**

| 已等待时间 | 轮询间隔 |
|-----------|---------|
| 0~15s | 3s |
| 15~60s | 5s |
| > 60s | 10s |

**响应示例（完成）**

```json
{
  "code": 0, "msg": "success",
  "data": {
    "taskId": "uuid",
    "status": 3,
    "statusDesc": "任务完成",
    "elapsedSeconds": 65.3,
    "result": { "...": "见第7节完整 JSON" }
  }
}
```

**响应示例（失败）**

```json
{
  "code": 0, "msg": "success",
  "data": {"taskId": "uuid", "status": 4, "statusDesc": "任务失败", "errorMsg": "PDF 已加密，需要密码才能解析"}
}
```

**错误码**：`2001` - taskId 不存在或已过期（TTL 10min）

**关于 taskId 字段的设计说明**

`status=3` 时响应中存在两处 `taskId`，值相同但语义层不同：

```
data.taskId          ← 轮询协议标识符（app.py:1427，来自 query param）
data.result.taskId   ← 结果记录的自描述字段（_conform_to_template 写入）
```

| 字段 | 始终存在 | 脱包存储有意义 | 职责 |
|------|---------|--------------|------|
| `data.taskId` | 是（全部状态 0~4） | 否 | 轮询协议锚点，status=0/1/2 时 `result` 字段不存在，调用方只能从此处读 taskId |
| `data.result.taskId` | 否（仅 status=3） | 是 | `result` 会被写入 `results/*.json` 磁盘文件、调用方 `all_tag_json` 快照字段，脱离 HTTP 上下文后需自描述来源 |

两者不可删除：`data.taskId` 保证轮询格式一致；`data.result.taskId` 保证结果自描述。在单次 status=3 响应里值冗余是正常的设计取舍。

---

### 6.3 批量异步接口

#### `POST /api/resume/async/batch`

批量提交（JSON body，仅支持 `type=url`），立即返回每份 taskId，上限 20 份。

**请求体**

```json
{
  "resumes": [
    {"type": "url", "file": {"name": "a.pdf", "path": "http://minio/resumes/a.pdf", "extra": "userId_123"}},
    {"type": "url", "file": {"name": "b.pdf", "path": "http://minio/resumes/b.pdf"}}
  ],
  "jobTitle": "算法工程师",
  "requiredSkills": "Python,PyTorch,LLM",
  "jobDescription": "负责大模型算法研发...",
  "jobSensitiveWords": "四川,应届"
}
```

**响应**

```json
{
  "code": 0, "msg": "success",
  "total": 2, "submitted": 2, "failed": 0,
  "estimatedSeconds": 90,
  "tasks": [
    {"filename": "a.pdf", "extra": "userId_123", "code": 0, "taskId": "uuid-a", "status": 0},
    {"filename": "b.pdf", "extra": null,          "code": 0, "taskId": "uuid-b", "status": 0}
  ]
}
```

> `extra` 字段原样透传，调用方可存入自己的业务 ID，方便结果关联

---

#### `POST /api/resume/async/batch/multiform`

批量异步，支持二进制上传（`files` 字段）与 URL（`resumes` 字段 JSON 字符串）**同时混用**，上限 20 份。

**请求（multipart/form-data）**

| 参数 | 类型 | 说明 |
|------|------|------|
| `files` | File（可多个） | 直接上传 PDF 文件 |
| `resumes` | string | JSON 数组字符串，`type=url` 格式（同上方 batch 接口） |
| `jobTitle` | string | 岗位名称 |
| `requiredSkills` | string | 必要技能 |
| `jobDescription` | string | JD 描述 |
| `jobSensitiveWords` | string | 敏感词 |

响应格式与 `/api/resume/async/batch` 相同。

---

### 6.4 任务查询接口

#### `GET /api/tasks`

查询内存中全部任务状态摘要，支持按状态过滤，结果按创建时间倒序，最多返回 200 条。

```
GET /api/tasks           ← 返回全部任务
GET /api/tasks?status=2  ← 只返回"打标中"任务
GET /api/tasks?status=3  ← 只返回"已完成"任务
```

**查询参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `status` | int | 按状态过滤：0/1/2/3/4，不传返回全部 |
| `include_result` | bool | `true` 时 status=3 的任务同时返回完整 result JSON，默认 false |

**响应**

```json
{
  "code": 0, "msg": "success",
  "data": {
    "total": 3,
    "tasks": [
      {
        "taskId": "uuid-a", "status": 3, "statusDesc": "任务完成",
        "elapsedSeconds": 68.2, "createdAt": "2026-05-25 10:30:00",
        "extra": "userId_123",
        "candidateName": "张三", "resumeParseTime": "2026-05-25 10:30:45",
        "tagParseTime": "2026-05-25 10:31:08", "completedAt": "2026-05-25 10:31:08"
      },
      {
        "taskId": "uuid-b", "status": 1, "statusDesc": "简历解析中",
        "elapsedSeconds": 12.5, "createdAt": "2026-05-25 10:30:50",
        "extra": null
      }
    ]
  }
}
```

---

### 6.5 标签配置接口（CRUD）

标签配置支持完整的增删改查，写操作持久化到 `tag_config_runtime.json`，服务重启后仍然生效；`POST /api/tags/config/reset` 可一键回滚到 `tag_config.py` 默认值。

#### 接口总览

| 方法 | 路径 | 功能 |
|------|------|------|
| `GET` | `/api/tags/config` | 查询全部配置 |
| `POST` | `/api/tags/config/positive` | 新增正向维度 |
| `PUT` | `/api/tags/config/positive/{dimension}` | 更新正向维度（displayName / enabled / allowedValues） |
| `DELETE` | `/api/tags/config/positive/{dimension}` | 删除正向维度 |
| `PUT` | `/api/tags/config/negative/{dimension}` | 更新负向维度（displayName / enabled） |
| `POST` | `/api/tags/config/negative/{dimension}/rules` | 向负向维度新增规则 |
| `PUT` | `/api/tags/config/negative/{dimension}/rules/{ruleKey}` | 更新负向规则参数 |
| `DELETE` | `/api/tags/config/negative/{dimension}/rules/{ruleKey}` | 删除负向规则 |
| `PUT` | `/api/tags/config/severity` | 更新扣分权重 |
| `POST` | `/api/tags/config/reset` | 重置为默认配置 |

所有接口统一返回格式：

```json
{"code": 0, "msg": "success", "data": { "positiveDimensions": [...], "negativeDimensions": [...], "severityDeduction": {...}, "updatedAt": "..." }}
```

---

#### `GET /api/tags/config` — 查询全部配置

```json
{
  "code": 0, "msg": "success",
  "data": {
    "positiveDimensions": [
      {
        "dimension": "education", "displayName": "学历背景", "enabled": true,
        "allowedValues": ["C9","985","211","双一流","海外名校(QS50)","海外名校(QS100)","海外硕士","博士","硕士","本科","专科及以下"]
      }
    ],
    "negativeDimensions": [
      {
        "dimension": "jobstable", "displayName": "职业稳定性风险", "enabled": true,
        "rules": {
          "frequent_job_change": {"tagName":"频繁跳槽","enabled":true,"window_years":3,"threshold":4,"severity":"medium"},
          "short_tenure":        {"tagName":"最近任职时长偏短","enabled":true,"threshold_months":3,"severity":"medium"},
          "gap_period":          {"tagName":"工作空窗期过长","enabled":true,"threshold_months":6,"severity":"low"}
        }
      }
    ],
    "severityDeduction": {"low": -5, "medium": -10, "high": -20},
    "updatedAt": "2026-05-25 14:00:00"
  }
}
```

---

#### `POST /api/tags/config/positive` — 新增正向维度

```json
// 请求 Body
{"dimension": "language", "displayName": "语言能力", "enabled": true, "allowedValues": ["英语CET6","英语流利","日语N2"]}
```

错误码：`1001` dimension 为空；`1002` dimension 已存在。

---

#### `PUT /api/tags/config/positive/{dimension}` — 更新正向维度

```json
// 请求 Body（字段均可选，只传需要修改的）
{"enabled": false, "allowedValues": ["应届","1-3年","3-5年","5-10年","10年+"]}
```

错误码：`1004` dimension 不存在。

---

#### `DELETE /api/tags/config/positive/{dimension}` — 删除正向维度

无 Body。错误码：`1004` dimension 不存在。

---

#### `PUT /api/tags/config/negative/{dimension}` — 更新负向维度

```json
// 请求 Body（字段均可选）
{"enabled": false, "displayName": "诚信风险"}
```

---

#### `POST /api/tags/config/negative/{dimension}/rules` — 新增规则

```json
// 请求 Body（ruleKey 必填，其他字段自定义）
{
  "ruleKey": "short_career",
  "tagName": "职业经历过短",
  "enabled": true,
  "severity": "low",
  "threshold_months": 6
}
```

错误码：`1001` ruleKey 为空；`1002` ruleKey 已存在；`1004` dimension 不存在。

---

#### `PUT /api/tags/config/negative/{dimension}/rules/{ruleKey}` — 更新规则参数

```json
// 请求 Body（字段均可选，只传需要修改的）
{"threshold": 3, "severity": "high", "enabled": true}
```

---

#### `DELETE /api/tags/config/negative/{dimension}/rules/{ruleKey}` — 删除规则

无 Body。错误码：`1004` dimension 或 ruleKey 不存在。

---

#### `PUT /api/tags/config/severity` — 更新扣分权重

```json
// 请求 Body（字段均可选，仅支持 low / medium / high）
{"low": -3, "medium": -8, "high": -15}
```

---

#### `POST /api/tags/config/reset` — 重置为默认

无 Body。删除 `tag_config_runtime.json`，回滚到 `tag_config.py` 中的初始值。

---

### 6.6 OpenAI 兼容接口

#### `POST /v1/chat/completions`

供 OneAPI / NewAPI 等渠道平台直接对接，兼容 OpenAI 请求格式。

**model 字段说明**

| model | 行为 |
|-------|------|
| `DeepSeek-OCR` | 仅解析，content 返回结构化简历 JSON 字符串 |
| `DeepSeek-OCR-JSON` | 解析 + 打标；`async_mode=false`（默认）同步等待；`async_mode=true` 立即返回 taskId |
| `AIHR-Resume` | 同 DeepSeek-OCR-JSON |

**content 传入格式（user message）**

```json
[
  {
    "type": "url",
    "file": {"name": "resume.pdf", "path": "http://minio/resumes/a.pdf"}
  }
]
```

或

```json
[
  {
    "type": "base64",
    "file": {"name": "resume.pdf", "content": "<base64_string>"}
  }
]
```

---

### 6.7 内部原子能力接口（内部使用）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/algorithm/positive-tag/extract` | 从已结构化简历提取正向标签 |
| `POST` | `/algorithm/sensitive-tag/detect` | 负面标签/风险检测 |

> 注：`/algorithm/resume/parse`（同步解析）已移除，全量解析请通过 `/api/resume/async/parse` 提交异步任务。

---

### 6.8 同步批量接口

#### `POST /api/resume/sync/batch`

同步批量**解析 + 打标**，上限 50 份，阻塞等待全部完成后一次性返回完整结果。
适合离线批处理、脚本批量导入等场景；在线高并发场景推荐使用 `/api/resume/async/batch`。

**请求体（Content-Type: application/json）**

```json
{
  "resumes": [
    {"type": "url",    "file": {"name": "a.pdf", "path": "http://minio/resumes/a.pdf", "extra": "userId_001"}},
    {"type": "file",   "file": {"name": "b.pdf", "path": "/app/uploads/b.pdf"}},
    {"type": "base64", "file": {"name": "c.pdf", "content": "<base64_string>"}}
  ],
  "jobTitle": "算法工程师",
  "requiredSkills": "Python,机器学习",
  "jobDescription": "...",
  "jobSensitiveWords": "竞业,保密协议",
  "max_concurrency": 4
}
```

与异步 batch 接口的主要差异：

| 项目 | `/api/resume/async/batch` | `/api/resume/sync/batch` |
|------|--------------------------|--------------------------|
| 文件来源 | 仅 `type=url` | `url` / `file` / `base64` 均支持 |
| 上限 | 20 份 | 50 份 |
| 返回时机 | 立即返回 taskId | 阻塞直至全部完成 |
| 结果获取 | 需轮询 `/api/resume/async/result` | 响应体直接包含完整结果 |

**响应格式**

外层统计字段 + `results` 数组，每条 `results[i]` 与 `GET /api/resume/async/result`（status=3）的完整响应**格式完全一致**：

```json
{
  "total": 2,
  "success": 2,
  "failed": 0,
  "elapsed_s": 42.5,
  "results": [
    {
      "code": 0,
      "msg": "success",
      "data": {
        "taskId": "uuid-a",
        "status": 3,
        "statusDesc": "任务完成",
        "elapsedSeconds": 41.2,
        "estimatedSeconds": 90,
        "extra": "userId_001",
        "result": { "...": "完整 v3.1 JSON，见第7节" }
      }
    }
  ]
}
```

**并发机制**：`asyncio.gather` 并发执行所有任务，batch 级 `Semaphore(max_concurrency)` 与全局 `_parse_semaphore(MAX_WORKERS)` 双层限流；任务记录写入 `_task_store`，可通过 `/api/resume/async/result?taskId=` 单独补查。

---

## 7. 统一输出 JSON 规范（v3.1）

### 7.1 顶层字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `taskId` | string | 任务唯一标识（UUID） |
| `resumeParseFinishTime` | string | 简历解析阶段完成时间（`YYYY-MM-DD HH:mm:ss`） |
| `tagParseFinishTime` | string | 标签打标阶段完成时间 |
| `candidate` | object | 候选人基础信息 |
| `educations` | array | 教育经历列表 |
| `works` | array | 工作经历列表 |
| `projects` | array | 项目经历列表 |
| `positiveTags` | object | 正向标签（`tagsArray` 按维度顺序排列） |
| `negativeTags` | object | 负面标签（`hitCount` + `deductionScore` + `tagsArray`） |

> v3.1 变化：`tagInfo` 已拆分为顶层的 `positiveTags` 和 `negativeTags`，`tagParseTime` 改名为 `tagParseFinishTime`

### 7.2 candidate 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 姓名 |
| `phone` | string | 手机号（格式 1xxxxxxxxxx） |
| `email` | string | 邮箱 |
| `gender` | int | 1=男，2=女，0=未知 |
| `age` | int | 年龄，0=未知 |
| `address` | string | 居住地，如"广东省深圳市南山区" |
| `nativePlace` | string | 籍贯 |
| `selfEvaluation` | string | 自我评价 |
| `expectedSalary` | string | 期望薪资，如"25-35K" |
| `expectedPosition` | string | 应聘职位 |

### 7.3 educations / works / projects 字段

**educations[]**

| 字段 | 说明 |
|------|------|
| `school` | 学校名称（保留原文） |
| `major` | 专业 |
| `degree` | 学位：学士 / 硕士 / 博士 |
| `education` | 学历：本科 / 硕士研究生 / 博士研究生 / 专科 等 |
| `startDate` | 开始时间，`YYYY-MM` |
| `endDate` | 结束时间，`YYYY-MM` 或"至今" |

**works[]**

| 字段 | 说明 |
|------|------|
| `company` | 公司全称（保留原文，不缩写） |
| `position` | 职位名称（按提取规则推断） |
| `description` | 该阶段核心职责总结（≤300字） |
| `startDate` | 开始时间 |
| `endDate` | 结束时间 |

**projects[]**

| 字段 | 说明 |
|------|------|
| `company` | 所属公司（保留原文） |
| `projectName` | 项目完整名称 |
| `role` | 项目角色/职位 |
| `description` | 项目描述（优先使用原文） |
| `startDate` | 开始时间 |
| `endDate` | 结束时间 |

### 7.4 完整输出示例

```json
{
  "taskId": "e39d009b-abd7-42f4-8e64-46b7e26e4b20",
  "resumeParseFinishTime": "2026-05-25 10:30:45",
  "tagParseFinishTime": "2026-05-25 10:31:08",
  "candidate": {
    "name": "张三",
    "phone": "13800138000",
    "email": "zhangsan@example.com",
    "gender": 1,
    "age": 28,
    "address": "广东省深圳市南山区",
    "nativePlace": "湖南长沙",
    "selfEvaluation": "具备多年后端开发经验，擅长微服务架构，学习能力强",
    "expectedSalary": "25-35K",
    "expectedPosition": "后端高级开发工程师"
  },
  "educations": [
    {
      "school": "上海交通大学",
      "major": "计算机科学与技术",
      "degree": "硕士",
      "education": "硕士研究生",
      "startDate": "2017-09",
      "endDate": "2020-07"
    }
  ],
  "works": [
    {
      "company": "字节跳动",
      "position": "高级后端工程师",
      "description": "负责推荐系统后端服务开发，主导微服务拆分与性能优化，QPS提升3倍",
      "startDate": "2022-03",
      "endDate": "至今"
    }
  ],
  "projects": [
    {
      "company": "字节跳动",
      "projectName": "实时推荐引擎重构",
      "role": "技术负责人",
      "description": "主导推荐引擎从单体架构向微服务架构迁移，引入Kafka实现异步解耦，吞吐量提升200%",
      "startDate": "2023-01",
      "endDate": "2023-09"
    }
  ],
  "positiveTags": {
    "tagsArray": [
      {
        "dimension": "education",
        "displayName": "学历背景",
        "value": [
          {"tag": "985",   "source": "ai"},
          {"tag": "211",   "source": "ai"},
          {"tag": "双一流","source": "ai"},
          {"tag": "硕士",  "source": "rule"}
        ]
      },
      {
        "dimension": "companyBg",
        "displayName": "公司背景",
        "confidence": 0.95,
        "value": [
          {"tag": "头部大厂", "source": "ai"}
        ]
      },
      {
        "dimension": "workYears",
        "displayName": "工作年限",
        "value": [
          {"tag": "3-5年", "source": "ai"}
        ]
      },
      {
        "dimension": "skills",
        "displayName": "技能标签",
        "confidence": 0.92,
        "value": [
          {"tag": "Java",         "source": "ai"},
          {"tag": "Spring Boot",  "source": "ai"},
          {"tag": "MySQL",        "source": "ai"},
          {"tag": "Redis",        "source": "ai"},
          {"tag": "Kafka",        "source": "ai"}
        ]
      },
      {
        "dimension": "industry",
        "displayName": "行业背景",
        "confidence": 0.92,
        "value": [
          {"tag": "互联网/电商", "source": "ai"},
          {"tag": "人工智能",   "source": "ai"}
        ]
      }
    ]
  },
  "negativeTags": {
    "hitCount": 0,
    "deductionScore": 0,
    "tagsArray": []
  }
}
```

---

## 8. Prompt 设计

### 8.1 简历解析 Prompt（OCR 阶段）

核心规则：
- 严格保留公司/学校/项目原文，不缩写不改写
- 家庭成员信息（配偶/父母/子女）一律不提取
- `works[].position` 无明确职位时按描述关键词推断（开发/算法/架构/测试/产品/数据/运营等映射规则）
- 实习职位后缀"（实习）"
- `works[].description`：汇总该阶段所有项目职责，≤300字

### 8.2 正向打标 Prompt

```
你是专业的 HR 招聘评估助手。根据候选人简历，提取结构化正向标签。

## 输入
### 候选人简历（已结构化）
{resume_json}

### 目标岗位参考
- 职位：{job_title}
- 必要技能：{required_skills}
- 岗位描述：{job_description}

## 输出要求
输出以下 JSON，禁止添加说明文字或 markdown 标记。
所有维度均使用数组字段，命名规则：{dimension}ValueArray。

{
  "positiveTags": [
    {
      "dimension": "education",
      "educationValueArray": ["<C9|985|211|双一流|海外名校(QS50)|海外名校(QS100)|海外硕士|博士|硕士|本科|专科及以下>"],
      "source": "ai"
    },
    {
      "dimension": "companyBg",
      "companyBgValueArray": ["<头部大厂|腰部中厂|外企500强|一线外企|独角兽|明星创业|国企/央企|传统企业转型>"],
      "source": "ai",
      "confidence": <0.0-1.0>
    },
    {
      "dimension": "workYears",
      "workYearsValueArray": ["<应届|2-3年|3-5年|5-10年|10-15年|15年+>"],
      "source": "ai"
    },
    {
      "dimension": "skills",
      "skillsValueArray": ["技能1", "技能2", "..."],
      "source": "ai",
      "confidence": <0.0-1.0>
    },
    {
      "dimension": "industry",
      "industryValueArray": ["行业1", "行业2"],
      "source": "ai",
      "confidence": <0.0-1.0>
    }
  ],
  "confidence": <所有positiveTags中confidence字段的均值，保留2位小数>
}

## 各维度打标规则

**education**（educationValueArray，并行输出所有适用等级）
- 层级关系：C9 ⊂ 985 ⊂ 211 ⊂ 双一流
  - 命中 C9 → ["C9","985","211","双一流"]
  - 命中 985（非C9）→ ["985","211","双一流"]
  - 命中 211（非985）→ ["211","双一流"]
- 海外：QS前50 → ["海外名校(QS50)","海外名校(QS100)"]；QS51-100 → ["海外名校(QS100)"]
- 未命中任何院校时按最高学历补充：博士研究生→"博士"，硕士研究生→"硕士"，本科→"本科"
- 遍历所有 educations[].school，合并去重

**companyBg**（companyBgValueArray，从 works[] 汇总所有命中）
- 头部大厂：阿里/腾讯/字节/百度/华为/美团/京东/拼多多/网易/快手/小红书
- confidence 取所有匹配项最高值

**workYears**（workYearsValueArray，单元素数组）
- 0~1年→应届 / 1~3年→2-3年 / 3~5年→3-5年 / 5~10年→5-10年 / 10~15年→10-15年 / 15年+→15年+

**skills**（skillsValueArray，数组）
- 合并 works[].description + projects[].description 提取核心技术栈
- 去重、归一化（"python"→"Python"），按职能大类归集

**industry**（industryValueArray，最多2个）
- 从公司业务和项目描述推断，confidence 取命中行业最高值
```

---

## 9. 数据模型

### 9.1 Pydantic 响应模型（简化）

```python
from pydantic import BaseModel
from typing import Optional

class TagValue(BaseModel):
    tag:    str
    source: str = "ai"

class NegTagValue(BaseModel):
    tag:            str
    severity:       str
    matchedContent: str
    source:         str = "ai"

class PositiveTagEntry(BaseModel):
    dimension:   str
    displayName: str
    confidence:  Optional[float] = None  # education/workYears 不含
    value:       list[TagValue]

class NegTagEntry(BaseModel):
    dimension:   str
    displayName: str
    value:       list[NegTagValue]

class PositiveTags(BaseModel):
    tagsArray: list[PositiveTagEntry]

class NegativeTags(BaseModel):
    hitCount:      int
    deductionScore: int
    tagsArray:     list[NegTagEntry]

class ResumeResult(BaseModel):
    taskId:                 str
    resumeParseFinishTime:  str
    tagParseFinishTime:     str
    candidate:              dict
    educations:             list[dict]
    works:                  list[dict]
    projects:               list[dict]
    positiveTags:           PositiveTags
    negativeTags:           NegativeTags
```

---

## 10. 标签配置热更新

### 10.1 配置文件层级

| 文件 | 职责 | 更新方式 |
|------|------|---------|
| `schemas/tag_config_runtime.json` | **运行时配置（优先）**：所有 CRUD 写操作的持久化目标 | `PUT/POST/DELETE /api/tags/config/*` 自动写入 |
| `tag/tag_config.py` | **默认配置（兜底）**：`runtime.json` 不存在时加载；`reset` 后回滚至此 | 手动编辑 + `POST /api/tags/config/reset` 重新生效 |
| `tag/tag_data.py` | 院校名单 Set、大厂名单 Set、行业词典 dict | 服务重启 |
| `schemas/tag_static_data.json` | `tag_data.py` 的人工可读版本，修改后需同步 `tag_data.py` | 手动维护 |

**优先级**：`tag_config_runtime.json` > `tag_config.py` > 服务默认值

**热更新生效机制**：
- `_tagger_load_cfg()`（`tag/tagger.py`）：mtime 缓存，仅当 `runtime.json` 文件变更时重新读取，避免每次打标重复 IO
- `_conform_to_template()`（`postprocess.py`）：每次调用时动态调用 `load_tag_cfg()`，立即感知最新配置；`displayName` 查找表（`_build_display_maps`）无需重启即可生效

### 10.2 tag_static_data.json 结构

```
tag_static_data.json
├── schools
│   ├── c9          ← C9 联盟 9所
│   ├── 985         ← 985 名单 39所
│   ├── 211         ← 211 名单 112所
│   ├── qsTop50     ← QS 前50（50+所，含中英文双名）
│   └── qsTop100    ← QS 51-100
├── educationHierarchy  ← 层级展开映射表
├── companies
│   ├── topTier          ← 头部大厂
│   ├── midTier          ← 腰部中厂
│   ├── fortune500       ← 外企500强
│   ├── firstTierForeign ← 一线外企
│   ├── unicorn          ← 独角兽
│   └── stateOwnedKeywords ← 国企/央企关键词
├── companyTierLabels   ← 层级标签中文名映射
├── industryKeywords    ← 行业关键词词典（10个行业）
├── negativeDimensions  ← 负面规则参数（4个维度）
└── severityDeduction   ← 扣分权重
```

### 10.3 修改工作流

**场景 A：通过 API 修改规则参数（推荐，无需重启）**

```
# 调整跳槽阈值：近3年≥3段即触发（原为4段）
PUT /api/tags/config/negative/jobstable/rules/frequent_job_change
Body: {"threshold": 3}

# 禁用某个负向维度
PUT /api/tags/config/negative/integrity
Body: {"enabled": false}

# 扩充 workYears 枚举值
PUT /api/tags/config/positive/workYears
Body: {"allowedValues": ["应届","1-3年","3-5年","5-10年","10年+"]}

# 重置所有修改
POST /api/tags/config/reset
```

**场景 B：修改院校/大厂静态名单（需重启）**

1. 编辑 `tag_static_data.json`（如新增学校到 qsTop50）
2. 同步更新 `tag_data.py` 对应的 Python Set/dict
3. 重启服务生效（院校/大厂数据在启动时加载，无热更新路径）

**场景 C：修改默认配置（修改 tag_config.py）**

1. 编辑 `tag_config.py`
2. 若容器内存在 `tag_config_runtime.json`，需先调用 `POST /api/tags/config/reset` 清除覆盖文件，否则 `.py` 修改不会生效（JSON 优先级更高）

---

## 11. 第三方对接指南

### 11.1 标准对接流程

```
调用方                                    AIHR 服务
──────────────────────────────────────────────────────
① POST /api/resume/async/parse           接收文件 → 生成 taskId → 入队（status=0）
   ↓ 立即得到 taskId（< 100ms）
② 本地记录 taskId，开始轮询
③ GET /api/resume/async/result           查询状态
   status=1 → 继续等待（3s 后重试）
   status=2 → 继续等待（5s 后重试）
   status=3 → 获取完整 v3.1 JSON ←────── 完成
   status=4 → 处理 errorMsg，按需重提
④ 拆解 JSON，自行持久化：
   candidate/educations/works/projects   → 简历主表 + 明细表
   positiveTags（整体 JSON 快照）         → resume.all_tag_json
   positiveTags.tagsArray                → 正向标签关联表
   negativeTags.tagsArray                → 负面标签关联表
```

### 11.2 注意事项

| 事项 | 说明 |
|------|------|
| 缓存 TTL | taskId 10分钟后过期，务必在有效期内轮询获取结果 |
| 重复提交 | 传入 `requestId` 实现幂等，相同 requestId 不会重复处理 |
| 超时重提 | 超时或 status=4 时，重新调用提交接口即可 |
| 批量建议 | 单次批量不超过 20 份；大批量分批提交，间隔 1~2s |
| 服务重启 | 内存缓存会清空，进行中的任务需要重新提交 |

### 11.3 存储建议（三张表）

**表1：简历主表快照字段**

```sql
ALTER TABLE resume
    ADD COLUMN all_tag_json    TEXT    COMMENT 'positiveTags + negativeTags 完整 JSON 快照',
    ADD COLUMN tag_parse_time  DATETIME COMMENT '打标完成时间';
```

**表2：标签字典表 `tag_dict`**

```sql
CREATE TABLE tag_dict (
    id          INT PRIMARY KEY AUTO_INCREMENT,
    tag_type    VARCHAR(20) NOT NULL COMMENT 'positive | negative',
    dimension   VARCHAR(50) COMMENT 'education|companyBg|workYears|skills|industry|jobstable|integrity|matchRisk|location',
    tag_value   VARCHAR(100) NOT NULL,
    severity    VARCHAR(10)  COMMENT 'low|medium|high，负向标签用',
    deduct_score INT DEFAULT 0,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

**表3：简历标签关联表 `resume_tag_rel`**

```sql
CREATE TABLE resume_tag_rel (
    id              BIGINT PRIMARY KEY AUTO_INCREMENT,
    resume_id       VARCHAR(64) NOT NULL,
    job_id          VARCHAR(64) COMMENT '岗位ID',
    tag_type        VARCHAR(20) NOT NULL COMMENT 'positive | negative',
    dimension       VARCHAR(50),
    tag_value       VARCHAR(100) NOT NULL,
    source          VARCHAR(20)  COMMENT 'ai | rule',
    confidence      DECIMAL(4,2),
    severity        VARCHAR(10),
    matched_content TEXT         COMMENT '证据原文（负向标签）',
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_resume_id (resume_id),
    INDEX idx_job_id    (job_id),
    INDEX idx_dimension (dimension)
);
```

### 11.4 Python 对接示例

```python
import time, requests

HOST = "http://localhost:21804"  # 请替换为实际部署地址
STATUS_DESC = {0: "待处理", 1: "简历解析中", 2: "标签打标中", 3: "任务完成", 4: "任务失败"}


def submit_resume(file_path: str, job_title: str = "",
                  required_skills: str = "", job_sensitive_words: str = "") -> str:
    with open(file_path, "rb") as f:
        resp = requests.post(
            f"{HOST}/api/resume/async/parse",
            files={"file": (file_path.split("/")[-1], f)},
            data={"jobTitle": job_title, "requiredSkills": required_skills,
                  "jobSensitiveWords": job_sensitive_words},
            timeout=15
        )
    resp.raise_for_status()
    return resp.json()["data"]["taskId"]


def poll_result(task_id: str, timeout: int = 180) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        data = requests.get(f"{HOST}/api/resume/async/result",
                            params={"taskId": task_id}, timeout=10).json()["data"]
        status = data["status"]
        print(f"  [{STATUS_DESC[status]}] 已耗时 {data.get('elapsedSeconds', 0):.0f}s")
        if status == 3:
            return data["result"]
        if status == 4:
            raise RuntimeError(f"任务失败: {data.get('errorMsg')}")
        time.sleep(3 if data.get("elapsedSeconds", 0) < 15 else 5)
    raise TimeoutError(f"超时（{timeout}s），请重新提交")


# 使用示例
task_id = submit_resume(
    "/data/resumes/zhangsan.pdf",
    job_title="后端高级开发工程师",
    required_skills="Java,SpringBoot,MySQL",
    job_sensitive_words="四川,应届"
)
print(f"提交成功，taskId: {task_id}")

result = poll_result(task_id)
c = result["candidate"]
print(f"候选人：{c['name']}  {c['expectedPosition']}  {c['expectedSalary']}")

print("\n[正向标签]")
for entry in result["positiveTags"]["tagsArray"]:
    vals = ", ".join(v["tag"] for v in entry["value"])
    conf = f"  置信度={entry['confidence']}" if entry.get("confidence") else ""
    print(f"  [{entry['dimension']}] {vals}{conf}")

print("\n[负面标签]")
neg = result["negativeTags"]
if neg["hitCount"] > 0:
    for group in neg["tagsArray"]:
        for hit in group["value"]:
            print(f"  [{group['displayName']}][{hit['severity']}] {hit['tag']}")
            print(f"    证据：{hit['matchedContent'][:80]}")
    print(f"合计扣分：{neg['deductionScore']}")
else:
    print("  无风险标签命中")
```

---

## 12. 性能与风险

### 12.1 性能预估

| 阶段 | 耗时 | 备注 |
|------|------|------|
| 简历解析（≤8页） | 20~40s | PDF 渲染 + Qwen3.5 多模态单次调用 |
| 简历解析（>8页） | 30~60s | 分批并发（每批 1~2 页），ThreadPoolExecutor 并发合并 |
| 正向打标（LLM） | 5~20s | 纯文本，Qwen3.5 |
| 负面检测（规则） | < 1s | 纯 Python 规则计算 |
| **全流程（异步）** | **45~90s** | 接口 < 100ms 返回，后台流水线完成后写入缓存 |
| 并发能力 | 24 并发线程 | 取决于 `MAX_WORKERS` 环境变量和模型侧吞吐 |

### 12.2 主要风险与缓解

| 风险 | 概率 | 缓解措施 |
|------|------|---------|
| LLM JSON 格式错误 | 中 | `json_repair` 自动修复；修复失败则降级规则层，简历数据正常返回 |
| LLM 超时（> 300s） | 低 | `API_TIMEOUT=300`；`MAX_RETRIES=2` 自动重试；超时后整个任务 status=4 |
| thinking block 错误 | 低 | 自动 fallback：去掉 thinking 参数重试一次 |
| 院校/大厂名单过时 | 中 | `tag_static_data.json` 独立维护，同步更新 `tag_data.py` 即可生效 |
| 内存缓存积压 | 低 | TTL 10min 定时清理；监控 `GET /api/tasks` 任务数量 |
| 服务重启任务丢失 | 低 | 通知调用方重新提交；`results/` 目录保存历史 JSON 可追溯 |
| PDF 加密无法解析 | 低 | `_open_pdf` 提前检测加密，立即返回明确错误信息 |
| MinIO URL 下载失败 | 低 | 校验 PDF 魔数（`%PDF`）；失败返回具体 errorMsg |

### 12.3 环境变量配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MODEL_SERVICE_URL` | —（**必填**） | LLM 服务地址（OpenAI 兼容，无默认值，启动时强制校验） |
| `MODEL_NAME` | `Qwen3.5-397B-A17B` | 模型名称 |
| `MAX_WORKERS` | `24` | 线程池大小 |
| `API_TIMEOUT` | `300` | LLM 调用超时（秒） |
| `MAX_RETRIES` | `2` | LLM 失败重试次数 |
| `TEMPERATURE` | `0.01` | LLM 温度（低温保证输出稳定） |
| `MAX_TOKENS` | `4096` | 最大输出 token 数 |
| `SERVICE_PORT` | `21804` | 服务端口 |
| `LOG_KEEP_DAYS` | `7` | 日志保留天数 |
| `LOG_MAX_MB` | `50` | 单日志文件最大 MB |
| `MAX_FILE_SIZE_MB` | `20` | 上传文件最大 MB |
| `TASK_TTL_MINUTES` | `10` | 任务缓存 TTL（分钟） |
| `TAGS_ADMIN_TOKEN` | `""（不鉴权）` | 打标配置写接口 Bearer Token；为空时跳过鉴权 |

---

## 附录：接口一览

| 方法 | 路径 | 说明 | 新/旧 |
|------|------|------|------|
| GET | `/health` | 健康检查 | 已有 |
| GET | `/v1/models` | OpenAI 兼容模型列表 | 已有 |
| POST | `/v1/chat/completions` | OpenAI 兼容对接（DeepSeek-OCR / DeepSeek-OCR-JSON / AIHR-Resume） | 已有 |
| POST | `/api/resume/sync/batch` | 同步批量解析+打标（≤50，url/file/base64，直接返回完整结果） | 新增 |
| POST | `/api/resume/async/parse` | 单文件异步解析+打标 | 已有 |
| GET | `/api/resume/async/result` | 轮询任务结果 | 已有 |
| POST | `/api/resume/async/batch` | 批量异步（JSON，type=url，≤20） | 已有 |
| POST | `/api/resume/async/batch/multiform` | 批量异步（files + urls 混用，≤20） | 已有 |
| GET | `/api/tasks` | 任务列表查询（支持状态过滤） | 已有 |
| GET | `/api/tags/config` | 标签配置查询 | 已有 |
| POST | `/api/tags/config/positive` | 新增正向维度 | 新增 |
| PUT | `/api/tags/config/positive/{dimension}` | 更新正向维度 | 新增 |
| DELETE | `/api/tags/config/positive/{dimension}` | 删除正向维度 | 新增 |
| PUT | `/api/tags/config/negative/{dimension}` | 更新负向维度 | 新增 |
| POST | `/api/tags/config/negative/{dimension}/rules` | 新增负向规则 | 新增 |
| PUT | `/api/tags/config/negative/{dimension}/rules/{ruleKey}` | 更新负向规则 | 新增 |
| DELETE | `/api/tags/config/negative/{dimension}/rules/{ruleKey}` | 删除负向规则 | 新增 |
| PUT | `/api/tags/config/severity` | 更新扣分权重 | 新增 |
| POST | `/api/tags/config/reset` | 重置为默认配置 | 新增 |
| POST | `/algorithm/positive-tag/extract` | 内部：正向标签提取 | 内部 |
| POST | `/algorithm/sensitive-tag/detect` | 内部：负面标签检测 | 内部 |
