# AIHR 简历解析打标 Postman 使用说明

> 更新日期：2026-08-07
>
> 适用应用：`3.1.0`
>
> 映射版本：`ai-resume-parsertag:ID-mapping-tag:V1.0.1-20260806`

完整字段和错误语义见 [03_接口设计文档.md](03_接口设计文档.md)，机器可读契约见
[openapi.json](openapi.json)。本文提供最常用的可执行请求。

## 1. 环境变量

在 Postman Environment 或 Collection Variables 中创建：

| 变量 | 示例 | 用途 |
|---|---|---|
| `base_url` | `http://localhost:21804` | 服务地址 |
| `task_id` | 空 | 异步提交后写入 |
| `admin_token` | 空 | 标签配置写接口 Token |
| `resume_url` | `http://host.docker.internal:9000/resumes/a.pdf` | 容器可访问文件 URL |

Mac Docker 容器访问宿主机不能使用 `localhost`，应使用
`host.docker.internal`。中文 URL 路径应正确百分号编码。

## 2. 接口选择

| 需求 | 方法和路径 |
|---|---|
| 同步仅解析 | `POST /api/resume/sync/parse` |
| 同步仅打标 | `POST /api/resume/sync/tag` |
| 同步解析并打标 | `POST /api/resume/sync/batch` |
| 单文件上传异步解析并打标 | `POST /api/resume/async/parse` |
| URL 批量异步解析并打标 | `POST /api/resume/async/batch` |
| URL 批量异步仅解析 | `POST /api/resume/async/batch/parse` |
| 已解析 JSON 批量异步仅打标 | `POST /api/resume/async/batch/tag` |
| 上传和 URL 混合异步 | `POST /api/resume/async/batch/multiform` |
| 轮询异步结果 | `GET /api/resume/async/result?taskId={{task_id}}` |
| 查询标签配置 | `GET /api/tags/config` |
| 查询 ID 映射 | `GET /api/tags/mapping` |

同步和异步文件入口均支持 PDF、DOCX、JPG、JPEG、PNG。异步 JSON 批量使用
`type=url`，单文件和混合批量使用 `multipart/form-data`；所有异步格式统一转为
图片后执行视觉解析。

## 3. 同步仅解析

创建请求：

```http
POST {{base_url}}/api/resume/sync/parse
Content-Type: application/json
```

Body：

```json
{
  "resumes": [
    {
      "type": "url",
      "file": {
        "name": "candidate.pdf",
        "path": "{{resume_url}}",
        "extra": "resume-001"
      }
    }
  ],
  "max_concurrency": 1
}
```

成功结果：

```json
{
  "total": 1,
  "success": 1,
  "failed": 0,
  "elapsed_s": 12.34,
  "results": [
    {
      "code": 0,
      "msg": "success",
      "data": {
        "filename": "candidate.pdf",
        "extra": "resume-001",
        "resumeParseFinishTime": "2026-08-07 10:00:00",
        "resume": {
          "candidate": {},
          "educations": [],
          "works": [],
          "projects": []
        }
      }
    }
  ]
}
```

`results[0].data` 是同步仅打标的一个完整输入元素，不要只复制其中的 `resume`。

## 4. 同步仅打标

```http
POST {{base_url}}/api/resume/sync/tag
Content-Type: application/json
```

Body：

```json
{
  "resumes": [
    {
      "filename": "candidate.pdf",
      "extra": "resume-001",
      "resumeParseFinishTime": "2026-08-07 10:00:00",
      "resume": {
        "candidate": {"name": "张三"},
        "educations": [
          {
            "school": "北京大学",
            "major": "计算机科学",
            "degree": "硕士",
            "education": "硕士研究生",
            "startDate": "2018-09",
            "endDate": "2021-07"
          }
        ],
        "works": [
          {
            "company": "示例科技有限公司",
            "position": "Python开发工程师",
            "description": "使用 FastAPI、MySQL 和 Docker 开发服务",
            "startDate": "2021-07",
            "endDate": "至今"
          }
        ],
        "projects": []
      }
    }
  ],
  "jobTitle": "后端工程师",
  "requiredSkills": "Python,FastAPI",
  "jobDescription": "负责后端服务开发",
  "jobSensitiveWords": "",
  "max_concurrency": 1
}
```

该接口不会读取原文件，也不会重新解析。公开 Resume 不包含 `employmentType`；即使
请求额外传入也会被契约层忽略。实习和兼职应在职位或描述中明确写出，以便年限和
风险规则排除。

## 5. 同步解析并打标

```http
POST {{base_url}}/api/resume/sync/batch
Content-Type: application/json
```

Body 与同步仅解析相同，可额外携带四个岗位字段。返回批次统计和每项完整结果。
一次解析只对应一个岗位时使用该接口；一次解析后需要多岗位重复打标时，使用
`sync/parse -> sync/tag`，避免重复解析文件。

## 6. 同步输入来源

URL：

```json
{"type":"url","file":{"name":"a.pdf","path":"http://server/a.pdf"}}
```

容器内文件：

```json
{"type":"file","file":{"name":"a.pdf","path":"/app/uploads/a.pdf"}}
```

Base64：

```json
{"type":"base64","file":{"name":"a.pdf","content":"JVBERi0x..."}}
```

`type=file` 的路径是服务端/容器路径，不是 Postman 所在电脑的路径。Base64 内容不
包含 `data:application/pdf;base64,` 前缀。

## 7. 异步提交和轮询

### 7.1 单文件上传

