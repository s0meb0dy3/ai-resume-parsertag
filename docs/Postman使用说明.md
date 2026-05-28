# AIHR 简历解析打标 — Postman 使用说明

> 版本：v3.3  
> 服务地址（生产）：`http://192.168.12.185:21804`  
> 服务地址（Mac本地）：`http://localhost:21804`  
> MinIO 地址：`http://192.168.12.185:9000`（bucket: `resumes`，账号: `minioadmin/minioadmin`）

---

## 一、接口总览

| 编号 | 方法 | 路径 | 说明 |
|:----:|:----:|------|------|
| 01 | GET | `/health` | 健康检查 |
| **02** | **POST** | `/api/resume/async/parse` | **单文件简历提交（multipart 上传）** |
| **03** | **POST** | `/api/resume/async/batch` | **批量简历提交（MinIO URL）** |
| **04** | **GET** | `/api/resume/async/result?taskId=` | **轮询单个任务结果** |
| **05** | **GET** | `/api/tasks` | **任务总览（全部任务状态）** |
| 06 | GET | `/api/tags/config` | 查询标签配置 |
| 07 | POST | `/api/tags/config/positive` | 新增正向维度 🔒 |
| 08 | PUT | `/api/tags/config/positive/{dimension}` | 更新正向维度 🔒 |
| 09 | DELETE | `/api/tags/config/positive/{dimension}` | 删除正向维度 🔒 |
| 10 | PUT | `/api/tags/config/negative/{dimension}` | 更新负向维度 🔒 |
| 11 | POST | `/api/tags/config/negative/{dimension}/rules` | 新增负向规则 🔒 |
| 12 | PUT | `/api/tags/config/negative/{dimension}/rules/{ruleKey}` | 更新负向规则 🔒 |
| 13 | DELETE | `/api/tags/config/negative/{dimension}/rules/{ruleKey}` | 删除负向规则 🔒 |
| 14 | PUT | `/api/tags/config/severity` | 更新扣分权重 🔒 |
| 15 | POST | `/api/tags/config/reset` | 重置为默认配置 🔒 |
| 16 | POST | `/v1/chat/completions` | OpenAI 兼容层（OneAPI 转发） |

> 🔒 标注的接口为写操作，需在 Header 中添加 `Authorization: Bearer <TAGS_ADMIN_TOKEN>`（服务未配置 `TAGS_ADMIN_TOKEN` 时可跳过）

---

## 二、集合变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `base_url` | `http://192.168.12.185:21804` | 解析服务地址 |
| `task_id` | （空） | 提交后手动填入，接口 04 读取 |
| `admin_token` | （空） | 写接口 Bearer Token，对应 `TAGS_ADMIN_TOKEN` 环境变量 |

---

## 三、接口 01 — 健康检查

```
GET /health
```

**响应：**
```json
{ "status": "healthy", "timestamp": "2026-05-25T14:00:00.000000" }
```

---

## 四、接口 02 — 单文件简历提交

```
POST /api/resume/async/parse
Content-Type: multipart/form-data
```

上传单份 PDF 文件，立即返回 `taskId`，后台异步完成解析+打标全流程。

### Postman 操作

1. Body → **form-data**
2. 添加 Key `file`，类型选 **File**，选择本地 PDF 文件
3. 按需添加以下可选字段

### 参数

| Key | 类型 | 必填 | 说明 |
|-----|------|:----:|------|
| `file` | File | ✅ | PDF 文件，≤20MB |
| `requestId` | Text | ✗ | 幂等标识，相同 requestId 直接返回已有 taskId |
| `jobTitle` | Text | ✗ | 目标岗位名称 |
| `requiredSkills` | Text | ✗ | 必要技能，逗号分隔 |
| `jobDescription` | Text | ✗ | JD 全文 |
| `jobSensitiveWords` | Text | ✗ | 自定义敏感词，逗号/分号分隔 |

### 响应

```json
{
  "code": 0, "msg": "success",
  "data": {
    "taskId": "aaa-111",
    "status": 0,
    "statusDesc": "待处理",
    "estimatedSeconds": 90
  }
}
```

> 拿到 `taskId` 后，通过接口 04 轮询结果。

---

## 五、接口 03 — 批量简历提交

```
POST /api/resume/async/batch
Content-Type: application/json
```

传入 MinIO URL 列表，服务端主动下载并解析，立即返回所有 taskId，后台并发处理。

### Postman 操作

