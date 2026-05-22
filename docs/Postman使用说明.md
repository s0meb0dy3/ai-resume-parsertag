# AIHR 简历解析打标 — Postman 使用说明

> Collection 文件：`AIHR_Resume_Postman_Collection.json`  
> 服务地址：`http://192.168.12.185:21801`  
> MinIO 地址：`http://192.168.12.185:9000`（bucket: `resumes`，账号: `minioadmin/minioadmin`）

---

## 一、接口总览

| 编号 | 方法 | 路径 | 说明 |
|:----:|:----:|------|------|
| 01 | GET | `/health` | 健康检查 |
| **02** | **POST** | `/api/resume/async/batch` | **批量简历提交（MinIO URL）** |
| **03** | **GET** | `/api/resume/async/result?taskId={{task_id}}` | **轮询单个任务结果** |
| **04** | **GET** | `/api/tasks` | **任务总览（一次查所有任务状态）** |
| 05 | GET | `/api/tags/config` | 获取全量标签配置（热更新） |

---

## 二、集合变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `base_url` | `http://192.168.12.185:21801` | 解析服务地址 |
| `task_id` | （空） | 提交后自动写入，03 直接读取 |

---

## 三、接口 01 — 健康检查

```
GET /health
```

**响应：**
```json
{ "status": "healthy", "timestamp": "2026-05-22T10:00:00.000000" }
```

---

## 四、接口 02 — 批量简历提交

```
POST /api/resume/async/batch
Content-Type: application/json
```

传入 MinIO URL 列表，服务端主动下载并解析，立即返回所有 taskId，后台并发处理。

### 请求体字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `resumes` | Array | ✅ | URL 列表，每项格式见下方 |

### Postman 操作

1. Body → **raw** → 右侧下拉选 **JSON**
2. 粘贴以下 JSON

### 请求示例

```json
{
  "resumes": [
    {
      "type": "url",
      "file": {
        "name": "31岁-5年经验-证券事务代表-猎聘简历.pdf",
        "path": "http://192.168.12.185:9000/resumes/31%E5%B2%81-5%E5%B9%B4%E7%BB%8F%E9%AA%8C-%E8%AF%81%E5%88%B8%E4%BA%8B%E5%8A%A1%E4%BB%A3%E8%A1%A8-%E7%8C%8E%E8%81%98%E7%AE%80%E5%8E%86.pdf"
      }
    },
    {
      "type": "url",
      "file": {
        "name": "PKU刘旗简历.pdf",
        "path": "http://192.168.12.185:9000/resumes/PKU%E5%88%98%E6%97%97%E7%AE%80%E5%8E%86.pdf"
      }
    },
    {
      "type": "url",
      "file": {
        "name": "伍先生-34岁-11年经验-证券事务代表-猎聘简历.pdf",
        "path": "http://192.168.12.185:9000/resumes/%E4%BC%8D%E5%85%88%E7%94%9F-34%E5%B2%81-11%E5%B9%B4%E7%BB%8F%E9%AA%8C-%E8%AF%81%E5%88%B8%E4%BA%8B%E5%8A%A1%E4%BB%A3%E8%A1%A8-%E7%8C%8E%E8%81%98%E7%AE%80%E5%8E%86.pdf"
      }
    }
  ]
}
```

> **中文文件名需 URL 编码**：Python `urllib.parse.quote(filename)` / Java `URLEncoder.encode(filename, "UTF-8")`

**MinIO bucket 配置：**

| 项目 | 值 |
|------|-----|
| MinIO 地址 | `http://192.168.12.185:9000` |
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
    { "filename": "31岁-5年经验-证券事务代表-猎聘简历.pdf", "code": 0, "taskId": "aaa-111", "status": 0, "statusDesc": "待处理" },
    { "filename": "PKU刘旗简历.pdf",                        "code": 0, "taskId": "bbb-222", "status": 0, "statusDesc": "待处理" },
    { "filename": "伍先生-34岁-11年经验-证券事务代表-猎聘简历.pdf", "code": 0, "taskId": "ccc-333", "status": 0, "statusDesc": "待处理" }
  ]
}
```

---

## 五、接口 03 — 轮询单个任务结果

```
GET /api/resume/async/result?taskId={{task_id}}

