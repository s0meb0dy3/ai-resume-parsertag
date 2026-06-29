# CLAUDE.md — AI-Resume-ParserTag

在此目录工作时请先阅读本文件。

---

## 项目概述

Python FastAPI 服务，将 PDF 简历通过多模态 LLM 解析为结构化 JSON，并对候选人进行动态正向标签打标 + 负面风险检测（规则 + LLM 双模式）。

**技术链路：**

```
PDF 文件
   │
   ▼
PyMuPDF 渲染为图像（144 DPI）
   ├── ≤8 页 ──→ 单次多模态 API 调用
   └── >8 页 ──→ 分批（smart_batch_size）+ ThreadPoolExecutor 并发 → 字段合并
   │
   ▼
Qwen3.x 多模态模型（内网 vLLM，OpenAI 兼容接口）
   │
   ▼
结构化简历 JSON（candidate / educations / works / projects）
   │
   ▼
打标模块（tag/）  — 三阶段流水线
   ├── Phase 1 正向标签：_build_positive_prompt() 动态组装 Prompt → LLM 提取，失败降级规则层
   ├── Phase 2 负向规则：4 个内置硬编码维度 + 自定义 rule 类型维度（_detect_keyword_rules）
   └── Phase 3 负向 LLM：detectionType=llm 的自定义维度 → extract_negative_tags_llm()（可选）
```

---

## 项目结构

