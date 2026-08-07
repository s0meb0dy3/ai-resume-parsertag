# AI 简历智能打标技术方案

## 文档信息

| 项目 | 内容 |
|---|---|
| 版本 | v4.0 |
| 更新日期 | 2026-08-07 |
| 适用代码 | FastAPI `3.1.0` |
| 标签映射 | `ai-resume-parsertag:ID-mapping-tag:V1.0.1-20260806` |
| 事实源 | `tag/`、`core/postprocess.py`、运行时配置、映射 JSON 和测试 |

接口请求与响应细节见 [03_接口设计文档.md](03_接口设计文档.md)，当前实现摘要见
[00_当前实现基线.md](00_当前实现基线.md)。本文只解释打标的调用过程和判定依据。

## 1. 设计目标

打标不是让大模型自由生成标签，而是让模型负责语义召回，让确定性规则、参考库和
最终约束器负责可复现性。这样可以同时利用模型对自然语言的理解能力，并限制幻觉、
别名漂移、标签膨胀和版本错配。

```text
标准 Resume + 岗位上下文
-> 正向候选 Prompt
-> 教育/工作年限规则覆盖
-> 公司/技能/行业规则复核
-> 内置负向规则
-> 可选自定义负向 LLM
-> enabled/allowedValues/数量/顺序约束
-> 映射 ID 注入
```

## 2. 入口与复用关系

| 入口 | 是否解析文件 | 是否打标 | 模型上下文 |
|---|:---:|:---:|---|
| `/api/resume/sync/parse` | 是 | 否 | `SYNC_MODEL_NAME` |
| `/api/resume/sync/tag` | 否 | 是 | `SYNC_MODEL_NAME` |
| `/api/resume/sync/batch` | 是 | 是 | `SYNC_MODEL_NAME` |
| `/api/resume/async/batch/parse` | 是 | 否 | `ASYNC_MODEL_NAME` |
| `/api/resume/async/batch/tag` | 否 | 是 | `ASYNC_MODEL_NAME` |
| `/api/resume/async/parse`、`/batch`、`/multiform` | 是 | 是 | `ASYNC_MODEL_NAME` |

同步与异步都复用 `tag.tagger.run_tagging()` 和
`core.postprocess._conform_to_template()`，因此规则、允许值、ID 和输出顺序一致。
解析策略和证据存在明确差异：

- 同步与异步配置了不同模型，LLM 召回候选可能不同。
- 同步解析+打标可以把 Python 抽取原文作为内部技能证据；异步多格式文档统一转为
  图片并直接视觉解析，因此只使用视觉模型生成的公开 Resume 作为技能证据。
- 同步和异步文件入口均支持 PDF/DOCX/JPEG/PNG；两者区别是文本优先与纯视觉策略。

## 3. 输入契约

`run_tagging()` 接收标准 Resume 和岗位上下文。Resume 由
`core.resume_contract.normalize_resume_payload()` 统一校验，公开结构不包含
`employmentType`：

```json
{
  "candidate": {},
  "educations": [],
  "works": [
    {
      "company": "",
      "position": "",
      "description": "",
      "startDate": "",
      "endDate": ""
    }
  ],
  "projects": []
}
```

岗位上下文 `jobTitle/requiredSkills/jobDescription` 只能辅助理解，不能作为候选人
已经具备技能或经历的证据。`jobSensitiveWords` 会作为请求级负向关键词；当前地域
规则还预设“广西”。

## 4. Prompt 调用

### 4.1 正向标签 Prompt

调用位置：

```text
run_tagging()
-> extract_positive_tags_llm()
-> _build_positive_prompt()
-> core.parser._call_llm_with_fallback()
```

模板位于 `core/prompting/positive_tags.py`：

- `POSITIVE_PROMPT_HEADER`：输入 Resume、岗位参考、证据边界、输出 Schema。
- `DIM_PROMPT`：五个内置维度的专用规则。
- `GENERIC_DIM_PROMPT`：兼容自定义正向维度；当前配置结构已锁定，API 不能新增维度。

教育和工作年限在 Prompt 中被要求固定输出空数组，最终完全由规则覆盖。技能 Prompt
不会注入 300 项全量列表，而是注入规则层从 Resume 和内部原文召回的候选集合。

### 4.2 负向标签 Prompt

模板位于 `core/prompting/negative_tags.py`。只有运行时配置中
`detectionType="llm"` 的自定义负向维度才调用该 Prompt。当前四个内置负向维度均走
本地规则，不调用负向大模型。

