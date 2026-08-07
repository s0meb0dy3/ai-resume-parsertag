# AI-Resume-ParserTag

基于 OpenAI 兼容多模态大模型的多格式简历解析与智能打标服务，支持 PDF、DOCX、JPG、JPEG、PNG，输出结构化 JSON 并自动完成正向/负向标签标注。

---

## 目录

- [技术架构](#技术架构)
- [项目结构](#项目结构)
- [快速启动](#快速启动)
- [API 接口总览](#api-接口总览)
- [业务接口详解](#业务接口详解)
  - [健康检查](#健康检查)
  - [单文件异步提交](#单文件异步提交-apiresumeasyncparse)
  - [批量异步提交](#批量异步提交-apiresumeaasyncbatch)
  - [轮询任务结果](#轮询任务结果-apiresumeaasyncresult)
  - [任务总览](#任务总览-apitasks)
  - [同步批量解析+打标](#同步批量解析打标-apiresumesyncbatch)
  - [同步批量仅解析](#同步批量仅解析-apiresumesyncparse)
  - [同步批量仅打标](#同步批量仅打标-apiresumesync tag)
  - [OpenAI 兼容接口](#openai-兼容接口)
- [打标配置接口](#打标配置接口)
- [Postman 使用指南](#postman-使用指南)
- [字段速查](#字段速查)
- [错误码](#错误码)
- [环境变量](#环境变量)
- [日志系统](#日志系统)
- [常见问题](#常见问题)

---

## 技术架构

```
PDF / DOCX / JPEG / PNG 上传
   │
   ▼
同步接口：Python 文本抽取
   ├── PDF：比较 pymupdf4llm 与 PyMuPDF 的内容完整性
   ├── DOCX：Mammoth 抽取文本并执行 OOXML 安全校验
   ├── 文本有效 ──→ 单次 LLM 统一生成完整 JSON
   ├── 扫描 PDF ──→ 渲染页面图片，降级到视觉解析
   └── 图片型 DOCX ──→ 按正文关系提取内嵌图片，降级到视觉解析
异步文件接口：支持真实 PDF/DOCX/JPG/JPEG/PNG，统一转为页面图片并走多模态视觉解析
   │
   ▼
由 `SYNC_MODEL_NAME` / `ASYNC_MODEL_NAME` 选择的内网模型（OpenAI 兼容接口）
   │
   ▼
结构化 JSON（候选人基本信息、工作经历、教育背景等）
   │
   ▼
打标模块（tag/）
   ├── 正向标签：教育背景、公司背景、工作年限、技能标签、行业背景
   └── 负向标签：敏感词、风险项、扣分维度
```

**模型服务**：通过 `MODEL_SERVICE_URL` 环境变量配置（必填）
**服务端口**：`21804`（容器）/ `30804`（Kubernetes NodePort）

---

## 项目结构

```
.
├── app.py                   # 入口：FastAPI 实例 + lifespan + /health
├── config.py                # 环境变量读取与全局配置
├── api/                     # FastAPI 路由层
│   ├── routes_async.py      # 异步解析 + 打标接口
│   ├── routes_openai.py     # OpenAI 兼容 + 同步解析/打标接口
│   ├── routes_tags.py       # 打标配置 CRUD（Bearer Token 鉴权）
│   ├── routes_algorithm.py  # 算法内部接口
│   └── response_schemas.py  # Swagger 成功/失败响应模型
├── core/                    # 业务核心
│   ├── parser.py            # 文本/视觉解析、模型回退与多页分批合并
│   ├── tasks.py             # 异步任务状态机与流水线编排
│   ├── postprocess.py       # 后处理、Schema 校验、批次合并、序列化
│   ├── prompts.py           # 旧 Prompt 导入路径兼容层
│   └── prompting/           # 按模型任务拆分的 Prompt
│       ├── resume_parsing.py # 简历解析结构、规则和字段白名单
│       ├── positive_tags.py  # 正向标签输出结构和维度规则
│       ├── negative_tags.py  # 负向语义风险输出结构和规则
│       ├── common.py         # 正负向打标共用系统角色
│       └── tag_id_mapping.py # 标签值与独立 ID 归一 Prompt
├── logger/                  # 日志子系统
│   ├── logger_manager.py    # 日志管理（滚动文件 + 请求上下文）
│   ├── langfuse_client.py   # Langfuse 可观测性客户端
│   └── log_config.yml       # 日志配置（支持热更新）
├── utils/                   # 工具函数
│   ├── pdf_utils.py         # PDF 渲染工具（fitz + PIL）
│   └── file_utils.py        # 简历文件下载与来源识别
├── tag/                     # 打标模块
│   ├── tagger.py            # 正向标签提取 + 敏感词检测
│   ├── tag_config.py        # 打标维度默认配置
│   ├── tag_data.py          # 教育/公司/行业确定性规则
│   └── reference_data.py    # 版本化院校和公司资质数据加载校验
├── schemas/                 # JSON Schema 与运行时配置
│   ├── resume_result_template.json        # 输出字段模板
│   ├── tag_config_runtime.json            # 运行时打标配置（可热更新）
│   ├── education_institution_data.json    # 完整国内院校等级、别名和排名快照
│   └── company_qualification_data.json    # 公司多资质及来源快照
├── evaluation/              # 黄金集契约、脱敏示例和离线评测器
├── test/                    # 测试工具
│   ├── test_tagging.py      # 打标功能测试
│   ├── stress_test_async.py # 并发压力测试
│   ├── test_minio_url.py    # MinIO URL 测试
│   └── upload_resume.py     # CLI 上传工具
├── docs/                    # 文档
│   ├── README.md            # 文档索引和维护规则
│   ├── 00_当前实现基线.md   # 当前代码事实入口
│   ├── 01_PRD_需求文档.md
│   ├── 02_系统概要设计文档.md
│   ├── 03_接口设计文档.md
│   ├── 04_测试用例.md
│   ├── 05_运维部署手册.md
│   ├── 06_系统详细设计文档.md
│   ├── 异步多格式视觉解析验证报告-20260807.md
│   └── openapi.json         # 自动导出的机器可读接口契约
├── uploads/                 # 上传文件暂存（运行时生成）
├── results/                 # 解析结果缓存（运行时生成）
├── logs/                    # 日志目录（运行时生成）
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

---

## 快速启动

### 本地启动

```bash
cp .env.example .env
# 编辑 .env，填写 API_KEY 和 MODEL_SERVICE_URL

pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 21804
```

### Docker Compose（推荐）

```bash
cp .env.example .env
# 编辑 .env，填写必填项

docker compose up --build -d
```

查看日志：

```bash
docker logs -f ai-resume-parsertag
```

健康检查：

```bash
curl http://localhost:21804/health
```

> CI/CD 通过 GitLab Pipeline 自动构建并部署到 Kubernetes（`demo` 命名空间，NodePort `30804`），触发分支：`dev` / `main`。

---

## API 接口总览

### 自动接口文档

FastAPI 路由和 Pydantic Schema 是接口契约的唯一来源，部署后可直接访问：

| 入口 | 地址 | 用途 |
| --- | --- | --- |
| Swagger UI | `http://localhost:21804/docs` | 在线查看并调试当前版本接口 |
| ReDoc | `http://localhost:21804/redoc` | 阅读式接口文档 |
| OpenAPI JSON | `http://localhost:21804/openapi.json` | 客户端生成、测试和平台导入 |

仓库内 [docs/openapi.json](docs/openapi.json) 由应用自动生成，不手工维护：

```bash
python scripts/export_openapi.py
python scripts/export_openapi.py --check
```

`pytest` 会比较已提交规范与 `app.openapi()`；路由、参数或 Pydantic Schema 发生
变化但未重新导出时测试失败，从而阻止接口文档与代码脱节。README 和 Postman
说明只提供业务示例，字段与请求约束以当前版本 Swagger 为准。

|     编号     |       方法       | 路径                                                      | 说明                                             |
| :-----------: | :--------------: | --------------------------------------------------------- | ------------------------------------------------ |
|      01      |       GET       | `/health`                                               | 健康检查                                         |
| **02** |  **POST**  | `/api/resume/async/parse`                               | **单文件提交，异步解析+打标，返回 taskId** |
| **03** |  **POST**  | `/api/resume/async/batch`                               | **批量提交（MinIO URL），异步处理**        |
| **03A** | **POST** | `/api/resume/async/batch/parse` | **批量异步仅解析** |
| **03B** | **POST** | `/api/resume/async/batch/tag` | **批量异步仅打标，输入解析 JSON** |
| **03C** | **POST** | `/api/resume/async/batch/multiform` | **上传与 URL 混合异步解析+打标** |
| **04** |  **GET**  | `/api/resume/async/result?taskId=`                      | **轮询单个任务结果**                       |
| **05** |  **GET**  | `/api/tasks`                                            | **任务总览（全部状态）**                   |
| **06** |  **POST**  | `/api/resume/sync/batch`                                | **同步批量解析+打标，阻塞返回完整结果**    |
| **06B** |  **POST**  | `/api/resume/sync/parse`                                | **同步批量仅解析，不打标**                 |
| **06C** |  **POST**  | `/api/resume/sync/tag`                                  | **同步批量仅打标，输入解析 JSON**          |
|      07      |       GET       | `/api/tags/config`                                      | 查询打标配置                                     |
| **07A** | **GET** | `/api/tags/mapping` | 查询版本化维度与标签 ID 映射 |
|      08      |       POST       | `/api/tags/config/positive`                             | 新增正向维度 🔒                                  |
|      09      |       PUT       | `/api/tags/config/positive/{dimension}`                 | 更新正向维度 🔒                                  |
|      10      |      DELETE      | `/api/tags/config/positive/{dimension}`                 | 删除正向维度 🔒                                  |
| **10A** |  **POST**  | `/api/tags/config/negative`                             | **新增负向维度（rule/llm 双模式）🔒**      |
|      11      |       PUT       | `/api/tags/config/negative/{dimension}`                 | 更新负向维度 🔒                                  |
| **11A** | **DELETE** | `/api/tags/config/negative/{dimension}`                 | **删除负向维度 🔒**                        |
|      12      |       POST       | `/api/tags/config/negative/{dimension}/rules`           | 新增负向规则 🔒                                  |
|      13      |       PUT       | `/api/tags/config/negative/{dimension}/rules/{ruleKey}` | 更新负向规则 🔒                                  |
|      14      |      DELETE      | `/api/tags/config/negative/{dimension}/rules/{ruleKey}` | 删除负向规则 🔒                                  |
|      15      |       PUT       | `/api/tags/config/severity`                             | 更新扣分权重 🔒                                  |
|      16      |       POST       | `/api/tags/config/reset`                                | 重置为默认配置 🔒                                |
|      17      |       POST       | `/v1/chat/completions`                                  | OpenAI 兼容接口（OneAPI 转发）                   |

> 🔒 写操作需在 Header 添加 `Authorization: Bearer <TAGS_ADMIN_TOKEN>`；服务未配置 `TAGS_ADMIN_TOKEN` 时可跳过。当前标签结构已锁定，新增/删除维度和规则的兼容路由固定返回 HTTP 409。

---

## 业务接口详解

### 服务地址

| 环境            | 地址                            |
| --------------- | ------------------------------- |
| 生产            | `http://192.168.12.185:21804` |
| Mac 本地 Docker | `http://localhost:21804`      |

---

### 健康检查

```
GET /health
```

**响应：**

```json
{ "status": "healthy", "timestamp": "2026-06-11T10:00:00.000000" }
```

---

### 单文件异步提交 `/api/resume/async/parse`

```
POST /api/resume/async/parse
Content-Type: multipart/form-data
```

上传单份真实 PDF、DOCX、JPG、JPEG 或 PNG，立即返回 `taskId`，后台统一转为
页面图片并通过多模态视觉模型完成解析+打标；通过接口 04 轮询结果。

**参数（form-data）：**

| Key                   | 类型 | 必填 | 说明                                         |
| --------------------- | ---- | :--: | -------------------------------------------- |
| `file`              | File |  ✅  | 真实 PDF/DOCX/JPG/JPEG/PNG，≤20MB           |
| `requestId`         | Text |  ✗  | 幂等标识，相同 requestId 直接返回已有 taskId |
| `jobTitle`          | Text |  ✗  | 目标岗位名称                                 |
| `requiredSkills`    | Text |  ✗  | 必要技能，逗号分隔                           |
| `jobDescription`    | Text |  ✗  | JD 全文                                      |
| `jobSensitiveWords` | Text |  ✗  | 自定义敏感词，逗号/分号分隔                  |

**响应：**

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

---

### 批量异步提交 `/api/resume/async/batch`

```
POST /api/resume/async/batch
Content-Type: application/json
```

传入 MinIO URL 列表，服务端主动下载并解析，立即返回所有 taskId，后台并发处理。

**请求体：**

```json
{
  "resumes": [
    {
      "type": "url",
      "file": {
        "name": "张三简历.pdf",
        "path": "http://192.168.12.185:9000/resumes/%E5%BC%A0%E4%B8%89%E7%AE%80%E5%8E%86.pdf",
        "extra": "userId_001"
      }
    }
  ],
  "jobTitle": "证券事务代表",
  "requiredSkills": "证券,信披,上市公司",
  "jobDescription": "负责上市公司证券事务管理",
  "jobSensitiveWords": ""
}
```

**字段说明：**

| 字段                     | 类型   | 必填 | 说明                                            |
| ------------------------ | ------ | :--: | ----------------------------------------------- |
| `resumes`              | Array  |  ✅  | URL 列表，每项`type` 固定为 `"url"`         |
| `resumes[].file.name`  | string |  ✅  | 简历文件名，扩展名为 `.pdf/.docx/.jpg/.jpeg/.png` |
| `resumes[].file.path`  | string |  ✅  | MinIO/HTTP 可访问的简历文件地址（中文需 URL 编码） |
| `resumes[].file.extra` | string |  ✗  | 透传字段，原样返回（可存业务 ID）               |
| `jobTitle`             | string |  ✗  | 目标岗位名称                                    |
| `requiredSkills`       | string |  ✗  | 必要技能，逗号分隔                              |
| `jobDescription`       | string |  ✗  | JD 全文                                         |
| `jobSensitiveWords`    | string |  ✗  | 自定义敏感词，逗号/分号分隔                     |

> 中文文件名需 URL 编码：Python `urllib.parse.quote(filename)`

**响应：**

```json
{
  "code": 0, "msg": "success",
  "total": 1, "submitted": 1, "failed": 0,
  "estimatedSeconds": 90,
  "tasks": [
    {"filename": "张三简历.pdf", "extra": "userId_001", "code": 0,
     "taskId": "aaa-111", "status": 0, "statusDesc": "待处理"}
  ]
}
```

---

### 轮询任务结果 `/api/resume/async/result`

```
GET /api/resume/async/result?taskId=aaa-111
```

|   status   | 含义                                       | 建议轮询间隔 |
| :---------: | ------------------------------------------ | :----------: |
|      0      | 待处理                                     |      3s      |
|      1      | 简历解析中                                 |      5s      |
|      2      | 标签打标中                                 |      5s      |
| **3** | **任务完成** → 读取 `data.result` |      —      |
|      4      | 任务失败 → 读取`data.errorMsg`          |      —      |

> 任务 TTL 默认 **120 分钟**，可通过 `TASK_TTL_MINUTES` 调整；从任务创建时间
> 开始计算，过期返回 `code=2001`，需重新提交。

**完成响应（status=3）：**

```json
{
  "code": 0, "msg": "success",
  "data": {
    "taskId": "aaa-111",
    "status": 3, "statusDesc": "任务完成",
    "elapsedSeconds": 35.2, "estimatedSeconds": 90,
    "extra": "userId_001",
    "result": {
      "taskId": "aaa-111",
      "resumeParseFinishTime": "2026-06-11 10:01:15",
      "tagParseFinishTime":    "2026-06-11 10:01:26",
      "candidate": {
        "name": "姚先生", "phone": "13800138000", "email": "",
        "gender": 1, "age": 31, "address": "深圳",
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
      "mappingVersion": "ai-resume-parsertag:ID-mapping-tag:V1.0.1-20260806",
      "positiveTags": {
        "tagsArray": [
          {"dimensionId": "1001", "dimension": "education", "displayName": "教育背景",
           "value": [{"tagId": "10010002", "tag": "985", "source": "rule"}, {"tagId": "10010009", "tag": "硕士", "source": "rule"}]},
          {"dimensionId": "1003", "dimension": "workYears", "displayName": "工作年限",
           "value": [{"tagId": "10030006", "tag": "5-10年（满5年）", "source": "rule"}]}
        ]
      },
      "negativeTags": {"hitCount": 0, "deductionScore": 0, "tagsArray": []}
    }
  }
}
```

---

### 任务总览 `/api/tasks`

```
GET /api/tasks
GET /api/tasks?status=3                         # 只看已完成
GET /api/tasks?status=4                         # 只看失败
GET /api/tasks?status=3&include_result=true     # 已完成且返回完整 result
```

批量提交后用此接口一次查看所有任务进度，按创建时间倒序，最多返回 200 条。

| 参数               | 类型 | 说明                                                       |
| ------------------ | ---- | ---------------------------------------------------------- |
| `status`         | int  | 0/1/2/3/4 按状态过滤，不传返回全部                         |
| `include_result` | bool | `true` 时 status=3 的条目同时返回完整 result，默认 false |

---

### 同步批量解析+打标 `/api/resume/sync/batch`

```
POST /api/resume/sync/batch
Content-Type: application/json
```

提交后**阻塞等待全部完成**，直接返回每份简历的完整解析+打标结果，无需轮询。适合离线批处理、脚本集成等场景；在线高并发场景推荐使用异步接口（03）。

**请求体：**

```json
{
  "resumes": [
    {
      "type": "url",
      "file": {
        "name": "张三简历.pdf",
        "path": "http://192.168.12.185:9000/resumes/%E5%BC%A0%E4%B8%89%E7%AE%80%E5%8E%86.pdf",
        "extra": "userId_001"
      }
    },
    {
      "type": "file",
      "file": {"name": "李四简历.pdf", "path": "/app/uploads/李四简历.pdf"}
    },
    {
      "type": "base64",
      "file": {"name": "王五简历.pdf", "content": "JVBERi0xLjQ..."}
    }
  ],
  "jobTitle": "证券事务代表",
  "requiredSkills": "信息披露,三会运作,投资者关系",
  "jobDescription": "负责上市公司证券事务管理",
  "jobSensitiveWords": "",
  "max_concurrency": 4
}
```

**字段说明：**

| 字段                       | 类型   |          必填          | 说明                                     |
| -------------------------- | ------ | :---------------------: | ---------------------------------------- |
| `resumes`                | Array  |           ✅           | 简历列表，单次上限 50 份                 |
| `resumes[].type`         | string |           ✅           | `url` / `file` / `base64` 三选一   |
| `resumes[].file.name`    | string |           ✅           | 文件名（含扩展名）                       |
| `resumes[].file.path`    | string | `url`/`file` 时必填 | MinIO URL 或容器内绝对路径               |
| `resumes[].file.content` | string |    `base64` 时必填    | 支持格式的文件 Base64 编码字符串             |
| `resumes[].file.extra`   | string |           ✗           | 透传字段，原样返回                       |
| `jobTitle`               | string |           ✗           | 目标岗位名称                             |
| `requiredSkills`         | string |           ✗           | 必要技能，逗号分隔                       |
| `jobDescription`         | string |           ✗           | JD 全文                                  |
| `jobSensitiveWords`      | string |           ✗           | 自定义敏感词，逗号/分号分隔              |
| `max_concurrency`        | int    |           ✗           | 并发数 1~30，默认同`MAX_WORKERS`（24） |

> Mac 本地 Docker 测试时，`type=url` 的 MinIO 地址须使用 `http://host.docker.internal:9000/...`

**响应：**

```json
{
  "total": 2, "success": 2, "failed": 0, "elapsed_s": 42.5,
  "results": [
    {
      "code": 0, "msg": "success",
      "data": {
        "taskId": "aaa-111", "status": 3, "statusDesc": "任务完成",
        "elapsedSeconds": 41.2, "extra": "userId_001",
        "result": { "...": "与接口 04 完成响应的 data.result 完全一致" }
      }
    }
  ]
}
```

---

### 同步批量仅解析 `/api/resume/sync/parse`

```
POST /api/resume/sync/parse
Content-Type: application/json
```

只执行 PDF → 结构化 JSON 解析，**不调用打标**，耗时更短（20~40s/份）。适合"先解析入库、后按岗位打标"的分步流程。输出的 `results[].data` 可直接传入 `/api/resume/sync/tag` 完成打标。

**请求体：**（与 sync/batch 一致，无需传打标字段）

```json
{
  "resumes": [
    {
      "type": "url",
      "file": {
        "name": "张三简历.pdf",
        "path": "http://192.168.12.185:9000/resumes/%E5%BC%A0%E4%B8%89%E7%AE%80%E5%8E%86.pdf",
        "extra": "userId_001"
      }
    }
  ],
  "max_concurrency": 4
}
```

**响应：**

```json
{
  "total": 1, "success": 1, "failed": 0, "elapsed_s": 34.2,
  "results": [
    {
      "code": 0, "msg": "success",
      "data": {
        "filename": "张三简历.pdf",
        "extra": "userId_001",
        "resumeParseFinishTime": "2026-06-11 10:00:05",
        "resume": {
          "candidate": {"name": "张三", "age": 28, "...": "..."},
          "educations": ["..."],
          "works": ["..."],
          "projects": []
        }
      }
    }
  ]
}
```

---

### 同步批量仅打标 `/api/resume/sync/tag`

```
POST /api/resume/sync/tag
Content-Type: application/json
```

只执行打标（LLM 正向标签 + 规则负面检测），**跳过原文件解析**，耗时约 5~20s/份。`resumes` 数组每项结构与 `sync/parse` 响应的 `results[].data` **完全一致**，可直接将解析结果透传，无需任何转换。

**sync/parse → sync/tag 串联示意：**

```
POST /api/resume/sync/parse
  └─ results[].data = { filename, extra, resumeParseFinishTime, resume }

直接将 results[].data 数组作为 resumes 传入：

POST /api/resume/sync/tag
  Body: {
    "resumes": [ <results[0].data>, <results[1].data>, ... ],
    "jobTitle": "证券事务代表",
    "requiredSkills": "信息披露,三会运作"
  }
```

**请求体：**

```json
{
  "resumes": [
    {
      "filename": "张三简历.pdf",
      "extra": "userId_001",
      "resumeParseFinishTime": "2026-06-11 10:00:05",
      "resume": {
        "candidate": {"name": "张三", "age": 28, "...": "..."},
        "educations": ["..."],
        "works": ["..."],
        "projects": []
      }
    }
  ],
  "jobTitle": "证券事务代表",
  "requiredSkills": "信息披露,三会运作,投资者关系",
  "jobDescription": "负责上市公司证券事务管理",
  "jobSensitiveWords": "",
  "max_concurrency": 4
}
```

**字段说明：**

| 字段                                | 类型   | 必填 | 说明                                                       |
| ----------------------------------- | ------ | :--: | ---------------------------------------------------------- |
| `resumes`                         | Array  |  ✅  | 每项为 sync/parse 响应的`results[].data`，单次上限 50 份 |
| `resumes[].resume`                | object |  ✅  | 结构化简历 JSON（sync/parse 的`data.resume` 字段）       |
| `resumes[].filename`              | string |  ✗  | 文件名，用于日志标识                                       |
| `resumes[].extra`                 | string |  ✗  | 透传字段，原样返回                                         |
| `resumes[].resumeParseFinishTime` | string |  ✗  | 解析完成时间，写入输出 result                              |
| `jobTitle`                        | string |  ✗  | 目标岗位名称                                               |
| `requiredSkills`                  | string |  ✗  | 必要技能，逗号分隔                                         |
| `jobDescription`                  | string |  ✗  | JD 全文                                                    |
| `jobSensitiveWords`               | string |  ✗  | 自定义敏感词，逗号/分号分隔                                |
| `max_concurrency`                 | int    |  ✗  | 并发数 1~30，默认同`MAX_WORKERS`                         |

> 同一批解析结果可用不同 `jobTitle` / `requiredSkills` 多次传入 sync/tag，按岗位分别打标

**响应格式与 sync/batch 完全一致。**

---

### OpenAI 兼容接口

```
POST /v1/chat/completions
Content-Type: application/json
```

兼容 **OneAPI / NewAPI** 等代理网关转发。

| model                 | 行为                                       |
| --------------------- | ------------------------------------------ |
| `DeepSeek-OCR`      | 仅解析，content 返回结构化简历 JSON 字符串 |
| `DeepSeek-OCR-JSON` | 解析 + 打标，同步等待                      |
| `AIHR-Resume`       | 同 DeepSeek-OCR-JSON                       |

**请求示例：**

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
            "path": "http://192.168.12.185:9000/resumes/resume.pdf"
          }
        }
      ]
    }
  ]
}
```

---

## 打标配置接口

所有写操作持久化到容器内 `schemas/tag_config_runtime.json`，**无需重启服务立即生效**。调用 reset 接口可一键恢复默认值。

> 🔒 写操作需在 Header 添加 `Authorization: Bearer {{admin_token}}`

### 查询配置

```
GET /api/tags/config
```

返回完整配置，包含正向维度（含 `allowedValues`）、负向维度（含 `rules` 详细参数）和扣分权重。

### 正向维度管理

```
# 新增
POST /api/tags/config/positive
Body: {"dimension": "language", "displayName": "语言能力", "enabled": true, "allowedValues": ["英语CET6", "英语流利"]}

# 更新（字段均可选）
PUT /api/tags/config/positive/{dimension}
Body: {"enabled": false}

# 删除
DELETE /api/tags/config/positive/{dimension}
```

### 负向维度与规则管理

```
# 新增自定义维度（rule 关键词类型）
POST /api/tags/config/negative
Body: {
  "dimension": "keywordRisk", "displayName": "关键词风险",
  "enabled": true, "detectionType": "rule",
  "rules": {
    "dispute_risk": {
      "tagName": "含劳动纠纷词", "enabled": true,
      "keywords": ["劳动仲裁", "离职纠纷"],
      "searchIn": ["works.description", "candidate.selfEvaluation"],
      "severity": "high"
    }
  }
}

# 新增自定义维度（llm 语义检测类型）
POST /api/tags/config/negative
Body: {
  "dimension": "attitudeRisk", "displayName": "候选人态度风险",
  "enabled": true, "detectionType": "llm",
  "rules": {
    "negative_attitude": {
      "tagName": "负面态度倾向", "enabled": true,
      "description": "自我评价中对前东家有抱怨或推卸责任的表述",
      "severity": "medium"
    }
  }
}

# 禁用整个维度
PUT /api/tags/config/negative/integrity
Body: {"enabled": false}

# 新增规则
POST /api/tags/config/negative/{dimension}/rules
Body: {"ruleKey": "short_career", "tagName": "职业经历过短", "severity": "low", "threshold_months": 6}

# 更新规则（调低跳槽阈值：近3年≥3段即触发）
PUT /api/tags/config/negative/jobstable/rules/frequent_job_change
Body: {"threshold": 3, "severity": "high"}

# 禁用某条规则
PUT /api/tags/config/negative/jobstable/rules/gap_period
Body: {"enabled": false}

# 删除规则
DELETE /api/tags/config/negative/{dimension}/rules/{ruleKey}
```

### 扣分权重

```
PUT /api/tags/config/severity
Body: {"low": -3, "medium": -8, "high": -15}
```

### 重置默认配置

```
POST /api/tags/config/reset
```

---

## Postman 使用指南

### 集合变量

| 变量名          | 默认值                          | 说明                         |
| --------------- | ------------------------------- | ---------------------------- |
| `base_url`    | `http://192.168.12.185:21804` | 解析服务地址                 |
| `task_id`     | （空）                          | 提交后手动填入，轮询接口读取 |
| `admin_token` | （空）                          | 写接口 Bearer Token          |

Mac 本地测试时，将 `base_url` 改为 `http://localhost:21804`。

### 接口 02 — 单文件上传（multipart）

1. Method: `POST`，URL: `{{base_url}}/api/resume/async/parse`
2. Body → **form-data**
3. 添加 Key `file`，类型选 **File**，选择本地 PDF、DOCX、JPG、JPEG 或 PNG 文件
4. 按需添加 `jobTitle`、`requiredSkills` 等 Text 字段
5. 发送后从响应中复制 `data.taskId`，填入集合变量 `task_id`

### 接口 03 — 批量提交（JSON）

1. Method: `POST`，URL: `{{base_url}}/api/resume/async/batch`
2. Body → **raw** → 右侧下拉选 **JSON**
3. 粘贴请求体，将 `file.path` 替换为实际 MinIO URL
4. 中文文件名需 URL 编码

> MinIO 配置：API `http://192.168.12.185:9000` | bucket `resumes` | 控制台 `http://192.168.12.185:9001`（minioadmin / minioadmin）

### 接口 04 — 轮询结果

1. Method: `GET`，URL: `{{base_url}}/api/resume/async/result?taskId={{task_id}}`
2. 重复发送直到 `data.status == 3`（完成）或 `4`（失败）
3. 建议轮询间隔 3~5s

### 接口 06 — 同步批量（推荐用于调试）

1. Method: `POST`，URL: `{{base_url}}/api/resume/sync/batch`
2. Body → **raw** → **JSON**
3. 粘贴请求体，一次性得到完整结果，无需轮询
4. Mac 本地 Docker 测试时，MinIO URL 使用 `http://host.docker.internal:9000/...`

### 接口 06B+06C — 分步解析与打标

```
步骤 1：POST /api/resume/sync/parse
        → 拿到 results[].data 数组（含 filename / resume 字段）

步骤 2：POST /api/resume/sync/tag
        Body: {
          "resumes": [ <上一步的 results[0].data>, ... ],
          "jobTitle": "目标岗位",
          "requiredSkills": "技能1,技能2"
        }
```

同一批解析结果可对多个岗位反复调用步骤 2，避免重复解析原始简历文件。

### 打标配置写接口鉴权

需在 Header 添加：

```
Authorization: Bearer {{admin_token}}
```

在集合变量中填入 `admin_token`（对应服务端 `TAGS_ADMIN_TOKEN` 环境变量）。服务未配置该变量时可跳过鉴权。

---

## 字段速查

### positiveTags 维度枚举

| dimension     | displayName | value[].tag                                                                                                            |
| ------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------- |
| `education` | 教育背景    | C9 / 985 / 211 / 双一流 / 海外名校QS50 / 海外名校QS100 / 海外硕士 / 博士 / 硕士 / 本科 / 专科及以下                  |
| `companyBg` | 公司背景    | 16 个规模声誉、所有制、资本状态和官方资质值；完整标准值见 `schemas/tag_config_runtime.json` |
| `workYears` | 工作年限    | 应届 / 往届无经验 / 1年以下 / 1-3年（满1年） / 3-5年（满3年） / 5-10年（满5年） / 10年及以上                              |
| `skills`    | 技能标签    | 受 `allowedValues` 约束，别名归一，最多 12 个                                                                          |
| `industry`  | 行业背景    | 16 个一级行业，按证据强度最多输出 3 个；完整标准值见 `schemas/tag_config_runtime.json` |

> 五个正向和四个负向维度都使用 4 位 `dimensionId`，标签值使用 8 位 `tagId`；
> 公共结果同时返回 ID、文本快照和 `mappingVersion`。完整映射通过
> `GET /api/tags/mapping` 查询。300 个技能名称、分类和别名源自
> `localmaterial/简历解析与打标/skill_300_new.xlsx`，运行时事实源是
> `schemas/tag_value_id_mapping.json` 与 `schemas/tag_config_runtime.json`。
>
> 正向打标 Prompt 会为固定维度的当前 `allowedValues` 生成独立判定说明。技能
> 维度先从结构化简历和内部原文召回标准候选，只注入候选项的分类、唯一别名和
> 判定说明，不再注入 300 项全量库；候选为空时强制返回空数组。模型结果仍使用
> 完整运行时配置和原文证据校验，漏提技能由规则补齐，最终最多保留 12 个。

> education 层级展开：命中 C9 → 同时输出 `[C9, 985, 211, 双一流]`
> education 是确定性维度；学校末尾的 `（985）` 等注记先规范化，日志与最终
> JSON 均使用同一规则结果，不合并模型分类。
> 国内院校等级来自 `schemas/education_institution_data.json`：C9 9 所、原 985
> 39 所、原 211 115 所、第二轮双一流 147 所。双一流独立匹配，不等同于 211。
> 同一文件固定保存完整 QS 2026 Top50 50 所和 Top100 100 所快照，并校验英文
> 别名、集合数量与子集关系；榜单内的中国内地院校不会生成“海外名校”标签。
>
> 公司资质来自版本化快照并允许多选，模型成功时也会补齐快照中的确定性资质；
> 模型公司背景值必须通过事实源或公司自身明确证据复核。规则置信度是固定证据
> 分值，不是统计概率。
> 行业模型值和规则值都要求至少一个强业务证据，`平台`、`网络`、`在线`、`教育`
> 等辅助词不能单独触发。技能只保留至少一处非否定、非学习计划、非岗位要求、
> 非单纯课程列举的真实证据。

### 真实简历黄金评测

评测格式和命令见 `evaluation/README.md`。真实 PDF/DOCX 及含 PII 人工标注放在
Git 忽略目录，通过匿名文件名和 SHA-256 关联。评测输出解析字段准确率、正向标签
micro/macro F1、负向标签 F1 和逐样本差异，低于清单阈值时返回非零状态。

### negativeTags 风险维度

| dimension     | displayName    | 标签                        | severity | 扣分 |
| ------------- | -------------- | --------------------------- | :------: | :--: |
| `jobstable` | 职业稳定性风险 | 频繁跳槽 / 最近任职时长偏短 |  medium  | -10 |
| `jobstable` | 职业稳定性风险 | 工作空窗期过长              |   low   |  -5  |
| `integrity` | 诚信与合规风险 | 工作时间线重叠              |   high   | -20 |
| `matchRisk` | 匹配度硬伤     | 疑似培训机构背景            |  medium  | -10 |
| `location`  | 地域相关       | 命中预设“广西”或请求附加敏感词 |   high   | -20 |

实习和兼职通过 `works[].position/description` 中的明确关键词识别，不计入正式
工作年限、频繁跳槽、短任职、空窗或工作时间线重叠。公开 Resume Schema 保持不变，
不增加 `employmentType` 字段。

### 标签来源与 ID

| 字段 | 约束 |
|---|---|
| `source` | 只允许 `rule`、`llm`、`hybrid`；旧值 `ai` 已废弃 |
| `mappingVersion` | 顶层映射快照版本，当前为 `ai-resume-parsertag:ID-mapping-tag:V1.0.1-20260806` |
| `dimensionId` | 4 位维度 ID，正向 `1001~1005`、负向 `2001~2004` |
| `tagId` | 8 位标签 ID，前四位与维度 ID 一致 |

教育和工作年限为 `rule`；公司背景、技能、行业通常是模型召回后经规则复核的
`hybrid`，纯规则补充为 `rule`；当前内置负向标签均为 `rule`。调用方应按
`mappingVersion + tagId` 持久化关联，同时保存 `tag` 文本快照，不得按数组下标绑定。

#### 业务后端调用与存储方式

1. 服务启动或发现新的 `mappingVersion` 时调用 `GET /api/tags/mapping`，按版本缓存
   `dimensions` 和 `mappings` 完整快照；`GET /api/tags/config` 可用于核对当前配置
   使用的映射版本。
2. 解析与打标接口返回后，先校验顶层 `mappingVersion`，再遍历
   `positiveTags.tagsArray[]` 和 `negativeTags.tagsArray[]`。维度使用
   `dimensionId`，每个 `value[]` 使用 `tagId` 建立稳定关联。
3. 推荐业务关联表至少保存 `businessId`、`mappingVersion`、`dimensionId`、
   `dimension`、`tagId`、`tag`、`source` 和创建时间，并以
   `businessId + mappingVersion + tagId` 建立唯一约束。负向标签按需要额外保存
   `severity` 和 `matchedContent`。
4. `tagId` 是关联键，`tag` 是当次结果的显示快照。不得使用数组位置、显示名称或
   alias 作为外键；版本升级时保留历史版本与文本快照，不应直接覆盖历史业务数据。

### gender 枚举

|  值  | 含义 |
| :---: | ---- |
| `1` | 男   |
| `2` | 女   |
| `0` | 未知 |

### 任务状态

| status | 含义       |
| :----: | ---------- |
| `0` | 待处理     |
| `1` | 简历解析中 |
| `2` | 标签打标中 |
| `3` | 任务完成   |
| `4` | 任务失败   |

---

## 错误码

|   code   | 含义                                       | 处理建议                    |
| :------: | ------------------------------------------ | --------------------------- |
|  `0`  | 成功                                       | —                          |
| `1001` | 参数错误 / URL 下载失败 / 字段非法         | 检查请求格式；确认 URL 可达 |
| `1002` | 重复（dimension/ruleKey 已存在）或文件超大 | 检查是否重复创建            |
| `1003` | 批量超过 50 份                             | 拆分多次提交                |
| `1004` | 资源不存在（dimension/ruleKey）            | 确认标识符拼写              |
| `2001` | taskId 不存在或已过期（TTL 默认 120 分钟） | 重新提交                    |

---

## 环境变量

参考 `.env.example` 创建 `.env`：

| 变量                    | 默认值                    | 说明                                        |
| ----------------------- | ------------------------- | ------------------------------------------- |
| `API_KEY`             | —（**必填**）      | 模型服务 API Key                            |
| `MODEL_SERVICE_URL`   | —（**必填**）      | 模型服务地址（OpenAI 兼容接口）             |
| `MODEL_NAME`          | `Qwen3.5-397B-A17B`     | 模型名称                                    |
| `TEMPERATURE`         | `0.01`                  | 推理温度                                    |
| `MAX_TOKENS`          | `4096`                  | 最大输出 token                              |
| `MAX_WORKERS`         | `24`                    | 并发线程数                                  |
| `API_TIMEOUT`         | `300`                   | 单次推理超时（秒）                          |
| `MAX_RETRIES`         | `2`                     | 失败重试次数                                |
| `LLM_PREFIX_CACHE_ENABLED` | `true`              | 为解析请求发送稳定 Prompt 缓存键；模型服务端需支持前缀缓存 |
| `DOWNLOAD_TIMEOUT`    | `60`                    | 文件下载超时（秒）                          |
| `SERVICE_PORT`        | `21804`                 | 服务监听端口                                |
| `LOG_DIR`             | `./logs`                | 日志目录                                    |
| `MAX_FILE_SIZE_MB`    | `20`                    | 上传文件大小上限                            |
| `TASK_TTL_MINUTES`    | `120`                   | 异步任务保留时长，从创建时间开始计算        |
| `TAGS_ADMIN_TOKEN`    | `""`                    | 打标配置写接口 Bearer Token；为空时跳过鉴权 |
| `LANGFUSE_SECRET_KEY` | `""`                    | Langfuse 密钥（可选）                       |
| `LANGFUSE_PUBLIC_KEY` | `""`                    | Langfuse 公钥（可选）                       |
| `LANGFUSE_HOST`       | `http://localhost:3100` | Langfuse 服务地址                           |

---

## 日志系统

### 文件结构

```
logs/
├── debug/YYYY-MM/debug_YYYY-MM-DD_<pid>.log   # 全量日志（DEBUG 及以上）
└── error/YYYY-MM/error_YYYY-MM-DD_<pid>.log   # 仅 ERROR 级别
```

每天零点自动滚动；单文件超 50MB 提前轮转。

### 日志格式

```
2026-08-07 16:03:03 | INFO  | [同步解析与打标签] [张三.pdf] | [routes_openai.py:417 sync_parse_and_tag_resumes] | [同步解析与打标完成]
2026-08-07 16:03:03 | INFO  | [同步解析与打标签] [张三.pdf] | [routes_openai.py:417 sync_parse_and_tag_resumes] | │  {
2026-08-07 16:03:03 | INFO  | [同步解析与打标签] [张三.pdf] | [routes_openai.py:417 sync_parse_and_tag_resumes] | │    "total": 1,
2026-08-07 16:03:03 | INFO  | [同步解析与打标签] [张三.pdf] | [routes_openai.py:417 sync_parse_and_tag_resumes] | │    "success": 1
2026-08-07 16:03:03 | INFO  | [同步解析与打标签] [张三.pdf] | [routes_openai.py:417 sync_parse_and_tag_resumes] | │  }
```

文本格式不显示 `trace/task` 前缀，固定为“秒级时间、级别、中文业务名、文件名、
调用位置、消息”。所有 JSON API（`/health` 和 `/openapi.json` 除外）都会在响应前
输出完成信息 JSON，再逐行输出与实际响应体一致的完整 JSON；异步轮询的处理中状态
使用 DEBUG，终态使用 INFO，业务错误/HTTP 4xx 使用 WARNING，HTTP 5xx 使用 ERROR。

控制台使用 ANSI 配色：日期为蓝色，DEBUG 为青色、INFO 为绿色、WARNING 为黄色、
ERROR 为红色、CRITICAL 为紫色；普通日志文件保持无颜色文本，便于检索和采集。

### 配置文件（`logger/log_config.yml`）

```yaml
log_config:
  log_level_console: INFO    # 控制台级别
  log_level_file: DEBUG      # 文件级别
  log_folder: logs
  max_bytes: 52428800        # 50MB 单文件上限
  json_format: false         # true = 单行 JSON（ELK/Loki 场景）
```

支持热更新（watchdog 监听，200ms 防抖后自动生效，无需重启）。

完整响应可能包含手机号、邮箱、地址等个人信息。生产环境必须限制日志读取权限、
采集范围和保留周期；如需最小化 PII，应将完整响应迁移到独立受控审计日志，而不是
直接关闭业务排障所需的接口结果记录。

---

## 常见问题

**Q: MinIO URL 中文件名有中文怎么处理？**
A: 中文需 URL 编码。Python：`urllib.parse.quote(filename)`；Java：`URLEncoder.encode(filename, "UTF-8")`（注意把 `+` 替换为 `%20`）。

URL 中的中文、空格和全角符号被显示为 `%E4%...`、`%20` 或
`%EF%BC%...` 是标准百分号编码，不是 Python 异常、字符集乱码或 PDF
解析失败。服务保留原始 URL 发起下载，仅对日志文件名和结果文件名执行一次
URL 解码并移除路径，因此 `/records/%E5%BC%A0%E4%B8%89.pdf` 在日志中显示为
`张三.pdf`。

**Q: URL 下载返回「文件下载失败」？**
A: URL 由**服务器端**发起下载，MinIO `192.168.12.185:9000` 需在解析服务所在机器上可访问。Mac 本地 Docker 部署时，URL 须使用 `http://host.docker.internal:9000/...`（不能用 localhost）。

**Q: 什么时候用 sync/parse + sync/tag，什么时候直接用 sync/batch？**
A: 需要"先解析入库、后按岗位打标"，或对同一批简历做多次不同岗位打标时用分步接口；只需一次性解析+打标直接拿结果时用 sync/batch 更简单。

**Q: 标签配置修改后如何生效？**
A: 通过配置接口修改后**立即生效**，无需重启服务（写入 `schemas/tag_config_runtime.json`）。调用 reset 接口可一键恢复默认值。

**Q: `extra` 字段有什么用？**
A: 调用方传入自己的业务 ID（如 userId、resumeId 等），服务原样存储并在响应中透传返回，方便业务侧数据关联。

**Q: 提交成功但 `/api/tasks` 看不到任务？**
A: 任务 TTL 默认 120 分钟并从创建时间开始计算，超时自动清除；也应检查是否
提交到了不同端口或不同实例。
