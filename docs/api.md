# 简历筛选 API 文档

## 概述

- **Base URL**: `http://10.2.45.212:8000/api/v1`
- **认证**: 无（内部服务）
- **数据格式**: `multipart/form-data`（POST）、`application/json`（响应）

调用流程：先解析 JD → 再提交简历筛选 → 轮询结果。

---

## 1. 解析 JD

```
POST /api/v1/jd
Content-Type: multipart/form-data

参数：
  jd_id     (string)  JD 唯一标识，由调用者指定，如 "jd_20260508_001"
  jd_file   (file)    JD 描述 .txt 文件

成功响应 200：
{
  "jd_id": "jd_20260508_001",
  "job_title": "天线工程师",
  "status": "completed"
}

错误响应：
  400 { "detail": "JD 文件格式错误或内容为空" }
```

> JD 解析是同步的，解析结果持久化到 `outputs/jd/{jd_id}/jd_analysis.json`。同一 `jd_id` 重复调用直接返回已有结果。

---

## 2. 查询 JD

```
GET /api/v1/jd/{jd_id}

成功响应 200：
{
  "jd_id": "jd_20260508_001",
  "job_title": "天线工程师",
  "jd_analysis": {
    "job_title": "天线工程师",
    "job_level": "",
    "locations": [],
    "responsibilities": ["负责自研项目的天线评估...", ...],
    "requirements": {
      "学历": "硕士及以上学历5年以上...",
      "专业": "电磁场与微波技术",
      "专业技能": "熟练使用HFSS微波仿真软件...",
      ...
    },
    "nice_to_have": ["具有技术创新能力..."],
    "raw_text": "原始JD全文"
  }
}

错误响应：
  404 { "detail": "JD 不存在" }
```

---

## 3. 删除 JD

```
DELETE /api/v1/jd/{jd_id}

成功响应 204 No Content
```

---

## 4. 提交简历筛选

```
POST /api/v1/screening
Content-Type: multipart/form-data

参数：
  jd_id           (string)  已解析的 JD 标识
  candidate_type  (string)  "社招" 或 "校招"
  resumes         (string)  JSON 数组，每项包含：
                  - filename (string)  简历文件名（不含路径）
                  - data     (object)  简历结构化 JSON 对象

成功响应 202：
{
  "task_id": "a1b2c3d4-...",
  "status": "processing"
}

错误响应：
  400 { "detail": "jd_id 不能为空" }
  400 { "detail": "candidate_type 必须是 '社招' 或 '校招'" }
  400 { "detail": "resumes 不是合法的 JSON: ..." }
  400 { "detail": "resumes 必须是非空 JSON 数组..." }
```

**resumes 参数示例**（每个 `data` 对象来自 DeepSeek-OCR 解析或客户端直接提供）：

```json
[
  {
    "filename": "张三.pdf",
    "data": {
      "candidate": {
        "name": "张三",
        "age": 30,
        "gender": 1,
        "phone": "13800000000",
        "email": "zhangsan@example.com",
        "nativePlace": "四川绵阳",
        "address": "深圳",
        "selfEvaluation": "责任心强，具备独立完成天线设计全流程的能力",
        "expectedSalary": "",
        "expectedPosition": ""
      },
      "educations": [
        {
          "school": "电子科技大学",
          "major": "通信工程",
          "degree": "硕士",
          "education": "硕士",
          "startDate": "2018-09",
          "endDate": "2021-06"
        },
        {
          "school": "郑州大学",
          "major": "电子信息",
          "degree": "学士",
          "education": "本科",
          "startDate": "2014-09",
          "endDate": "2018-06"
        }
      ],
      "works": [
        {
          "company": "华为",
          "position": "天线工程师",
          "description": "负责基站天线设计，主导完成 5G Massive MIMO 天线方案仿真与验证",
          "startDate": "2022-07",
          "endDate": "至今"
        },
        {
          "company": "中兴通讯",
          "position": "射频工程师",
          "description": "负责射频前端电路设计与调试",
          "startDate": "2021-07",
          "endDate": "2022-06"
        }
      ],
      "projects": [
        {
          "company": "华为",
          "projectName": "5G 基站天线研发",
          "role": "项目负责人",
          "description": "负责整体方案设计、HFSS 仿真、调试优化",
          "startDate": "2022-01",
          "endDate": "2023-06"
        },
        {
          "company": "华为",
          "projectName": "4G 天线小型化优化",
          "role": "核心开发",
          "description": "负责天线辐射单元优化设计，体积缩减 30%",
          "startDate": "2021-07",
          "endDate": "2021-12"
        }
      ]
    }
  },
  {
    "filename": "李四.pdf",
    "data": {
      "candidate": {
        "name": "李四",
        "age": 28,
        "gender": 2,
        "phone": "13900000000",
        "email": "lisi@example.com",
        "nativePlace": "贵州省贵阳市",
        "address": "广州",
        "selfEvaluation": "学习能力强，善于解决复杂电磁仿真问题",
        "expectedSalary": "",
        "expectedPosition": ""
      },
      "educations": [
        {
          "school": "华南理工大学",
          "major": "电磁场与微波技术",
          "degree": "硕士",
          "education": "硕士",
          "startDate": "2020-09",
          "endDate": "2023-06"
        }
      ],
      "works": [],
      "projects": [
        {
          "company": "华南理工大学",
          "projectName": "毫米波天线阵列设计",
          "role": "独立完成",
          "description": "使用 HFSS 设计 4x4 贴片天线阵列，实现增益 15dBi",
          "startDate": "2021-09",
          "endDate": "2022-12"
        }
      ]
    }
  }
]
```

