"""管理请求上下文、日志格式、轮转文件和多进程写入。

模块使用 `ContextVar` 隔离异步请求，并在线程执行器中显式传播上下文。日志配置
支持 YAML 热更新和环境变量覆盖；文本及 JSON 格式可输出到控制台、分级轮转文件
和进程内队列监听器，适配 Gunicorn、Docker、K8s、ELK 与 Loki 部署。

重要配置:
    - `LOG_LEVEL`：覆盖运行时日志级别。
    - `LOG_CONFIG_PATH`：指定日志 YAML 配置文件路径。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
import yaml
import coloredlogs
from contextlib          import contextmanager
from contextvars         import ContextVar, copy_context
from datetime            import datetime, timezone, timedelta
from logging.handlers    import TimedRotatingFileHandler, QueueHandler, QueueListener
from watchdog.observers  import Observer
from watchdog.events     import FileSystemEventHandler

# 强制所有 Formatter 使用北京时间（CST = UTC+8），不依赖容器 TZ 环境变量
_CST = timezone(timedelta(hours=8))
logging.Formatter.converter = staticmethod(
    lambda timestamp: datetime.fromtimestamp(timestamp, _CST).timetuple()
)

__all__ = [
    # 日志实例与上下文
    "logger", "set_log_context", "log_context", "set_extra_context", "clear_extra_context",
    # 线程工具
    "run_in_thread", "wrap_for_executor",
    # 单例访问
    "get_logger", "get_logger_manager",
    # 类与初始化函数（供项目集成使用）
    "LoggerManager", "initialize_logger",
    # ContextVar 直接访问（高级用法）
    "request_id_var", "task_id_var", "business_type_var", "filename_var",
]


# 哨兵对象

_UNSET = object()   # ContextVar 默认值：区分"未设置"与任意合法字符串（含 '-'）
_KEEP  = object()   # set_log_context 参数默认值：不传时保留 ContextVar 当前值


# 请求上下文变量

# ContextVar 在 asyncio 协程间天然隔离；线程池线程通过 wrap_for_executor / run_in_thread 继承
request_id_var   : ContextVar = ContextVar("request_id",    default=_UNSET)
task_id_var      : ContextVar = ContextVar("task_id",       default=_UNSET)
business_type_var: ContextVar = ContextVar("business_type", default=_UNSET)
filename_var     : ContextVar = ContextVar("filename",       default=_UNSET)
# 项目自定义扩展字段，JSON 格式下完整输出，Text 格式下附加在行尾
_extra_ctx_var   : ContextVar = ContextVar("_extra_ctx",    default={})

# 公共上下文函数

def set_log_context(
    request_id:    str | None | object = _KEEP,
    task_id:       str | object = _KEEP,
    business_type: str | object = _KEEP,
    filename:      str | object = _KEEP,
) -> None:
    """设置当前请求的日志上下文。

    Args:
        request_id: 请求/链路标识；不传时保留，显式传空值时生成 8 位标识。
        task_id: 单份简历任务标识。不传时保留，传空值时重置为 ``-``。
        business_type: 业务类型。不传时保留当前值，传空值时重置为 ``-``。
        filename: 文件名。不传时保留当前值，传空值时重置为 ``-``。

    Note:
        在线程池中继续传播上下文时，调用方仍需使用 ``wrap_for_executor`` 或
        ``run_in_thread`` 捕获当前 ``ContextVar`` 快照。
    """
    if request_id is not _KEEP:
        rid = request_id or uuid.uuid4().hex[:8]
        request_id_var.set(rid)

    if task_id is not _KEEP:
        val = task_id or "-"
        task_id_var.set(val)

    if business_type is not _KEEP:
        val = business_type or "-"
        business_type_var.set(val)

    if filename is not _KEEP:
        val = filename or "-"
        filename_var.set(val)


@contextmanager
def log_context(
    request_id:    str | None | object = _KEEP,
    task_id:       str | object = _KEEP,
    business_type: str | object = _KEEP,
    filename:      str | object = _KEEP,
):
    """在当前协程或线程中临时绑定日志上下文，并在退出时完整恢复。

    Args:
        request_id: 请求/链路标识；显式传空值时生成新标识。
        task_id: 单份简历任务标识。
        business_type: 业务操作名称。
        filename: 当前简历文件名。

    Yields:
        None: 绑定后的执行作用域。

    Note:
        该作用域以 ``ContextVar`` token 恢复状态，适用于 asyncio 并发任务。
        线程池仍需通过 ``wrap_for_executor`` 捕获当前上下文快照。
    """
    bindings = (
        (request_id_var, request_id, lambda value: value or uuid.uuid4().hex[:8]),
        (task_id_var, task_id, lambda value: value or "-"),
        (business_type_var, business_type, lambda value: value or "-"),
        (filename_var, filename, lambda value: value or "-"),
    )
    tokens = []
    for variable, value, normalize in bindings:
        if value is _KEEP:
            continue
        tokens.append((variable, variable.set(normalize(value))))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def set_extra_context(**kwargs) -> None:
    """增量合并当前请求的自定义日志字段。

    Args:
        **kwargs: 要加入日志上下文的字段和值。

    Note:
        文本格式把字段附加为 ``key=value``，JSON 格式则展开为顶层字段。
        调用 ``clear_extra_context`` 前，已有字段会持续保留。
    """
    _extra_ctx_var.set({**_extra_ctx_var.get(), **kwargs})


def clear_extra_context() -> None:
    """清空当前上下文中的所有扩展字段。"""
    _extra_ctx_var.set({})


# 线程工具

def run_in_thread(fn, *args, daemon: bool = False, **kwargs) -> threading.Thread:
    """在继承当前日志上下文的新线程中运行函数。

    Args:
        fn: 在线程中执行的可调用对象。
        *args: 传递给 ``fn`` 的位置参数。
        daemon: 是否创建守护线程，默认为 ``False``。
        **kwargs: 传递给 ``fn`` 的关键字参数。

    Returns:
        已启动的线程对象。

    Note:
        守护线程可能在主进程退出时被直接终止，只适用于无需保证完成的任务。
    """
    ctx = copy_context()
    t = threading.Thread(target=lambda: ctx.run(fn, *args, **kwargs), daemon=daemon)
    t.start()
    return t


def wrap_for_executor(fn):
    """包装函数，使线程池调用继承当前 ``ContextVar`` 快照。

    Args:
        fn: 需要在线程池中执行的可调用对象。

    Returns:
        接受原函数参数并在捕获上下文中调用它的包装函数。

    Note:
        ``asyncio`` 线程池不会自动继承上下文且会复用线程。应在请求处理期间
        调用本函数，以捕获当前请求而非模块加载时的上下文。
    """
    ctx = copy_context()

    def _wrapped(*args, **kwargs):
        """在捕获的日志上下文中调用原函数。

        Args:
            *args: 传递给原函数的位置参数。
            **kwargs: 传递给原函数的关键字参数。

        Returns:
            原函数的返回值。
        """
        return ctx.run(fn, *args, **kwargs)
    return _wrapped


# LogRecord 上下文注入过滤器

class _NoiseFilter(logging.Filter):
    """过滤掉探针/健康检查请求的 uvicorn.access 日志，避免污染业务日志。"""
    _SKIP = ("GET /health ", "GET /favicon.ico ", "HEAD /health ")

    def filter(self, record: logging.LogRecord) -> bool:
        """判断访问日志是否属于应忽略的健康探针。

        Args:
            record: 待过滤的日志记录。

        Returns:
            非探针日志返回 ``True``，应跳过的探针日志返回 ``False``。
        """
        msg = record.getMessage()
        return not any(s in msg for s in self._SKIP)


class _ContextFilter(logging.Filter):
    """
    将请求上下文字段注入每条 LogRecord。

    挂在 logger 层（而非 handler 层），每条记录仅执行一次，
    无论有多少个 handler 都不会重复计算。

    运行时只读取 ContextVar，防止同一事件循环线程上的并发协程通过
    ``threading.local`` 读到其他任务残留值。
    """

    @staticmethod
    def _get(cvar: ContextVar, fallback: str = "-") -> str:
        """读取协程/线程当前 ContextVar，未绑定时返回默认值。

        Args:
            cvar: 优先读取的 ``ContextVar``。
            fallback: 两类上下文均无值时使用的默认文本。

        Returns:
            可安全写入日志记录的字符串值。
        """
        v = cvar.get()
        return fallback if v is _UNSET else str(v)

    def filter(self, record: logging.LogRecord) -> bool:
        """向日志记录注入请求上下文并允许其继续处理。

        Args:
            record: 待补充上下文字段的日志记录。

        Returns:
            始终返回 ``True``，该过滤器不负责丢弃日志。
        """
        record.request_id    = self._get(request_id_var)
        record.task_id       = self._get(task_id_var)
        record.task_id_short = record.task_id[:8] if record.task_id != "-" else "-"
        record.business_type = self._get(business_type_var)
        record.filename_ctx  = self._get(filename_var)

        # 扩展字段：Text 格式拼接为字符串，JSON 格式由 _JsonFormatter 直接处理
        extra = _extra_ctx_var.get()
        record.extra_ctx = (" ".join(f"{k}={v}" for k, v in extra.items())) if extra else "-"
        record.extra_fields = dict(extra)

        return True


# 文件 Handler 时间和大小双重轮转

class _SizeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """
    在标准 TimedRotatingFileHandler 基础上叠加文件大小检查。

    max_bytes > 0：文件超过阈值时立即触发轮转，防止单文件无限增长。
    max_bytes = 0：行为与标准 TimedRotatingFileHandler 完全一致（向后兼容）。
    """

    def __init__(self, *args, max_bytes: int = 0, **kwargs):
        """初始化同时支持时间和大小阈值的文件处理器。

        Args:
            *args: 传递给 ``TimedRotatingFileHandler`` 的位置参数。
            max_bytes: 单个日志文件的大小上限；``0`` 表示不启用大小轮转。
            **kwargs: 传递给 ``TimedRotatingFileHandler`` 的关键字参数。
        """
        super().__init__(*args, **kwargs)
        self.max_bytes = max_bytes
        self._size_rollover_pending = False

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        """判断当前日志文件是否达到大小或时间轮转条件。

        Args:
            record: 即将写入的日志记录。

        Returns:
            任一轮转条件满足时返回 ``True``。
        """
        self._size_rollover_pending = False
        if int(time.time()) >= self.rolloverAt:
            return True
        if self.max_bytes > 0 and self.stream:
            self.stream.seek(0, 2)
            message = f"{self.format(record)}{self.terminator}"
            message_bytes = len(message.encode(self.encoding or "utf-8", errors="replace"))
            if self.stream.tell() + message_bytes > self.max_bytes:
                self._size_rollover_pending = True
                return True
        return False

    def _switch_to_current_day_file(self) -> bool:
        """长驻进程跨日后切换到当天文件和月份目录。"""
        current = datetime.now(_CST)
        old_path = os.path.abspath(self.baseFilename)
        parent = os.path.dirname(old_path)
        filename = os.path.basename(old_path)
        new_filename, filename_changes = re.subn(
            r"\d{4}-\d{2}-\d{2}", current.strftime("%Y-%m-%d"), filename, count=1
        )
        parent_name = os.path.basename(parent)
        if re.fullmatch(r"\d{4}-\d{2}", parent_name):
            new_parent_name = os.path.join(
                os.path.dirname(parent), current.strftime("%Y-%m")
            )
            parent_changes = 1
        else:
            new_parent_name = parent
            parent_changes = 0
        if not filename_changes:
            return False
        new_path = os.path.join(new_parent_name if parent_changes else parent, new_filename)
        if new_path == old_path:
            return False
        if self.stream:
            self.stream.close()
            self.stream = None
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        self.baseFilename = os.path.abspath(new_path)
        if not self.delay:
            self.stream = self._open()
        current_time = int(time.time())
        self.rolloverAt = self.computeRollover(current_time)
        while self.rolloverAt <= current_time:
            self.rolloverAt += self.interval
        return True

    def doRollover(self) -> None:
        """按唯一序号执行大小轮转，跨日时直接切换到当天文件。"""
        if not self._size_rollover_pending:
            if self._switch_to_current_day_file():
                return
            super().doRollover()
            return

        if self.stream:
            self.stream.close()
            self.stream = None
        date_suffix = datetime.now(_CST).strftime("%Y-%m-%d")
        index = 1
        while True:
            destination = self.rotation_filename(
                f"{self.baseFilename}.{date_suffix}.{index:03d}"
            )
            if not os.path.exists(destination):
                break
            index += 1
        if os.path.exists(self.baseFilename):
            self.rotate(self.baseFilename, destination)
        if not self.delay:
            self.stream = self._open()
        self._size_rollover_pending = False


# JSON 格式化器，适配 K8s、ELK、Loki 和 Fluentd

class _JsonFormatter(logging.Formatter):
    """
    将每条日志格式化为单行 JSON，便于日志采集系统直接解析，无需正则提取字段。

    固定输出字段：ts / level / trace_id / task_id / business_type / filename / caller / msg
    扩展字段    ：set_extra_context() 注入的 key-value 作为顶层字段输出
    异常信息    ：exc_info 不为空时追加 exc 字段（完整堆栈字符串）
    """

    def format(self, record: logging.LogRecord) -> str:
        """将日志记录序列化为单行 JSON。

        Args:
            record: 已由上下文过滤器补充字段的日志记录。

        Returns:
            保留中文且适合日志采集系统解析的 JSON 字符串。
        """
        d: dict = {
            "ts":            datetime.fromtimestamp(record.created, _CST).isoformat(timespec="milliseconds"),
            "level":         record.levelname,
            "trace_id":      getattr(record, "request_id",    "-"),
            "task_id":       getattr(record, "task_id",       "-"),
            "business_type": getattr(record, "business_type", "-"),
            "filename":      getattr(record, "filename_ctx",  "-"),
            "caller":        f"{record.filename}:{record.lineno}:{record.funcName}",
            "msg":           record.getMessage(),
        }
        # 扩展字段直接展开为顶层 key，方便 ELK 按字段过滤
        for key, value in getattr(record, "extra_fields", {}).items():
            if key not in d and key != "exc":
                d[key] = value
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


def _build_colored_console_formatter(text_format: str) -> logging.Formatter:
    """构造控制台彩色格式器，日期和不同严重级别使用稳定配色。"""
    return coloredlogs.ColoredFormatter(
        text_format,
        datefmt="%Y-%m-%d %H:%M:%S",
        level_styles={
            "debug":    {"color": "cyan"},
            "info":     {"color": "green"},
            "warning":  {"color": "yellow",  "bold": True},
            "error":    {"color": "red",     "bold": True},
            "critical": {"color": "magenta", "bold": True},
        },
        field_styles={
            "asctime":   {"color": "blue", "bold": True},
            "levelname": {"bold": True},
            "filename":  {"color": "blue"},
            "lineno":    {"color": "blue"},
        },
    )


# LoggerManager 核心管理类

class LoggerManager:
    """
    日志管理器，负责 logger 的创建、配置热更新和生命周期管理。

    典型用法（FastAPI lifespan）：
        lm = LoggerManager("log_config.yml")
        lm.start_config_watcher()    # 应用启动
        ...
        lm.stop_config_watcher()     # 应用关闭
        lm.stop_queue_listener()

    其他项目接入：
        lm  = LoggerManager("/path/to/your/log_config.yml")
        log = initialize_logger(lm, default_log_name="your_app")
    """

    # 默认接管的第三方 logger（可通过 YAML log_config.third_party_loggers 覆盖）
    _DEFAULT_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
        "sqlalchemy", "sqlalchemy.engine", "sqlalchemy.pool",
        "uvicorn", "uvicorn.access", "uvicorn.error",
        "gunicorn.access", "gunicorn.error",
        "httpx", "httpcore",
        # Langfuse v3/v4 的 OTLP HTTP 导出器；纳管后网络异常也使用统一
        # 时间戳和级别格式，不再向 stderr 输出无上下文裸文本。
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    )

    def __init__(self, config_path: str) -> None:
        """初始化日志配置管理器并立即加载 YAML 配置。

        Args:
            config_path: 日志配置文件路径。

        Note:
            主 logger 初始化前使用独立引导 logger 输出配置错误，避免产生
            日志系统对自身的循环依赖。
        """
        self.config_path  = config_path
        self.CONFIG: dict = {}
        self.config_lock  = threading.Lock()
        self.observer: Observer | None = None
        self._logger_name = ""
        self._queue_listener: QueueListener | None = None

        # 引导 logger：在主 logger 就绪前记录配置系统自身的日志，避免循环依赖
        self._bootstrap = logging.getLogger("logger_manager.bootstrap")
        if not self._bootstrap.handlers:
            _h = logging.StreamHandler()
            _h.setFormatter(logging.Formatter(
                "%(asctime)s | %(levelname)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            self._bootstrap.addHandler(_h)
            self._bootstrap.setLevel(logging.DEBUG)
            self._bootstrap.propagate = False

        self.reload_config()

    # ── 配置加载 ──────────────────────────────────────────────────

    def _load_yaml(self, path: str) -> dict:
        """读取 YAML 配置并将空文件归一化为空字典。

        Args:
            path: YAML 配置文件路径。

        Returns:
            YAML 根对象；空文件返回空字典。

        Raises:
            OSError: 配置文件无法读取。
            yaml.YAMLError: 配置内容不是合法 YAML。
        """
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def reload_config(self) -> None:
        """重新加载配置文件；若 logger 已初始化则同步重建 handlers。"""
        try:
            new_cfg = self._load_yaml(self.config_path)
            with self.config_lock:
                self.CONFIG = new_cfg
            self._bootstrap.info("[CONFIG] 配置已重新加载")
            if self._logger_name:
                self._rebuild_handlers()
        except Exception as e:
            self._bootstrap.error(f"[CONFIG] 加载配置失败: {e}")

    def get_config(self, key: str, default=None):
        """在锁保护下按点号路径读取日志配置。

        Args:
            key: 配置路径，例如 ``log_config.log_level``。
            default: 任一级不存在或不是字典时返回的默认值。

        Returns:
            配置值，或调用方提供的默认值。
        """
        with self.config_lock:
            cur = self.CONFIG
            for part in key.split("."):
                if not isinstance(cur, dict):
                    return default
                cur = cur.get(part, _UNSET)
                if cur is _UNSET:
                    return default
            return cur

    def _rebuild_handlers(self) -> None:
        """配置热更新后重建 logger handlers，使新配置立即生效。"""
        cfg = self.get_config("log_config")
        if not isinstance(cfg, dict):
            return
        self.setup_logger(
            # logger 名称是全局单例访问键，不作为可热更新字段。
            name              = self._logger_name,
            log_folder        = cfg.get("log_folder",        "logs"),
            log_level_console = cfg.get("log_level_console", "DEBUG"),
            log_level_file    = cfg.get("log_level_file",    "DEBUG"),
            log_pid           = cfg.get("log_pid",           ""),
            max_bytes         = int(cfg.get("max_bytes",     0)),
            to_console        = cfg.get("to_console",        True),
            use_queue         = cfg.get("use_queue",         False),
            json_format       = cfg.get("json_format",       False),
        )
        self._bootstrap.info("[CONFIG] logger handlers 已重建")

    # ── 文件监听（热更新）────────────────────────────────────────

    class _ConfigFileEventHandler(FileSystemEventHandler):
        """
        监听配置文件的修改与创建事件。

        同时处理 on_modified 和 on_created，覆盖 vim / Sublime Text 等
        "写临时文件 → 原子重命名" 保存方式（此方式只触发 on_created）。
        防抖 200ms 过滤编辑器在一次保存中连续触发的多个事件。
        """

        def __init__(self, manager: "LoggerManager") -> None:
            """绑定所属管理器并初始化事件防抖时间。

            Args:
                manager: 接收配置重载通知的日志管理器。
            """
            self._manager     = manager
            self._last_reload = 0.0

        def _handle(self, event) -> None:
            """校验事件路径并在防抖窗口外触发配置重载。

            Args:
                event: ``watchdog`` 产生的文件系统事件。

            Note:
                仅处理目标配置文件，200 毫秒内的连续事件视为同一次保存。
            """
            if os.path.abspath(event.src_path) != os.path.abspath(self._manager.config_path):
                return
            now = time.time()
            if now - self._last_reload < 0.2:   # 200ms 防抖
                return
            self._last_reload = now
            self._manager._bootstrap.info("[CONFIG] 检测到配置文件变化，重新加载…")
            self._manager.reload_config()

        def on_modified(self, event) -> None:
            """处理配置文件内容修改事件。

            Args:
                event: ``watchdog`` 产生的文件修改事件。
            """
            self._handle(event)

        def on_created(self, event) -> None:
            """处理编辑器原子保存产生的文件创建事件。

            Args:
                event: ``watchdog`` 产生的文件创建事件。
            """
            # vim / Sublime Text 的原子保存触发 on_created 而非 on_modified
            self._handle(event)

    def start_config_watcher(self) -> None:
        """启动文件监听，实现配置文件实时热更新。重复调用安全。"""
        if self.observer is not None:
            self._bootstrap.warning("[CONFIG] 文件监听器已在运行，忽略重复启动")
            return
        handler       = self._ConfigFileEventHandler(self)
        self.observer = Observer()
        self.observer.daemon = True
        self.observer.schedule(
            handler,
            path      = os.path.dirname(self.config_path),
            recursive = False,
        )
        self.observer.start()
        self._bootstrap.info("[CONFIG] 文件监听器已启动")

    def stop_config_watcher(self) -> None:
        """停止文件监听器，等待最多 5s 后强制放弃。"""
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            if self.observer.is_alive():
                self._bootstrap.warning("[CONFIG] 文件监听器未能在 5s 内停止")
            self.observer = None
            self._bootstrap.info("[CONFIG] 文件监听器已停止")

    def stop_queue_listener(self) -> None:
        """停止队列写入监听器（应用退出时调用，确保缓冲区日志全部落盘）。"""
        if self._queue_listener:
            self._queue_listener.stop()
            self._queue_listener = None
            self._bootstrap.info("[CONFIG] QueueListener 已停止")

    # ── Logger 构建 ───────────────────────────────────────────────

    def setup_logger(
        self,
        name:              str  = "app",
        log_folder:        str  = "logs",
        log_level_console: str  = "DEBUG",
        log_level_file:    str  = "DEBUG",
        log_pid:           str  = "",
        max_bytes:         int  = 0,
        to_console:        bool = True,
        use_queue:         bool = False,
        json_format:       bool = False,
    ) -> logging.Logger:
        """完整重建并返回指定名称的 logger。

        Args:
            name: logger 名称，也是 ``logging.getLogger`` 的全局访问键。
            log_folder: 日志根目录；相对路径以本模块目录为基准。
            log_level_console: 控制台日志级别。
            log_level_file: debug 文件日志级别。
            log_pid: 日志文件名标识；为空时读取环境变量或进程号。
            max_bytes: 单个日志文件上限，单位为字节；``0`` 表示不限。
            to_console: 是否创建控制台 handler。
            use_queue: 是否通过 ``QueueHandler`` 串行写入文件。
            json_format: 是否输出适合日志采集系统解析的单行 JSON。

        Returns:
            已配置上下文过滤器、输出 handler 和第三方 logger 的主 logger。

        Note:
            ``LOG_LEVEL`` 可覆盖控制台和文件级别，``LOG_PID`` 仅在参数为空时
            生效。每次调用都会替换既有 handler，以支持配置热更新。
        """
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))

        # 配置热更新前先停止旧队列并关闭所有受管 handler，避免文件描述符泄漏。
        self.stop_queue_listener()
        managed_names = {
            name,
            self._logger_name,
            *self._DEFAULT_THIRD_PARTY_LOGGERS,
            *(self.get_config("log_config.third_party_loggers") or []),
        }
        old_handlers: set[logging.Handler] = set()
        for managed_name in filter(None, managed_names):
            managed_logger = logging.getLogger(managed_name)
            old_handlers.update(managed_logger.handlers)
            managed_logger.handlers.clear()
            managed_logger.filters.clear()
        for old_handler in old_handlers:
            try:
                old_handler.close()
            except Exception:
                pass

        def _to_level(s) -> int:
            """将日志级别名称或数值归一化为整数。

            Args:
                s: 日志级别名称或 ``logging`` 使用的整数值。

            Returns:
                可传给 logger 和 handler 的日志级别整数。
            """
            return logging._nameToLevel.get(str(s).upper(), logging.DEBUG) if isinstance(s, str) else int(s)

        # 环境变量可在不改配置文件的情况下临时调整日志级别（K8s ConfigMap 场景常用）
        env_level     = os.getenv("LOG_LEVEL")
        console_level = _to_level(env_level or log_level_console)
        file_level    = _to_level(env_level or log_level_file)

        # LOG_DIR 是部署层目录覆盖；相对 YAML 路径仍基于本文件位置。
        log_folder = os.getenv("LOG_DIR", "").strip() or log_folder
        if not os.path.isabs(log_folder):
            log_folder = os.path.join(BASE_DIR, log_folder)

        _now   = datetime.now()
        _month = _now.strftime("%Y-%m")
        _today = _now.strftime("%Y-%m-%d")
        _label = (log_pid or "").strip() or os.getenv("LOG_PID", "").strip()
        _pid = f"{_label}_{os.getpid()}" if _label else str(os.getpid())

        # 按月分目录归档；backupCount=0 表示不自动删除，部署侧必须配置保留策略。
        debug_dir = os.path.join(log_folder, "debug", _month)
        error_dir = os.path.join(log_folder, "error", _month)
        os.makedirs(debug_dir, exist_ok=True)
        os.makedirs(error_dir, exist_ok=True)

        # ── 格式化器 ─────────────────────────────────────────────
        _TEXT_FMT = (
            "%(asctime)s | %(levelname)-5s"
            " | [%(business_type)s] [%(filename_ctx)s]"
            " | [%(filename)s:%(lineno)d %(funcName)s]"
            " | %(message)s"
        )
        file_fmt: logging.Formatter = (
            _JsonFormatter() if json_format
            else logging.Formatter(_TEXT_FMT, datefmt="%Y-%m-%d %H:%M:%S")
        )
        ctx_filter = _ContextFilter()

        # ── 文件 handlers ─────────────────────────────────────────
        debug_handler = _SizeTimedRotatingFileHandler(
            os.path.join(debug_dir, f"debug_{_today}_{_pid}.log"),
            when="midnight", interval=1, backupCount=0, encoding="utf-8",
            max_bytes=max_bytes,
        )
        debug_handler.setLevel(file_level)
        debug_handler.setFormatter(file_fmt)

        error_handler = _SizeTimedRotatingFileHandler(
            os.path.join(error_dir, f"error_{_today}_{_pid}.log"),
            when="midnight", interval=1, backupCount=0, encoding="utf-8",
            max_bytes=max_bytes,
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(file_fmt)

        # ── 控制台 handler ────────────────────────────────────────
        console_handler: logging.Handler | None = None
        if to_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(console_level)
            if json_format:
                console_handler.setFormatter(_JsonFormatter())
            else:
                # 用户明确要求 Docker 控制台同样显示颜色，因此文本模式下不再
                # 依赖 isatty()；文件 handler 仍使用不含 ANSI 的 file_fmt。
                console_handler.setFormatter(
                    _build_colored_console_formatter(_TEXT_FMT)
                )

        if console_handler:
            console_handler.addFilter(ctx_filter)

        # ── QueueHandler 模式（单进程内异步落盘）──────────────────
        # 单一 QueueHandler 入队 → QueueListener 在后台线程分流到 debug/error handler，
        # 减少业务线程文件 IO。queue.Queue 不跨进程；多 worker 依赖实际 PID 隔离文件。
        if use_queue:
            log_queue = queue.Queue(maxsize=-1)
            self._queue_listener = QueueListener(
                log_queue, debug_handler, error_handler,
                respect_handler_level=True,
            )
            self._queue_listener.start()
            queue_handler = QueueHandler(log_queue)
            queue_handler.addFilter(ctx_filter)
            file_handlers: list[logging.Handler] = [queue_handler]
        else:
            debug_handler.addFilter(ctx_filter)
            error_handler.addFilter(ctx_filter)
            file_handlers = [debug_handler, error_handler]

        # ── 主 logger ─────────────────────────────────────────────
        root_logger = logging.getLogger(name)
        root_logger.handlers.clear()
        root_logger.filters.clear()
        root_logger.setLevel(logging.DEBUG)
        root_logger.propagate = False
        if console_handler:
            root_logger.addHandler(console_handler)
        for h in file_handlers:
            root_logger.addHandler(h)

        self._logger_name = name

        # ── 第三方 logger 接管 ─────────────────────────────────────
        # 可通过 YAML log_config.third_party_loggers 列表覆盖默认列表，适配不同项目的依赖
        tp_names: list[str] = (
            self.get_config("log_config.third_party_loggers")
            or list(self._DEFAULT_THIRD_PARTY_LOGGERS)
        )
        # 第三方 logger 同时加 file_handlers 和 console_handler，
        # 修复原版 queue 模式下第三方日志不输出到控制台的问题
        tp_handlers = list(file_handlers)
        if console_handler:
            tp_handlers.append(console_handler)

        noise_filter = _NoiseFilter()
        for svc_name in tp_names:
            svc = logging.getLogger(svc_name)
            svc.handlers.clear()
            svc.filters.clear()
            if svc_name == "uvicorn.access":
                svc.addFilter(noise_filter)
            for h in tp_handlers:
                svc.addHandler(h)
            svc.propagate = False
            if svc.level == logging.NOTSET:
                svc.setLevel(logging.INFO)

        return root_logger


# 初始化入口

def initialize_logger(
    logger_manager:   LoggerManager,
    default_log_name: str = "app",
) -> logging.Logger:
    """根据管理器配置初始化 logger，并为缺失配置提供默认名称。

    Args:
        logger_manager: 已加载配置的日志管理器。
        default_log_name: 未配置 ``log_config.name`` 时使用的 logger 名称。

    Returns:
        根据最终名称取得的已配置 logger。
    """
    cfg = logger_manager.get_config("log_config")
    if not isinstance(cfg, dict):
        logger_manager._bootstrap.warning(
            f"[CONFIG] 'log_config' 未找到，使用默认配置 name={default_log_name}"
        )
        cfg = {}

    log_name = cfg.get("name", default_log_name)
    logger_manager.setup_logger(
        name              = log_name,
        log_folder        = cfg.get("log_folder",        "logs"),
        log_level_console = cfg.get("log_level_console", "DEBUG"),
        log_level_file    = cfg.get("log_level_file",    "DEBUG"),
        log_pid           = cfg.get("log_pid",           ""),
        max_bytes         = int(cfg.get("max_bytes",     0)),
        to_console        = cfg.get("to_console",        True),
        use_queue         = cfg.get("use_queue",         False),
        json_format       = cfg.get("json_format",       False),
    )
    return logging.getLogger(log_name)


def _find_config_path() -> str:
    """按环境变量和模块默认位置查找日志配置文件。

    Returns:
        存在的 ``LOG_CONFIG_PATH``，否则返回本模块同级默认配置路径。

    Note:
        环境变量指向不存在的文件时会静默回退，适配未挂载 ConfigMap 的环境。
    """
    env_path = os.getenv("LOG_CONFIG_PATH", "")
    if env_path and os.path.exists(env_path):
        return env_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_config.yml")


# 模块级懒加载单例

_logger_manager: LoggerManager | None = None
_logger:         logging.Logger | None = None
_init_lock = threading.Lock()


def _bootstrap_singletons() -> None:
    """内部：初始化单例（调用方须持有 _init_lock）。"""
    global _logger_manager, _logger
    _logger_manager = LoggerManager(_find_config_path())
    _logger         = initialize_logger(_logger_manager, default_log_name="aihr")


def get_logger() -> logging.Logger:
    """以线程安全的懒加载方式获取全局 logger。

    Returns:
        首次调用时创建、后续调用复用的 logger 实例。
    """
    global _logger
    if _logger is not None:
        return _logger
    with _init_lock:
        if _logger is not None:
            return _logger
        _bootstrap_singletons()
    return _logger  # type: ignore[return-value]


def get_logger_manager() -> LoggerManager:
    """以线程安全的懒加载方式获取全局日志管理器。

    Returns:
        首次调用时创建、后续调用复用的 ``LoggerManager`` 实例。

    Note:
        应用生命周期负责启动和停止配置监听器及队列监听器。
    """
    global _logger_manager
    if _logger_manager is not None:
        return _logger_manager
    with _init_lock:
        if _logger_manager is not None:
            return _logger_manager
        _bootstrap_singletons()
    return _logger_manager  # type: ignore[return-value]


# 模块级 `logger` 和 `set_log_context` 兼容导出
# 现有代码 `from logger.logger_manager import logger, set_log_context` 无需任何修改

class _LazyProxy:
    """将属性访问代理到 get_logger()，实现 logger 的懒加载。"""

    def __getattr__(self, name: str):
        """将未定义属性访问转发给全局 logger。

        Args:
            name: 要访问的 logger 属性名。

        Returns:
            全局 logger 上的同名属性。
        """
        return getattr(get_logger(), name)

    def __repr__(self) -> str:
        """返回全局 logger 的调试表示。

        Returns:
            全局 logger 的 ``repr`` 字符串。
        """
        return repr(get_logger())


logger             = _LazyProxy()
set_log_context    = set_log_context        # noqa: F811  显式重绑定，保证 __all__ 可导出
set_extra_context  = set_extra_context      # noqa: F811
clear_extra_context = clear_extra_context   # noqa: F811
wrap_for_executor  = wrap_for_executor      # noqa: F811
get_logger_manager = get_logger_manager     # noqa: F811
