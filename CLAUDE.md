# CLAUDE.md — AIHR Resume Parser

项目指南：在此目录工作时请先阅读本文件。

## 项目概述

Python FastAPI 服务，将 PDF 简历通过多模态 LLM 解析为结构化 JSON，并对候选人进行 5 维度正向标签打标 + 4 维度负面风险检测。

## 模块结构

| 文件 | 职责 |
|------|------|
| `app.py` | 入口：FastAPI 实例、CORS、lifespan、`/health` |
| `config.py` | 所有环境变量读取；`MODEL_SERVICE_URL` 为必填项，未配置时启动报错 |
| `parser.py` | PDF 解析核心：`run()`、`_llm_call()`、`_parse_with_logging()` |
| `tasks.py` | 异步任务状态机（`TaskRecord`）、流水线编排（`_run_parse_and_tag`）、线程池 `executor` |
| `postprocess.py` | `_post_process()`、`_validate_schema()`、`_merge_results()`、`_conform_to_template()` |
| `pdf_utils.py` | `pdf_to_images()`、`pdf_to_batches()`、`smart_batch_size()`，DPI=144 |
| `file_utils.py` | `get_pdf_source_from_content()`、`download_file()` |
| `prompts.py` | 所有 LLM prompt 常量；`COMPANY_SCHEMA`、`PROMPT_HR`、`POSITIVE_TAG_PROMPT_TPL` |
| `logger_manager.py` | 滚动日志 + `_ContextFilter`（请求上下文）+ `_NoiseFilter`（过滤 `/health` 日志） |
| `api/routes_async.py` | 异步解析+打标接口（5个路由） |
| `api/routes_openai.py` | OpenAI 兼容接口（`/v1/chat/completions`、`/v1/batch/resumes`） |
| `api/routes_tags.py` | 打标配置 CRUD（写接口需 `TAGS_ADMIN_TOKEN` Bearer Token） |
| `api/routes_algorithm.py` | 算法内部接口（仅打标，无解析） |
| `tag/tagger.py` | `run_tagging()`、`extract_positive_tags_llm()`、`detect_sensitive_tags()` |
| `tag/tag_config.py` | 打标维度默认配置（兜底值） |
| `tag/tag_data.py` | 院校/大厂/行业静态数据（启动时加载，改后需重启） |
| `schemas/tag_config_runtime.json` | 运行时配置（热更新目标，优先级高于 `tag_config.py`） |

## 关键约定

### LLM 调用
- 所有 LLM 调用统一通过 `parser._llm_call()`，不要在其他模块直接调用 `client`
- `extra_body=_THINKING_DISABLE_PARAMS` 用于关闭 thinking block；遇到签名错误时自动去掉重试

### 异步任务
- 任务状态：`0` 待处理 → `1` 解析中 → `2` 打标中 → `3` 完成 / `4` 失败
- 所有任务状态修改通过 `tasks._set_task_field()`，持有 `_task_lock`（asyncio.Lock）
- 阻塞 IO（PDF 渲染、LLM 调用）均通过 `loop.run_in_executor(executor, ...)` 卸载到线程池

### 配置热更新
- 写接口写入 `schemas/tag_config_runtime.json`（原子替换：先写 `.tmp` 再 `rename`）
- `_tagger_load_cfg()` 用 mtime 缓存，文件未变则不重新读取
- `_conform_to_template()` 每次调用时动态读取配置，`displayName` 映射立即生效

### 日志上下文
- `_thread_local.request_id / business_type / filename` 在每个请求开始时由 `_reset_log_context` 中间件重置
- 不要在模块级别缓存 `_thread_local` 值

### 输出序列化
- 最终输出格式 v3.1，由 `postprocess._conform_to_template()` 统一组装
- `positiveTags.tagsArray[].confidence`：`education` 和 `workYears` 维度不输出此字段（见 `_NO_CONF_DIM`）

## 开发注意

- `MODEL_SERVICE_URL` 必须通过 `.env` 或环境变量配置，不要写硬编码 IP
- 服务端口默认 `21804`（容器）/ `30804`（K8s NodePort）
- `TAGS_ADMIN_TOKEN` 未配置时写接口不鉴权（内网部署兼容模式）
- `tag_data.py` 中的院校/大厂名单修改后需重启服务才能生效
- `schemas/tag_config_runtime.json` 若需跨容器重启持久化，挂载 `/app/schemas` 为 volume

## 本地运行

```bash
cp .env.example .env          # 填写 MODEL_SERVICE_URL
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 21804 --reload
```

## Docker

```bash
docker build -t aihr-ocr:latest .
docker run -d -p 21804:21804 --env-file .env aihr-ocr:latest
```

## 测试

```bash
python test/test_tagging.py --file uploads/test_full/PKU刘旗简历.pdf
python test/stress_test_async.py --concurrency 4
```