```
.
├── app.py                    # 入口：FastAPI 实例、CORS、lifespan、/health、/v1/models
├── config.py                 # 所有环境变量读取；MODEL_SERVICE_URL / API_KEY 未配置时启动报错
│
├── api/                      # HTTP 路由层
│   ├── routes_async.py       # 异步解析+打标接口（6 个路由 + /api/tasks）
│   ├── routes_openai.py      # OpenAI 兼容接口（/v1/chat/completions）+ 同步解析/打标接口
│   ├── routes_tags.py        # 打标配置 CRUD（写接口需 TAGS_ADMIN_TOKEN Bearer Token）
│   ├── routes_algorithm.py   # 算法内部调试接口（独立运行正向/负向打标，跳过解析）
│   └── schemas.py            # Pydantic 请求体模型（AsyncBatchRequest / SyncBatchRequest 等）
│
├── core/                     # 核心业务逻辑
│   ├── parser.py             # PDF → JSON 主流程：run()、_llm_call()、_llm_try_model()
│   ├── postprocess.py        # 后处理：_post_process()、_validate_schema()、_merge_results()、_conform_to_template()
│   ├── tasks.py              # 异步任务状态机（TaskRecord）、流水线（_run_parse_and_tag）、全局线程池 executor
│   └── prompts.py            # 所有 LLM Prompt 常量：PROMPT_HR、BATCH_SYSTEM、POSITIVE_PROMPT_HEADER/DIM_PROMPT/GENERIC_DIM_PROMPT、NEGATIVE_PROMPT_HEADER/GENERIC_NEG_DIM_PROMPT
│
├── utils/                    # 基础工具
│   ├── pdf_utils.py          # PDF 渲染：pdf_to_images()、pdf_to_batches()、get_page_count()、smart_batch_size()
│   └── file_utils.py         # 文件获取：download_file()、get_pdf_source_from_content()（支持 url/file/base64）
│
├── logger/                   # 日志与可观测性
│   ├── logger_manager.py     # 滚动日志 + ContextVar 请求上下文 + watchdog 热更新
│   ├── langfuse_client.py    # Langfuse v3+ 可观测性客户端（未配置时降级为空操作）
│   └── log_config.yml        # 日志配置（watchdog 监听热更新，200ms 防抖）
│
├── tag/                      # 打标模块
│   ├── __init__.py           # 公开接口：run_tagging、load_tag_cfg 等（其他模块通过此处 import）
│   ├── tagger.py             # run_tagging()（三阶段）、extract_positive_tags_llm()、detect_sensitive_tags()、extract_negative_tags_llm()
│   ├── tag_config.py         # 打标维度静态默认配置（代码兜底值，运行时可被 runtime.json 覆盖）
│   └── tag_data.py           # 院校/大厂/行业分类数据（模块加载时初始化，修改后需重启）
│
├── schemas/                  # 配置文件
│   └── tag_config_runtime.json   # 运行时打标配置（热更新目标，优先级高于 tag_config.py）
│
├── test/                     # 测试工具
├── docs/                     # 技术文档
├── localmaterial/            # 本地素材（不提交 git，不打包进镜像）
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

---

## 关键约定

### LLM 调用

- 所有 LLM 调用统一通过 `core.parser._llm_call()`，禁止在其他模块直接调用 `client`
- 支持模型回退链：主模型失败后依次尝试 `FALLBACK_MODELS`（`APIConnectionError` / 401/403 不回退）
- `get_thinking_disable_params(model_name)` 按模型系列（Qwen / Kimi）返回对应的关闭 CoT 参数
- thinking block 签名校验失败时自动去掉 `extra_body` 重试（`_llm_try_model` 内处理）

### 异步任务

- 任务状态：`0` 待处理 → `1` 解析中 → `2` 打标中 → `3` 完成 / `4` 失败
- 所有任务状态修改必须通过 `core.tasks._set_task_field()`，持有 `_task_lock`（asyncio.Lock）
- 阻塞 IO（PDF 渲染、LLM 调用）均通过 `loop.run_in_executor(executor, wrap_for_executor(fn), ...)` 卸载到线程池
- `_parse_semaphore`（asyncio.Semaphore）在 asyncio 层限流，防止 event loop 分发超出线程池容量的任务

### 配置热更新

- 写接口写入 `schemas/tag_config_runtime.json`（原子替换：先写 `.tmp` 再 `os.rename`）
- `_tagger_load_cfg()` 用 mtime 缓存，文件 mtime 未变则跳过 JSON 反序列化
- `_conform_to_template()` 每次调用时动态读取配置，`displayName` 映射修改后立即生效
- **正向 Prompt 动态生成**：`_build_positive_prompt()` 每次打标前按运行时 `positiveDimensions` 重新组装；5 个内置维度用 `DIM_PROMPT` 字典中的详细指令，自定义维度用 `GENERIC_DIM_PROMPT` 模板，新增/删除/禁用维度下次打标即生效
- **负向维度三类路径**：① 4 个硬编码维度（`jobstable/integrity/location/matchRisk`）走专用算法函数；② 自定义 `detectionType: "rule"` 维度由 `_detect_keyword_rules()` 执行通用关键词匹配；③ 自定义 `detectionType: "llm"` 维度由 `extract_negative_tags_llm()` 单独调 LLM，带白名单过滤防止幻觉

### 日志上下文

- 每请求入口由 `_reset_log_context` 中间件重置 `_thread_local`，防止上下文跨请求污染
- 线程池任务必须用 `wrap_for_executor(fn)` 包装，确保 ContextVar 快照正确传递到子线程
- `set_log_context()` 必须在 `wrap_for_executor()` 之前调用，使 `copy_context()` 捕获正确快照
- 日志配置在 `logger/log_config.yml`，修改后通过 watchdog 自动热更新，无需重启

### 输出格式

- 最终输出格式 v3.1，由 `_conform_to_template()` 统一组装（`core/postprocess.py`）
- `positiveTags.tagsArray[].confidence`：`education` 和 `workYears` 维度不输出此字段（规则层确定性计算，见 `_NO_CONF_DIM`）
- education 层级展开：命中 985 → 同时输出 `[985, 211, 双一流]`（见 `EDUCATION_HIERARCHY`）

---

## 开发注意

- `MODEL_SERVICE_URL` 和 `API_KEY` 必须通过 `.env` 或环境变量配置，禁止硬编码 IP
- 服务端口默认 `21804`（容器）/ `30804`（K8s NodePort）
- `TAGS_ADMIN_TOKEN` 未配置时写接口不鉴权（内网隔离部署兼容模式）
- `tag_data.py` 中的院校/大厂名单修改后需重启服务才能生效（模块加载时初始化）
- `tag_config.py` 中 `_ALLOWED_*` 常量在模块加载时从静态默认值构建，运行时配置更新不刷新这些常量
- `schemas/tag_config_runtime.json` 若需跨容器重启持久化，需在 `docker-compose.yml` 中挂载 `/app/schemas` 为 volume
- Langfuse 配置使用 `LANGFUSE_BASE_URL`（优先）或 `LANGFUSE_HOST`，两个 key 均配置时追踪自动启用

---

## 本地运行

```bash
cp .env.example .env
# 填写 API_KEY 和 MODEL_SERVICE_URL（必填）

pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 21804 --reload
```

---

## Docker

```bash
# 构建并启动（推荐）
docker compose up --build -d

# 查看日志
docker logs -f ai-resume-parsertag

# 健康检查
curl http://localhost:21804/health
```

---

## 测试

```bash
# 单文件打标测试
python test/test_tagging.py --file uploads/test_full/PKU刘旗简历.pdf

# 异步并发压力测试
python test/stress_test_async.py --concurrency 4

# 模型回退测试
python test/test_model_fallback.py
```