1. Body → **raw** → 右侧下拉选 **JSON**
2. 粘贴以下请求体

### 请求体（接口 03）

```json
{
  "resumes": [
    {
      "type": "url",
      "file": {
        "name": "31岁-5年经验-证券事务代表-猎聘简历.pdf",
        "path": "http://192.168.12.185:9000/resumes/31%E5%B2%81-5%E5%B9%B4%E7%BB%8F%E9%AA%8C-%E8%AF%81%E5%88%B8%E4%BA%8B%E5%8A%A1%E4%BB%A3%E8%A1%A8-%E7%8C%8E%E8%81%98%E7%AE%80%E5%8E%86.pdf",
        "extra": "userId_001"
      }
    },
    {
      "type": "url",
      "file": {
        "name": "PKU刘旗简历.pdf",
        "path": "http://192.168.12.185:9000/resumes/PKU%E5%88%98%E6%97%97%E7%AE%80%E5%8E%86.pdf",
        "extra": "userId_002"
      }
    },
    {
      "type": "url",
      "file": {
        "name": "伍先生-34岁-11年经验-证券事务代表-猎聘简历.pdf",
        "path": "http://192.168.12.185:9000/resumes/%E4%BC%8D%E5%85%88%E7%94%9F-34%E5%B2%81-11%E5%B9%B4%E7%BB%8F%E9%AA%8C-%E8%AF%81%E5%88%B8%E4%BA%8B%E5%8A%A1%E4%BB%A3%E8%A1%A8-%E7%8C%8E%E8%81%98%E7%AE%80%E5%8E%86.pdf"
      }
    }
  ],
  "jobTitle": "证券事务代表",
  "requiredSkills": "证券,信披,上市公司",
  "jobDescription": "负责上市公司证券事务管理",
  "jobSensitiveWords": ""
}
```

**字段说明**

| 字段 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `resumes` | Array | ✅ | URL 列表，每项 `type` 固定为 `"url"` |
| `resumes[].file.name` | string | ✅ | 文件名（含扩展名） |
| `resumes[].file.path` | string | ✅ | MinIO/HTTP 可访问的 PDF 地址（中文需 URL 编码） |
| `resumes[].file.extra` | string | ✗ | 透传字段，原样返回（可存业务 ID） |
| `jobTitle` | string | ✗ | 目标岗位名称 |
| `requiredSkills` | string | ✗ | 必要技能，逗号分隔 |
| `jobDescription` | string | ✗ | JD 全文 |
| `jobSensitiveWords` | string | ✗ | 自定义敏感词，逗号/分号分隔 |

> **中文文件名需 URL 编码**：Python `urllib.parse.quote(filename)` / Java `URLEncoder.encode(filename, "UTF-8")`（注意把 `+` 替换为 `%20`）

**MinIO 配置**

| 项目 | 值 |
|------|-----|
| MinIO API | `http://192.168.12.185:9000` |
| bucket | `resumes` |
| 访问策略 | 公开读（s3:GetObject） |
| 管理控制台 | `http://192.168.12.185:9001`（minioadmin / minioadmin） |

### 响应

```json
{
  "code": 0,
  "msg": "success",
  "total": 3,
  "submitted": 3,
  "failed": 0,
  "estimatedSeconds": 90,
  "tasks": [
    {"filename": "31岁-5年经验-证券事务代表-猎聘简历.pdf", "extra": "userId_001", "code": 0, "taskId": "aaa-111", "status": 0, "statusDesc": "待处理"},
    {"filename": "PKU刘旗简历.pdf",                        "extra": "userId_002", "code": 0, "taskId": "bbb-222", "status": 0, "statusDesc": "待处理"},
    {"filename": "伍先生-34岁-11年经验-证券事务代表-猎聘简历.pdf", "extra": null, "code": 0, "taskId": "ccc-333", "status": 0, "statusDesc": "待处理"}
  ]
}
```

---

## 六、接口 04 — 轮询单个任务结果

```
GET /api/resume/async/result?taskId={{task_id}}
```

| status | 含义 | 建议轮询间隔 |
|:------:|------|:-----------:|
| 0 | 待处理 | 3s |
| 1 | 简历解析中 | 5s |
| 2 | 标签打标中 | 5s |
| **3** | **任务完成** → 读取 `data.result` | — |
| 4 | 任务失败 → 读取 `data.errorMsg` | — |

> 任务 TTL = **10 分钟**，超时返回 `code=2001`，需重新提交。

