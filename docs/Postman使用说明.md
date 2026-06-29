# AIHR 简历解析打标 — Postman 使用说明

> 版本：v3.4
> 服务地址（生产）：`http://10.2.5.119:30804`
> 服务地址（Mac本地）：`http://localhost:21804`
> MinIO 地址：`http://10.2.5.122:9010`

---

## 一、接口总览

| 编号 | 方法 | 路径 | 说明 |
|:----:|:----:|------|------|
| 01 | GET | `/health` | 健康检查 |
| **02** | **POST** | `/api/resume/async/parse` | **单文件简历提交（multipart 上传）** |
| **03** | **POST** | `/api/resume/async/batch` | **批量简历提交（MinIO URL）** |
| **03A** | **POST** | `/api/resume/async/batch/parse` | **批量异步仅解析（立即返回 taskId，不打标）** |
| **03B** | **POST** | `/api/resume/async/batch/tag` | **批量异步仅打标（输入解析 JSON，立即返回 taskId）** |
| **04** | **GET** | `/api/resume/async/result?taskId=` | **轮询单个任务结果** |
| **05** | **GET** | `/api/tasks` | **任务总览（全部任务状态）** |
| **06** | **POST** | `/api/resume/sync/batch` | **同步批量解析+打标（直接返回完整结果）** |
| **06B** | **POST** | `/api/resume/sync/parse` | **同步批量仅解析（返回结构化简历 JSON，不打标）** |
| **06C** | **POST** | `/api/resume/sync/tag` | **同步批量仅打标（输入解析 JSON，返回完整结果）** |
| 07 | GET | `/api/tags/config` | 查询标签配置 |
| 08 | POST | `/api/tags/config/positive` | 新增正向维度 🔒 |
| 09 | PUT | `/api/tags/config/positive/{dimension}` | 更新正向维度 🔒 |
| 10 | DELETE | `/api/tags/config/positive/{dimension}` | 删除正向维度 🔒 |
| **07A** | **POST** | `/api/tags/config/negative` | **新增负向维度 🔒** |
| 11 | PUT | `/api/tags/config/negative/{dimension}` | 更新负向维度 🔒 |
| **11A** | **DELETE** | `/api/tags/config/negative/{dimension}` | **删除负向维度 🔒** |
| 12 | POST | `/api/tags/config/negative/{dimension}/rules` | 新增负向规则 🔒 |
| 13 | PUT | `/api/tags/config/negative/{dimension}/rules/{ruleKey}` | 更新负向规则 🔒 |
| 14 | DELETE | `/api/tags/config/negative/{dimension}/rules/{ruleKey}` | 删除负向规则 🔒 |
| 15 | PUT | `/api/tags/config/severity` | 更新扣分权重 🔒 |
| 16 | POST | `/api/tags/config/reset` | 重置为默认配置 🔒 |
| 17 | POST | `/v1/chat/completions` | OpenAI 兼容层（OneAPI 转发） |

> 🔒 标注的接口为写操作，需在 Header 中添加 `Authorization: Bearer <TAGS_ADMIN_TOKEN>`（服务未配置 `TAGS_ADMIN_TOKEN` 时可跳过）
>
> 模型路由：`/api/resume/async/*` 使用 `ASYNC_MODEL_NAME=Qwen3.5-397B-A17B`；`/api/resume/sync/*` 与 `/v1/chat/completions` 使用 `SYNC_MODEL_NAME=Qwen3.6-35B-A3B`；`MODEL_NAME=Qwen3.6-35B-A3B` 作为默认兜底。

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

## 五A、接口 03A — 批量异步仅解析

```
POST /api/resume/async/batch/parse
Content-Type: application/json
```

**区别于接口 03（async/batch）**：只提交 PDF 解析任务，**不触发打标**，立即返回 taskId 列表。适合"先解析入库、后按岗位打标"的分步流程；解析完成后通过接口 04 轮询，拿到的 `result` 可直接作为接口 03B 的 `resumes` 数组输入。

> 预计等待时间约 60s（仅解析阶段），比全流程（解析+打标）更短。

### Postman 操作

1. Body → **raw** → 右侧下拉选 **JSON**
2. 粘贴以下请求体

### 请求体

