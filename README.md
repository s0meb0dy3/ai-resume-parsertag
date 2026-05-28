# AIHR 简历解析与打标服务

基于 Qwen3.5 多模态大模型的 PDF 简历解析服务，输出结构化 JSON 并自动完成正向/负向标签打标。

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
Qwen3.5-397B-A17B（内网 vLLM 服务）
   │
   ▼
结构化 JSON（候选人基本信息、工作经历、教育背景等）
   │
   ▼
打标模块（tag/）
   ├── 正向标签：学历、公司背景、行业经验、技能匹配等
   └── 负向标签：敏感词、风险项、扣分维度
```

**模型服务**：通过 `MODEL_SERVICE_URL` 环境变量配置（OpenAI 兼容接口，必填）  
**服务端口**：`21804`（容器）/ `30804`（Kubernetes NodePort）

---

## 项目结构

```
.
├── app.py                   # 入口：FastAPI 实例 + lifespan + /health
├── config.py                # 环境变量与全局配置
├── logger_manager.py        # 日志管理（滚动文件 + 请求上下文过滤）
├── parser.py                # PDF 解析核心（LLM 调用 + 多批并发）
├── tasks.py                 # 异步任务管理（状态机 + 流水线编排）
├── postprocess.py           # 后处理、Schema 校验、批次合并、输出序列化
├── pdf_utils.py             # PDF 渲染工具（fitz + PIL）
├── file_utils.py            # 文件下载与 PDF 来源识别
├── prompts.py               # LLM Prompt 常量集中管理
├── api/                     # FastAPI 路由模块
│   ├── __init__.py
│   ├── routes_async.py      # 异步解析+打标接口
│   ├── routes_openai.py     # OpenAI 兼容接口
│   ├── routes_tags.py       # 打标配置 CRUD（Bearer Token 鉴权）
│   └── routes_algorithm.py  # 算法内部接口（仅打标，无解析）
├── tag/                     # 打标模块
│   ├── __init__.py
│   ├── tagger.py            # 正向标签提取 + 敏感词检测
│   ├── tag_config.py        # 打标维度默认配置
│   └── tag_data.py          # 学历/公司/行业分类数据
├── schemas/                 # JSON Schema 与运行时配置
│   ├── resume_result_template.json   # 输出字段模板
│   ├── tag_config_runtime.json       # 运行时打标配置（可热更新）
│   ├── tag_static_data.json          # 静态标签数据
│   └── AIHR_Resume_Postman_Collection.json  # Postman 测试集合
├── test/                    # 测试工具
│   ├── test_tagging.py      # 打标功能测试
│   ├── stress_test_async.py # 并发压力测试
│   ├── test_minio_url.py    # MinIO URL 测试
│   └── upload_resume.py     # CLI 上传工具
├── docs/                    # 文档
│   ├── api.md               # 接口文档
│   ├── Postman使用说明.md
│   └── AI智能打标技术方案.md
├── uploads/                 # 上传文件暂存目录（运行时生成）
├── results/                 # 解析结果缓存目录（运行时生成）
├── logs/                    # 日志目录（运行时生成）
├── Dockerfile
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

### OpenAI 兼容接口（供旧系统对接）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET`  | `/v1/models` | 返回可用模型列表 |
| `POST` | `/v1/chat/completions` | 兼容 OpenAI 格式，`model=DeepSeek-OCR`（仅解析）或 `model=DeepSeek-OCR-JSON`（解析+打标） |
| `POST` | `/v1/batch/resumes` | 批量解析，最多 50 份 |

### 算法内部接口（供内部服务/调试使用）

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

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MODEL_SERVICE_URL` | —（**必填**） | 模型服务地址（OpenAI 兼容接口，无默认值） |
| `API_KEY` | *(内置)* | 模型服务 API Key |
| `MODEL_NAME` | `Qwen3.5-397B-A17B` | 模型名称 |
| `TEMPERATURE` | `0.01` | 推理温度 |
| `MAX_TOKENS` | `4096` | 最大输出 token |
| `MAX_WORKERS` | `24` | 并发线程数 |
| `API_TIMEOUT` | `300` | 单次推理超时（秒） |
| `MAX_RETRIES` | `2` | 失败重试次数 |
| `SERVICE_PORT` | `21804` | 服务监听端口 |
| `LOG_DIR` | `./logs` | 日志目录 |
| `LOG_KEEP_DAYS` | `7` | 日志保留天数 |
| `LOG_MAX_MB` | `50` | 单日志文件最大 MB |
| `MAX_FILE_SIZE_MB` | `20` | 上传文件大小上限 |
| `TASK_TTL_MINUTES` | `10` | 异步任务结果保留时长 |
| `TAGS_ADMIN_TOKEN` | `""（不鉴权）` | 打标配置写接口 Bearer Token；为空时跳过鉴权 |

---

## 本地启动

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 21804
```

---

## Docker 部署

```bash
# 构建镜像
docker build -t aihr-ocr:latest .

# 运行容器
docker run -d \
  -p 21804:21804 \
  -e MODEL_SERVICE_URL=http://<your-llm-host>:13001/v1 \
  aihr-ocr:latest
```

CI/CD 通过 GitLab Pipeline 自动构建并部署到 Kubernetes（`demo` 命名空间，NodePort `30804`），触发分支：`dev` / `main`。

---

## 打标配置热更新

打标维度配置支持运行时修改，无需重启服务。修改后写入 `schemas/tag_config_runtime.json`，服务重启后自动加载；调用 `POST /api/tags/config/reset` 可恢复默认配置。

---

## 测试

```bash
# 单文件打标测试
python test/test_tagging.py --file uploads/test_full/PKU刘旗简历.pdf

# 批量打标测试
python test/test_tagging.py --all

# 并发压力测试（默认读取 uploads/test_full/）
python test/stress_test_async.py --concurrency 4

# CLI 上传工具
python test/upload_resume.py resume.pdf --url http://localhost:21804
```