```http
POST {{base_url}}/api/resume/async/parse
Content-Type: multipart/form-data
```

Body 选择 form-data：

| Key | 类型 | 必填 |
|---|---|:---:|
| `file` | File | 是 |
| `requestId` | Text | 否 |
| `jobTitle` | Text | 否 |
| `requiredSkills` | Text | 否 |
| `jobDescription` | Text | 否 |
| `jobSensitiveWords` | Text | 否 |

`requestId` 在当前进程内用于幂等；进程重启后记录丢失。
上传文件必须是扩展名与真实内容一致的 PDF、DOCX、JPG、JPEG 或 PNG。

### 7.2 URL 批量解析并打标

```http
POST {{base_url}}/api/resume/async/batch
Content-Type: application/json
```

```json
{
  "resumes": [
    {
      "type": "url",
      "file": {"name": "candidate.pdf", "path": "{{resume_url}}", "extra": "resume-001"}
    }
  ],
  "jobTitle": "后端工程师"
}
```

一次最多 20 份。`/api/resume/async/batch/parse` 使用同一请求体，但只解析。

### 7.3 异步仅打标

```http
POST {{base_url}}/api/resume/async/batch/tag
Content-Type: application/json
```

`resumes[]` 结构与同步仅打标相同，一次最多 20 份。提交后每份返回独立 taskId。

### 7.4 轮询

```http
GET {{base_url}}/api/resume/async/result?taskId={{task_id}}
```

| status | 含义 | 操作 |
|:---:|---|---|
| `0` | 待处理 | 继续轮询 |
| `1` | 解析中 | 继续轮询 |
| `2` | 打标中 | 继续轮询 |
| `3` | 完成 | 读取 `data.result` |
| `4` | 失败 | 读取 `data.errorMsg` |

建议间隔 3~5 秒。任务默认按创建时间保留 120 分钟，只存在当前进程内存中。

可在 Tests 中自动保存 taskId：

```javascript
const body = pm.response.json();
const taskId = body?.data?.taskId || body?.tasks?.[0]?.taskId;
if (taskId) pm.collectionVariables.set("task_id", taskId);
```

## 8. 当前打标响应

完整结果顶层包含标准 Resume 字段和：

```json
{
  "mappingVersion": "ai-resume-parsertag:ID-mapping-tag:V1.0.1-20260806",
  "positiveTags": {
    "tagsArray": [
      {
        "dimensionId": "1001",
        "dimension": "education",
        "displayName": "教育背景",
        "value": [
          {"tagId": "10010002", "tag": "985", "source": "rule"}
        ]
      },
      {
        "dimensionId": "1004",
        "dimension": "skills",
        "displayName": "技能标签",
        "value": [
          {"tagId": "10040001", "tag": "Python", "source": "hybrid"}
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

`source` 只允许：

- `rule`：纯规则；教育、工作年限和当前内置负向标签。
- `llm`：纯模型；当前只可能来自配置为 LLM 的自定义负向维度。
- `hybrid`：模型召回后通过规则、事实库或证据复核；主要用于公司、技能和行业。

旧值 `ai` 已废弃。教育和工作年限不返回 `confidence`。

## 9. 工作年限值

```text
应届
往届无经验
1年以下                 [0, 12个月)
1-3年（满1年）          [12, 36个月)
3-5年（满3年）          [36, 60个月)
5-10年（满5年）         [60, 120个月)
10年及以上              [120个月, +∞)
```

档位为左闭右开。实习和兼职不计入正式年限，也不参与时间线重叠和职业稳定性检测。

## 10. 标签配置和映射

查询当前配置：

```http
GET {{base_url}}/api/tags/config
```

查询完整映射：

```http
GET {{base_url}}/api/tags/mapping
```

两个接口返回同一个 `mappingVersion`。后端应以 `tagId/dimensionId` 关联，以文本作为
历史快照，不要依赖数组顺序。

配置写接口 Header：

```http
Authorization: Bearer {{admin_token}}
```

当前可更新既有维度、规则字段和扣分，可调用 reset；新增/删除维度或规则的兼容
接口固定返回 HTTP 409。未配置 `TAGS_ADMIN_TOKEN` 时服务跳过鉴权，只适合隔离环境。

扣分接口请求值使用非负绝对值：

```http
PUT {{base_url}}/api/tags/config/severity
Content-Type: application/json
Authorization: Bearer {{admin_token}}

{"low":5,"medium":10,"high":20}
```

服务写入运行时配置时保持实际扣分语义。

## 11. 常见问题

**`sync/parse` 如何接 `sync/tag`？** 直接把每个成功项的 `results[].data` 放进下一次
请求的 `resumes[]`，不要再套一层 `data` 或只传 Resume 根对象。

**为什么同步和异步标签略有差异？** 同步先用 Python 抽取文本再结构化，并可把原文
用于技能证据；异步多格式文档均转图片视觉解析，没有 Python 原文。还应检查
`SYNC_MODEL_NAME` 与 `ASYNC_MODEL_NAME` 是否相同。

**为什么空 `endDate` 变成“至今”？** 只有不存在其他显式当前任职，且空结束日期
唯一、可排序并属于最新开始记录时才会保守补全。多个空值保持为空。

**为什么标签没有命中？** 检查当前配置是否启用、值是否在允许列表、技能/行业是否
有直接证据，以及是否属于否定、学习计划、岗位要求或客户语境。

**如何验证文档与服务一致？** 打开 `{{base_url}}/docs`，或执行：

```bash
python scripts/export_openapi.py --check
pytest -q
```