> 筛选是异步任务，返回 `task_id` 后需轮询获取结果。

---

## 5. 查询筛选结果

```
GET /api/v1/screening/{task_id}

处理中 200：
{
  "status": "processing",
  "result": null,
  "error": ""
}

完成 200：
{
  "status": "completed",
  "result": {
    "candidates": [
      {
        "jd_id": "jd_20260508_001",
        "name": "张三",
        "passed": true,
        "total_score": 85.5,
        "dimension_scores": [
          {
            "dimension": "项目经验",
            "score": 90.0,
            "weight": 0.25,
            "weighted_score": 22.5,
            "reason": "候选人主导过多个基站天线项目，独立完成方案设计到交付"
          },
          {
            "dimension": "技术能力",
            "score": 85.0,
            "weight": 0.2,
            "weighted_score": 17.0,
            "reason": "熟练使用 HFSS、ADS 等仿真工具"
          }
        ],
        "recommend_reason": "候选人项目经验丰富，技术栈匹配度高",
        "rejection_reasons": [],
        "sensitive_word_matches": [
          { "word": "四川", "context": "老家户籍: 四川绵阳" }
        ]
      },
      {
        "jd_id": "jd_20260508_001",
        "name": "李四",
        "passed": false,
        "total_score": 42.0,
        "dimension_scores": [
          {
            "dimension": "项目经验",
            "score": 55.0,
            "weight": 0.25,
            "weighted_score": 13.75,
            "reason": "仅有一个在校期间参与的学术研究项目，缺乏企业级产品开发经验"
          },
          {
            "dimension": "技术能力",
            "score": 48.0,
            "weight": 0.2,
            "weighted_score": 9.6,
            "reason": "了解 HFSS 基本操作但无实际工程项目实践，未接触过 ADS 仿真工具"
          }
        ],
        "recommend_reason": "",
        "rejection_reasons": [
          "缺乏企业实习或实际项目经验，未体现出 JD 要求的独立项目开发能力",
          "技术栈仅停留在学术仿真阶段，距离工程落地有明显差距"
        ],
        "sensitive_word_matches": []
      }
    ]
  },
  "error": ""
}

失败 200：
{
  "status": "failed",
  "result": null,
  "error": "错误描述信息"
}

错误响应：
  404 { "detail": "任务不存在" }
```

---

## 字段说明

### candidate（简历候选人）

| 字段 | 类型 | 说明 |
|------|------|------|
| name | string | 姓名 |
| age | int | 年龄 |
| gender | int | 1=男, 2=女 |
| phone | string | 手机号 |
| email | string | 邮箱 |
| nativePlace | string | 籍贯/老家 |
| address | string | 现居地 |
| selfEvaluation | string | 自我评价 |

### education（教育经历）

| 字段 | 类型 | 说明 |
|------|------|------|
| school | string | 学校名称 |
| major | string | 专业 |
| degree | string | 学位 |
| education | string | 学历层次（本科/硕士/博士） |
| startDate | string | 开始时间，格式 `YYYY-MM` |
| endDate | string | 结束时间 |

### work（工作经历）

| 字段 | 类型 | 说明 |
|------|------|------|
| company | string | 公司名称 |
| position | string | 职位 |
| description | string | 工作描述 |
| startDate | string | 开始时间 |
| endDate | string | 结束时间 |

### dimension_scores（评估维度得分）

| 字段 | 类型 | 说明 |
|------|------|------|
| dimension | string | 维度名称 |
| score | number | 该维度原始分（0-100） |
| weight | number | 权重（0-1） |
| weighted_score | number | 加权分（score × weight） |
| reason | string | 打分理由 |

### sensitive_word_matches（敏感词命中）

| 字段 | 类型 | 说明 |
|------|------|------|
| word | string | 命中的敏感词 |
| context | string | 上下文，如 "老家户籍: 四川绵阳" |

---

## 并发限制

服务端通过 `max_concurrency` 配置控制 LLM 并行度（默认 2-10），客户端无需额外处理并发策略。大批量简历建议分批提交，每批不超过 20 份。