每个模型风险命中必须返回维度、标签、严重程度和可复核证据；模型值随后经过维度、
规则名、severity 白名单过滤。

### 4.3 解析 Prompt 与打标的关系

文件入口先调用 `core/prompting/resume_parsing.py` 中的解析 Prompt，生成 Resume，
之后才调用正向打标 Prompt。仅打标接口不调用解析 Prompt。Prompt 顺序如下：

```text
解析+打标：resume_parsing -> positive_tags -> 可选 negative_tags
仅打标：positive_tags -> 可选 negative_tags
仅解析：resume_parsing
```

## 5. 五个正向维度

### 5.1 教育背景 `1001/education`

依据：全部 `educations[].school`、`education/degree`、版本化院校库和最高学历。

- 模型结果不参与最终判定，`source=rule`。
- C9 命中时层级展开为 C9、985、211、双一流。
- 985 展开为 985、211、双一流；211 展开为 211、双一流。
- QS50 同时输出 QS50 和 QS100；中国内地院校不生成“海外名校”。
- 海外硕士、博士、硕士、本科等学历值独立判断。
- 学校和学历均无法识别时输出空数组，不默认本科。

### 5.2 公司背景 `1002/companyBg`

依据：`works[].company`、公司资质参考库、任职公司自身明确资质文本和 LLM 候选。

1. LLM 召回规模、所有制、资本状态和官方资质候选。
2. 规则用公司别名解析实体，查询版本化公司资质快照。
3. 不在库中的公司，只接受描述中与任职公司直接绑定的明确资质证据。
4. 客户、供应商、合作方的资质不得归到候选人的任职公司。
5. 模型候选通过规则复核后为 `hybrid`；参考库独立补齐为 `rule`。

同一公司可以同时命中上市公司、高新技术企业、专精特新等多个客观资质。

### 5.3 工作年限 `1003/workYears`

依据：教育结束时间、正式工作时间区间和当前统计日期，不使用 LLM 估算。

| 标签 ID | 标签 | 左闭右开定义 |
|---|---|---|
| `10030001` | 应届 | 毕业日期在应届窗口且无正式工作证据 |
| `10030002` | 往届无经验 | 已过应届窗口且无正式工作证据 |
| `10030003` | 1年以下 | `[0,12)` 月，且存在有效正式区间 |
| `10030004` | 1-3年（满1年） | `[12,36)` 月 |
| `10030005` | 3-5年（满3年） | `[36,60)` 月 |
| `10030006` | 5-10年（满5年） | `[60,120)` 月 |
| `10030007` | 10年及以上 | `[120,+∞)` 月 |

正式工作区间先合并再累计，避免并行任职重复计时。实习、兼职、校园经历和科研助理
通过职位/描述关键词识别，不进入正式月份。日期不可计算且无法判断应届/往届时，
该维度值为空，不伪造兜底标签。所有结果 `source=rule`。

### 5.4 技能标签 `1004/skills`

依据：300 个标准技能、分类、唯一别名、结构化 Resume 和解析链内部原文证据。

1. 规则层先做标准值/别名召回并收集证据位置。
2. 排除“不熟悉、未使用、仅了解、计划学习”、岗位要求、优先条件和纯课程列举。
3. 短词和共享别名需要边界或上下文，歧义别名不做无条件映射。
4. 只把候选集合放进技能 Prompt，由模型结合经历描述筛选和排序。
5. 模型漏提但证据明确时由规则补齐；最终按标准库顺序去重，最多 12 个。

模型候选通过证据复核为 `hybrid`，纯规则补齐为 `rule`。只有同步解析+打标链拥有
内部 Python 原文；异步视觉链和同步/异步仅打标都只使用公开 Resume，因此技能结果
可能少于同步组合链。

### 5.5 行业背景 `1005/industry`

依据：任职公司主营、工作/项目强关键词、辅助关键词、排除语境和 LLM 候选。

- 辅助词如“平台、网络、在线、教育”不能单独触发行业。
- “使用 AI 工具”不等于人工智能行业；客户项目也不能无条件改变任职公司行业。
- 模型值和规则召回经过同一强证据复核，最多输出 3 个。
- 通过规则复核的模型候选为 `hybrid`，纯规则结果为 `rule`。

## 6. 负向维度

