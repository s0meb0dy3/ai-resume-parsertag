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
from contextvars import ContextVar
from pathlib import Path

from dotenv import load_dotenv
# 本地开发：自动加载同目录 .env；生产容器不存在 .env 时静默跳过
load_dotenv()

from logger.logger_manager import (  # noqa: F401  供其他模块 from config import
    logger,
    set_log_context,
    log_context,
    set_extra_context,
    clear_extra_context,
    wrap_for_executor,
    get_logger_manager,
)

# 模型和推理配置
# 所有变量必须通过环境变量注入，无内置默认值，防止将内网 IP 烘焙进镜像
MODEL_SERVICE_URL = os.getenv("MODEL_SERVICE_URL", "")
if not MODEL_SERVICE_URL:
    raise RuntimeError("环境变量 MODEL_SERVICE_URL 未设置，请在 .env 或启动命令中配置")
API_KEY           = os.getenv("API_KEY", "")
if not API_KEY:
    raise RuntimeError("环境变量 API_KEY 未设置，请在 .env 或启动命令中配置")
MODEL_NAME   = os.getenv("MODEL_NAME",   "Qwen3.5-397B-A17B")
ASYNC_MODEL_NAME = os.getenv("ASYNC_MODEL_NAME", MODEL_NAME)
SYNC_MODEL_NAME = os.getenv("SYNC_MODEL_NAME", MODEL_NAME)
FALLBACK_MODELS  = [m.strip() for m in os.getenv("FALLBACK_MODELS", "").split(",") if m.strip()]
_current_model_name_var: ContextVar[str] = ContextVar("current_model_name", default=MODEL_NAME)
TEMPERATURE  = float(os.getenv("TEMPERATURE", "0.01"))  # 低温度减少幻觉，稳定 JSON 输出
MAX_TOKENS   = int(os.getenv("MAX_TOKENS",    "16384"))
MAX_WORKERS  = int(os.getenv("MAX_WORKERS",   "24"))     # 全局线程池上界，同时也是 Semaphore 并发上限
API_TIMEOUT  = int(os.getenv("API_TIMEOUT",   "300"))    # 单次 LLM 调用超时（秒）
MAX_RETRIES  = int(os.getenv("MAX_RETRIES",   "2"))      # OpenAI 客户端自动重试次数
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "60"))  # URL 文件下载超时（秒）
LLM_PREFIX_CACHE_ENABLED = os.getenv(
    "LLM_PREFIX_CACHE_ENABLED",
    "true",
).strip().lower() not in {"0", "false", "no", "off"}

# 网关地址/密钥仍由部署管理，页面仅管理模型名称。管理令牌缺省时禁用管理 API。
MODEL_CONFIG_FILE = os.getenv("MODEL_CONFIG_FILE", str(Path(__file__).parent / "runtime" / "model_config.json"))
MODEL_ADMIN_TOKEN = os.getenv("MODEL_ADMIN_TOKEN", "")

# 模型上下文
def get_current_model_name() -> str:
    """读取当前请求上下文使用的模型名。

    Returns:
        str: `ContextVar` 中的模型名；未覆盖时为 `MODEL_NAME`。
    """
    return _current_model_name_var.get()


def set_current_model_name(model_name: str):
    """为当前上下文设置模型名并返回恢复令牌。

    Args:
        model_name (str): 本次调用使用的模型名；空值回退到 `MODEL_NAME`。

    Returns:
        Token: 传给 `reset_current_model_name` 以恢复上一上下文的令牌。
    """
    return _current_model_name_var.set(model_name or MODEL_NAME)


def reset_current_model_name(token) -> None:
    """使用 `ContextVar` 令牌恢复上一个模型上下文。

    Args:
        token (Token): `set_current_model_name` 返回的恢复令牌。
    """
    _current_model_name_var.reset(token)


def get_thinking_disable_params(model_name: str | None = None) -> dict:
    """根据模型系列构造关闭思考模式的兼容参数。

    Args:
        model_name (str | None): 目标模型名；为空时使用当前请求上下文。

    Returns:
        dict: 传入 OpenAI 兼容接口 `extra_body` 的 `chat_template_kwargs`。
    """
    active_model = model_name or get_current_model_name()
    return (
        {"chat_template_kwargs": {"enable_thinking": False, "reasoning_effort": "high"}}
        if active_model.lower().startswith("qwen")
        else {"chat_template_kwargs": {"thinking": False, "reasoning_effort": "high"}}
    )


# 模型思考模式关闭参数
# 不同厂商对"关闭思考链"的参数名不一致，需按模型系列分别处理：
#   Qwen 系列：chat_template_kwargs.enable_thinking = false
#   Kimi / DeepSeek 系列：chat_template_kwargs.thinking = false
# 以下参数将作为 extra_body 传递给 OpenAI 兼容接口。
# reasoning_effort="high" 在关闭思考时仍建议保留，部分服务端会忽略它。
_THINKING_DISABLE_PARAMS: dict = get_thinking_disable_params(MODEL_NAME)

# PDF 处理配置
PDF_DPI            = 144          # 渲染分辨率：144 DPI = 72×2，平衡清晰度与内存占用
PDF_MAX_PAGES_ONCE = 8            # ≤8 页单次调用；>8 页触发分批并发，避免超出模型上下文窗口

# 目录配置
BASE_DIR   = Path(__file__).parent
# UPLOAD_DIR / RESULT_DIR 可通过环境变量重定向到挂载卷，容器部署时设置为 /app/uploads
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "uploads")))
RESULT_DIR = Path(os.getenv("RESULT_DIR", str(BASE_DIR / "results")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
RESULT_DIR.mkdir(parents=True, exist_ok=True)

# 运行时打标配置文件
RUNTIME_CFG_FILE = BASE_DIR / "schemas" / "tag_config_runtime.json"

# 异步任务
TASK_TTL_MINUTES   = int(os.getenv("TASK_TTL_MINUTES", "120"))   # 任务结果在内存中保留时间
# 公开解析入口只允许已有真实类型校验、资源预算和降级策略覆盖的格式。
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".jpg", ".jpeg", ".png"}
MAX_FILE_SIZE_MB   = int(os.getenv("MAX_FILE_SIZE_MB", "20"))
SYNC_DOCUMENT_MAX_TEXT_CHARS = int(
    os.getenv("SYNC_DOCUMENT_MAX_TEXT_CHARS", "120000")
)

# 管理接口鉴权
# 设置后，/api/tags/config 的写接口（POST/PUT/DELETE）要求 Authorization: Bearer <token>
# 未设置时跳过鉴权（适合内网隔离部署），生产环境建议配置
TAGS_ADMIN_TOKEN = os.getenv("TAGS_ADMIN_TOKEN", "")

# Langfuse 可观测性
# 未配置 LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY 时，@observe() 降级为空操作
# 配置项集中在 langfuse_client.py 读取，此处仅声明供文档查阅
# LANGFUSE_SECRET_KEY  = sk-lf-...
# LANGFUSE_PUBLIC_KEY  = pk-lf-...
# LANGFUSE_HOST        = http://localhost:3100   （默认值）
# LANGFUSE_TAGS        = dev_v2.3               （逗号分隔多个 tag）
