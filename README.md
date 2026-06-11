# AI-Resume-ParserTag

基于 Qwen3.5 多模态大模型的 PDF 简历解析与智能打标服务，输出结构化 JSON 并自动完成正向/负向标签标注。

---

## 技术架构

```
PDF 上传
   │
   ▼
PyMuPDF 渲染为图像（144 DPI）
   │
   ├── ≤8 页 ──→ 单次多模态 API 调用
   └── >8 页 ──→ 分批（2页/批）+ ThreadPoolExecutor 并发 → 字段合并
   │
   ▼
Qwen3.5-397B-A17B（内网 vLLM 服务，OpenAI 兼容接口）
   │
   ▼
结构化 JSON（候选人基本信息、工作经历、教育背景等）
   │
   ▼
打标模块（tag/）
   ├── 正向标签：学历、公司背景、行业经验、技能匹配等
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
│   ├── routes_openai.py     # OpenAI 兼容接口
│   ├── routes_tags.py       # 打标配置 CRUD（Bearer Token 鉴权）
│   └── routes_algorithm.py  # 算法内部接口（仅打标，无解析）
├── core/                    # 业务核心
│   ├── parser.py            # PDF 解析（LLM 调用 + 多批并发）
│   ├── tasks.py             # 异步任务状态机与流水线编排
│   ├── postprocess.py       # 后处理、Schema 校验、批次合并、序列化
│   └── prompts.py           # LLM Prompt 常量集中管理
├── logger/                  # 日志子系统
│   ├── logger_manager.py    # 日志管理（滚动文件 + 请求上下文过滤）
│   ├── langfuse_client.py   # Langfuse 可观测性客户端
│   └── log_config.yml       # 日志配置（支持热更新）
├── utils/                   # 工具函数
│   ├── pdf_utils.py         # PDF 渲染工具（fitz + PIL）
│   └── file_utils.py        # 文件下载与 PDF 来源识别
├── tag/                     # 打标模块
│   ├── tagger.py            # 正向标签提取 + 敏感词检测
│   ├── tag_config.py        # 打标维度默认配置
│   └── tag_data.py          # 学历/公司/行业分类数据
├── schemas/                 # JSON Schema 与运行时配置
│   ├── resume_result_template.json        # 输出字段模板
│   ├── tag_config_runtime.json            # 运行时打标配置（可热更新）
│   ├── tag_static_data.json               # 静态标签数据
│   └── AIHR_Resume_Postman_Collection.json  # Postman 测试集合
├── test/                    # 测试工具
│   ├── test_tagging.py      # 打标功能测试
│   ├── stress_test_async.py # 并发压力测试
│   ├── test_minio_url.py    # MinIO URL 测试
│   └── upload_resume.py     # CLI 上传工具
├── docs/                    # 文档
│   ├── AI智能打标技术方案.md
│   ├── Postman使用说明.md
│   └── 优化方向分析.md
├── uploads/                 # 上传文件暂存（运行时生成）
├── results/                 # 解析结果缓存（运行时生成）
├── logs/                    # 日志目录（运行时生成）
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .dockerignore
├── .gitlab-ci.yml
└── requirements.txt
```

---

## API 接口

### 业务接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/resume/async/parse` | 上传简历，异步解析+打标，返回 `taskId` |
| `GET`  | `/api/resume/async/result` | 轮询任务结果（`?taskId=xxx`） |
| `POST` | `/api/resume/async/batch` | 批量异步解析（JSON 数组，base64 编码） |
| `POST` | `/api/resume/async/batch/multiform` | 批量异步解析（multipart 文件上传） |
| `GET`  | `/api/tasks` | 查询当前所有任务状态 |

### 打标配置接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET`    | `/api/tags/config` | 获取当前打标配置 |
| `POST`   | `/api/tags/config/reset` | 重置为默认配置 |
| `POST`   | `/api/tags/config/positive` | 新增正向维度 |
| `PUT`    | `/api/tags/config/positive/{dimension}` | 更新正向维度 |
| `DELETE` | `/api/tags/config/positive/{dimension}` | 删除正向维度 |
| `PUT`    | `/api/tags/config/negative/{dimension}` | 更新负向维度 |
| `PUT`    | `/api/tags/config/severity` | 更新扣分等级 |

### OpenAI 兼容接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET`  | `/v1/models` | 返回可用模型列表 |
| `POST` | `/v1/chat/completions` | `model=DeepSeek-OCR`（仅解析）或 `model=DeepSeek-OCR-JSON`（解析+打标） |
| `POST` | `/v1/batch/resumes` | 批量解析，最多 50 份 |

### 算法内部接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/algorithm/positive-tag/extract` | 从结构化简历中提取正向标签 |
| `POST` | `/algorithm/sensitive-tag/detect` | 敏感词/风险检测 |