**完成响应（status=3）：**

```json
{
  "code": 0, "msg": "success",
  "data": {
    "taskId": "aaa-111",
    "status": 3,
    "statusDesc": "任务完成",
    "elapsedSeconds": 35.2,
    "estimatedSeconds": 90,
    "extra": "userId_001",
    "result": {
      "taskId": "aaa-111",
      "resumeParseFinishTime": "2026-05-25 14:01:15",
      "tagParseFinishTime":    "2026-05-25 14:01:26",
      "candidate": {
        "name": "姚先生", "phone": "13800138000", "email": "",
        "gender": 1, "age": 31, "address": "深圳",
        "nativePlace": "", "selfEvaluation": "",
        "expectedSalary": "20-30K", "expectedPosition": "证券事务代表"
      },
      "educations": [
        {"school": "武汉大学", "major": "金融学", "degree": "硕士",
         "education": "硕士研究生", "startDate": "2015-09", "endDate": "2018-07"}
      ],
      "works": [
        {"company": "广州广钢气体能源股份有限公司", "position": "证券事务代表",
         "description": "负责上市公司信息披露、三会运作及投资者关系管理",
         "startDate": "2020-07", "endDate": "至今"}
      ],
      "projects": [],
      "positiveTags": {
        "tagsArray": [
          {"dimension": "education",  "displayName": "学历背景",
           "value": [{"tag": "985", "source": "ai"}, {"tag": "211", "source": "ai"}, {"tag": "双一流", "source": "ai"}, {"tag": "硕士", "source": "ai"}]},
          {"dimension": "companyBg",  "displayName": "公司背景",  "confidence": 0.85,
           "value": [{"tag": "国企/央企", "source": "ai"}]},
          {"dimension": "workYears",  "displayName": "工作年限",
           "value": [{"tag": "5-10年", "source": "ai"}]},
          {"dimension": "skills",     "displayName": "技能标签",  "confidence": 0.92,
           "value": [{"tag": "信息披露", "source": "ai"}, {"tag": "三会运作", "source": "ai"}]},
          {"dimension": "industry",   "displayName": "行业背景",  "confidence": 0.88,
           "value": [{"tag": "金融科技", "source": "ai"}]}
        ]
      },
      "negativeTags": {
        "hitCount": 0, "deductionScore": 0, "tagsArray": []
      }
    }
  }
}
```

---

## 七、接口 05 — 任务总览

```
GET /api/tasks
GET /api/tasks?status=3              只看已完成
GET /api/tasks?status=4              只看失败
GET /api/tasks?status=3&include_result=true   已完成且返回完整 result JSON
```

批量提交后用此接口一次查看所有任务进度，按创建时间倒序，最多返回 200 条。

| 参数 | 类型 | 说明 |
|------|------|------|
| `status` | int | 0/1/2/3/4 按状态过滤，不传返回全部 |
| `include_result` | bool | `true` 时 status=3 的条目同时返回完整 result JSON，默认 false |

**响应：**

```json
{
  "code": 0, "msg": "success",
  "data": {
    "total": 3,
    "tasks": [
      {
        "taskId": "aaa-111", "status": 3, "statusDesc": "任务完成",
        "elapsedSeconds": 35.2, "createdAt": "2026-05-25 14:01:00",
        "extra": "userId_001",
        "candidateName": "姚先生",
        "resumeParseTime": "2026-05-25 14:01:17",
        "tagParseTime":    "2026-05-25 14:01:26",
        "completedAt":     "2026-05-25 14:01:26"
      },
      {
        "taskId": "bbb-222", "status": 2, "statusDesc": "标签打标中",
        "elapsedSeconds": 45.0, "createdAt": "2026-05-25 14:01:00",
        "extra": "userId_002"
      },
      {
        "taskId": "ccc-333", "status": 4, "statusDesc": "任务失败",
        "elapsedSeconds": 3.1, "createdAt": "2026-05-25 14:01:00",
        "extra": null,
        "errorMsg": "PDF 已加密，需要密码才能解析"
      }
    ]
  }
}
```

> 完整结果需用各 taskId 单独调用接口 03，或使用 `?include_result=true`（注意响应体较大）。

---

## 八、接口 06~15 — 标签配置 CRUD

> 所有写操作（接口 07~15）持久化到容器内 `schemas/tag_config_runtime.json`，**无需重启服务**。  
> 调用接口 15（reset）可一键恢复默认值。  
> **鉴权**：写接口需在 Header 中添加 `Authorization: Bearer {{admin_token}}`（服务未配置 `TAGS_ADMIN_TOKEN` 时可跳过）。