| 维度 ID | 维度 | 规则 | 主要判定 |
|---|---|---|---|
| `2001` | `jobstable` | 频繁跳槽 | 近 `window_years` 内正式任职数达到阈值 |
| `2001` | `jobstable` | 最近任职时长偏短 | 最近一段已结束正式任职短于阈值 |
| `2001` | `jobstable` | 工作空窗期过长 | 相邻正式任职间隔超过阈值 |
| `2002` | `integrity` | 工作时间线重叠 | 两段正式工作重叠达到 2 个月 |
| `2003` | `matchRisk` | 疑似培训机构背景 | 教育/培训文本命中配置关键词 |
| `2004` | `location` | 敏感地域 | 命中预设“广西”或请求敏感词 |

稳定性和时间线重叠检测均排除实习与兼职。当前未增加结构化任职类型字段，依赖
`position/description` 的显式关键词，因此结果应作为人工复核提示，不应直接作为
诚信定性或自动淘汰依据。内置命中均为 `source=rule`。

## 7. 最终约束与输出

`core.postprocess._conform_to_template()` 是公共结果的最后边界：

1. 读取当前运行时配置，过滤禁用维度和非允许值。
2. 强制正向顺序并限制技能、行业数量。
3. 再次执行教育确定性覆盖。
4. 规范 `source` 为 `rule/llm/hybrid`；旧内部值 `ai` 转为 `llm` 兼容。
5. 根据映射文件注入顶层版本、维度 ID 和标签 ID。

示例：

```json
{
  "mappingVersion": "ai-resume-parsertag:ID-mapping-tag:V1.0.1-20260806",
  "positiveTags": {
    "tagsArray": [
      {
        "dimensionId": "1003",
        "dimension": "workYears",
        "displayName": "工作年限",
        "value": [
          {"tagId": "10030005", "tag": "3-5年（满3年）", "source": "rule"}
        ]
      }
    ]
  },
  "negativeTags": {
    "hitCount": 1,
    "deductionScore": -20,
    "tagsArray": [
      {
        "dimensionId": "2004",
        "dimension": "location",
        "displayName": "地域相关",
        "value": [
          {
            "tagId": "20040001",
            "tag": "敏感地域",
            "severity": "high",
            "matchedContent": "广西",
            "source": "rule"
          }
        ]
      }
    ]
  }
}
```

教育和工作年限不输出 `confidence`；公司和行业可使用模型或规则证据置信度。置信度
不是跨维度可比较的统计概率，不应直接用于统一排序。

## 8. 映射和存储

`schemas/tag_value_id_mapping.json` 当前包含 9 个维度和 356 条标签记录。维度 ID 为
4 位，标签 ID 为 8 位且前四位等于所属维度 ID。维度和值均按 ID 递增排列，顺序
用于稳定展示，但后端必须按 ID 关联。

后端建议保存：

```text
resume_id
mapping_version
dimension_id, dimension_code, dimension_name_snapshot
tag_id, tag_name_snapshot, source
severity, matched_content, deduction_snapshot  # 负向命中
```

首次接入或发现未知版本时调用 `GET /api/tags/mapping` 刷新字典。标签改名时保留 ID，
标签含义发生变化、拆分或合并时必须分配新 ID 并升级 `mappingVersion`。

## 9. 配置热更新和结构锁定

`schemas/tag_config_runtime.json` 管理启用状态、展示名、允许值、负向规则和扣分。
读取使用 mtime 缓存；配置 API 写入后新任务立即生效。

结构守卫禁止增删维度和规则，相关兼容接口返回 HTTP 409。允许值修改必须先在映射
文件中建立唯一 ID，否则保存失败。生产环境必须配置 `TAGS_ADMIN_TOKEN` 并持久化
`schemas/` 卷。

## 10. 已知边界和优化点

- 仅打标链没有原始抽取文本，技能证据覆盖与组合链可能不同。
- 实习/兼职依赖文本关键词，缺失明确任职类型时仍可能污染年限或风险规则。
- 公司和行业参考数据需要版本化维护，不能只靠模型常识。
- 内存任务不跨进程，当前异步服务不适合多副本轮询。
- 负向标签可能涉及合规风险，应保留证据和人工复核，不直接自动决策。
- 大模型不能替代确定性日期计算和事实库；全部改为纯 LLM 会降低一致性，且未必更快，
  因为当前正向候选已经在一次调用中完成，规则复核主要是本地计算。

## 11. 验证

```bash
pytest -q
python scripts/export_openapi.py --check
```

2026-08-07 当前自动化基线为 `212 passed`。真实模型准确率和耗时需使用固定模型、
固定映射版本和带日期的真实简历评测报告单独验收。
