# CLAUDE.md - AI-Resume-ParserTag

在此目录工作时，先阅读本文件和 `docs/00_当前实现基线.md`。接口字段以
FastAPI/Pydantic 和 `docs/openapi.json` 为准，标签值以运行时配置与版本化映射为准。

## 项目定位

本项目是 FastAPI 简历解析与打标服务。同步入口支持 PDF、DOCX、JPG、JPEG、PNG；
异步文件入口仅支持视觉 PDF；仅打标入口接收已解析 Resume JSON。服务不保存业务主数据；异步任务只保存在当前进程内存中，调用方负责
轮询和持久化最终结果。

当前主要链路：

```text
文件输入
-> 真实类型与资源预算校验
-> 同步：PDF/DOCX 文本优先抽取，无文本、汉字低熵乱码、图片型文档走视觉降级
-> 异步：PDF 页面直接走视觉解析，不执行 Python 文本抽取
-> LLM 生成标准 Resume
-> Resume 契约归一
-> 正向 LLM 候选召回 + 确定性规则复核/覆盖
-> 负向规则 + 可选自定义负向 LLM
-> 统一输出 mappingVersion/dimensionId/tagId/tag/source
```

## 关键目录

```text
api/
  routes_openai.py       同步接口和 OpenAI 兼容入口
  routes_async.py        异步提交、轮询和任务列表
  routes_tags.py         标签配置和 ID 映射查询
  response_schemas.py    公共响应 Pydantic 契约
core/
  document_text.py       类型检测、PDF/DOCX 文本抽取和安全预算
  parser.py              文本/视觉解析、模型调用和回退
  resume_contract.py     标准 Resume Schema 与归一化
  postprocess.py         结果修复、合并和最终序列化
  tasks.py               异步任务状态机和流水线
  prompting/             解析、正向、负向和 ID Prompt
tag/
  tagger.py              正向召回、规则复核和负向检测
  tag_config.py          默认标签配置
  tag_config_guard.py    运行时结构保护
  tag_value_id_mapper.py ID/维度映射加载和校验
  skill_library.py       标准技能库和别名
  reference_data.py      院校与公司资质数据
schemas/
  tag_config_runtime.json       当前标签值和规则配置
  tag_value_id_mapping.json     版本化维度/标签 ID 唯一事实源
  resume_result_template.json   输出模板
docs/openapi.json         由应用导出的接口快照
```

## 公开接口

- 同步：`/api/resume/sync/parse`、`/api/resume/sync/tag`、
  `/api/resume/sync/batch`。
- 异步：`/api/resume/async/parse`、`/api/resume/async/batch`、
  `/api/resume/async/batch/parse`、`/api/resume/async/batch/tag`、
  `/api/resume/async/batch/multiform`、`/api/resume/async/result`。
- 标签：`/api/tags/config`、`/api/tags/mapping` 和受保护的配置写接口。
- 兼容：`/v1/chat/completions`、`/v1/models`。

`sync/parse` 的 `results[].data` 可直接作为 `sync/tag` 的 `resumes[]` 元素。异步
`batch/parse` 的完成结果也可直接作为异步 `batch/tag` 的输入。

## Resume 契约

公开 Resume 只有四个顶层字段：`candidate`、`educations`、`works`、`projects`。
`works[]` 固定为 `company/position/description/startDate/endDate`，不要加入
`employmentType`、`isInternship` 等字段。

- `candidate=null` 归一为 `{}`；三个数组为 `null` 时归一为 `[]`。
- 数组收到字符串、对象等错误标量时拒绝，不能静默吞掉。
- 当前任职的 `目前/现在/在职/current/present/now` 统一为 `至今`。
- 原文未提供工作结束日期时保持空 `endDate`，不得根据经历顺序推断为 `至今`。
- 实习/兼职由职位和描述中的明确关键词兼容识别，不改变公开 Schema。

## 打标约定

正向固定顺序：`education`、`companyBg`、`workYears`、`skills`、`industry`。

