"""
Langfuse v3/v4 可观测性客户端单例（基于 OTEL 传输）。

LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY 未配置时自动降级为空操作，
不影响主流程，服务可在无 Langfuse 的环境中正常启动。

使用方式（其他模块）：
    from logger.langfuse_client import observe, propagate_attributes, LANGFUSE_TAGS
"""
import os

LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_HOST       = os.getenv("LANGFUSE_BASE_URL", os.getenv("LANGFUSE_HOST", "http://localhost:3100"))
LANGFUSE_TAGS       = [t.strip() for t in os.getenv("LANGFUSE_TAGS", "").split(",") if t.strip()]
_media_upload_enabled = os.getenv("LANGFUSE_MEDIA_UPLOAD_ENABLED", "false").lower() == "true"

_enabled = bool(LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY)

if _enabled:
    from langfuse import Langfuse, observe, propagate_attributes, get_client  # noqa: F401

    def update_current_observation(**kwargs):
        """将当前 span 的 output / metadata 等字段写入 Langfuse。"""
        get_client().update_current_span(**kwargs)

    langfuse = Langfuse(
        secret_key=LANGFUSE_SECRET_KEY,
        public_key=LANGFUSE_PUBLIC_KEY,
        host=LANGFUSE_HOST,
        # media_upload_thread_count=0 可彻底禁用媒体上传线程
        media_upload_thread_count=0 if not _media_upload_enabled else None,
    )

else:
    langfuse = None  # type: ignore[assignment]

    def update_current_observation(**kwargs):  # type: ignore[misc]
        pass

    def observe(name=None, **kwargs):  # type: ignore[misc]
        """Langfuse 未配置时的空操作装饰器。"""
        def decorator(fn):
            return fn
        if callable(name):
            return name
        return decorator

    class propagate_attributes:  # type: ignore[no-redef]
        """Langfuse 未配置时的空操作上下文管理器。"""
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
