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
    from langfuse import (  # noqa: F401
        Langfuse,
        get_client,
        observe as _langfuse_observe,
        propagate_attributes,
    )

    def observe(func=None, **kwargs):
        """创建默认不采集函数参数和返回值的 Langfuse span。

        简历、岗位描述、原文、图片和模型消息都可能包含 PII。业务代码如需记录
        可观测字段，应通过 ``update_current_observation`` 显式提交最小化摘要。
        """
        kwargs.setdefault("capture_input", False)
        kwargs.setdefault("capture_output", False)
        if func is not None:
            return _langfuse_observe(func, **kwargs)
        return _langfuse_observe(**kwargs)

    def update_current_observation(**kwargs):
        """更新当前 Langfuse span 的观测字段。

        Args:
            **kwargs: 传递给 Langfuse ``update_current_span`` 的字段。
        """
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
        """在 Langfuse 未启用时忽略观测字段更新。

        Args:
            **kwargs: 为保持与启用分支一致而接受的任意观测字段。

        Note:
            空操作是预期降级行为，缺少可观测性凭据不得影响业务执行。
        """
        pass

    def observe(name=None, **kwargs):  # type: ignore[misc]
        """返回与 Langfuse ``observe`` 兼容的空操作装饰器。

        Args:
            name: 装饰器名称，或不带括号使用时传入的被装饰函数。
            **kwargs: 为兼容 Langfuse 装饰器而接受的其他参数。

        Returns:
            原函数，或一个直接返回原函数的装饰器。
        """
        def decorator(fn):
            """原样返回被装饰函数。

            Args:
                fn: 被装饰的可调用对象。

            Returns:
                未包装的原可调用对象。
            """
            return fn
        if callable(name):
            return name
        return decorator

    class propagate_attributes:  # type: ignore[no-redef]
        """Langfuse 未配置时保持调用结构不变的上下文管理器。"""

        def __init__(self, **kwargs):
            """接受并忽略待传播的观测属性。

            Args:
                **kwargs: 为兼容 Langfuse 上下文管理器而接受的属性。
            """
            pass

        def __enter__(self):
            """进入空操作上下文。

            Returns:
                当前上下文管理器实例。
            """
            return self

        def __exit__(self, *args):
            """退出上下文且不抑制业务异常。

            Args:
                *args: 上下文协议传入的异常类型、异常值和回溯。

            Returns:
                ``None``，表示业务异常应继续传播。
            """
            pass