http://192.168.12.185:21801/api/resume/async/result?taskId=bb85d76b-2f21-48c6-b02d-452b5e4b4a28
```

| status | 含义 | 建议轮询间隔 |
|:------:|------|:-----------:|
| 0 | 待处理 | 3s |
| 1 | 简历解析中 | 5s |
| 2 | 标签打标中 | 5s |
| **3** | **任务完成** → 读取 `data.result` | — |
| 4 | 任务失败 → 读取 `data.errorMsg` | — |

> 任务 TTL = **10 分钟**，超时返回 `code=2001`，需重新提交。

**完成时 `data.result` 结构：**
```json
{
  "taskId": "aaa-111",
  "resumeParseFinishTime": "2026-05-22 10:40:17",
  "tagParseFinishTime":    "2026-05-22 10:40:26",
  "candidate": {
    "name": "姚先生", "phone": "", "email": "",
    "gender": 1, "age": 31, "address": "",
    "nativePlace": "", "selfEvaluation": "",
    "expectedSalary": "", "expectedPosition": ""
  },
  "educations": [
    { "school": "武汉大学", "major": "金融学", "degree": "硕士",
      "education": "硕士研究生", "startDate": "2015-09", "endDate": "2018-07" }
  ],
  "works": [
    { "company": "广州广钢气体能源股份有限公司", "position": "证券事务代表",
      "description": "...", "startDate": "2020-07", "endDate": "至今" }
  ],
  "projects": [],
  "positiveTags": {
    "tagsArray": [
      { "dimension": "education", "displayName": "学历背景",
        "value": [{"tag": "985", "source": "ai"}, {"tag": "硕士", "source": "ai"}, {"tag": "海外硕士", "source": "ai"}] },
      { "dimension": "companyBg", "displayName": "公司背景", "confidence": 0.85,
        "value": [{"tag": "国企/央企", "source": "ai"}] },
      { "dimension": "workYears", "displayName": "工作年限",
        "value": [{"tag": "3-5年", "source": "ai"}] },
      { "dimension": "skills", "displayName": "技能", "confidence": 0.92,
        "value": [{"tag": "信息披露", "source": "ai"}, {"tag": "三会运作", "source": "ai"}, {"tag": "投资者关系", "source": "ai"}] },
      { "dimension": "industry", "displayName": "行业", "confidence": 0.88,
        "value": [{"tag": "金融科技", "source": "ai"}] }
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

## 六、接口 04 — 任务总览

```
GET /api/tasks
GET /api/tasks?status=3    只看已完成
GET /api/tasks?status=4    只看失败
```

批量提交后用此接口一次查看所有任务进度，按创建时间倒序。

| status | 含义 |
|:------:|------|
| 0 | 待处理 |
| 1 | 简历解析中 |
| 2 | 标签打标中 |
| 3 | 已完成（含 `candidateName`、完成时间） |
| 4 | 失败（含 `errorMsg`） |

**响应示例：**
```json
{
  "total": 3,
  "tasks": [
    { "taskId": "aaa-111", "status": 3, "statusDesc": "任务完成",
      "elapsedSeconds": 35.2, "createdAt": "2026-05-22 10:40:00",
      "candidateName": "姚先生",
      "resumeParseTime": "2026-05-22 10:40:17",
      "tagParseTime":    "2026-05-22 10:40:26",
      "completedAt":     "2026-05-22 10:40:26" },
    { "taskId": "bbb-222", "status": 2, "statusDesc": "标签打标中",
      "elapsedSeconds": 45.0, "createdAt": "2026-05-22 10:40:00" },
    { "taskId": "ccc-333", "status": 4, "statusDesc": "任务失败",
      "elapsedSeconds": 3.1,  "createdAt": "2026-05-22 10:40:00",
      "errorMsg": "PDF 已加密，需要密码才能解析" }
  ]
}
```

> 完整结果需用各 taskId 单独调用接口 03。

---

## 七、接口 05 — 标签配置（热更新）

```
GET /api/tags/config
```

每次调用自动重新加载 `tag_config.py`，**修改配置文件后无需重启服务**。

**响应结构：**
```json
{
  "positiveDimensions": [
    { "dimension": "education", "displayName": "学历背景", "enabled": true,
      "allowedValues": ["C9","985","211","双一流","海外名校(QS50)","海外名校(QS100)","海外硕士","博士","硕士","本科","专科及以下"] },
    { "dimension": "companyBg", "displayName": "公司背景", "enabled": true,
      "allowedValues": ["头部大厂","腰部中厂","外企500强","一线外企","独角兽","明星创业","国企/央企","传统企业转型"] },
    { "dimension": "workYears", "displayName": "工作年限", "enabled": true,
      "allowedValues": ["应届","2-3年","3-5年","5-10年","10-15年","15年+"] },
    { "dimension": "skills",    "displayName": "技能",     "enabled": true, "allowedValues": [] },
    { "dimension": "industry",  "displayName": "行业",     "enabled": true,
      "allowedValues": ["互联网/电商","金融科技","企业服务/SaaS","游戏","在线教育","医疗健康","人工智能","汽车/出行","物联网/硬件","物流/供应链"] }
  ],
  "negativeDimensions": [
    { "dimension": "jobstable", "displayName": "职业稳定性风险", "enabled": true,
      "tags": [
        { "tagName": "频繁跳槽",        "severity": "medium", "enabled": true },
        { "tagName": "最近任职时长偏短", "severity": "medium", "enabled": true },
        { "tagName": "工作空窗期过长",   "severity": "low",    "enabled": true }
      ] },
    { "dimension": "integrity", "displayName": "诚信与合规风险", "enabled": true,
      "tags": [{ "tagName": "工作时间线重叠",   "severity": "high",   "enabled": true }] },
    { "dimension": "matchRisk", "displayName": "匹配度硬伤",     "enabled": true,
      "tags": [{ "tagName": "疑似培训机构背景", "severity": "medium", "enabled": true }] },
    { "dimension": "location",  "displayName": "地域相关",       "enabled": true,
      "tags": [{ "tagName": "自定义敏感词（提交时动态传入）", "severity": "high", "enabled": true }] }
  ],
  "severityDeduction": { "low": -5, "medium": -10, "high": -20 },
  "reloadedAt": "2026-05-22 10:40:00"
}
```

---

## 八、字段速查

### positiveTags 维度枚举

| dimension | displayName | confidence | value[].tag |
|-----------|-------------|:----------:|-------------|
| `education` | 学历背景 | ✗ | C9 / 985 / 211 / 双一流 / 海外名校(QS50) / 海外名校(QS100) / 海外硕士 / 博士 / 硕士 / 本科 / 专科及以下 |
| `companyBg` | 公司背景 | ✅ | 头部大厂 / 腰部中厂 / 外企500强 / 一线外企 / 独角兽 / 明星创业 / 国企/央企 / 传统企业转型 |
| `workYears` | 工作年限 | ✗ | 应届 / 2-3年 / 3-5年 / 5-10年 / 10-15年 / 15年+ |
| `skills`    | 技能 | ✅ | 动态提取，无枚举限制 |
| `industry`  | 行业 | ✅ | 互联网/电商 / 金融科技 / 企业服务/SaaS / 游戏 / 在线教育 / 医疗健康 / 人工智能 / 汽车/出行 / 物联网/硬件 / 物流/供应链 |

### negativeTags 风险维度

| dimension | displayName | 标签 | severity | 扣分 |
|-----------|-------------|------|:--------:|:----:|
| `jobstable` | 职业稳定性风险 | 频繁跳槽 / 最近任职时长偏短 | medium | -10 |
| `jobstable` | 职业稳定性风险 | 工作空窗期过长 | low | -5 |
| `integrity` | 诚信与合规风险 | 工作时间线重叠 | high | -20 |
| `matchRisk` | 匹配度硬伤 | 疑似培训机构背景 | medium | -10 |
| `location`  | 地域相关 | 命中自定义敏感词 | high | -20 |

### gender 枚举

| 值 | 含义 |
|:--:|------|
| `1` | 男 |
| `2` | 女 |
| `0` | 未知 |

---

## 九、错误码

| code | 含义 | 处理建议 |
|:----:|------|---------|
| `0` | 成功 | — |
| `1001` | 参数错误 / URL 下载失败 | 检查 resumes 格式；确认 URL 服务器可达 |
| `1003` | 批量超过 20 份 | 拆分多次提交 |
| `2001` | taskId 不存在或已过期 | 重新提交（TTL=10分钟） |

---

## 十、常见问题

**Q: MinIO URL 中文件名有中文怎么处理？**  
A: 中文需 URL 编码。Python：`urllib.parse.quote(filename)`；Java：`URLEncoder.encode(filename, "UTF-8")`（注意把 `+` 替换为 `%20`）。

**Q: URL 下载返回「文件下载失败」？**  
A: URL 是由**服务器端**发起下载，MinIO 的 `192.168.12.185:9000` 需要在解析服务所在机器上可访问，确认网络互通。

**Q: 提交成功但 `/api/tasks` 看不到？**  
A: 任务 TTL=10 分钟，超时自动清除；或检查是否提交到了不同端口的实例。
