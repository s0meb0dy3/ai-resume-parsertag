"""
Langfuse v3/v4 可观测性客户端单例（基于 OTEL 传输）。

Langfuse 是什么：一个用于"记录和追踪 AI 应用运行过程"的平台。它能记录
每次 LLM 调用花了多少钱、耗时多久、传了什么、返回了什么，并把这些信息
按"一次任务 -> 一条链路"组织起来，方便在网页界面上排查问题和看成本。

这个模块做什么：把 Langfuse 的客户端集中在这里创建一次（单例），整个服务
共用同一个实例，其他模块只需要 import 这里提供的工具函数，不需要自己
再去创建 Langfuse 客户端。

重要设计（降级机制）：如果环境变量 LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY
没有配置，模块会自动进入"空操作"（no-op）模式——所有函数依然存在、
依然可以被调用，但调用后什么都不做。这样服务在没有 Langfuse 的环境中
也能正常启动和运行：可观测性只是"锦上添花"，它的缺失绝不能影响业务。

对外使用方式（其他模块）：
    from logger.langfuse_client import observe, propagate_attributes, LANGFUSE_TAGS
"""
import os

# ---- 配置读取（全部来自环境变量） ----
# LANGFUSE_SECRET_KEY / LANGFUSE_PUBLIC_KEY：Langfuse 的鉴权密钥。
#   注意：只有这两个值都非空，Langfuse 才被启用；任何一个为空都视为未启用。
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
# LANGFUSE_BASE_URL（或旧变量名 LANGFUSE_HOST）：Langfuse 服务的地址，
#   默认指向本机 3100 端口（Langfuse 自托管的标准端口）。
LANGFUSE_HOST       = os.getenv("LANGFUSE_BASE_URL", os.getenv("LANGFUSE_HOST", "http://localhost:3100"))
# LANGFUSE_TAGS：逗号分隔的标签列表。这些标签会打在每个 trace/span 上，
#   方便之后在 Langfuse 界面里按标签筛选（比如按环境、按业务类型过滤）。
LANGFUSE_TAGS       = [t.strip() for t in os.getenv("LANGFUSE_TAGS", "").split(",") if t.strip()]
# LANGFUSE_MEDIA_UPLOAD_ENABLED：是否允许把图片等媒体附件上传到 Langfuse。
#   默认关闭，因为简历里的图片、扫描件可能包含 PII（个人隐私），不应默认上传。
_media_upload_enabled = os.getenv("LANGFUSE_MEDIA_UPLOAD_ENABLED", "false").lower() == "true"

# 是否启用 Langfuse：两个密钥都配置了才算启用。
_enabled = bool(LANGFUSE_SECRET_KEY and LANGFUSE_PUBLIC_KEY)

if _enabled:
    # ---- 启用分支（密钥已配置）：直接使用 Langfuse 官方能力 ----
    # observe / propagate_attributes 是 Langfuse 官方提供的功能：
    #   observe             —— 把函数包装成"span"（一次可观测的调用记录）
    #   propagate_attributes—— 把当前链路的归属关系传播到新创建的 span
    from langfuse import (  # noqa: F401
        Langfuse,
        get_client,
        observe as _langfuse_observe,
        propagate_attributes,
    )

    def observe(func=None, **kwargs):
        """把函数包装成 Langfuse span 的装饰器，用于记录一次调用的开始/结束/耗时。

        用法（两种写法都可以）：
            @observe                       # 直接装饰，span 名默认取函数名
            @observe(name="自定义名字")     # 带括号，可自定义 span 名

        注意：默认不采集函数的入参和返回值！因为简历、岗位描述、原文、图片
        和模型消息都可能包含 PII（个人隐私）。如果业务代码确实想记录一些
        可观测字段，请用 ``update_current_observation`` 显式提交最小化摘要。

        Args:
            func: 被装饰的函数。@observe 不带括号使用时，func 就是这个函数；
                带括号使用时，func 为 None。
            **kwargs: 透传给 Langfuse observe 的其他参数（如 name、tags）。
                其中 capture_input / capture_output 会被强制设为 False，
                即默认不记录函数的输入输出，防止 PII 泄漏。

        Returns:
            包装后的函数（带 span 记录）；func 为 None 时返回一个装饰器。
        """
        kwargs.setdefault("capture_input", False)
        kwargs.setdefault("capture_output", False)
        if func is not None:
            return _langfuse_observe(func, **kwargs)
        return _langfuse_observe(**kwargs)

    def update_current_observation(**kwargs):
        """往"当前正在执行的 span"上补充自定义观测字段。

        和 observe 的区别：observe 是在函数执行前/后自动记录；这个函数是在
        函数执行过程中，手动把业务关心的信息（比如 task_id、解析耗时）
        附加到当前 span 上，之后可以在 Langfuse 界面里看到这些字段。

        Args:
            **kwargs: 要附加到当前 span 的字段，例如
                update_current_observation(task_id="xxx", stage="parse")。
                原样透传给 Langfuse 的 update_current_span。
        """
        get_client().update_current_span(**kwargs)

    langfuse = Langfuse(
        secret_key=LANGFUSE_SECRET_KEY,
        public_key=LANGFUSE_PUBLIC_KEY,
        host=LANGFUSE_HOST,
        # media_upload_thread_count=0 可彻底禁用媒体上传线程。
        # 默认置 0：不开媒体上传线程，避免自动上传含 PII 的附件；
        # 只有显式开启 LANGFUSE_MEDIA_UPLOAD_ENABLED 时才使用官方默认线程数。
        media_upload_thread_count=0 if not _media_upload_enabled else None,
    )

