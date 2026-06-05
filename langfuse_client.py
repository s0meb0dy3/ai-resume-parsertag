"""
Langfuse v2 可观测性客户端单例（兼容 Langfuse server 2.x）。

Langfuse SDK v3+ 改用 OTEL 传输，需要 Langfuse server v3+。
本项目使用 Langfuse server 2.x，因此锁定 SDK < 3.0.0。

LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY 未配置时自动降级为空操作，
不影响主流程，服务可在无 Langfuse 的环境中正常启动。

使用方式（其他模块）：
    from langfuse_client import observe, propagate_attributes, LANGFUSE_TAGS
"""
import os

LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_HOST       = os.getenv("LANGFUSE_HOST", "http://localhost:3100")
LANGFUSE_TAGS       = [t.strip() for t in os.getenv("LANGFUSE_TAGS", "").split(",") if t.strip()]

_enabled = bool(LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY)

if _enabled:
    from langfuse import Langfuse
    from langfuse.decorators import observe, langfuse_context  # noqa: F401

    def update_current_observation(**kwargs):
        """将当前 span 的 output / metadata 等字段写入 Langfuse。"""
        langfuse_context.update_current_observation(**kwargs)

    langfuse = Langfuse(
        secret_key=LANGFUSE_SECRET_KEY,
        public_key=LANGFUSE_PUBLIC_KEY,
        host=LANGFUSE_HOST,
    )

    class propagate_attributes:
        """
        langfuse v2 没有 propagate_attributes；
        用 langfuse_context.update_current_trace() 模拟相同效果。
        """
        def __init__(self, tags=None, session_id=None, user_id=None, **kwargs):
            self._update = {}
            if tags:
                self._update["tags"] = tags
            if session_id:
                self._update["session_id"] = session_id
            if user_id:
                self._update["user_id"] = user_id

        def __enter__(self):
            if self._update:
                langfuse_context.update_current_trace(**self._update)
            return self

        def __exit__(self, *args):
            pass

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
