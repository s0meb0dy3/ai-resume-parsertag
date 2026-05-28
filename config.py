"""
全局配置、常量与目录初始化。

职责：
  - 从环境变量（或 .env）读取所有运行时参数，集中在此文件管理，
    其他模块只做 import，不直接调用 os.getenv。
  - 根据 MODEL_NAME 自动计算 _THINKING_DISABLE_PARAMS，
    供 parser.py 构造 extra_body。
  - 初始化 uploads / results 目录（不存在时自动创建）。
  - 重新导出 logger / set_log_context 等日志工具，保持其他模块的
    import 路径稳定（from config import logger）。
"""
import os
from pathlib import Path

from dotenv import load_dotenv
# 本地开发：自动加载同目录 .env；生产容器不存在 .env 时静默跳过
load_dotenv()

from logger_manager import (        # noqa: F401  供其他模块 from config import
    logger,
    set_log_context,
    set_extra_context,
    clear_extra_context,
    wrap_for_executor,
    get_logger_manager,
)

# ── 模型 & 推理配置 ───────────────────────────────────────────────
# 所有变量必须通过环境变量注入，无内置默认值，防止将内网 IP 烘焙进镜像
MODEL_SERVICE_URL = os.getenv("MODEL_SERVICE_URL", "")
if not MODEL_SERVICE_URL:
    raise RuntimeError("环境变量 MODEL_SERVICE_URL 未设置，请在 .env 或启动命令中配置")
API_KEY           = os.getenv("API_KEY", "")
if not API_KEY:
    raise RuntimeError("环境变量 API_KEY 未设置，请在 .env 或启动命令中配置")
MODEL_NAME   = os.getenv("MODEL_NAME",   "Qwen3.5-397B-A17B")
TEMPERATURE  = float(os.getenv("TEMPERATURE", "0.01"))  # 低温度减少幻觉，稳定 JSON 输出
MAX_TOKENS   = int(os.getenv("MAX_TOKENS",    "4096"))
MAX_WORKERS  = int(os.getenv("MAX_WORKERS",   "24"))     # 全局线程池上界，同时也是 Semaphore 并发上限
API_TIMEOUT  = int(os.getenv("API_TIMEOUT",   "300"))    # 单次 LLM 调用超时（秒）
MAX_RETRIES  = int(os.getenv("MAX_RETRIES",   "2"))      # OpenAI 客户端自动重试次数
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "60"))  # URL 文件下载超时（秒）

# ── 模型思考模式关闭参数 ──────────────────────────────────────────
# 不同厂商对"关闭思考链"的参数名不一致，需按模型系列分别处理：
#   Qwen 系列：chat_template_kwargs.enable_thinking = false
#   Kimi / DeepSeek 系列：chat_template_kwargs.thinking = false
# 以下参数将作为 extra_body 传递给 OpenAI 兼容接口。
# reasoning_effort="high" 在关闭思考时仍建议保留，部分服务端会忽略它。
_THINKING_DISABLE_PARAMS: dict = (
    {"chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "high"}}
    if MODEL_NAME.lower().startswith("qwen")
    else {"chat_template_kwargs": {"thinking": False, "reasoning_effort": "high"}}
)

# ── PDF 处理配置 ──────────────────────────────────────────────────
PDF_DPI            = 144          # 渲染分辨率：144 DPI = 72×2，平衡清晰度与内存占用
PDF_MAX_PAGES_ONCE = 8            # ≤8 页单次调用；>8 页触发分批并发，避免超出模型上下文窗口
BATCH_SIZE         = 2            # 分批时每批页数默认值（smart_batch_size 会动态调整）

# ── 目录配置 ──────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
# UPLOAD_DIR / RESULT_DIR 可通过环境变量重定向到挂载卷，容器部署时设置为 /app/uploads
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads")))
RESULT_DIR = Path(os.getenv("RESULT_DIR", str(BASE_DIR / "results")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)

# ── 运行时打标配置文件 ────────────────────────────────────────────
RUNTIME_CFG_FILE = BASE_DIR / "schemas" / "tag_config_runtime.json"

# ── 异步任务 ──────────────────────────────────────────────────────
TASK_TTL_MINUTES   = int(os.getenv("TASK_TTL_MINUTES", "10"))   # 任务结果在内存中保留时间
ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png"}
MAX_FILE_SIZE_MB   = int(os.getenv("MAX_FILE_SIZE_MB", "20"))

# ── 管理接口鉴权 ──────────────────────────────────────────────────
# 设置后，/api/tags/config 的写接口（POST/PUT/DELETE）要求 Authorization: Bearer <token>
# 未设置时跳过鉴权（适合内网隔离部署），生产环境建议配置
TAGS_ADMIN_TOKEN = os.getenv("TAGS_ADMIN_TOKEN", "")