| 维度 | 最终来源 |
|---|---|
| education | `rule`，院校/学历规则覆盖模型 |
| workYears | `rule`，日期并集和毕业窗口覆盖模型 |
| companyBg | `hybrid` 或 `rule`，模型召回后由资质库/明确文本复核 |
| skills | `hybrid` 或 `rule`，模型筛选后由技能库和证据复核 |
| industry | `hybrid` 或 `rule`，模型召回后由强证据和排除语境复核 |
| 内置负向标签 | `rule` |
| 自定义 LLM 负向标签 | `llm` |

`source` 只允许 `rule`、`llm`、`hybrid`，禁止新增或继续输出旧值 `ai`。

工作年限采用左闭右开边界：

```text
10030001 应届
10030002 往届无经验
10030003 1年以下                  [0, 12个月)
10030004 1-3年（满1年）           [12, 36个月)
10030005 3-5年（满3年）           [36, 60个月)
10030006 5-10年（满5年）          [60, 120个月)
10030007 10年及以上               [120个月, +∞)
```

实习和兼职不计入正式工作年限，也不参与频繁跳槽、短任职、空窗和时间线重叠检测。
当前实现没有结构化任职类型字段，依赖职位和描述关键词，因此漏写类型的简历仍有误判风险。

## ID 和配置

- 映射版本当前为 `ai-resume-parsertag:ID-mapping-tag:V1.0.1-20260806`。
- 维度 ID 为 4 位：正向 `1001` 至 `1005`，负向 `2001` 至 `2004`。
- 标签 ID 为 8 位，前四位必须等于维度 ID；维度内部按 ID 递增排列。
- 公共打标结果返回顶层 `mappingVersion`、维度 `dimensionId`、值 `tagId`。
- `GET /api/tags/mapping` 返回完整映射；`GET /api/tags/config` 返回同一
  `mappingVersion`。调用方必须保存 ID、文本快照和版本，不能按数组位置关联。
- `tag_value_id_mapping.json` 是 ID 唯一事实源；`tag_config_runtime.json` 是当前
  启用状态、允许值、规则和扣分事实源。项目不再使用 `tag_taxonomy_v1*`。
- 配置结构已锁定：新增/删除维度和规则返回 HTTP 409；只允许更新现有字段并重置。

## 模型和并发

- 同步入口使用 `SYNC_MODEL_NAME`，异步入口使用 `ASYNC_MODEL_NAME`。
- LLM 调用统一走 `core.parser._call_llm_with_fallback()`；不要在业务模块新建客户端。
- 教育和工作年限虽然出现在正向 Prompt Schema 中，但模型固定返回空数组，最终由规则计算。
- 只有同步解析+打标链可将内部 `skill_evidence_text` 传给打标；异步视觉链和所有
  仅打标入口只有公开 Resume，技能证据覆盖可能略低。
- 阻塞解析和打标通过全局线程池执行；线程池函数使用 `wrap_for_executor()` 传播上下文。
- 异步状态只能通过持锁的任务辅助函数修改：`0 -> 1 -> 2 -> 3/4`。

## 开发与验证

```bash
cp .env.example .env
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 21804 --reload

python scripts/export_openapi.py
python scripts/export_openapi.py --check
pytest -q
```

修改 API、Pydantic 响应或字段后必须重新导出 OpenAPI。修改标签值或 ID 时必须同步
映射版本、配置守卫和映射测试。修改解析 Prompt 时不得改变公开 Resume Schema。

## 部署边界

- `MODEL_SERVICE_URL` 和 `API_KEY` 必填，不得硬编码内网地址或密钥。
- 任务存储在单进程内存，当前生产部署应保持单进程/单副本。
- `TAGS_ADMIN_TOKEN` 未配置时写接口不鉴权，只适合可信隔离网络。
- `uploads/`、`results/`、`logs/` 和完整响应可能包含 PII，生产环境必须设置访问和清理策略。
- `localmaterial/offline` 当前打包资产不是最新代码基线；重新执行
  `scripts/package-offline.sh` 后才可作为本版本离线交付物。