### 健康检查

```
GET /health
```

---

## 环境变量

参考 `.env.example` 创建 `.env`：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_KEY` | —（**必填**） | 模型服务 API Key |
| `MODEL_SERVICE_URL` | —（**必填**） | 模型服务地址（OpenAI 兼容接口） |
| `MODEL_NAME` | `Qwen3.5-397B-A17B` | 模型名称 |
| `TEMPERATURE` | `0.01` | 推理温度 |
| `MAX_TOKENS` | `4096` | 最大输出 token |
| `MAX_WORKERS` | `24` | 并发线程数 |
| `API_TIMEOUT` | `300` | 单次推理超时（秒） |
| `MAX_RETRIES` | `2` | 失败重试次数 |
| `DOWNLOAD_TIMEOUT` | `60` | 文件下载超时（秒） |
| `SERVICE_PORT` | `21804` | 服务监听端口 |
| `LOG_DIR` | `./logs` | 日志目录 |
| `LOG_KEEP_DAYS` | `7` | 日志保留天数 |
| `LOG_MAX_MB` | `50` | 单日志文件最大 MB |
| `MAX_FILE_SIZE_MB` | `20` | 上传文件大小上限 |
| `TASK_TTL_MINUTES` | `10` | 异步任务结果保留时长 |
| `TAGS_ADMIN_TOKEN` | `""` | 打标配置写接口 Bearer Token；为空时跳过鉴权 |
| `LANGFUSE_SECRET_KEY` | `""` | Langfuse 密钥（可选，两个 key 均填才启用追踪） |
| `LANGFUSE_PUBLIC_KEY` | `""` | Langfuse 公钥（可选） |
| `LANGFUSE_HOST` | `http://localhost:3100` | Langfuse 服务地址 |

---

## 日志系统

### 文件结构

```
logs/
├── debug/
│   └── YYYY-MM/
│       └── debug_YYYY-MM-DD_<pid>.log   # 全量日志（DEBUG 及以上）
└── error/
    └── YYYY-MM/
        └── error_YYYY-MM-DD_<pid>.log   # 仅 ERROR 级别
```

每天零点自动滚动；单文件超 50MB 提前轮转。

### 日志格式

```
2026-06-02 10:30:45 | INFO | [a1b2c3d4] [简历解析] [张三.pdf] [-] | [parser.py:128 _log_result] | 解析结果 文件: 张三.pdf 耗时: 32s
```

字段：`时间 | 级别 | [request_id] [业务类型] [文件名] [扩展] | [文件:行号 函数] | 消息`

### 每次任务的三段日志块

| 序号 | 标题 | 内容 |
|------|------|------|
| ① | `解析结果 文件: xxx 耗时: Ns` | 候选人基础信息摘要 + 完整解析 JSON |
| ② | `打标结果 文件: xxx taskId: xxx` | 正向标签（含置信度）/ 风险标签 / 扣分合计 |
| ③ | `完整输出 JSON 文件: xxx taskId: xxx` | 与接口返回完全一致的完整 JSON |

同步落盘到 `results/<日期>_<时间>_<姓名>_<taskId前8位>.json`。

### 配置文件（`logger/log_config.yml`）

```yaml
log_config:
  name: aihr
  log_level_console: INFO
  log_level_file: DEBUG
  log_folder: logs
  log_pid: ""          # 空 = 进程 PID；生产建议填固定标识
  max_bytes: 52428800  # 50MB
  use_queue: false     # Gunicorn 多 worker 时改为 true
  json_format: false   # true = 单行 JSON（ELK/Loki 场景）
```

支持热更新（watchdog 监听，200ms 防抖后自动生效，无需重启）。

---

## 本地启动

```bash
cp .env.example .env
# 编辑 .env，填写 API_KEY 和 MODEL_SERVICE_URL

pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 21804
```

---

## Docker 部署

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

CI/CD 通过 GitLab Pipeline 自动构建并部署到 Kubernetes（`demo` 命名空间，NodePort `30804`），触发分支：`dev` / `main`。

---

## 打标配置热更新

打标维度支持运行时修改，无需重启。修改后写入 `schemas/tag_config_runtime.json`，服务重启后自动加载；调用 `POST /api/tags/config/reset` 可恢复默认配置。

---

## 测试

```bash
# 单文件打标测试
python test/test_tagging.py --file uploads/test_full/张三.pdf

# 批量打标测试
python test/test_tagging.py --all

# 并发压力测试（默认读取 uploads/test_full/）
python test/stress_test_async.py --concurrency 4

# CLI 上传工具
python test/upload_resume.py resume.pdf --url http://localhost:21804
```