所有接口统一响应格式：

```json
{
  "code": 0, "msg": "success",
  "data": {
    "positiveDimensions": [...],
    "negativeDimensions": [...],
    "severityDeduction": {"low": -5, "medium": -10, "high": -20},
    "updatedAt": "2026-05-25 14:00:00"
  }
}
```

---

### 接口 06 — 查询全部配置

```
GET /api/tags/config
```

无需 Body。返回完整配置，包含正向维度（含 `allowedValues`）、负向维度（含 `rules` 详细参数）和扣分权重。

**响应示例（data 部分）：**

```json
{
  "positiveDimensions": [
    {
      "dimension": "education", "displayName": "学历背景", "enabled": true,
      "allowedValues": ["C9","985","211","双一流","海外名校(QS50)","海外名校(QS100)","海外硕士","博士","硕士","本科","专科及以下"]
    },
    {
      "dimension": "companyBg", "displayName": "公司背景", "enabled": true,
      "allowedValues": ["头部大厂","腰部中厂","外企500强","一线外企","独角兽","明星创业","国企/央企","传统企业转型"]
    },
    {
      "dimension": "workYears", "displayName": "工作年限", "enabled": true,
      "allowedValues": ["应届","2-3年","3-5年","5-10年","10-15年","15年+"]
    },
    {"dimension": "skills",   "displayName": "技能标签", "enabled": true, "allowedValues": []},
    {
      "dimension": "industry", "displayName": "行业背景", "enabled": true,
      "allowedValues": ["互联网/电商","金融科技","企业服务/SaaS","游戏","在线教育","医疗健康","人工智能","汽车/出行","物联网/硬件","物流/供应链"]
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
    },
    {
      "dimension": "integrity", "displayName": "诚信与合规风险", "enabled": true,
      "rules": {
        "timeline_overlap": {"tagName":"工作时间线重叠","enabled":true,"threshold_months":2,"severity":"high"}
      }
    },
    {
      "dimension": "matchRisk", "displayName": "匹配度硬伤", "enabled": true,
      "rules": {
        "training_institution": {"tagName":"疑似培训机构背景","enabled":true,"keywords":["培训","训练营","bootcamp","在线课程","慕课"],"severity":"medium"}
      }
    },
    {
      "dimension": "location", "displayName": "地域相关", "enabled": true,
      "rules": {
        "sensitive_words": {"tagName":"敏感地域","enabled":true,"severity":"high"}
      }
    }
  ],
  "severityDeduction": {"low": -5, "medium": -10, "high": -20},
  "updatedAt": "2026-05-25 14:00:00"
}
```

---

### 接口 07 — 新增正向维度

```
POST /api/tags/config/positive
Content-Type: application/json
```

**请求 Body：**

```json
{
  "dimension": "language",
  "displayName": "语言能力",
  "enabled": true,
  "allowedValues": ["英语CET6", "英语流利", "日语N2", "德语"]
}
```

| 字段 | 必填 | 说明 |
|------|:----:|------|
| `dimension` | ✅ | 唯一标识符（英文，不可重复） |
| `displayName` | ✗ | 中文显示名，默认同 dimension |
| `enabled` | ✗ | 是否启用，默认 true |
| `allowedValues` | ✗ | 枚举值列表，默认空数组 |

错误码：`1001` dimension 为空；`1002` dimension 已存在。

---

### 接口 08 — 更新正向维度

```
PUT /api/tags/config/positive/{dimension}
Content-Type: application/json
```

字段均可选，只传需要修改的字段：

```json
{
  "displayName": "工作年资",
  "enabled": true,
  "allowedValues": ["应届", "1-3年", "3-5年", "5-10年", "10年+"]
}
```

**典型用法：禁用某个维度**

```json
{"enabled": false}
```

错误码：`1004` dimension 不存在。

---

### 接口 09 — 删除正向维度

```
DELETE /api/tags/config/positive/{dimension}
```

无 Body。例：

```
DELETE /api/tags/config/positive/language
```

错误码：`1004` dimension 不存在。

---

### 接口 10 — 更新负向维度

```
PUT /api/tags/config/negative/{dimension}
Content-Type: application/json
```

只能修改维度级别的 `displayName` 和 `enabled`（规则参数通过接口 11 修改）：

```json
{"enabled": false}
```