else:
    # ---- 降级分支（密钥未配置）：提供"长得一样但什么都不做"的替代品 ----
    # 目的：无论有没有配置 Langfuse，调用方的 import 和调用方式都完全一样，
    # 业务代码不需要写 if langfuse is not None 之类的判断。
    langfuse = None  # type: ignore[assignment]

    def update_current_observation(**kwargs):  # type: ignore[misc]
        """在 Langfuse 未启用时，忽略"更新当前 span 字段"的调用（空操作）。

        保证调用方代码不用判断 Langfuse 是否启用：这里接受所有参数，
        但什么都不做。

        Args:
            **kwargs: 与启用分支保持一致的观测字段参数，全部被忽略。

        Note:
            空操作是预期降级行为——缺少可观测性凭据不得影响业务执行。
        """
        pass

    def observe(name=None, **kwargs):  # type: ignore[misc]
        """Langfuse 未启用时的空操作装饰器。

        调用方式和启用分支完全一致：既可以 @observe 直接装饰函数，
        也可以 @observe(name="...")。区别是这里不做任何记录，
        原样返回被装饰的函数——相当于"装饰了个寂寞"，但代码不用改。

        Args:
            name: @observe 不带括号时，传入的是被装饰函数本身；
                带括号时，传入的是装饰器参数（如自定义 span 名）。
            **kwargs: 与启用分支保持兼容的其他参数，全部被忽略。

        Returns:
            未包装的原函数。当 name 本身是可调用对象（不带括号用法）时
            直接返回它；否则返回一个原样返回函数的装饰器。
        """
        def decorator(fn):
            """原样返回被装饰函数的内部装饰器。

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
        """Langfuse 未启用时的空操作上下文管理器。

        背景：启用分支（langfuse 包里的原版）会在进入 with 时，把当前链路
        的归属关系（trace_id、parent_span_id）和属性复制到上下文里，
        使得之后新建的 span 能正确挂到同一条链路上——常用于跨线程、
        跨异步任务保持"这是一条线"的关联。

        这里降级为什么都不做，但用法保持一致，调用方无需改动。

        用法：
            with propagate_attributes(tags=LANGFUSE_TAGS, session_id=task_id):
                ...  # 其中的 span 本应继承外部 trace 关系，降级时无效果
        """

        def __init__(self, **kwargs):
            """接受并忽略待传播的观测属性。

            Args:
                **kwargs: 为兼容 Langfuse 上下文管理器而接受的属性，全部忽略。
            """
            pass

        def __enter__(self):
            """进入上下文：空操作，返回自身。

            Returns:
                当前上下文管理器实例（with 语句中 as 拿到的对象）。
            """
            return self

        def __exit__(self, *args):
            """退出上下文：空操作，并且不抑制业务异常。

            Args:
                *args: 上下文协议传入的三元组
                    （异常类型, 异常值, 异常回溯）；无异常时均为 None。

            Returns:
                ``None``——表示 with 块里抛出的业务异常应继续向上传播，
                不会被这个空操作"吞掉"。
            """
            pass