请求体格式与接口 03（async/batch）**完全一致**，仅支持 `type=url`：

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
    }
  ]
}
```

**字段说明**（同接口 03，无 jobTitle 等打标字段）

| 字段 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `resumes` | Array | ✅ | URL 列表，每项 `type` 固定为 `"url"`，单次上限 20 份 |
| `resumes[].file.name` | string | ✅ | 文件名（含扩展名） |
| `resumes[].file.path` | string | ✅ | MinIO/HTTP 可访问的 PDF 地址 |
| `resumes[].file.extra` | string | ✗ | 透传字段，原样返回 |

### 响应

```json
{
  "code": 0,
  "msg": "success",
  "total": 2,
  "submitted": 2,
  "failed": 0,
  "estimatedSeconds": 60,
  "tasks": [
    {"filename": "31岁-5年经验-证券事务代表-猎聘简历.pdf", "extra": "userId_001", "code": 0, "taskId": "aaa-111", "status": 0, "statusDesc": "待处理"},
    {"filename": "PKU刘旗简历.pdf",                        "extra": "userId_002", "code": 0, "taskId": "bbb-222", "status": 0, "statusDesc": "待处理"}
  ]
}
```

### 轮询结果（status=3 时的 result 格式）

通过接口 04 轮询，**status=3** 时 `data.result` 为解析后的简历结构（无打标字段），可直接作为接口 03B 的输入：

```json
{
  "code": 0, "msg": "success",
  "data": {
    "taskId": "aaa-111",
    "status": 3,
    "statusDesc": "任务完成",
    "elapsedSeconds": 42.3,
    "estimatedSeconds": 60,
    "extra": "userId_001",
    "result": {
      "filename": "31岁-5年经验-证券事务代表-猎聘简历.pdf",
      "extra": "userId_001",
      "resumeParseFinishTime": "2026-06-16 10:01:15",
      "resume": {
        "candidate": {"name": "姚先生", "age": 31, "...": "..."},
        "educations": ["..."],
        "works": ["..."],
        "projects": []
      }
    }
  }
}
```

---

## 五B、接口 03B — 批量异步仅打标

```
POST /api/resume/async/batch/tag
Content-Type: application/json
```

**区别于接口 03（async/batch）**：只提交打标任务，**跳过 PDF 解析**，立即返回 taskId 列表。`resumes` 数组的每项结构与接口 03A 轮询结果（status=3 时的 `result` 字段）**完全一致**，可直接透传，无需转换。打标完成后通过接口 04 轮询，`result` 格式与接口 03（async/batch）完全一致。

> 预计等待时间约 30s（仅打标阶段）。同一批解析结果可用不同 `jobTitle` 多次传入本接口，按岗位分别打标。

### Postman 操作

1. 先调用接口 03A，等各 taskId 通过接口 04 轮询到 status=3
2. 将每个任务的 `data.result` 字段收集为数组，作为本接口的 `resumes` 传入
3. Body → **raw** → 右侧下拉选 **JSON**，粘贴以下请求体

### 请求体

```json
{
  "resumes": [
    {
      "filename": "31岁-5年经验-证券事务代表-猎聘简历.pdf",
      "extra": "userId_001",
      "resumeParseFinishTime": "2026-06-16 10:01:15",
      "resume": {
        "candidate": {"name": "姚先生", "age": 31, "...": "..."},
        "educations": ["..."],
        "works": ["..."],
        "projects": []
      }
    },
    {
      "filename": "PKU刘旗简历.pdf",
      "extra": "userId_002",
      "resumeParseFinishTime": "2026-06-16 10:01:18",
      "resume": {"candidate": {"name": "刘旗"}, "...": "..."}
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
| `resumes` | Array | ✅ | 解析结果列表，每项为接口 03A 轮询到的 `result` 字段，单次上限 20 份 |
| `resumes[].resume` | object | ✅ | 结构化简历 JSON（接口 03A `result.resume` 字段） |
| `resumes[].filename` | string | ✗ | 文件名，用于日志标识 |
| `resumes[].extra` | string | ✗ | 透传字段，原样返回 |
| `resumes[].resumeParseFinishTime` | string | ✗ | 解析完成时间，写入输出 `result.resumeParseFinishTime` |
| `jobTitle` | string | ✗ | 目标岗位名称 |
| `requiredSkills` | string | ✗ | 必要技能，逗号分隔 |
| `jobDescription` | string | ✗ | JD 全文 |
| `jobSensitiveWords` | string | ✗ | 自定义敏感词，逗号/分号分隔 |

### 响应

```json
{
  "code": 0,
  "msg": "success",
  "total": 2,
  "submitted": 2,
  "failed": 0,
  "estimatedSeconds": 30,
  "tasks": [
    {"filename": "31岁-5年经验-证券事务代表-猎聘简历.pdf", "extra": "userId_001", "code": 0, "taskId": "ccc-333", "status": 0, "statusDesc": "待处理"},
    {"filename": "PKU刘旗简历.pdf",                        "extra": "userId_002", "code": 0, "taskId": "ddd-444", "status": 0, "statusDesc": "待处理"}
  ]
}
```

`resume` 字段缺失或非 dict 时，该条不入队，直接返回 `code=1001`：

```json
{"filename": "bad.pdf", "extra": null, "code": 1001, "msg": "resume 字段缺失或格式错误", "taskId": null}
```

### 轮询结果（status=3 时的 result 格式）

打标完成后，`data.result` 格式与接口 03（async/batch）轮询结果**完全一致**（含 `positiveTags` / `negativeTags` 完整打标 JSON）：

```json
{
  "code": 0, "msg": "success",
  "data": {
    "taskId": "ccc-333",
    "status": 3,
    "statusDesc": "任务完成",
    "elapsedSeconds": 8.5,
    "estimatedSeconds": 30,
    "extra": "userId_001",
    "result": {
      "taskId": "ccc-333",
      "resumeParseFinishTime": "2026-06-16 10:01:15",
      "tagParseFinishTime":    "2026-06-16 10:01:24",
      "candidate": {"name": "姚先生", "...": "..."},
      "educations": ["..."],
      "works": ["..."],
      "projects": [],
      "positiveTags": {"tagsArray": ["..."]},
      "negativeTags": {"hitCount": 0, "deductionScore": 0, "tagsArray": []}
    }
  }
}
```

### 接口 03A → 03B 串联示意

```
POST /api/resume/async/batch/parse      # 第一步：批量提交解析
  └─ 返回 taskId 列表

GET /api/resume/async/result?taskId=xxx # 第二步：逐个轮询解析结果（status=3）
  └─ data.result = {
       "filename": "a.pdf",
       "extra": "userId_001",
       "resumeParseFinishTime": "...",
       "resume": {...}
     }

POST /api/resume/async/batch/tag        # 第三步：将解析结果传入打标
  Body: {
    "resumes": [ <data.result>, <data.result>, ... ],
    "jobTitle": "证券事务代表",
    "requiredSkills": "信息披露,三会运作"
  }
  └─ 返回新的 taskId 列表

GET /api/resume/async/result?taskId=yyy # 第四步：逐个轮询打标结果（status=3）
  └─ data.result 含完整 positiveTags / negativeTags
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
      "dimension": "jobstable", "displayName": "职业稳定性风险", "enabled": true, "detectionType": "rule",
      "rules": {
        "frequent_job_change": {"tagName":"频繁跳槽","enabled":true,"window_years":3,"threshold":4,"severity":"medium"},
        "short_tenure":        {"tagName":"最近任职时长偏短","enabled":true,"threshold_months":3,"severity":"medium"},
        "gap_period":          {"tagName":"工作空窗期过长","enabled":true,"threshold_months":6,"severity":"low"}
      }
    },
    {
      "dimension": "integrity", "displayName": "诚信与合规风险", "enabled": true, "detectionType": "rule",
      "rules": {
        "timeline_overlap": {"tagName":"工作时间线重叠","enabled":true,"threshold_months":2,"severity":"high"}
      }
    },
    {
      "dimension": "matchRisk", "displayName": "匹配度硬伤", "enabled": true, "detectionType": "rule",
      "rules": {
        "training_institution": {"tagName":"疑似培训机构背景","enabled":true,"keywords":["培训","训练营","bootcamp","在线课程","慕课"],"severity":"medium"}
      }
    },
    {
      "dimension": "location", "displayName": "地域相关", "enabled": true, "detectionType": "rule",
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

### 接口 07A — 新增负向维度

```
POST /api/tags/config/negative
Content-Type: application/json
```

新增自定义负向维度。`detectionType` 控制检测方式：
- `rule`（默认）：关键词规则检测，每条规则需提供 `keywords` + 可选 `searchIn` 字段
- `llm`：大模型语义检测，每条规则需提供 `description` 描述检测标准（有 LLM 调用时执行）

**示例 A：关键词规则类型（rule）**

```json
{
  "dimension": "keywordRisk",
  "displayName": "关键词风险",
  "enabled": true,
  "detectionType": "rule",
  "rules": {
    "dispute_risk": {
      "tagName": "含劳动纠纷词",
      "enabled": true,
      "keywords": ["劳动仲裁", "离职纠纷", "强制离职"],
      "searchIn": ["works.description", "candidate.selfEvaluation"],
      "severity": "high"
    }
  }
}
```

`searchIn` 支持的字段路径：`all`（默认，全文）/ `works.company` / `works.description` / `works.position` / `educations.school` / `educations.major` / `projects.description` / `candidate.address` / `candidate.nativePlace` / `candidate.selfEvaluation`

**示例 B：大模型语义类型（llm）**

```json
{
  "dimension": "attitudeRisk",
  "displayName": "候选人态度风险",
  "enabled": true,
  "detectionType": "llm",
  "rules": {
    "negative_attitude": {
      "tagName": "负面态度倾向",
      "enabled": true,
      "description": "自我评价中对前东家有抱怨或推卸责任的表述",
      "severity": "medium"
    },
    "unrealistic_salary": {
      "tagName": "薪资期望脱离实际",
      "enabled": true,
      "description": "期望薪资与工作年限和背景明显不符（超过市场均价50%以上）",
      "severity": "low"
    }
  }
}
```

| 字段 | 必填 | 说明 |
|------|:----:|------|
| `dimension` | ✅ | 唯一标识符（英文，不可重复） |
| `displayName` | ✗ | 中文显示名，默认同 dimension |
| `enabled` | ✗ | 是否启用，默认 true |
| `detectionType` | ✗ | `rule`（默认）/ `llm` |
| `rules` | ✗ | 规则字典，key 为 ruleKey |

错误码：`1001` dimension 为空；`1002` dimension 已存在。

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

### 接口 11A — 删除负向维度

```
DELETE /api/tags/config/negative/{dimension}
```

删除整个负向维度及其所有规则。4 个内置维度（`jobstable` / `integrity` / `location` / `matchRisk`）可以删除，删除后该维度的检测逻辑不再执行。

**示例：删除自定义维度**

```
DELETE /api/tags/config/negative/keywordRisk
```

**示例：删除内置维度（谨慎操作，可通过 reset 恢复）**

```
DELETE /api/tags/config/negative/matchRisk
```

错误码：`1004` dimension 不存在。

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

## 六B、接口 06 — 同步批量解析+打标

```
POST /api/resume/sync/batch
Content-Type: application/json
```

**区别于异步接口**：提交后 **阻塞等待全部完成**，直接返回每份简历的完整解析+打标结果，无需轮询。适合离线批处理、脚本集成等场景；在线并发场景推荐使用接口 03（异步）。

### Postman 操作

1. Body → **raw** → 右侧下拉选 **JSON**
2. 粘贴以下请求体

### 请求体

```json
{
  "resumes": [
    {
      "type": "url",
      "file": {
        "name": "java开发工程师_深圳 18-26K谭志兵 7年.pdf",
        "path": "http://10.2.5.122:9010/kuangchi-ehr-resumes/20260528/1779935325155_【java开发工程师_深圳 18-26K】谭志兵 7年.pdf",
        "extra":""
      }
    },
    {
      "type": "url",
      "file": {
        "name": "杨强华.pdf",
        "path": "http://10.2.5.122:9010/kuangchi-ehr-resumes/20260528/1779938351507_杨强华.pdf",
        "extra":""
      }
    },
    {
      "type": "url",
      "file": {
        "name": "刘江波 10年以上.pdf",
        "path": "http://10.2.5.122:9010/kuangchi-ehr-resumes/20260528/1779937641593_【Java开发工程师_深圳 18-26K】刘江波 10年以上.pdf"
      }
    }
  ]
}
```

### 请求字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `resumes` | Array | ✅ | 简历列表，每项必须含 `type` 字段 |
| `resumes[].type` | string | ✅ | `url` / `file` / `base64` 三选一 |
| `resumes[].file.name` | string | ✅ | 文件名（含扩展名），用于日志标识 |
| `resumes[].file.path` | string | `url`/`file` 时必填 | MinIO URL 或容器内绝对路径 |
| `resumes[].file.content` | string | `base64` 时必填 | PDF 文件 Base64 编码字符串 |
| `resumes[].file.extra` | string | ✗ | 透传字段，原样返回（可存业务 ID） |
| `jobTitle` | string | ✗ | 目标岗位名称 |
| `requiredSkills` | string | ✗ | 必要技能，逗号分隔 |
| `jobDescription` | string | ✗ | JD 全文 |
| `jobSensitiveWords` | string | ✗ | 自定义敏感词，逗号/分号分隔 |
| `max_concurrency` | int | ✗ | 并发数上限 1~30，默认同 `MAX_WORKERS`（24） |

> **单次上限 50 份**；任务提交后同步阻塞，完整耗时 = 最慢一份的处理时间（并发执行）

### 响应格式

外层包含汇总统计，`results` 数组中每条结构与 `GET /api/resume/async/result`（status=3）的 `data` 字段**完全一致**：

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
        "taskId": "aaa-111",
        "status": 3,
        "statusDesc": "任务完成",
        "elapsedSeconds": 41.2,
        "estimatedSeconds": 90,
        "extra": "",
        "result": {
          "taskId": "aaa-111",
          "resumeParseFinishTime": "2026-06-25 16:13:08",
          "tagParseFinishTime": "2026-06-25 16:13:12",
          "candidate": {
            "name": "谭志兵",
            "phone": "18575579612",
            "email": "18575579612@163.com",
            "gender": 1,
            "age": 29,
            "address": "",
            "nativePlace": "",
            "selfEvaluation": "...",
            "expectedSalary": "",
            "expectedPosition": "Java工程师"
          },
          "educations": [ "..." ],
          "works": [ "..." ],
          "projects": [],
          "positiveTags": {
            "tagsArray": [
              {"dimension": "education",  "displayName": "学历背景",
               "value": [{"tag": "本科", "source": "ai"}]},
              {"dimension": "companyBg",  "displayName": "公司背景", "confidence": 0.90,
               "value": [{"tag": "国企/央企", "source": "ai"}]},
              {"dimension": "workYears",  "displayName": "工作年限",
               "value": [{"tag": "5-10年", "source": "ai"}]},
              {"dimension": "skills",     "displayName": "技能标签", "confidence": 0.90,
               "value": [{"tag": "Java", "source": "ai"}, {"tag": "SpringBoot", "source": "ai"}, {"tag": "微服务", "source": "ai"}]},
              {"dimension": "industry",   "displayName": "行业背景", "confidence": 0.90,
               "value": [{"tag": "金融科技", "source": "ai"}, {"tag": "企业服务/SaaS", "source": "ai"}]}
            ]
          },
          "negativeTags": { "hitCount": 0, "deductionScore": 0, "tagsArray": [] }
        }
      }
    },
    {
      "code": 0,
      "msg": "success",
      "data": { "taskId": "bbb-222", "status": 3, "...": "..." }
    }
  ]
}
```

**失败时（文件下载失败 / 解析异常）单条结果：**

```json
{
  "code": 1,
  "msg": "无法获取 PDF，请检查 type/path/content 字段",
  "data": {
    "taskId": null,
    "status": 4,
    "statusDesc": "任务失败",
    "elapsedSeconds": 0,
    "estimatedSeconds": 90,
    "extra": null,
    "errorMsg": "无法获取 PDF，请检查 type/path/content 字段"
  }
}
```

### 三种 type 输入示例

**type=url（MinIO / HTTP）**
```json
{"type": "url", "file": {"name": "张三.pdf", "path": "http://host.docker.internal:9000/resumes/PKU%E5%88%98%E6%97%97%E7%AE%80%E5%8E%86.pdf", "extra": "投递ID_001"}}
```

**type=file（容器内本地文件）**
```json
{"type": "file", "file": {"name": "张三.pdf", "path": "/app/uploads/PKU刘旗简历.pdf"}}
```

**type=base64**
```json
{"type": "base64", "file": {"name": "张三.pdf", "content": "JVBERi0xLjQ..."}}
```

---

## 六C、接口 06B — 同步批量仅解析

```
POST /api/resume/sync/parse
Content-Type: application/json
```

**区别于接口 06（sync/batch）**：只执行 PDF → 结构化 JSON 解析，**不调用打标**，耗时更短（20~40s/份）。适合"先解析入库、后按岗位打标"的分步流程。输出的 `results[].data` 可直接传入接口 06C（sync/tag）完成打标，字段结构完全匹配，无需转换。

### Postman 操作

1. Body → **raw** → 右侧下拉选 **JSON**
2. 粘贴以下请求体

### 请求体

```json
{
  "resumes": [
    {
      "type": "url",
      "file": {
        "name": "java开发工程师_深圳 18-26K谭志兵 7年.pdf",
        "path": "http://10.2.5.122:9010/kuangchi-ehr-resumes/20260528/1779935325155_【java开发工程师_深圳 18-26K】谭志兵 7年.pdf",
        "extra":""
      }
    },
    {
      "type": "url",
      "file": {
        "name": "杨强华.pdf",
        "path": "http://10.2.5.122:9010/kuangchi-ehr-resumes/20260528/1779938351507_杨强华.pdf",
        "extra":""
      }
    },
    {
      "type": "url",
      "file": {
        "name": "刘江波 10年以上.pdf",
        "path": "http://10.2.5.122:9010/kuangchi-ehr-resumes/20260528/1779937641593_【Java开发工程师_深圳 18-26K】刘江波 10年以上.pdf"
      }
    }
  ]
}
```

### 请求字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `resumes` | Array | ✅ | 简历列表，每项含 `type` + `file`，单次上限 50 份 |
| `resumes[].type` | string | ✅ | `url` / `file` / `base64` 三选一 |
| `resumes[].file.name` | string | ✅ | 文件名（含扩展名） |
| `resumes[].file.path` | string | `url`/`file` 时必填 | MinIO URL 或容器内绝对路径 |
| `resumes[].file.content` | string | `base64` 时必填 | PDF Base64 编码字符串 |
| `resumes[].file.extra` | string | ✗ | 透传字段，原样返回 |
| `max_concurrency` | int | ✗ | 并发数上限 1~30，默认 MAX_WORKERS |

> 无需传 `jobTitle` / `requiredSkills` 等打标字段，本接口不执行打标

### 响应格式

```json
{
    "total": 3,
    "success": 3,
    "failed": 0,
    "elapsed_s": 156.03,
    "results": [
        {
            "code": 0,
            "msg": "success",
            "data": {
                "filename": "java开发工程师_深圳 18-26K谭志兵 7年.pdf",
                "extra": null,
                "resumeParseFinishTime": "2026-06-11 09:47:41",
                "resume": {
                    "candidate": {
                        "name": "谭志兵",
                        "phone": "18575579612",
                        "email": "18575579612@163.com",
                        "gender": 1,
                        "age": 29,
                        "address": "",
                        "nativePlace": "",
                        "selfEvaluation": "了解计算机和软件原理，了解系统性能瓶颈，并能进行系统性能分析和优化；了解设计原则、设计模式，了解OOD和DDD，具备抽象和分层能力，能设计开发可复用的优质代码；了解技术债务问题，了解知识管理和代码管理；有丰富的业务面和技术面，能用宏观和架构的视角看待问题；了解数仓和大数据，了解数据分析、机器学习和LLM，能用模型和数据的视角看待问题。",
                        "expectedSalary": "",
                        "expectedPosition": "Java工程师"
                    },
                    "educations": [
                        {
                            "school": "齐齐哈尔大学",
                            "major": "机械电子工程",
                            "degree": "学士",
                            "education": "本科",
                            "startDate": "2014-09",
                            "endDate": "2018-07"
                        }
                    ],
                    "works": [
                        {
                            "company": "东莞怡合达自动化股份有限公司",
                            "position": "Java工程师",
                            "description": "负责ERP日常功能维护及外围服务迁移合并。使用SpringCloudAlibaba、SpringBoot、MyBatis、VUE等框架，涉及Redis、Tomcat、Maven、Git、Nginx、MySQL、SQLServer、Nacos等中间件。通过自定义BeanNameGenerator解决Spring Bean重名问题，使用网关层实现反向代理处理多租户、多地域、多数据源代码拆分。使用责任链模式和管道处理复杂业务规则，使用TtlExecutors和TaskExecutor异步处理多平台同步业务，使用Fibonacci序列、重试和数据校验机制处理异步导致的跨会话数据库事务延迟问题。",
                            "startDate": "2025-06",
                            "endDate": "2025-11"
                        },
                        {
                            "company": "中航工业通飞华南飞机工业有限公司",
                            "position": "Java工程师",
                            "description": "负责珠海中航通飞项目，涉及JavaEE、ERP、SaaS技术范畴。使用SpringCloudAlibaba、SpringBoot、MyBatis、EasyUI等开发框架，Redis、Tomcat、Maven、Git、Nginx、MySQL、Nacos等中间件，以及BPM工作流。主要工作包括学习和梳理成飞代码以及新业务，根据业务需求完成对模块的重构、优化以及新功能的开发，对接其他系统进行接口联调。优化代码架构，消除冗余逻辑，拆分“上帝类”，重构包结构，提升代码可维护性与可读性。",
                            "startDate": "2024-02",
                            "endDate": "2025-04"
                        },
                        {
                            "company": "北京瑞天欣实数据科技有限公司",
                            "position": "Java工程师",
                            "description": "参与四川农信项目、重庆农商信用卡项目、南昌农商项目、顺德农商供应链项目。技术范畴包括评分卡模型、决策引擎、JavaEE。使用SpringBoot、MyBatis开发框架，Redis、Tomcat、Maven、Git、Nginx、Oracle中间件，以及Python、VBA、Excel、SaaS、Dataworks、Hive/Hadoop等技术。负责数据清洗和提取，数据标准化与归一化处理，特征开发和测试，模型构建、训练、测试及调整，第三方接口对接调试，对外接口开发和测试，系统运维和部署。设计开发Excel+VBA自动化验算工具，使用Python优化自动化工具，利用Oracle窗口函数加工监控变量，基于Dataworks设计自动化数据清洗流程，参与特征工程。",
                            "startDate": "2021-05",
                            "endDate": "2024-02"
                        },
                        {
                            "company": "成都鸿业远图科技有限公司",
                            "position": "Java工程师",
                            "description": "参与江西社保项目、成达停车场项目、金堂消费贷项目、金堂商户贷项目等。技术范畴包括JavaEE、微信支付宝SDK。使用SpringBoot、Dubbo、MyBatis、Vue/Uniapp开发框架，Redis、Tomcat、Zookeeper、Maven、Git、Nginx、Oracle、MySQL中间件，以及支付宝SDK、微信SDK、SSL、Socket、JWT等技术。学习Uniapp、VUE以及微信支付宝SDK的开发使用，完成对项目和模块的设计开发，进行压力测试，配合客户进行业务测试。基于组件化与模块化思想设计并开发社保缴费平台，采用SSL加密传输、JWT认证以及数字签名验签机制，设计并实现支付状态-业务状态异步校验机制，采用多因素认证、微信OpenID认证及Shiro权限管理框架进行接口保护。",
                            "startDate": "2019-04",
                            "endDate": "2021-04"
                        },
                        {
                            "company": "四川集创科技有限责任公司",
                            "position": "Java工程师",
                            "description": "参与德阳项目、达州项目、成都高速三支队项目。技术范畴包括JavaEE、GIS。使用SpringCloud、Mybatis、Angular、Oracle开发框架，Tomcat、Maven、Git、Redis、MQTT、Kafka、Hive/Hadoop中间件，以及MineMap SDK、Echarts、Websocket等技术。负责智能交通管理系统开发，实现交通违法行为实时监控和处理，提供数据可视化和实时决策支持。开发接口和对接调试前端页面，完成功能模块的开发和自测，修复测试人员提交的bug，根据客户要求进行项目维护和功能改造。通过IDEA调试工具深入分析定位接口响应缓慢原因，优化SQL语句执行效率，使用Zoom组件完成对交通违法证据图片的缩小放大功能，利用钩子函数、定时器和鼠标事件实现交通违法图像的播放功能和鼠标暂停功能。",
                            "startDate": "2018-09",
                            "endDate": "2019-03"
                        }
                    ],
                    "projects": [
                        {
                            "company": "东莞怡合达自动化股份有限公司",
                            "projectName": "怡合达ERP项目",
                            "role": "Java工程师",
                            "description": "负责ERP日常功能维护及外围服务迁移合并。使用SpringCloudAlibaba、SpringBoot、MyBatis、VUE等框架，涉及Redis、Tomcat、Maven、Git、Nginx、MySQL、SQLServer、Nacos等中间件。通过自定义BeanNameGenerator解决Spring Bean重名问题，使用网关层实现反向代理处理多租户、多地域、多数据源代码拆分。使用责任链模式和管道处理复杂业务规则，使用TtlExecutors和TaskExecutor异步处理多平台同步业务，使用Fibonacci序列、重试和数据校验机制处理异步导致的跨会话数据库事务延迟问题。",
                            "startDate": "2025-06",
                            "endDate": "2025-11"
                        },
                        {
                            "company": "中航工业通飞华南飞机工业有限公司",
                            "projectName": "珠海中航通飞项目",
                            "role": "Java工程师",
                            "description": "负责珠海中航通飞项目，涉及JavaEE、ERP、SaaS技术范畴。使用SpringCloudAlibaba、SpringBoot、MyBatis、EasyUI等开发框架，Redis、Tomcat、Maven、Git、Nginx、MySQL、Nacos等中间件，以及BPM工作流。主要工作包括学习和梳理成飞代码以及新业务，根据业务需求完成对模块的重构、优化以及新功能的开发，对接其他系统进行接口联调。优化代码架构，消除冗余逻辑，拆分“上帝类”，重构包结构，提升代码可维护性与可读性。",
                            "startDate": "2024-02",
                            "endDate": "2025-04"
                        },
                        {
                            "company": "北京瑞天欣实数据科技有限公司",
                            "projectName": "四川农信项目",
                            "role": "Java工程师",
                            "description": "参与四川农信项目，技术范畴包括评分卡模型、决策引擎、JavaEE。使用SpringBoot、MyBatis开发框架，Redis、Tomcat、Maven、Git、Nginx、Oracle中间件，以及Python、VBA、Excel、SaaS、Dataworks、Hive/Hadoop等技术。负责数据清洗和提取，数据标准化与归一化处理，特征开发和测试，模型构建、训练、测试及调整，第三方接口对接调试，对外接口开发和测试，系统运维和部署。设计开发Excel+VBA自动化验算工具，使用Python优化自动化工具，利用Oracle窗口函数加工监控变量，基于Dataworks设计自动化数据清洗流程，参与特征工程。",
                            "startDate": "2021-05",
                            "endDate": "2024-02"
                        },
                        {
                            "company": "北京瑞天欣实数据科技有限公司",
                            "projectName": "重庆农商信用卡项目",
                            "role": "Java工程师",
                            "description": "参与重庆农商信用卡项目，技术范畴包括评分卡模型、决策引擎、JavaEE。使用SpringBoot、MyBatis开发框架，Redis、Tomcat、Maven、Git、Nginx、Oracle中间件，以及Python、VBA、Excel、SaaS、Dataworks、Hive/Hadoop等技术。负责数据清洗和提取，数据标准化与归一化处理，特征开发和测试，模型构建、训练、测试及调整，第三方接口对接调试，对外接口开发和测试，系统运维和部署。",
                            "startDate": "2021-05",
                            "endDate": "2024-02"
                        },
                        {
                            "company": "北京瑞天欣实数据科技有限公司",
                            "projectName": "南昌农商项目",
                            "role": "Java工程师",
                            "description": "参与南昌农商项目，技术范畴包括评分卡模型、决策引擎、JavaEE。使用SpringBoot、MyBatis开发框架，Redis、Tomcat、Maven、Git、Nginx、Oracle中间件，以及Python、VBA、Excel、SaaS、Dataworks、Hive/Hadoop等技术。负责数据清洗和提取，数据标准化与归一化处理，特征开发和测试，模型构建、训练、测试及调整，第三方接口对接调试，对外接口开发和测试，系统运维和部署。",
                            "startDate": "2021-05",
                            "endDate": "2024-02"
                        },
                        {
                            "company": "北京瑞天欣实数据科技有限公司",
                            "projectName": "顺德农商供应链项目",
                            "role": "Java工程师",
                            "description": "参与顺德农商供应链项目，技术范畴包括评分卡模型、决策引擎、JavaEE。使用SpringBoot、MyBatis开发框架，Redis、Tomcat、Maven、Git、Nginx、Oracle中间件，以及Python、VBA、Excel、SaaS、Dataworks、Hive/Hadoop等技术。负责数据清洗和提取，数据标准化与归一化处理，特征开发和测试，模型构建、训练、测试及调整，第三方接口对接调试，对外接口开发和测试，系统运维和部署。",
                            "startDate": "2021-05",
                            "endDate": "2024-02"
                        },
                        {
                            "company": "成都鸿业远图科技有限公司",
                            "projectName": "江西社保项目",
                            "role": "Java工程师",
                            "description": "参与江西社保项目，技术范畴包括JavaEE、微信支付宝SDK。使用SpringBoot、Dubbo、MyBatis、Vue/Uniapp开发框架，Redis、Tomcat、Zookeeper、Maven、Git、Nginx、Oracle、MySQL中间件，以及支付宝SDK、微信SDK、SSL、Socket、JWT等技术。学习Uniapp、VUE以及微信支付宝SDK的开发使用，完成对项目和模块的设计开发，进行压力测试，配合客户进行业务测试。基于组件化与模块化思想设计并开发社保缴费平台，采用SSL加密传输、JWT认证以及数字签名验签机制，设计并实现支付状态-业务状态异步校验机制，采用多因素认证、微信OpenID认证及Shiro权限管理框架进行接口保护。",
                            "startDate": "2019-04",
                            "endDate": "2021-04"
                        },
                        {
                            "company": "成都鸿业远图科技有限公司",
                            "projectName": "成达停车场项目",
                            "role": "Java工程师",
                            "description": "参与成达停车场项目，技术范畴包括JavaEE、微信支付宝SDK。使用SpringBoot、Dubbo、MyBatis、Vue/Uniapp开发框架，Redis、Tomcat、Zookeeper、Maven、Git、Nginx、Oracle、MySQL中间件，以及支付宝SDK、微信SDK、SSL、Socket、JWT等技术。学习Uniapp、VUE以及微信支付宝SDK的开发使用，完成对项目和模块的设计开发，进行压力测试，配合客户进行业务测试。",
                            "startDate": "2019-04",
                            "endDate": "2021-04"
                        },
                        {
                            "company": "成都鸿业远图科技有限公司",
                            "projectName": "金堂消费贷项目",
                            "role": "Java工程师",
                            "description": "参与金堂消费贷项目，技术范畴包括JavaEE、微信支付宝SDK。使用SpringBoot、Dubbo、MyBatis、Vue/Uniapp开发框架，Redis、Tomcat、Zookeeper、Maven、Git、Nginx、Oracle、MySQL中间件，以及支付宝SDK、微信SDK、SSL、Socket、JWT等技术。学习Uniapp、VUE以及微信支付宝SDK的开发使用，完成对项目和模块的设计开发，进行压力测试，配合客户进行业务测试。",
                            "startDate": "2019-04",
                            "endDate": "2021-04"
                        },
                        {
                            "company": "成都鸿业远图科技有限公司",
                            "projectName": "金堂商户贷项目",
                            "role": "Java工程师",
                            "description": "参与金堂商户贷项目，技术范畴包括JavaEE、微信支付宝SDK。使用SpringBoot、Dubbo、MyBatis、Vue/Uniapp开发框架，Redis、Tomcat、Zookeeper、Maven、Git、Nginx、Oracle、MySQL中间件，以及支付宝SDK、微信SDK、SSL、Socket、JWT等技术。学习Uniapp、VUE以及微信支付宝SDK的开发使用，完成对项目和模块的设计开发，进行压力测试，配合客户进行业务测试。",
                            "startDate": "2019-04",
                            "endDate": "2021-04"
                        },
                        {
                            "company": "四川集创科技有限责任公司",
                            "projectName": "德阳项目",
                            "role": "Java工程师",
                            "description": "参与德阳项目，技术范畴包括JavaEE、GIS。使用SpringCloud、Mybatis、Angular、Oracle开发框架，Tomcat、Maven、Git、Redis、MQTT、Kafka、Hive/Hadoop中间件，以及MineMap SDK、Echarts、Websocket等技术。负责智能交通管理系统开发，实现交通违法行为实时监控和处理，提供数据可视化和实时决策支持。开发接口和对接调试前端页面，完成功能模块的开发和自测，修复测试人员提交的bug，根据客户要求进行项目维护和功能改造。通过IDEA调试工具深入分析定位接口响应缓慢原因，优化SQL语句执行效率，使用Zoom组件完成对交通违法证据图片的缩小放大功能，利用钩子函数、定时器和鼠标事件实现交通违法图像的播放功能和鼠标暂停功能。",
                            "startDate": "2018-09",
                            "endDate": "2019-03"
                        },
                        {
                            "company": "四川集创科技有限责任公司",
                            "projectName": "达州项目",
                            "role": "Java工程师",
                            "description": "参与达州项目，技术范畴包括JavaEE、GIS。使用SpringCloud、Mybatis、Angular、Oracle开发框架，Tomcat、Maven、Git、Redis、MQTT、Kafka、Hive/Hadoop中间件，以及MineMap SDK、Echarts、Websocket等技术。负责智能交通管理系统开发，实现交通违法行为实时监控和处理，提供数据可视化和实时决策支持。开发接口和对接调试前端页面，完成功能模块的开发和自测，修复测试人员提交的bug，根据客户要求进行项目维护和功能改造。使用Oracle窗口函数以及多表查询，完成汇总分析，使用Echarts完成汇总信息的可视化，独立完成勤务查看模块，实现警员出勤的查看、汇总统计功能。",
                            "startDate": "2018-09",
                            "endDate": "2019-03"
                        },
                        {
                            "company": "四川集创科技有限责任公司",
                            "projectName": "成都高速三支队项目",
                            "role": "Java工程师",
                            "description": "参与成都高速三支队项目，技术范畴包括JavaEE、GIS。使用SpringCloud、Mybatis、Angular、Oracle开发框架，Tomcat、Maven、Git、Redis、MQTT、Kafka、Hive/Hadoop中间件，以及MineMap SDK、Echarts、Websocket等技术。负责智能交通管理系统开发，实现交通违法行为实时监控和处理，提供数据可视化和实时决策支持。开发接口和对接调试前端页面，完成功能模块的开发和自测，修复测试人员提交的bug，根据客户要求进行项目维护和功能改造。",
                            "startDate": "2018-09",
                            "endDate": "2019-03"
                        }
                    ]
                }
            }
        },
        {
            "code": 0,
            "msg": "success",
            "data": {
                "filename": "杨强华.pdf",
                "extra": null,
                "resumeParseFinishTime": "2026-06-11 09:47:44",
                "resume": {
                    "candidate": {
                        "name": "杨强华",
                        "phone": "18665937875",
                        "email": "yangqianghuayiyi@163.com",
                        "gender": 1,
                        "age": 0,
                        "address": "",
                        "nativePlace": "湖南娄底",
                        "selfEvaluation": "多年 FPGA 开发的工作经历，熟悉 Xilinx，altera 等公司的 FPGA 器件。可以加班。",
                        "expectedSalary": "",
                        "expectedPosition": ""
                    },
                    "educations": [
                        {
                            "school": "电子科技大学",
                            "major": "通信与信息工程",
                            "degree": "硕士",
                            "education": "硕士研究生",
                            "startDate": "2013-09",
                            "endDate": "2016-07"
                        },
                        {
                            "school": "电子科技大学",
                            "major": "通信与信息工程",
                            "degree": "学士",
                            "education": "本科",
                            "startDate": "2009-09",
                            "endDate": "2013-07"
                        }
                    ],
                    "works": [
                        {
                            "company": "深圳市塞防科技有限公司",
                            "position": "高级 FPGA 工程师",
                            "description": "负责无人机侦测平台及反制枪产品的 FPGA 开发。主要工作包括：9361 到 FPGA 的接口解调模块开发，ZC 序列及前导码序列相关运算的 Matlab 仿真与 FPGA 实现，SPI 接口配置，以及干扰信号的频域伪随机信号生成、IFFT 计算与 FPGA 实现。",
                            "startDate": "2024-08",
                            "endDate": "2025-01"
                        },
                        {
                            "company": "深圳市雷赛智能控制股份有限公司",
                            "position": "高级 FPGA 工程师",
                            "description": "负责伺服系统从机平台开发及多编码器通信协议的 FPGA 实现。主要工作包括：MCU 与 FPGA 之间的通信接口设计与实现（并口、XSPI、高速串行通信），伺服系统位置比较功能模块实现，EtherCAT 协议 FPGA 实现，MPSOC 异构平台 10GEthernet SFP+接口开发，MIPI 摄像头接入系统开发，以及基于 Artix7 和 ZYNQ7000 系列芯片的接口与数据采集系统开发。",
                            "startDate": "2020-04",
                            "endDate": "2024-08"
                        },
                        {
                            "company": "深圳市太赫兹科学技术创新研究院",
                            "position": "信号处理算法工程",
                            "description": "负责高速通信解调器原型样机的研制，包括方案选择、指标设计及整体架构确定。主要工作涵盖免混频下变频器、频域匹配滤波、高速并行定时同步、高速并行均衡及载波同步算法的 MATLAB 仿真与 FPGA 实现。此外，还参与了 E-Band 微波通信设备研发及导弹通讯设备研发设计，负责基带通信链路的编码译码（RS 码、LDPC 码）部分的算法分析、Matlab 建模、RTL 设计及仿真验证。",
                            "startDate": "2016-07",
                            "endDate": "2020-07"
                        },
                        {
                            "company": "华为成都研究所",
                            "position": "无线通信 FPGA 助理工程师（实习）",
                            "description": "参与华为 5G 高频基带 LDPC 编码译码项目，负责高吞吐率编译码算法的开发及 FPGA 实现。独立完成模块开发进度设计与工作计划，对比多种译码算法性能，改进 Off-set Min-Sum 算法以提高吞吐率，并负责 Verilog 代码编写、逻辑仿真、上板验证和测试。",
                            "startDate": "2014-07",
                            "endDate": "2015-08"
                        }
                    ],
                    "projects": [
                        {
                            "company": "深圳市塞防科技有限公司",
                            "projectName": "无人机侦测平台 tracer 产品开发（多类型侦测）",
                            "role": "高级 FPGA 工程师",
                            "description": "项目介绍：无人机侦测产品线架构使用的是 ad9361+MPSOC（5EV）的架构，2 发 2 收，软件控制 SPI 配置信息，来控制 9361 的接收频带范围（4 个中心频点），9361 接收的信号进过解调模块之后得到原始的 adc 信号，然后，再在 FPGA 上进行 URAM 缓存，同时，与 ZC 序列、广播前导码序列进行相关运算，在相关的峰值达到要求之后，就开始向 DDR 的共享内存段，写入一段数据，之后给 PC 一个中断，然后 SOC 中进行信号的解调。然后输出解调的结果。同时也会将原始数据写入一段共享的 DDR，使用 SOC 传给 DGX 平台进行解调，获取到相关的结果之后就上报到终端，表示侦测到相关的无人机类型。本人主要负责的内容是：9361 到 FPGA 的接口解调模块，FPGA 的 ZC 序列相关运算模块，数据发送到 DDR 的打包模块。主要的工作内容：1，参考 ADI 官网的 9361 到 FPGA 的接口模块。2，ZC 序列相关运算的 matlab 仿真 3，前导码序列相关运算的 matlab 仿真 4，ZC 序列相关运算的 FPGA 实现 5，前导码序列相关运算的 FPGA 实现 6，SPI 接口配置 9361 接口的 FPGA 实现",
                            "startDate": "2024-08",
                            "endDate": "2025-01"
                        },
                        {
                            "company": "深圳市塞防科技有限公司",
                            "projectName": "无人机打击产品（反制枪）开发（侦测 + 识别 + 定位 + 干扰）",
                            "role": "高级 FPGA 工程师",
                            "description": "项目介绍：无人机侦测产品线架构使用的是 ad9361+MPSOC（5EV）的架构，2 发 2 收，软件控制 SPI 配置信息，来控制 9361 的接收频带范围（4 个中心频点），9361 接收的信号进过解调模块之后得到原始的 adc 信号，然后，再在 FPGA 上进行 URAM 缓存，以及同步进行定位计算（功率和相位），同时，ZC 序列、广播前导码序列进行相关运算，在相关的峰值达到要求之后，就开始向 DDR 的共享内存段，写入一段数据，之后给 PC 一个中断，然后 SOC 中进行信号的解调。然后输出解调的结果。这就完成了侦测 + 识别 + 定位。干扰：干扰的完成，需要有干扰策略，根据干扰策略，再根据侦测的频段，发射对应频段的干扰信号，同时反制枪发射天线需要在发射方向上对准侦测到的信号源。干扰信号的发送：根据干扰策略，生成一段随机的频率伪随机信号，在 FPGA 经过 1024（2048）点 IFFT，然后经数据通过 9361 和 FPGA 的接口发送出去，这就是干扰信号的产生。本人主要负责的内容是：频域伪随机信号的生成，IFFT 计算，然后经过 9361 与 FPGA 的发送接口。主要工作内容：1，根据干扰策略生成对应频段的伪随机信号，matlab 仿真。2，使用 matlab 的 ifft 函数计算生成的伪随机信号，3，FPGA 实现干扰信号的产生。",
                            "startDate": "2024-08",
                            "endDate": "2025-01"
                        },
                        {
                            "company": "深圳市雷赛智能控制股份有限公司",
                            "projectName": "雷赛 L9 伺服系统从机平台开发",
                            "role": "高级 FPGA 工程师",
                            "description": "项目介绍：雷赛 L9 伺服从机平台开发，支持多编码器，速度环、电流环等更短的周期的实现，配置了 22KLUT 的 FPGA；使用的是瑞萨 T2L 系列 MCU，自带 ethercat 通信模块。本人主要负责的是：1，MCU 与 FPGA 之间的通信接口设计与实现；2，伺服系统的位置比较功能的 FPGA 实现。主要完成以下工作：1、完成了第一版本的并口通信，实现 MCU 与 FPGA 之间的通信。2、MCU 与 FPGA 之间实现 XSPI 接口方案可行性评审。3、MCU 与 FPGA 之间实现高速串行通信方案（差分信号，FPGA 实现 clock data recovery CDR 过程）可行性评审。4、伺服系统位置比较功能设计方案可行性评审。5、MCU 与 FPGA 之间通信使用 xspi 接口方案，hyperbus 总线协议的简化版本的 FPGA 实现，验证，老化测试，EMC 测试等。6、伺服系统位置比较功能模块的实现。",
                            "startDate": "2020-04",
                            "endDate": "2024-08"
                        },
                        {
                            "company": "深圳市雷赛智能控制股份有限公司",
                            "projectName": "伺服系统中的多编码器通信协议的 FPGA 实现",
                            "role": "高级 FPGA 工程师",
                            "description": "项目介绍：多摩川编码器、nikon 编码器、biss-c 编码器、ABZ 编码器、正余弦编码器、龙门通信等，用于伺服系统控制板卡与编码器位置反馈之间的通信。选型紫光的 logos 系列 PGL22 FPGA。本人主要负责的是：主要负责公司伺服与编码器之间的通信接口的 FPGA 实现。主要完成以下工作：1、需求分析：1，一线同事反馈对不同编码器的同时支持需求。2，FPGA 的实现的通信协议越多，降低产品线数量，覆盖的客户群体越多。2、竞品分析：主要分析了汇川 680 伺服系统、松下 A6 伺服系统各自支持的多种编码器，都有各自应用领域的优势，需要确定的是多编码器协议支持是趋势。3、各种编码器协议的 FPGA 实现，仿真，验证，测试，产品化技术支持。",
                            "startDate": "2020-04",
                            "endDate": "2024-08"
                        },
                        {
                            "company": "深圳市雷赛智能控制股份有限公司",
                            "projectName": "EtherCAT 协议的 FPGA 实现",
                            "role": "高级 FPGA 工程师",
                            "description": "项目介绍：基于紫光的 logos 系列 PGL22 FPGA 做 EtherCAT 通信协议的 FPGA 实现。本人主要负责的是：主要负责整个协议的竞品分析、方案设计、仿真验证，白盒测试方案、竞品的性能对比测试以及产品化工作。主要完成以下工作：1、需求分析：缺少芯片、或者芯片价格过高的时候，支持 ethercat 协议的 MCU 芯片价格高昂，FPGA 实现方案，灵活，节省成本，作为可替代方案。2、竞品分析：主要分析了 beckhoff 的 asic 方案、瑞萨的 MCU 方案，以及 beckhoff 的 FPGA 实现方案，最后发现自研 FPGA 方案作为替代方案，可以为公司节省成本，产品方案设计更加灵活。3、协议的了解，matlab 算法模型开发。4、详细的 RTL 实现方案设计，模块划分，各个子模块的编码，仿真验证。5、整个方案的白盒测试。6、与竞品 beckhoff 的 FPGA 方案的性能对比：自研方案所需的逻辑资源，14K LUT，RAM 4K，竞品方案 altera 实现方案：25KLUT，RAM16K。速率都是使用 100M ethernet 网口；自研方案实现的功能减少，竞品方案是协议的完整实现。",
                            "startDate": "2020-04",
                            "endDate": "2024-08"
                        },
                        {
                            "company": "深圳市雷赛智能控制股份有限公司",
                            "projectName": "MPSOC 异构平台 10GEthernet SFP+接口开发",
                            "role": "高级 FPGA 工程师",
                            "description": "项目介绍：基于 Xilinx 的 MPSOC 平台，搭建一个数据吞吐率在 10Gbps 的以太网模块，PC 端使用的是 10GE 网卡，使用的是 SFP+物理层接口，基于 XCZU3EG 的芯片开发出 ZYNQ 到 PC 端的 10G 数据传输链路。本人主要负责的是：主要负责 FPGA 端的工程开发，从立项需求到下板验证的工作，最后达到的结果是在 PC 端正常情况下达到 2.8Gbps 的速率，配置成全部处理 SFP+接口的时候可以达到 9.8Gbps 的速率。",
                            "startDate": "2020-04",
                            "endDate": "2024-08"
                        },
                        {
                            "company": "深圳市雷赛智能控制股份有限公司",
                            "projectName": "MPSOC 异构平台 MIPI 摄像头接入系统",
                            "role": "高级 FPGA 工程师",
                            "description": "项目介绍：基于 Xilinx 的 MPSOC 平台，搭建一个从 IMX334 摄像头到 mipi 接入，在从 PL 到 PS 端，从 DP 接口到 4K 显示器的硬件平台。基于 XCZU5EV 的芯片开发的开发板的 FPGA 工程建立，以及使用这个工程完成调试和验证。本人主要负责的是：1，主要负责 FPGA 端的工程开发，从立项需求到下板验证的工作，最后达到的结果是在显示器端可以看到正常的 4K 实时拍摄的视频，2，因为摄像头原始的视频与人眼看奥的有差距，所以需要做 3A 算法以适应人眼的习惯。在完成 1 的基础上重新开发出自动白平衡、自动曝光、自动聚焦的算法，并且嵌入进 FPGA 工程 中，最后达到的效果是在显示器看到人眼正常看到的流畅拍摄的视频。",
                            "startDate": "2020-04",
                            "endDate": "2024-08"
                        },
                        {
                            "company": "深圳市雷赛智能控制股份有限公司",
                            "projectName": "基于 Artix7 系列 FPGA 芯片的接口开发",
                            "role": "高级 FPGA 工程师",
                            "description": "项目介绍：基于 Xilinx 的 Artix7 系列 FPGA 芯片，做接口开发：1，千兆网口；2,PCIE 接口；3，SFP 接口；4，HDMI 接口；5；DDR4 axi 接口。本人主要负责的是：1，主要负责 FPGA 端的工程开发，从立项需求到下板验证的工作，最后达到的完成接口使用具体传输的数据都是 PC 端通过 HDMI 传输过来的视频，视频数据实时处理，再显示，只是实现的方式是通过网口，PCIE，HDMI，SFP 等个接口。",
                            "startDate": "2020-04",
                            "endDate": "2024-08"
                        },
                        {
                            "company": "深圳市雷赛智能控制股份有限公司",
                            "projectName": "基于 ZYNQ7000 系列芯片的 ADC 数据采集系统开发",
                            "role": "高级 FPGA 工程师",
                            "description": "项目介绍：基于 Xilinx 的 YNQ7000 系列芯片，ADC 采集数据，通过 FPGA 初步捕获，然后通过 PS 端的网口传输到另一个 ARM 平台进行处理。本人主要负责的是：1，建立 FPGA 工程，完成接入 ADC 数据捕获模块，ADC 数据开关模块，ADC 数据传输进 PS 端 DDR 的模块，最后在 PS 端完成对 ADC 数据的处理。",
                            "startDate": "2020-04",
                            "endDate": "2024-08"
                        },
                        {
                            "company": "深圳市太赫兹科学技术创新研究院",
                            "projectName": "高速通信解调器——方案选择、指标设计、整体架构确定",
                            "role": "信号处理算法工程",
                            "description": "项目介绍：2Gbps 高速通信解调原型样机的研制，主要用于 0.14THz 波段通信的验证。本人主要负责的是：方案选择、指标设计、整体架构确定。主要工作内容：1，方案选择：选择低中频数字并行解调结构。1、确定高速通信解调器的指标：码率 2Gbps，调制方式 16QAM，符号率 500Msps，ADC 采样率 2Gsps，并行路数 32，单路速率 62.5MHz。2、确定整体并行解调架构：基于 APRX 并行架构，整个接收机由下变频器、高速 ADC，数字正交下变频、DFT、频域匹配滤波以及定时校正、IDFT、定时相位误差估计以及定时频偏检测、并行自适应均衡、并行载波同步等模块组成。",
                            "startDate": "2016-07",
                            "endDate": "2020-07"
                        },
                        {
                            "company": "深圳市太赫兹科学技术创新研究院",
                            "projectName": "高速通信解调器——免混频下变频器算法的 MATLAB 仿真与 FPGA 实现",
                            "role": "信号处理算法工程",
                            "description": "项目介绍：2Gbps 高速通信解调原型样机，主要工作过程为：利用下变频器讲射频信号变到低中频，再利用高速 ADC 直接中频采样，采样后数据进入 FPGA 进行串并转换后，直接存入并行 FIFO 中，后续解调器操作以固定频率的门控时钟从 FIFO 中读取数据进行数字正交下变频，以及 DFT 变换，然后进行频域匹配滤波，定时相位误差校正，最后经定时同步、均衡、和载波同步后判决输出。在发送端需要加上去相位模糊的差分编码。本人主要负责的是：免混频下变频器的算法设计，MATLAB 仿真验证，以及 FPGA 实现。主要工作内容：1，免混频下变频的算法设计：通过带通采样定理，确定采样率是符号速率的",
                            "startDate": "",
                            "endDate": ""
                        }
                    ]
                }
            }
        },
        {
            "code": 0,
            "msg": "success",
            "data": {
                "filename": "刘江波 10年以上.pdf",
                "extra": null,
                "resumeParseFinishTime": "2026-06-11 09:46:53",
                "resume": {
                    "candidate": {
                        "name": "刘江波",
                        "phone": "18575579612",
                        "email": "18575579612@qq.com",
                        "gender": 1,
                        "age": 34,
                        "address": "",
                        "nativePlace": "",
                        "selfEvaluation": "10年多MES、WMS、SRM、供应链、ERP企业数字化转型系统开发、运维经验，6年多项目管理经验，带领过3-5人的团队，能够准确把握项目进度，合理分配资源，确保项目按时交付，语言主要使用Java。擅长数据库设计，系统优化，系统分析，开发。持有Prompt提示词工程师、AI微调工程师、AI智能体工程师等专业证书，参与过“总经理驾驶舱”、FineBI等系统的开发工作。",
                        "expectedSalary": "25-30K",
                        "expectedPosition": ""
                    },
                    "educations": [
                        {
                            "school": "江西理工大学",
                            "major": "软件工程",
                            "degree": "学士",
                            "education": "本科",
                            "startDate": "2010-09",
                            "endDate": "2014-07"
                        }
                    ],
                    "works": [
                        {
                            "company": "深圳市道通科技股份有限公司",
                            "position": "高级java开发工程师",
                            "description": "主导MES&WMS、SRM、Salesforce等企业级系统的Java/Python后端设计开发；实施MySQL查询优化计划，设计多级缓存架构；构建海外公有云AWS Generative Chat BI系统，开发FineBI增强分析模块；攻克MES&WMS系统数据同步异常等技术难题，重构WMS库存管理模块；主导SRM系统智能化改造，开发库存预测模型；设计基于事件驱动的企业级集成中台，实现MES与设备IoT平台的无缝集成。",
                            "startDate": "2023-07",
                            "endDate": "2025-12"
                        },
                        {
                            "company": "金蝶软件（中国）有限公司",
                            "position": "java高级软件工程师",
                            "description": "负责供应链系统的整体架构设计，确保系统的稳定性、可扩展性和可维护性；参与详细设计文档的编写，明确系统各模块的功能、接口及数据交互方式；使用Java及相关技术栈进行系统的开发实现，确保代码质量、性能及安全性；负责供应链系统中关键模块库存管理、采购管理的开发；对现有模块进行维护和优化，修复系统中的缺陷，提升系统性能；根据业务需求，对系统进行迭代开发，实现新功能或改进现有功能；参与系统的测试工作，包括单元测试、集成测试和系统测试，确保软件质量；协助测试团队定位问题，提供技术支持和解决方案；负责系统的部署和上线工作，确保系统平稳运行；编写和更新系统相关的技术文档，包括接口文档、用户手册等；为其他部门或客户提供技术支持和培训，解答技术问题；跟踪行业动态和技术发展趋势，为系统的升级和改进提供建议。",
                            "startDate": "2020-07",
                            "endDate": "2023-07"
                        },
                        {
                            "company": "中软国际有限公司",
                            "position": "MES开发工程师",
                            "description": "参与制造执行系统需求分析、MES系统技术总体框架设计、接口标准设计、数据库表结构设计；负责MES系统的软件开发，包括：核心代码开发、新需求开发、系统异常运维；对其他开发工程师的代码进行审核；参与完成系统测试（单元测试、压力测试）；参与与其业务相关的需求变更评审；负责系统部署、系统版本更新，解决项目中的关键问题和技术难题；负责MES开发的开发标准拟定和管理，负责代码版本管理，负责技术文档的撰写，包括：数据库表结构设计文档、开发设计文档等。",
                            "startDate": "2017-11",
                            "endDate": "2020-07"
                        },
                        {
                            "company": "宝明科技有限公司",
                            "position": "MES运维工程师",
                            "description": "制造执行系统（MES）、工业自动化软件项目实施；组织业务部门探讨业务需求，根据需求分析，提出完整的技术解决方案；根据项目计划，完成项目交付；编制SOP项目文档、手册，对用户进行相关培训；阅读SpringMVC系统源码，解决系统运行过程中出现的问题；数据库建模设计、存储过程编写、SQL语句优化。",
                            "startDate": "2014-07",
                            "endDate": "2017-11"
                        }
                    ],
                    "projects": [
                        {
                            "company": "深圳市道通科技股份有限公司",
                            "projectName": "供应链SRM-SAP-WMS急料配置与集成项目",
                            "role": "高级Java后端开发",
                            "description": "本项目聚焦于供应链中急料标识的配置与多系统（SRM、SAP、WMS）间的数据集成，旨在通过自动化同步急料信息、优化跨系统协作流程，提升供应链对急料需求的响应速度和准确性。项目核心包括SRM系统的急料规则配置、SAP工单欠料数据同步、WMS送货单与SAP急料标识集成，以及SRM送货通知单的急料可视化功能开发，确保采购、生产、仓储等环节高效协同。项目技术：Spring Boot + Spring Cloud + MyBatis-Plus + MySQL + Redis + SAP RFC接口 + Restful API + Logback。项目职责：1. 参与供应链急料配置需求评审，对齐业务部门优先级规则；2. 负责SRM系统中深圳电子料急料配置规则的开发与维护；3. 开发SRM与SAP的接口，实现深圳已开工单欠料数据的实时同步与急料标识触发逻辑；4. 设计WMS与SAP的急料配置同步机制，确保送货单自动携带急料标识；5. 主导SRM送货通知单的急料标识前端展示功能开发；6. 制定跨系统数据交互规范，优化接口性能与异常处理机制；7. 完成单元测试及集成测试，保障多系统数据一致性与稳定性。",
                            "startDate": "2024-10",
                            "endDate": "2024-12"
                        },
                        {
                            "company": "深圳市道通科技股份有限公司",
                            "projectName": "供应链BOM展开特性",
                            "role": "高级Java开发",
                            "description": "主要负责BOM（Bill of Materials，物料清单）的展开算法设计、系统实现及优化工作。BOM展开是供应链系统中的关键环节，它涉及到物料需求的精准计算、生产计划的合理安排以及库存的有效管理。项目技术:Springboot+Spring+SpringCloud+Mybatis-plus+Druid+Apache Shiro+JWT+Quartz+Springfox-swagger+Logback +Redis+Mysql。1、参与供应链BOM展开特性需求评审；2、参与制造跨部门流程协作、沟通；3、负责制造BOM列表查询开发工作；4、负责BOM接口开发，定义了与BOM展开功能相关的规范；5、负责BOM展开级次开发工作；6、负责BOM展开子项权限鉴定开发工作；7、负责单元测试和集成测试，确保代码的质量和稳定性。",
                            "startDate": "2022-07",
                            "endDate": "2022-12"
                        },
                        {
                            "company": "金蝶软件（中国）有限公司",
                            "projectName": "MES自动化运维工具",
                            "role": "开发",
                            "description": "使用SSM框架读取邮箱用户发来的请求，匹配系统中已有模板，自动处理并回复用户邮件。开发工具：Eclipse+Oracle+Tomcat8.0+JDK1.8+SVN+MAVEN。项目职责：主要参与需求分析、业务模块的划分、Bug修复。",
                            "startDate": "2019-12",
                            "endDate": "2020-06"
                        },
                        {
                            "company": "金蝶软件（中国）有限公司",
                            "projectName": "终端MES大数据",
                            "role": "开发",
                            "description": "为方便终端海量出货数据获取、存储、管理、分析、提升用户响应能力及更优质的服务。项目职责：使用PL/SQL Developer 13工具开发Oracle 11g数据库。1、发货数据状态表、包装表、发货表、履历表数据采集；2、发货数据状态表、包装表、发货表、履历表数据采集对应的表模型、结构设计；3、从MES系统状态表、包装表、发货表、履历表按时间插入对应归档数据表并删除存储过程编写；4、从归档状态表、包装表、发货表、履历表按条码插入对应当前业务数据表并删除存储过程编写；5、定时任务部署。",
                            "startDate": "2019-06",
                            "endDate": "2019-12"
                        },
                        {
                            "company": "金蝶软件（中国）有限公司",
                            "projectName": "制造MES与WMS系统集成",
                            "role": "开发",
                            "description": "使用PL/SQLDeveloper13工具开发Oracle11g数据库。项目职责：1、仓储管理系统头表、包装表、发货表、库位表数据采集；2、仓储管理系统头表、包装表、发货表、库位表数据采集对应的表模型、结构设计；3、从MES系统头表、包装表、发货表插入仓储管理系统数据齐套判断存储过程编写；4、从MES系统获取库位函数编写；5、定时任务部署。",
                            "startDate": "2018-10",
                            "endDate": "2019-05"
                        },
                        {
                            "company": "金蝶软件（中国）有限公司",
                            "projectName": "Spring研发装备型号添加系统",
                            "role": "开发",
                            "description": "该项目使用Eclipse工具，基于MVC模式开发的研发装备型号添加系统。项目职责：参与总体设计，代码编写，测试。",
                            "startDate": "2018-02",
                            "endDate": "2018-09"
                        }
                    ]
                }
            }
        }
    ]
}
```

**失败时单条结果：**

```json
{
  "code": 1,
  "msg": "无法获取 PDF，请检查 type/path/content 字段",
  "data": {
    "filename": "b.pdf",
    "extra": null,
    "errorMsg": "无法获取 PDF，请检查 type/path/content 字段"
  }
}
```

---

## 六D、接口 06C — 同步批量仅打标

```
POST /api/resume/sync/tag
Content-Type: application/json
```

**区别于接口 06（sync/batch）**：只执行打标（LLM 正向标签 + 规则负面检测），**跳过 PDF 解析**，耗时约 5~20s/份。

`resumes` 数组每项结构与接口 06B（sync/parse）响应的 `results[].data` 字段**完全一致**，可直接将解析接口的 `results` 数组透传，无需任何转换。输出格式与接口 06（sync/batch）完全相同。

### Postman 操作

1. 先调用接口 06B，获取解析结果
2. 将解析结果的 `results` 数组中各项 `data` 字段组成新数组，作为本接口的 `resumes` 传入
3. Body → **raw** → 右侧下拉选 **JSON**，粘贴以下请求体

### 请求体

```json
{
  "resumes": [
    {
      "filename": "java开发工程师_深圳 18-26K谭志兵 7年.pdf",
      "extra": null,
      "resumeParseFinishTime": "2026-06-11 09:47:41",
      "resume": {
        "candidate": {
          "name": "谭志兵",
          "phone": "18575579612",
          "email": "18575579612@163.com",
          "gender": 1,
          "age": 29,
          "address": "",
          "nativePlace": "",
          "selfEvaluation": "了解计算机和软件原理，了解系统性能瓶颈，并能进行系统性能分析和优化；了解设计原则、设计模式，了解OOD和DDD，具备抽象和分层能力，能设计开发可复用的优质代码；了解技术 债务问题，了解知识管理和代码管理；有丰富的业务面和技术面，能用宏观和架构的视角看待问题；了解数仓和大数据，了解数据分析、机器学习和LLM，能用模型和数据的视角看待问题。",
          "expectedSalary": "",
          "expectedPosition": "Java工程师"
        },
        "educations": [
          {
            "school": "齐齐哈尔大学",
            "major": "机械电子工程",
            "degree": "学士",
            "education": "本科",
            "startDate": "2014-09",
            "endDate": "2018-07"
          }
        ],
        "works": [
          {
            "company": "东莞怡合达自动化股份有限公司",
            "position": "Java工程师",
            "description": "负责ERP日常功能维护及外围服务迁移合并。使用SpringCloudAlibaba、SpringBoot、MyBatis、VUE等框架，结合Redis、RocketMQ、Nacos等中间件，解决Spring Bean重名问题，实现反 向代理处理多租户、多地域、多数据源代码拆分。负责MES系统维护和升级，使用责任链模式和管道处理复杂业务规则，利用TtlExecutors和TaskExecutor异步处理多平台同步业务，确保数据一致性。",
            "startDate": "2025-06",
            "endDate": "2025-11"
          },
          {
            "company": "中航工业通飞华南飞机工业有限公司",
            "position": "Java工程师",
            "description": "参与珠海中航通飞项目，负责学习和梳理成飞代码以及新业务，根据业务需求完成模块重构、优化及新功能开发，对接其他系统进行接口联调。使用SpringCloudAlibaba、SpringBoot、M yBatis、EasyUI等框架，优化采购订单模块代码架构，消除冗余逻辑，提升代码可维护性与可读性。",
            "startDate": "2024-02",
            "endDate": "2025-04"
          },
          {
            "company": "北京瑞天欣实数据科技有限公司",
            "position": "Java工程师",
            "description": "在专注于银行风控的数据公司工作，负责数据清洗、提取、标准化与归一化处理，完成特征开发和测试，协助模型构建、训练、测试及调整。设计开发Excel+VBA自动化验算工具，利用Py thon优化数据处理流程，基于Dataworks设计自动化数据清洗流程，提升数据可用性和模型开发效率。",
            "startDate": "2021-05",
            "endDate": "2024-02"
          },
          {
            "company": "成都鸿业远图科技有限公司",
            "position": "Java工程师",
            "description": "参与江西社保、成达停车场、金堂消费贷、金堂商户贷等项目，学习Uniapp、VUE及微信支付宝SDK开发使用，完成项目和模块的设计开发，进行压力测试及业务测试。基于组件化与模块 化思想设计开发社保缴费平台，采用SSL加密传输、JWT认证及数字签名验签机制保障通信安全，实现支付状态异步校验机制确保交易一致性。",
            "startDate": "2019-04",
            "endDate": "2021-04"
          },
          {
            "company": "四川集创科技有限责任公司",
            "position": "Java工程师",
            "description": "参与德阳、达州、成都高速三支队项目，负责智能交通管理系统开发，集成GIS、JavaEE、实时地图、监控卡口等功能，实现交通违法行为实时监控和处理。开发接口和对接调试前端页面 ，完成功能模块开发和自测，修复bug，根据客户要求对项目进行维护和功能改造。",
            "startDate": "2018-09",
            "endDate": "2019-03"
          }
        ],
        "projects": [
          {
            "company": "东莞怡合达自动化股份有限公司",
            "projectName": "怡合达ERP项目",
            "role": "Java工程师",
            "description": "负责ERP日常功能维护及外围服务迁移合并。技术栈：JavaEE、ERP、SaaS；开发框架：SpringCloudAlibaba, SpringBoot, MyBatis, VUE；中间件：Redis, Tomcat, Maven, Git, Nginx, MySQL, SQLServer, Nacos；其他技术：TtlExecutors, XXLJob, RocketMQ。",
            "startDate": "2025-06",
            "endDate": "2025-11"
          },
          {
            "company": "北京瑞天欣实数据科技有限公司",
            "projectName": "四川农信项目、重庆农商信用卡项目、南昌农商项目、顺德农商供应链项目",
            "role": "Java工程师",
            "description": "在专注于银行风控的数据公司工作，负责数据清洗、特征开发和模型协助。技术栈：评分卡模型、决策引擎、JavaEE；中间件：Python, VBA, Excel, Dataworks, Hive/Hadoop。",
            "startDate": "2021-05",
            "endDate": "2024-02"
          }
        ]
      }
    },
    {
      "filename": "杨强华.pdf",
      "extra": null,
      "resumeParseFinishTime": "2026-06-11 09:47:44",
      "resume": {
        "candidate": {
          "name": "杨强华",
          "phone": "18665937875",
          "email": "yangqianghuayiyi@163.com",
          "gender": 1,
          "age": 0,
          "address": "",
          "nativePlace": "湖南娄底",
          "selfEvaluation": "多年 FPGA 开发的工作经历，熟悉 Xilinx，altera 等公司的 FPGA 器件。可以加班。",
          "expectedSalary": "",
          "expectedPosition": ""
        },
        "educations": [
          {
            "school": "电子科技大学",
            "major": "通信与信息工程",
            "degree": "硕士",
            "education": "硕士研究生",
            "startDate": "2013-09",
            "endDate": "2016-07"
          },
          {
            "school": "电子科技大学",
            "major": "通信与信息工程",
            "degree": "学士",
            "education": "本科",
            "startDate": "2009-09",
            "endDate": "2013-07"
          }
        ],
        "works": [
          {
            "company": "深圳市塞防科技有限公司",
            "position": "高级 FPGA 工程师",
            "description": "负责无人机侦测与打击产品的 FPGA 开发，包括 9361 到 FPGA 的接口解调模块、ZC 序列相关运算模块、数据打包模块、干扰信号生成与 IFFT 计算等。涉及 ad9361+MPSOC 架构，使用 URAM、DDR 缓存，实现信号解调、定位计算及上报终端。",
            "startDate": "2024-08",
            "endDate": "2025-01"
          },
          {
            "company": "深圳市雷赛智能控制股份有限公司",
            "position": "高级 FPGA 工程师",
            "description": "负责伺服系统从机平台开发，实现 MCU 与 FPGA 通信接口、位置比较功能、多编码器通信协议的 FPGA 实现。完成 EtherCAT 协议 FPGA 实现、MPSOC 异构平台 10GEthernet SFP+  接口开发、MIPI 摄像头接入系统、Artix7/ZYNQ7000 系列芯片接口与 ADC 数据采集系统开发。",
            "startDate": "2020-04",
            "endDate": "2024-08"
          },
          {
            "company": "深圳市太赫兹科学技术创新研究院",
            "position": "信号处理算法工程",
            "description": "负责 2Gbps 高速通信解调原型样机研制，完成免混频下变频器、频域匹配滤波、高速并行定时同步、高速并行均衡、高速并行载波同步等算法的 MATLAB 仿真与 FPGA 实现。参与  E-Band 微波通信设备研发及导弹通讯设备中 RS 码与 LDPC 码编码译码模块的 FPGA 实现。",
            "startDate": "2016-07",
            "endDate": "2020-07"
          },
          {
            "company": "华为成都研究所",
            "position": "无线通信 FPGA 助理工程师（实习）",
            "description": "参与华为 5G 高频基带 LDPC 编码译码项目，负责高吞吐率编译码算法的开发及 FPGA 实现。改进 Off-set Min-Sum 算法提升吞吐率，实现 6Gbps 吞吐率。",
            "startDate": "2014-07",
            "endDate": "2015-08"
          }
        ],
        "projects": [
          {
            "company": "深圳市塞防科技有限公司",
            "projectName": "无人机侦测平台 tracer 产品开发",
            "role": "高级 FPGA 工程师",
            "description": "无人机侦测产品线，ad9361+MPSOC 架构，负责 9361 到 FPGA 的接口解调模块、ZC 序列相关运算模块、数据打包模块。",
            "startDate": "2024-08",
            "endDate": "2025-01"
          },
          {
            "company": "深圳市雷赛智能控制股份有限公司",
            "projectName": "EtherCAT 协议的 FPGA 实现",
            "role": "高级 FPGA 工程师",
            "description": "基于紫光 logos 系列 PGL22 FPGA 做 EtherCAT 通信协议的 FPGA 实现，完成竞品分析、方案设计、仿真验证及产品化工作。自研方案 14K LUT，RAM 4K。",
            "startDate": "2020-04",
            "endDate": "2024-08"
          }
        ]
      }
    },
    {
      "filename": "刘江波 10年以上.pdf",
      "extra": null,
      "resumeParseFinishTime": "2026-06-11 09:46:53",
      "resume": {
        "candidate": {
          "name": "刘江波",
          "phone": "18575579612",
          "email": "18575579612@qq.com",
          "gender": 1,
          "age": 34,
          "address": "",
          "nativePlace": "",
          "selfEvaluation": "10年多MES、WMS、SRM、供应链、ERP企业数字化转型系统开发、运维经验，6年多项目管理经验，带领过3-5人的团队，能够准确把握项目进度，合理分配资源，确保项目按时交付，语言 主要使用Java。持有Prompt提示词工程师、AI微调工程师、AI智能体工程师等专业证书。",
          "expectedSalary": "25-30K",
          "expectedPosition": ""
        },
        "educations": [
          {
            "school": "江西理工大学",
            "major": "软件工程",
            "degree": "学士",
            "education": "本科",
            "startDate": "2010-09",
            "endDate": "2014-07"
          }
        ],
        "works": [
          {
            "company": "深圳市道通科技股份有限公司",
            "position": "高级java开发工程师",
            "description": "主导MES&WMS、SRM、Salesforce等企业级系统的Java/Python后端设计开发；实施MySQL查询优化计划，设计多级缓存架构；构建海外公有云AWS Generative Chat  BI系统，开发FineBI增强分析模块；主导SRM系统智能化改造，开发库存预测模型；设计基于事件驱动的企业级集成中台，实现MES与设备IoT平台的无缝集成。",
            "startDate": "2023-07",
            "endDate": "2025-12"
          },
          {
            "company": "金蝶软件（中国）有限公司",
            "position": "java高级软件工程师",
            "description": "负责供应链系统的整体架构设计，确保系统的稳定性、可扩展性和可维护性；负责供应链系统中关键模块库存管理、采购管理的开发；对现有模块进行维护和优化，修复系统中的缺陷， 提升系统性能；参与系统的测试工作，包括单元测试、集成测试和系统测试。",
            "startDate": "2020-07",
            "endDate": "2023-07"
          },
          {
            "company": "中软国际有限公司",
            "position": "MES开发工程师",
            "description": "参与制造执行系统需求分析、MES系统技术总体框架设计、接口标准设计、数据库表结构设计；负责MES系统的软件开发，包括核心代码开发、新需求开发、系统异常运维；负责系统部署 、系统版本更新，解决项目中的关键问题和技术难题。",
            "startDate": "2017-11",
            "endDate": "2020-07"
          },
          {
            "company": "宝明科技有限公司",
            "position": "MES运维工程师",
            "description": "制造执行系统（MES）、工业自动化软件项目实施；组织业务部门探讨业务需求，根据需求分析提出完整的技术解决方案；数据库建模设计、存储过程编写、SQL语句优化。",
            "startDate": "2014-07",
            "endDate": "2017-11"
          }
        ],
        "projects": [
          {
            "company": "深圳市道通科技股份有限公司",
            "projectName": "供应链SRM-SAP-WMS急料配置与集成项目",
            "role": "高级Java后端开发",
            "description": "聚焦于供应链中急料标识的配置与多系统（SRM、SAP、WMS）间的数据集成，提升供应链对急料需求的响应速度和准确性。技术：Spring Boot + Spring Cloud + MyBatis-Plus + MySQL  + Redis + SAP RFC接口。",
            "startDate": "2024-10",
            "endDate": "2024-12"
          },
          {
            "company": "金蝶软件（中国）有限公司",
            "projectName": "供应链BOM展开特性",
            "role": "高级Java开发",
            "description": "负责BOM展开算法设计、系统实现及优化工作。技术：Springboot + Spring Cloud + Mybatis-plus + Redis + MySQL。",
            "startDate": "2022-07",
            "endDate": "2022-12"
          },
          {
            "company": "金蝶软件（中国）有限公司",
            "projectName": "终端MES大数据",
            "role": "开发",
            "description": "为方便终端海量出货数据获取、存储、管理、分析，使用PL/SQL Developer工具开发Oracle数据库，完成发货数据采集、表模型设计及定时任务部署。",
            "startDate": "2019-06",
            "endDate": "2019-12"
          }
        ]
      }
    }
  ]
}

```

### 请求字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `resumes` | Array | ✅ | 简历列表，每项为接口 06B 的 `results[].data`，单次上限 50 份 |
| `resumes[].resume` | object | ✅ | 结构化简历 JSON（接口 06B 的 `data.resume` 字段） |
| `resumes[].filename` | string | ✗ | 文件名，用于日志标识 |
| `resumes[].extra` | string | ✗ | 透传字段，原样返回 |
| `resumes[].resumeParseFinishTime` | string | ✗ | 解析完成时间，原样写入输出 `result.resumeParseFinishTime` |
| `jobTitle` | string | ✗ | 目标岗位名称 |
| `requiredSkills` | string | ✗ | 必要技能，逗号分隔 |
| `jobDescription` | string | ✗ | JD 全文 |
| `jobSensitiveWords` | string | ✗ | 自定义敏感词，逗号/分号分隔 |
| `max_concurrency` | int | ✗ | 并发数上限 1~30，默认 MAX_WORKERS |

### 响应格式

与接口 06（sync/batch）**完全一致**，`data.result` 包含解析字段 + 正负向标签完整 v3.1 JSON：

```json
{
    "total": 3,
    "success": 3,
    "failed": 0,
    "elapsed_s": 9.25,
    "results": [
        {
            "code": 0,
            "msg": "success",
            "data": {
                "taskId": "e5f73008-f989-4657-a8b4-7f07686042b2",
                "status": 3,
                "statusDesc": "任务完成",
                "elapsedSeconds": 8.8,
                "estimatedSeconds": 90,
                "extra": null,
                "result": {
                    "taskId": "e5f73008-f989-4657-a8b4-7f07686042b2",
                    "resumeParseFinishTime": "2026-06-11 09:47:41",
                    "tagParseFinishTime": "2026-06-11 09:54:43",
                    "candidate": {
                        "name": "谭志兵",
                        "phone": "18575579612",
                        "email": "18575579612@163.com",
                        "gender": 1,
                        "age": 29,
                        "address": "",
                        "nativePlace": "",
                        "selfEvaluation": "了解计算机和软件原理，了解系统性能瓶颈，并能进行系统性能分析和优化；了解设计原则、设计模式，了解OOD和DDD，具备抽象和分层能力，能设计开发可复用的优质代码；了解技术 债务问题，了解知识管理和代码管理；有丰富的业务面和技术面，能用宏观和架构的视角看待问题；了解数仓和大数据，了解数据分析、机器学习和LLM，能用模型和数据的视角看待问题。",
                        "expectedSalary": "",
                        "expectedPosition": "Java工程师"
                    },
                    "educations": [
                        {
                            "school": "齐齐哈尔大学",
                            "major": "机械电子工程",
                            "degree": "学士",
                            "education": "本科",
                            "startDate": "2014-09",
                            "endDate": "2018-07"
                        }
                    ],
                    "works": [
                        {
                            "company": "东莞怡合达自动化股份有限公司",
                            "position": "Java工程师",
                            "description": "负责ERP日常功能维护及外围服务迁移合并。使用SpringCloudAlibaba、SpringBoot、MyBatis、VUE等框架，结合Redis、RocketMQ、Nacos等中间件，解决Spring Bean重名问题，实现反 向代理处理多租户、多地域、多数据源代码拆分。负责MES系统维护和升级，使用责任链模式和管道处理复杂业务规则，利用TtlExecutors和TaskExecutor异步处理多平台同步业务，确保数据一致性。",
                            "startDate": "2025-06",
                            "endDate": "2025-11"
                        },
                        {
                            "company": "中航工业通飞华南飞机工业有限公司",
                            "position": "Java工程师",
                            "description": "参与珠海中航通飞项目，负责学习和梳理成飞代码以及新业务，根据业务需求完成模块重构、优化及新功能开发，对接其他系统进行接口联调。使用SpringCloudAlibaba、SpringBoot、M yBatis、EasyUI等框架，优化采购订单模块代码架构，消除冗余逻辑，提升代码可维护性与可读性。",
                            "startDate": "2024-02",
                            "endDate": "2025-04"
                        },
                        {
                            "company": "北京瑞天欣实数据科技有限公司",
                            "position": "Java工程师",
                            "description": "在专注于银行风控的数据公司工作，负责数据清洗、提取、标准化与归一化处理，完成特征开发和测试，协助模型构建、训练、测试及调整。设计开发Excel+VBA自动化验算工具，利用Py thon优化数据处理流程，基于Dataworks设计自动化数据清洗流程，提升数据可用性和模型开发效率。",
                            "startDate": "2021-05",
                            "endDate": "2024-02"
                        },
                        {
                            "company": "成都鸿业远图科技有限公司",
                            "position": "Java工程师",
                            "description": "参与江西社保、成达停车场、金堂消费贷、金堂商户贷等项目，学习Uniapp、VUE及微信支付宝SDK开发使用，完成项目和模块的设计开发，进行压力测试及业务测试。基于组件化与模块 化思想设计开发社保缴费平台，采用SSL加密传输、JWT认证及数字签名验签机制保障通信安全，实现支付状态异步校验机制确保交易一致性。",
                            "startDate": "2019-04",
                            "endDate": "2021-04"
                        },
                        {
                            "company": "四川集创科技有限责任公司",
                            "position": "Java工程师",
                            "description": "参与德阳、达州、成都高速三支队项目，负责智能交通管理系统开发，集成GIS、JavaEE、实时地图、监控卡口等功能，实现交通违法行为实时监控和处理。开发接口和对接调试前端页面 ，完成功能模块开发和自测，修复bug，根据客户要求对项目进行维护和功能改造。",
                            "startDate": "2018-09",
                            "endDate": "2019-03"
                        }
                    ],
                    "projects": [
                        {
                            "company": "东莞怡合达自动化股份有限公司",
                            "projectName": "怡合达ERP项目",
                            "role": "Java工程师",
                            "description": "负责ERP日常功能维护及外围服务迁移合并。技术栈：JavaEE、ERP、SaaS；开发框架：SpringCloudAlibaba, SpringBoot, MyBatis, VUE；中间件：Redis, Tomcat, Maven, Git, Nginx, MySQL, SQLServer, Nacos；其他技术：TtlExecutors, XXLJob, RocketMQ。",
                            "startDate": "2025-06",
                            "endDate": "2025-11"
                        },
                        {
                            "company": "北京瑞天欣实数据科技有限公司",
                            "projectName": "四川农信项目、重庆农商信用卡项目、南昌农商项目、顺德农商供应链项目",
                            "role": "Java工程师",
                            "description": "在专注于银行风控的数据公司工作，负责数据清洗、特征开发和模型协助。技术栈：评分卡模型、决策引擎、JavaEE；中间件：Python, VBA, Excel, Dataworks, Hive/Hadoop。",
                            "startDate": "2021-05",
                            "endDate": "2024-02"
                        }
                    ],
                    "positiveTags": {
                        "tagsArray": [
                            {
                                "dimension": "education",
                                "displayName": "学历背景",
                                "value": [
                                    {
                                        "tag": "本科",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "workYears",
                                "displayName": "工作年限",
                                "value": [
                                    {
                                        "tag": "5-10年",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "skills",
                                "displayName": "技能标签",
                                "confidence": 0.9,
                                "value": [
                                    {
                                        "tag": "Java",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "SpringCloudAlibaba",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "SpringBoot",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "MyBatis",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Redis",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "RocketMQ",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Nacos",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Vue",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Python",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "数据清洗",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "机器学习",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "大数据",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "设计模式",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "DDD",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "系统性能优化",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "companyBg",
                                "displayName": "公司背景",
                                "confidence": 0.9,
                                "value": [
                                    {
                                        "tag": "国企/央企",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "industry",
                                "displayName": "行业背景",
                                "confidence": 0.9,
                                "value": [
                                    {
                                        "tag": "金融科技",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "企业服务/SaaS",
                                        "source": "ai"
                                    }
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
            }
        },
        {
            "code": 0,
            "msg": "success",
            "data": {
                "taskId": "36098285-da59-4367-92c4-4b6d688c7392",
                "status": 3,
                "statusDesc": "任务完成",
                "elapsedSeconds": 9.2,
                "estimatedSeconds": 90,
                "extra": null,
                "result": {
                    "taskId": "36098285-da59-4367-92c4-4b6d688c7392",
                    "resumeParseFinishTime": "2026-06-11 09:47:44",
                    "tagParseFinishTime": "2026-06-11 09:54:43",
                    "candidate": {
                        "name": "杨强华",
                        "phone": "18665937875",
                        "email": "yangqianghuayiyi@163.com",
                        "gender": 1,
                        "age": 0,
                        "address": "",
                        "nativePlace": "湖南娄底",
                        "selfEvaluation": "多年 FPGA 开发的工作经历，熟悉 Xilinx，altera 等公司的 FPGA 器件。可以加班。",
                        "expectedSalary": "",
                        "expectedPosition": ""
                    },
                    "educations": [
                        {
                            "school": "电子科技大学",
                            "major": "通信与信息工程",
                            "degree": "硕士",
                            "education": "硕士研究生",
                            "startDate": "2013-09",
                            "endDate": "2016-07"
                        },
                        {
                            "school": "电子科技大学",
                            "major": "通信与信息工程",
                            "degree": "学士",
                            "education": "本科",
                            "startDate": "2009-09",
                            "endDate": "2013-07"
                        }
                    ],
                    "works": [
                        {
                            "company": "深圳市塞防科技有限公司",
                            "position": "高级 FPGA 工程师",
                            "description": "负责无人机侦测与打击产品的 FPGA 开发，包括 9361 到 FPGA 的接口解调模块、ZC 序列相关运算模块、数据打包模块、干扰信号生成与 IFFT 计算等。涉及 ad9361+MPSOC 架构，使用 URAM、DDR 缓存，实现信号解调、定位计算及上报终端。",
                            "startDate": "2024-08",
                            "endDate": "2025-01"
                        },
                        {
                            "company": "深圳市雷赛智能控制股份有限公司",
                            "position": "高级 FPGA 工程师",
                            "description": "负责伺服系统从机平台开发，实现 MCU 与 FPGA 通信接口、位置比较功能、多编码器通信协议的 FPGA 实现。完成 EtherCAT 协议 FPGA 实现、MPSOC 异构平台 10GEthernet SFP+  接口开发、MIPI 摄像头接入系统、Artix7/ZYNQ7000 系列芯片接口与 ADC 数据采集系统开发。",
                            "startDate": "2020-04",
                            "endDate": "2024-08"
                        },
                        {
                            "company": "深圳市太赫兹科学技术创新研究院",
                            "position": "信号处理算法工程",
                            "description": "负责 2Gbps 高速通信解调原型样机研制，完成免混频下变频器、频域匹配滤波、高速并行定时同步、高速并行均衡、高速并行载波同步等算法的 MATLAB 仿真与 FPGA 实现。参与  E-Band 微波通信设备研发及导弹通讯设备中 RS 码与 LDPC 码编码译码模块的 FPGA 实现。",
                            "startDate": "2016-07",
                            "endDate": "2020-07"
                        },
                        {
                            "company": "华为成都研究所",
                            "position": "无线通信 FPGA 助理工程师（实习）",
                            "description": "参与华为 5G 高频基带 LDPC 编码译码项目，负责高吞吐率编译码算法的开发及 FPGA 实现。改进 Off-set Min-Sum 算法提升吞吐率，实现 6Gbps 吞吐率。",
                            "startDate": "2014-07",
                            "endDate": "2015-08"
                        }
                    ],
                    "projects": [
                        {
                            "company": "深圳市塞防科技有限公司",
                            "projectName": "无人机侦测平台 tracer 产品开发",
                            "role": "高级 FPGA 工程师",
                            "description": "无人机侦测产品线，ad9361+MPSOC 架构，负责 9361 到 FPGA 的接口解调模块、ZC 序列相关运算模块、数据打包模块。",
                            "startDate": "2024-08",
                            "endDate": "2025-01"
                        },
                        {
                            "company": "深圳市雷赛智能控制股份有限公司",
                            "projectName": "EtherCAT 协议的 FPGA 实现",
                            "role": "高级 FPGA 工程师",
                            "description": "基于紫光 logos 系列 PGL22 FPGA 做 EtherCAT 通信协议的 FPGA 实现，完成竞品分析、方案设计、仿真验证及产品化工作。自研方案 14K LUT，RAM 4K。",
                            "startDate": "2020-04",
                            "endDate": "2024-08"
                        }
                    ],
                    "positiveTags": {
                        "tagsArray": [
                            {
                                "dimension": "education",
                                "displayName": "学历背景",
                                "value": [
                                    {
                                        "tag": "985",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "211",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "双一流",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "硕士",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "workYears",
                                "displayName": "工作年限",
                                "value": [
                                    {
                                        "tag": "5-10年",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "skills",
                                "displayName": "技能标签",
                                "confidence": 0.93,
                                "value": [
                                    {
                                        "tag": "FPGA",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Xilinx",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Altera",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "ZYNQ",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "MPSOC",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "EtherCAT",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "AD9361",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "信号处理",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "LDPC",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "MATLAB",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "DDR",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "MIPI",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "10G Ethernet",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "companyBg",
                                "displayName": "公司背景",
                                "confidence": 0.95,
                                "value": [
                                    {
                                        "tag": "头部大厂",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "industry",
                                "displayName": "行业背景",
                                "confidence": 0.9,
                                "value": [
                                    {
                                        "tag": "物联网/硬件",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "人工智能",
                                        "source": "ai"
                                    }
                                ]
                            }
                        ]
                    },
                    "negativeTags": {
                        "hitCount": 2,
                        "deductionScore": -25,
                        "tagsArray": [
                            {
                                "dimension": "jobstable",
                                "displayName": "职业稳定性风险",
                                "value": [
                                    {
                                        "tag": "工作空窗期过长",
                                        "severity": "low",
                                        "matchedContent": "华为成都研究所 离职后至 深圳市太赫兹科学技术创新研究院 入职，空窗约11个月",
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
                                        "matchedContent": "深圳市太赫兹科学技术创新研究院(2016-07~2020-07) 与 深圳市雷赛智能控制股份有限公司(2020-04~2024-08) 时间重叠3个月",
                                        "source": "ai"
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        },
        {
            "code": 0,
            "msg": "success",
            "data": {
                "taskId": "aa1a472e-0f63-4e87-bd9a-7add0a75d5bb",
                "status": 3,
                "statusDesc": "任务完成",
                "elapsedSeconds": 9.2,
                "estimatedSeconds": 90,
                "extra": null,
                "result": {
                    "taskId": "aa1a472e-0f63-4e87-bd9a-7add0a75d5bb",
                    "resumeParseFinishTime": "2026-06-11 09:46:53",
                    "tagParseFinishTime": "2026-06-11 09:54:43",
                    "candidate": {
                        "name": "刘江波",
                        "phone": "18575579612",
                        "email": "18575579612@qq.com",
                        "gender": 1,
                        "age": 34,
                        "address": "",
                        "nativePlace": "",
                        "selfEvaluation": "10年多MES、WMS、SRM、供应链、ERP企业数字化转型系统开发、运维经验，6年多项目管理经验，带领过3-5人的团队，能够准确把握项目进度，合理分配资源，确保项目按时交付，语言 主要使用Java。持有Prompt提示词工程师、AI微调工程师、AI智能体工程师等专业证书。",
                        "expectedSalary": "25-30K",
                        "expectedPosition": ""
                    },
                    "educations": [
                        {
                            "school": "江西理工大学",
                            "major": "软件工程",
                            "degree": "学士",
                            "education": "本科",
                            "startDate": "2010-09",
                            "endDate": "2014-07"
                        }
                    ],
                    "works": [
                        {
                            "company": "深圳市道通科技股份有限公司",
                            "position": "高级java开发工程师",
                            "description": "主导MES&WMS、SRM、Salesforce等企业级系统的Java/Python后端设计开发；实施MySQL查询优化计划，设计多级缓存架构；构建海外公有云AWS Generative Chat  BI系统，开发FineBI增强分析模块；主导SRM系统智能化改造，开发库存预测模型；设计基于事件驱动的企业级集成中台，实现MES与设备IoT平台的无缝集成。",
                            "startDate": "2023-07",
                            "endDate": "2025-12"
                        },
                        {
                            "company": "金蝶软件（中国）有限公司",
                            "position": "java高级软件工程师",
                            "description": "负责供应链系统的整体架构设计，确保系统的稳定性、可扩展性和可维护性；负责供应链系统中关键模块库存管理、采购管理的开发；对现有模块进行维护和优化，修复系统中的缺陷， 提升系统性能；参与系统的测试工作，包括单元测试、集成测试和系统测试。",
                            "startDate": "2020-07",
                            "endDate": "2023-07"
                        },
                        {
                            "company": "中软国际有限公司",
                            "position": "MES开发工程师",
                            "description": "参与制造执行系统需求分析、MES系统技术总体框架设计、接口标准设计、数据库表结构设计；负责MES系统的软件开发，包括核心代码开发、新需求开发、系统异常运维；负责系统部署 、系统版本更新，解决项目中的关键问题和技术难题。",
                            "startDate": "2017-11",
                            "endDate": "2020-07"
                        },
                        {
                            "company": "宝明科技有限公司",
                            "position": "MES运维工程师",
                            "description": "制造执行系统（MES）、工业自动化软件项目实施；组织业务部门探讨业务需求，根据需求分析提出完整的技术解决方案；数据库建模设计、存储过程编写、SQL语句优化。",
                            "startDate": "2014-07",
                            "endDate": "2017-11"
                        }
                    ],
                    "projects": [
                        {
                            "company": "深圳市道通科技股份有限公司",
                            "projectName": "供应链SRM-SAP-WMS急料配置与集成项目",
                            "role": "高级Java后端开发",
                            "description": "聚焦于供应链中急料标识的配置与多系统（SRM、SAP、WMS）间的数据集成，提升供应链对急料需求的响应速度和准确性。技术：Spring Boot + Spring Cloud + MyBatis-Plus + MySQL  + Redis + SAP RFC接口。",
                            "startDate": "2024-10",
                            "endDate": "2024-12"
                        },
                        {
                            "company": "金蝶软件（中国）有限公司",
                            "projectName": "供应链BOM展开特性",
                            "role": "高级Java开发",
                            "description": "负责BOM展开算法设计、系统实现及优化工作。技术：Springboot + Spring Cloud + Mybatis-plus + Redis + MySQL。",
                            "startDate": "2022-07",
                            "endDate": "2022-12"
                        },
                        {
                            "company": "金蝶软件（中国）有限公司",
                            "projectName": "终端MES大数据",
                            "role": "开发",
                            "description": "为方便终端海量出货数据获取、存储、管理、分析，使用PL/SQL Developer工具开发Oracle数据库，完成发货数据采集、表模型设计及定时任务部署。",
                            "startDate": "2019-06",
                            "endDate": "2019-12"
                        }
                    ],
                    "positiveTags": {
                        "tagsArray": [
                            {
                                "dimension": "education",
                                "displayName": "学历背景",
                                "value": [
                                    {
                                        "tag": "本科",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "workYears",
                                "displayName": "工作年限",
                                "value": [
                                    {
                                        "tag": "10-15年",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "skills",
                                "displayName": "技能标签",
                                "confidence": 0.88,
                                "value": [
                                    {
                                        "tag": "Java",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Python",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Spring Boot",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Spring Cloud",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "MySQL",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Redis",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "MES",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "WMS",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "SRM",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "ERP",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "AWS",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "AI 微调",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Prompt 工程",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "大数据",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "Oracle",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "系统架构设计",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "项目管理",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "companyBg",
                                "displayName": "公司背景",
                                "confidence": 0.85,
                                "value": [
                                    {
                                        "tag": "传统企业转型",
                                        "source": "ai"
                                    }
                                ]
                            },
                            {
                                "dimension": "industry",
                                "displayName": "行业背景",
                                "confidence": 0.9,
                                "value": [
                                    {
                                        "tag": "企业服务/SaaS",
                                        "source": "ai"
                                    },
                                    {
                                        "tag": "物联网/硬件",
                                        "source": "ai"
                                    }
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
            }
        }
    ]
}
```

**`resume` 字段格式错误时单条结果：**

```json
{
  "code": 1,
  "msg": "resume 字段缺失或格式错误",
  "data": {
    "taskId": "xxx",
    "status": 4,
    "statusDesc": "任务失败",
    "elapsedSeconds": 0,
    "estimatedSeconds": 90,
    "extra": null,
    "errorMsg": "resume 字段缺失或格式错误"
  }
}
```

### 接口 06B → 06C 串联示意

```
POST /api/resume/sync/parse
  └─ results[0].data = {
       "filename": "a.pdf",
       "extra": "userId_001",
       "resumeParseFinishTime": "...",
       "resume": {...}
     }

直接将 results[].data 数组作为 resumes 传入：

POST /api/resume/sync/tag
  Body: {
    "resumes": [ <results[0].data>, <results[1].data>, ... ],
    "jobTitle": "证券事务代表",
    "requiredSkills": "信息披露,三会运作"
  }
```

---

## 九、接口 17 — OpenAI 兼容层（OneAPI 转发）

```
POST /v1/chat/completions
Content-Type: application/json
```

通过标准 OpenAI 格式调用，兼容 **OneAPI / NewAPI** 等代理网关转发。

> 该兼容层走同步接口模型配置，解析和打标均使用 `SYNC_MODEL_NAME=Qwen3.6-35B-A3B`；请求体中的 `model` 字段仅决定 AIHR 网关行为（仅解析 / 解析+打标），不直接切换底层 LLM。

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

**Q: 接口 06B（sync/parse）和接口 06C（sync/tag）怎么串联使用？**  
A: 先调用接口 06B 解析 PDF，拿到 `results` 数组后，将每项的 `data` 字段（含 `filename` / `extra` / `resumeParseFinishTime` / `resume`）组成新数组，作为接口 06C 的 `resumes` 字段传入即可。接口 06C 会直接在解析 JSON 上跑打标，不重复解析 PDF，耗时约 5~20s/份（仅打标阶段）。同一批解析结果可以用不同的 `jobTitle` / `requiredSkills` 多次传入接口 06C，按岗位分别打标。

**Q: 什么时候用 06B+06C，什么时候直接用 06（sync/batch）？**  
A: 需要"先解析入库、后按岗位打标"或者对同一批简历做多次不同岗位打标时用 06B+06C；只需要一次性解析+打标直接拿结果时用 06（sync/batch）更简单。

**Q: 异步接口 03A（batch/parse）+ 03B（batch/tag）与同步接口 06B+06C 有什么区别？**  
A: 两组接口实现相同的"分步"流程，区别在于等待方式。同步接口（06B+06C）阻塞等待全部完成后一次返回，适合脚本/离线批处理；异步接口（03A+03B）立即返回 taskId，客户端轮询接口 04 获取进度，适合在线业务接入和大批量并发场景。流程：先调 03A 拿到 taskId → 轮询到 status=3 后取 `data.result` → 将 `result` 数组传入 03B → 再次轮询 taskId 到 status=3 取打标完整结果。

**Q: 异步、同步和 OpenAI 兼容接口分别使用哪个模型？**
A: 异步接口 `/api/resume/async/*` 使用 `ASYNC_MODEL_NAME=Qwen3.5-397B-A17B`；同步接口 `/api/resume/sync/*` 和 OpenAI 兼容接口 `/v1/chat/completions` 使用 `SYNC_MODEL_NAME=Qwen3.6-35B-A3B`；`MODEL_NAME=Qwen3.6-35B-A3B` 作为默认兜底，接口专用变量未配置时使用。

**Q: Mac 本地测试和生产的区别？**

| 项目 | 生产 | Mac 本地 |
|------|------|---------|
| 服务地址 | `http://192.168.12.185:21804` | `http://localhost:21804` |
| MinIO URL（容器内访问） | `http://192.168.12.185:9000` | `http://host.docker.internal:9000` |
| 模型服务 | 直连内网 vLLM 服务；异步用 `ASYNC_MODEL_NAME`，同步/OpenAI 用 `SYNC_MODEL_NAME` | 同生产配置，通过 `.env` 注入模型变量 |
| 写接口鉴权 | 需配置 `TAGS_ADMIN_TOKEN` | 本地测试可不配置（跳过鉴权） |