**示例：禁用时间线重叠检测**

```
PUT /api/tags/config/negative/integrity
Body: {"enabled": false}
```

---

### 接口 11 — 新增负向规则

```
POST /api/tags/config/negative/{dimension}/rules
Content-Type: application/json
```

**示例：向 matchRisk 维度新增一条规则**

```json
{
  "ruleKey": "short_career",
  "tagName": "职业经历过短",
  "enabled": true,
  "severity": "low",
  "threshold_months": 6
}
```

| 字段 | 必填 | 说明 |
|------|:----:|------|
| `ruleKey` | ✅ | 规则唯一标识（英文，不可重复） |
| `tagName` | ✗ | 标签显示名，默认同 ruleKey |
| `enabled` | ✗ | 是否启用，默认 true |
| `severity` | ✗ | `low` / `medium` / `high`，默认 medium |
| 其他字段 | ✗ | 规则自定义参数（如 threshold、keywords 等） |

错误码：`1001` ruleKey 为空；`1002` ruleKey 已存在；`1004` dimension 不存在。

---

### 接口 12 — 更新负向规则

```
PUT /api/tags/config/negative/{dimension}/rules/{ruleKey}
Content-Type: application/json
```

字段均可选，只传需要修改的：

**示例：调低跳槽阈值（近3年≥3段即触发，原为4段）**

```
PUT /api/tags/config/negative/jobstable/rules/frequent_job_change
```
```json
{"threshold": 3, "severity": "high"}
```

**示例：调整时间线重叠容忍度（1个月即触发，原为2个月）**

```
PUT /api/tags/config/negative/integrity/rules/timeline_overlap
```
```json
{"threshold_months": 1}
```

**示例：禁用空窗期检测**

```
PUT /api/tags/config/negative/jobstable/rules/gap_period
```
```json
{"enabled": false}
```

---

### 接口 13 — 删除负向规则

```
DELETE /api/tags/config/negative/{dimension}/rules/{ruleKey}
```

无 Body。例：

```
DELETE /api/tags/config/negative/matchRisk/rules/short_career
```

错误码：`1004` dimension 或 ruleKey 不存在。

---

### 接口 14 — 更新扣分权重

```
PUT /api/tags/config/severity
Content-Type: application/json
```

字段均可选，仅支持 `low` / `medium` / `high`：

```json
{"low": -3, "medium": -8, "high": -15}
```

错误码：`1001` 包含非法字段名。

---

### 接口 15 — 重置为默认配置

```
POST /api/tags/config/reset
```

无 Body。删除容器内 `schemas/tag_config_runtime.json`，所有修改回滚到 `tag_config.py` 默认值。

---

## 九、接口 16 — OpenAI 兼容层（OneAPI 转发）

```
POST /v1/chat/completions
Content-Type: application/json
```

通过标准 OpenAI 格式调用，兼容 **OneAPI / NewAPI** 等代理网关转发。

### model 字段说明

| model | 行为 |
|-------|------|
| `DeepSeek-OCR` | 仅解析，content 返回结构化简历 JSON 字符串 |
| `DeepSeek-OCR-JSON` | 解析 + 打标，同步等待（默认）或异步返回 taskId |
| `AIHR-Resume` | 同 DeepSeek-OCR-JSON |

### content 格式（user message）

```json
{
  "model": "DeepSeek-OCR-JSON",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "url",
          "file": {
            "name": "resume.pdf",
            "path": "http://192.168.12.185:9000/resumes/PKU%E5%88%98%E6%97%97%E7%AE%80%E5%8E%86.pdf"
          }
        }
      ]
    }
  ]
}
```

或 base64 格式：

```json
{
  "type": "base64",
  "file": {"name": "resume.pdf", "content": "<base64_string>"}
}
```

### 统一响应格式（OpenAI 结构）

```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "model": "DeepSeek-OCR-JSON",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "<v3.1 完整 JSON 字符串，需再次 JSON.parse>"
      },
      "finish_reason": "stop"
    }
  ]
}
```

---

## 十、字段速查

### positiveTags 维度枚举

| dimension | displayName | confidence | value[].tag |
|-----------|-------------|:----------:|-------------|
| `education` | 学历背景 | ✗ | C9 / 985 / 211 / 双一流 / 海外名校(QS50) / 海外名校(QS100) / 海外硕士 / 博士 / 硕士 / 本科 / 专科及以下 |
| `companyBg` | 公司背景 | ✅ | 头部大厂 / 腰部中厂 / 外企500强 / 一线外企 / 独角兽 / 明星创业 / 国企/央企 / 传统企业转型 |
| `workYears` | 工作年限 | ✗ | 应届 / 2-3年 / 3-5年 / 5-10年 / 10-15年 / 15年+ |
| `skills`    | 技能标签 | ✅ | 动态提取，无枚举限制 |
| `industry`  | 行业背景 | ✅ | 互联网/电商 / 金融科技 / 企业服务/SaaS / 游戏 / 在线教育 / 医疗健康 / 人工智能 / 汽车/出行 / 物联网/硬件 / 物流/供应链 |

> education 层级展开：命中 C9 → 同时输出 `[C9, 985, 211, 双一流]`

### negativeTags 风险维度

| dimension | displayName | 标签 | severity | 扣分 |
|-----------|-------------|------|:--------:|:----:|
| `jobstable` | 职业稳定性风险 | 频繁跳槽 / 最近任职时长偏短 | medium | -10 |
| `jobstable` | 职业稳定性风险 | 工作空窗期过长 | low | -5 |
| `integrity` | 诚信与合规风险 | 工作时间线重叠 | high | -20 |
| `matchRisk` | 匹配度硬伤 | 疑似培训机构背景 | medium | -10 |
| `location`  | 地域相关 | 命中自定义敏感词 | high | -20 |

> 扣分权重可通过接口 13 动态调整

### gender 枚举

| 值 | 含义 |
|:--:|------|
| `1` | 男 |
| `2` | 女 |
| `0` | 未知 |

### 任务状态

| status | 含义 | 说明 |
|:------:|------|------|
| `0` | 待处理 | 已入队，未开始 |
| `1` | 简历解析中 | OCR/多模态解析进行中 |
| `2` | 标签打标中 | 正向打标 + 负面检测进行中 |
| `3` | 任务完成 | `data.result` 可读取 |
| `4` | 任务失败 | `data.errorMsg` 有错误信息 |

---

## 十一、错误码

| code | 含义 | 处理建议 |
|:----:|------|---------|
| `0` | 成功 | — |
| `1001` | 参数错误 / URL 下载失败 / 字段非法 | 检查请求格式；确认 URL 可达 |
| `1002` | 重复（dimension/ruleKey 已存在）或文件超大 | 检查是否重复创建 |
| `1003` | 批量超过 20 份 | 拆分多次提交 |
| `1004` | 资源不存在（dimension/ruleKey） | 确认标识符拼写 |
| `2001` | taskId 不存在或已过期（TTL 10分钟） | 重新提交 |

---

## 十二、常见问题

**Q: MinIO URL 中文件名有中文怎么处理？**  
A: 中文需 URL 编码。Python：`urllib.parse.quote(filename)`；Java：`URLEncoder.encode(filename, "UTF-8")`（注意把 `+` 替换为 `%20`）。

**Q: URL 下载返回「文件下载失败」？**  
A: URL 由**服务器端**发起下载，MinIO `192.168.12.185:9000` 需在解析服务所在机器上可访问，确认网络互通。  
Mac 本地 Docker 部署时，URL 须使用 `http://host.docker.internal:9000/...`（不能用 localhost）。

**Q: 提交成功但 `/api/tasks` 看不到？**  
A: 任务 TTL=10 分钟，超时自动清除；或检查是否提交到了不同端口的实例。

**Q: 标签配置修改后如何生效？**  
A: 通过接口 07~14 修改后立即生效，无需重启服务（写入 `schemas/tag_config_runtime.json`，服务通过 mtime 缓存自动感知）。调用接口 15（reset）可一键恢复默认值。

**Q: `extra` 字段有什么用？**  
A: 调用方在提交时传入自己的业务 ID（如 userId、resumeId 等），服务原样存储并在接口 03 / 接口 04 响应中透传返回，方便业务侧做数据关联。

**Q: Mac 本地测试和生产的区别？**

| 项目 | 生产 | Mac 本地 |
|------|------|---------|
| 服务地址 | `http://192.168.12.185:21804` | `http://localhost:21804` |
| MinIO URL（容器内访问） | `http://192.168.12.185:9000` | `http://host.docker.internal:9000` |
| 模型服务 | 直连内网 vLLM 服务 | socat 转发到 `host.docker.internal` |
| 写接口鉴权 | 需配置 `TAGS_ADMIN_TOKEN` | 本地测试可不配置（跳过鉴权） |