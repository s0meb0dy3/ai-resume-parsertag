"""
通用日志管理模块

适配场景：asyncio / 多线程 / Gunicorn 多 worker / K8s + ELK / Docker

核心特性
────────
● 请求上下文自动传播   ContextVar（asyncio 协程隔离）+ threading.local（线程池兜底）双轨
● 配置热更新           watchdog 监听 YAML，on_modified + on_created 双事件，防抖 200ms
● 多进程安全写入        QueueHandler + QueueListener，Gunicorn 多 worker 无竞争写入
● 双格式输出           Text（默认）/ JSON（适配 K8s Loki / ELK Logstash / Fluentd）
● 分级归档             debug + error 双目录，时间 + 文件大小双重轮转，永久保留
● 第三方 logger 接管   列表可通过 YAML 配置，控制台 + 文件统一输出，queue 模式同步修复
● 环境变量覆盖          LOG_LEVEL / LOG_CONFIG_PATH 运行时生效，无需改配置文件
● 扩展上下文字段        set_extra_context() 注入项目自定义字段，JSON 格式完整输出
● 懒加载单例           import 时无副作用，首次调用时初始化

快速接入（其他项目）
────────────────────
  # 方式一：放同目录，自动发现 log_config.yml
  from logger.logger_manager import logger, set_log_context

  # 方式二：环境变量指定配置路径
  LOG_CONFIG_PATH=/etc/myapp/log_config.yml python app.py

  # 方式三：编程式初始化
  from logger.logger_manager import LoggerManager, initialize_logger
  lm  = LoggerManager('/path/to/log_config.yml')
  log = initialize_logger(lm, default_log_name='myapp')

已修复的原版问题
────────────────
  ● 哨兵值 '-' 碰撞 → 改用 object() 哨兵，业务代码设置 '-' 不再误判为"未设置"
  ● set_log_context 语义不一致 → _KEEP 哨兵区分"不传"与"置空"
  ● queue 模式下第三方 logger 无控制台输出 → console_handler 同步注入
  ● on_created 事件未监听 → 覆盖 vim/Sublime 等"写临时文件→重命名"保存方式
  ● observer.join() 无超时 → 5s 超时保护，防止关闭阻塞
  ● run_in_thread 强制 daemon → 新增 daemon 参数，默认 False 保证任务完成
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
import yaml
import coloredlogs
from contextvars         import ContextVar, copy_context
from datetime            import datetime, timezone, timedelta
from logging.handlers    import TimedRotatingFileHandler, QueueHandler, QueueListener
from watchdog.observers  import Observer
from watchdog.events     import FileSystemEventHandler

# 强制所有 Formatter 使用北京时间（CST = UTC+8），不依赖容器 TZ 环境变量
_CST = timezone(timedelta(hours=8))
logging.Formatter.converter = lambda *_: datetime.now(_CST).timetuple()

__all__ = [
    # 日志实例与上下文
    "logger", "set_log_context", "set_extra_context", "clear_extra_context",
    # 线程工具
    "run_in_thread", "wrap_for_executor",
    # 单例访问
    "get_logger", "get_logger_manager",
    # 类与初始化函数（供项目集成使用）
    "LoggerManager", "initialize_logger",
    # ContextVar 直接访问（高级用法）
    "request_id_var", "business_type_var", "filename_var",
]


# ══════════════════════════════════════════════════════════════════
# 哨兵对象
# ══════════════════════════════════════════════════════════════════

_UNSET = object()   # ContextVar 默认值：区分"未设置"与任意合法字符串（含 '-'）
_KEEP  = object()   # set_log_context 参数默认值：不传时保留 ContextVar 当前值


# ══════════════════════════════════════════════════════════════════
# 请求上下文变量
# ══════════════════════════════════════════════════════════════════

# ContextVar 在 asyncio 协程间天然隔离；线程池线程通过 wrap_for_executor / run_in_thread 继承
request_id_var   : ContextVar = ContextVar("request_id",    default=_UNSET)
business_type_var: ContextVar = ContextVar("business_type", default=_UNSET)
filename_var     : ContextVar = ContextVar("filename",       default=_UNSET)
# 项目自定义扩展字段，JSON 格式下完整输出，Text 格式下附加在行尾
_extra_ctx_var   : ContextVar = ContextVar("_extra_ctx",    default={})

# 多线程环境兜底（ThreadPoolExecutor 线程不自动继承 ContextVar）
_thread_local = threading.local()


# ══════════════════════════════════════════════════════════════════
# 公共上下文函数
# ══════════════════════════════════════════════════════════════════

def set_log_context(
    request_id:    str | None = None,
    business_type: str | object = _KEEP,
    filename:      str | object = _KEEP,
) -> None:
    """
    在请求入口设置本次请求的日志上下文，后续整条调用链自动携带。

    参数说明：
      request_id    : 传 None 时自动生成 8 位十六进制 ID；传具体值则使用该值
      business_type : 不传（默认）→ 保持当前值；传 "" 或 None → 重置为 '-'
      filename      : 同上

    场景示例：
      # FastAPI 请求入口
      set_log_context(request_id=req.headers.get("X-Request-ID"),
                      business_type="简历解析", filename="张三.pdf")

      # 新请求只更新 request_id，保留 business_type 与 filename
      set_log_context(request_id="abc123")

      # 重置 filename
      set_log_context(filename="")
    """
    rid = request_id or uuid.uuid4().hex[:8]
    request_id_var.set(rid)
    _thread_local.request_id = rid

    if business_type is not _KEEP:
        val = business_type or "-"
        business_type_var.set(val)
        _thread_local.business_type = val

    if filename is not _KEEP:
        val = filename or "-"
        filename_var.set(val)
        _thread_local.filename = val


def set_extra_context(**kwargs) -> None:
    """
    注入项目自定义上下文字段（增量合并，不清除已有字段）。

    Text 格式：附加在日志行尾，格式 key=value
    JSON 格式：作为顶层字段输出，便于 ELK/Loki 直接过滤

    示例：
      set_extra_context(tenant_id="T001", env="prod")
      logger.info("处理完成")
      # JSON 输出中含 {"tenant_id": "T001", "env": "prod", ...}
    """
    _extra_ctx_var.set({**_extra_ctx_var.get(), **kwargs})


def clear_extra_context() -> None:
    """清空当前上下文中的所有扩展字段。"""
    _extra_ctx_var.set({})


# ══════════════════════════════════════════════════════════════════
# 线程工具
# ══════════════════════════════════════════════════════════════════

def run_in_thread(fn, *args, daemon: bool = False, **kwargs) -> threading.Thread:
    """
    在新线程中运行函数，自动将当前 ContextVar 快照复制到子线程。

    参数：
      daemon : False（默认）→ 主进程退出时等待线程完成，任务不丢失
               True  → 守护线程，适合不需要保证完成的后台任务（如日志上报）

    注意：daemon=True 时主进程退出会强制终止线程，有数据丢失风险，
          仅在明确不关心任务完成的场景使用。
    """
    ctx = copy_context()
    t = threading.Thread(target=lambda: ctx.run(fn, *args, **kwargs), daemon=daemon)
    t.start()
    return t


def wrap_for_executor(fn):
    """
    包装函数，使其在 asyncio run_in_executor / ThreadPoolExecutor 中
    继承调用时的 ContextVar 快照。

    asyncio 的线程池线程会被复用，不会自动继承 ContextVar。
    此包装器在「调用时」（而非模块加载时）捕获上下文快照并注入子线程，
    确保 request_id / business_type / filename 等字段在线程内正确显示。

    用法：
      result = await loop.run_in_executor(executor, wrap_for_executor(my_func), arg1, arg2)

    注意：每次调用都产生一次 copy_context()，应在请求处理函数内调用，不要在模块级使用。
    """
    ctx = copy_context()
    def _wrapped(*args, **kwargs):
        return ctx.run(fn, *args, **kwargs)
    return _wrapped


# ══════════════════════════════════════════════════════════════════
# LogRecord 上下文注入过滤器
# ══════════════════════════════════════════════════════════════════

class _NoiseFilter(logging.Filter):
    """过滤掉探针/健康检查请求的 uvicorn.access 日志，避免污染业务日志。"""
    _SKIP = ("GET /health ", "GET /favicon.ico ", "HEAD /health ")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(s in msg for s in self._SKIP)


class _ContextFilter(logging.Filter):
    """
    将请求上下文字段注入每条 LogRecord。

    挂在 logger 层（而非 handler 层），每条记录仅执行一次，
    无论有多少个 handler 都不会重复计算。

    优先级：ContextVar（asyncio 协程隔离）> threading.local（线程兜底）> 默认值 '-'
    """

    @staticmethod
    def _get(cvar: ContextVar, attr: str, fallback: str = "-") -> str:
        v = cvar.get()
        if v is not _UNSET:
            return str(v)
        return str(getattr(_thread_local, attr, fallback))

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id    = self._get(request_id_var,    "request_id")
        record.business_type = self._get(business_type_var, "business_type")
        record.filename_ctx  = self._get(filename_var,      "filename")

        # 扩展字段：Text 格式拼接为字符串，JSON 格式由 _JsonFormatter 直接处理
        extra = _extra_ctx_var.get()
        record.extra_ctx = (" ".join(f"{k}={v}" for k, v in extra.items())) if extra else "-"

        return True


# ══════════════════════════════════════════════════════════════════
# 文件 Handler：时间 + 大小双重轮转
# ══════════════════════════════════════════════════════════════════

class _SizeTimedRotatingFileHandler(TimedRotatingFileHandler):
    """
    在标准 TimedRotatingFileHandler 基础上叠加文件大小检查。

    max_bytes > 0：文件超过阈值时立即触发轮转，防止单文件无限增长。
    max_bytes = 0：行为与标准 TimedRotatingFileHandler 完全一致（向后兼容）。
    """

    def __init__(self, *args, max_bytes: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_bytes = max_bytes

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self.max_bytes > 0 and self.stream:
            self.stream.seek(0, 2)
            if self.stream.tell() >= self.max_bytes:
                return True
        return super().shouldRollover(record)


# ══════════════════════════════════════════════════════════════════
# JSON 格式化器（适配 K8s / ELK / Loki / Fluentd）
# ══════════════════════════════════════════════════════════════════

class _JsonFormatter(logging.Formatter):
    """
    将每条日志格式化为单行 JSON，便于日志采集系统直接解析，无需正则提取字段。

    固定输出字段：ts / level / request_id / business_type / filename_ctx / caller / msg
    扩展字段    ：set_extra_context() 注入的 key-value 作为顶层字段输出
    异常信息    ：exc_info 不为空时追加 exc 字段（完整堆栈字符串）
    """

    def format(self, record: logging.LogRecord) -> str:
        d: dict = {
            "ts":            self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":         record.levelname,
            "request_id":    getattr(record, "request_id",    "-"),
            "business_type": getattr(record, "business_type", "-"),
            "filename_ctx":  getattr(record, "filename_ctx",  "-"),
            "caller":        f"{record.filename}:{record.lineno}:{record.funcName}",
            "msg":           record.getMessage(),
        }
        # 扩展字段直接展开为顶层 key，方便 ELK 按字段过滤
        d.update(_extra_ctx_var.get())
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════
# LoggerManager：核心管理类
# ══════════════════════════════════════════════════════════════════

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
    )

    def __init__(self, config_path: str) -> None:
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
        """
        安全读取配置项，支持点号路径（如 'log_config.log_level'）。
        中间路径不存在时返回 default，不抛出 KeyError。
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
            name              = cfg.get("name",              self._logger_name),
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
            self._manager     = manager
            self._last_reload = 0.0

        def _handle(self, event) -> None:
            if os.path.abspath(event.src_path) != os.path.abspath(self._manager.config_path):
                return
            now = time.time()
            if now - self._last_reload < 0.2:   # 200ms 防抖
                return
            self._last_reload = now
            self._manager._bootstrap.info("[CONFIG] 检测到配置文件变化，重新加载…")
            self._manager.reload_config()

        def on_modified(self, event) -> None:
            self._handle(event)

        def on_created(self, event) -> None:
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
        """
        构建并返回配置好的 logger。每次调用会完整重建所有 handlers（热更新安全）。

        参数：
          name              : logger 名称，也用于 logging.getLogger(name) 全局访问
          log_folder        : 日志根目录（相对路径基于本文件位置，绝对路径直接使用）
          log_level_console : 控制台输出级别（生产建议 WARNING/INFO）
          log_level_file    : debug 文件输出级别（通常保持 DEBUG）
          log_pid           : 日志文件名中的标识；空则读 LOG_PID 环境变量，再次为空则用进程 PID
          max_bytes         : 单文件大小上限（字节），0 = 不限
          to_console        : 是否输出到控制台
          use_queue         : 是否启用 QueueHandler（Gunicorn 多 worker 时开启）
          json_format       : True = 输出 JSON 格式（适配 K8s/ELK），False = 带颜色的文本格式

        环境变量优先级：
          LOG_LEVEL       : 运行时覆盖 console 和 file 日志级别（优先级高于参数）
          LOG_PID         : 日志文件名标识（优先级低于 log_pid 参数）
          LOG_CONFIG_PATH : 指定配置文件路径（由 _find_config_path 使用）
        """
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))

        def _to_level(s) -> int:
            return logging._nameToLevel.get(str(s).upper(), logging.DEBUG) if isinstance(s, str) else int(s)

        # 环境变量可在不改配置文件的情况下临时调整日志级别（K8s ConfigMap 场景常用）
        env_level     = os.getenv("LOG_LEVEL")
        console_level = _to_level(env_level or log_level_console)
        file_level    = _to_level(env_level or log_level_file)

        # 日志目录：相对路径基于本文件位置，绝对路径直接使用
        if not os.path.isabs(log_folder):
            log_folder = os.path.join(BASE_DIR, log_folder)

        _now   = datetime.now()
        _month = _now.strftime("%Y-%m")
        _today = _now.strftime("%Y-%m-%d")
        _pid   = (log_pid or "").strip() or os.getenv("LOG_PID", str(os.getpid()))

        # 按月分目录归档，永久保留（backupCount=0），防止 inode 耗尽
        debug_dir = os.path.join(log_folder, "debug", _month)
        error_dir = os.path.join(log_folder, "error", _month)
        os.makedirs(debug_dir, exist_ok=True)
        os.makedirs(error_dir, exist_ok=True)

        # ── 格式化器 ─────────────────────────────────────────────
        _TEXT_FMT = (
            "%(asctime)s | %(levelname)-5s"
            " | [%(request_id)s] [%(business_type)s] [%(filename_ctx)s] [%(extra_ctx)s]"
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
                console_handler.setFormatter(coloredlogs.ColoredFormatter(
                    _TEXT_FMT,
                    datefmt="%Y-%m-%d %H:%M:%S",
                    level_styles={
                        "debug":    {"color": "cyan"},
                        "info":     {"color": "white"},
                        "warning":  {"color": "yellow"},
                        "error":    {"color": "red",     "bold": True},
                        "critical": {"color": "magenta", "bold": True},
                    },
                    field_styles={
                        "asctime":   {"color": "blue"},
                        "levelname": {"color": "black", "bold": True},
                        "filename":  {"color": "blue"},
                        "lineno":    {"color": "blue"},
                    },
                ))

        # ── QueueHandler 模式（Gunicorn 多 worker）────────────────
        # 单一 QueueHandler 入队 → QueueListener 在后台线程分流到 debug/error handler，
        # 消除多 worker 同时写同一文件的竞争，同时避免旧版双 QueueHandler 双写问题。
        if use_queue:
            self.stop_queue_listener()
            log_queue = queue.Queue(maxsize=-1)
            self._queue_listener = QueueListener(
                log_queue, debug_handler, error_handler,
                respect_handler_level=True,
            )
            self._queue_listener.start()
            file_handlers: list[logging.Handler] = [QueueHandler(log_queue)]
        else:
            file_handlers = [debug_handler, error_handler]

        # ── 主 logger ─────────────────────────────────────────────
        root_logger = logging.getLogger(name)
        root_logger.handlers.clear()
        root_logger.filters.clear()
        root_logger.setLevel(logging.DEBUG)
        root_logger.propagate = False
        # 过滤器挂在 logger 层，每条记录只执行一次，不随 handler 数量线性增加
        root_logger.addFilter(ctx_filter)

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
            svc.addFilter(ctx_filter)
            if svc_name == "uvicorn.access":
                svc.addFilter(noise_filter)
            for h in tp_handlers:
                svc.addHandler(h)
            svc.propagate = False
            if svc.level == logging.NOTSET:
                svc.setLevel(logging.INFO)

        return root_logger


# ══════════════════════════════════════════════════════════════════
# 初始化入口
# ══════════════════════════════════════════════════════════════════

def initialize_logger(
    logger_manager:   LoggerManager,
    default_log_name: str = "app",
) -> logging.Logger:
    """
    从 LoggerManager 的配置中初始化 logger，失败时使用 default_log_name 作为兜底。

    其他项目接入示例：
      lm  = LoggerManager("/etc/myapp/log_config.yml")
      log = initialize_logger(lm, default_log_name="myapp")
      log.info("服务启动")
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
    """
    自动定位 log_config.yml：
      1. LOG_CONFIG_PATH 环境变量（最高优先级，适合 K8s ConfigMap 挂载）
      2. 本文件同级目录（默认行为，向后兼容）

    其他项目可通过设置 LOG_CONFIG_PATH 指向自己的配置文件，
    无需修改 logger_manager.py 本身。
    """
    env_path = os.getenv("LOG_CONFIG_PATH", "")
    if env_path and os.path.exists(env_path):
        return env_path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_config.yml")


# ══════════════════════════════════════════════════════════════════
# 模块级懒加载单例
# ══════════════════════════════════════════════════════════════════

_logger_manager: LoggerManager | None = None
_logger:         logging.Logger | None = None
_init_lock = threading.Lock()


def _bootstrap_singletons() -> None:
    """内部：初始化单例（调用方须持有 _init_lock）。"""
    global _logger_manager, _logger
    _logger_manager = LoggerManager(_find_config_path())
    _logger         = initialize_logger(_logger_manager, default_log_name="aihr")


def get_logger() -> logging.Logger:
    """
    获取全局 logger 单例（线程安全懒加载，import 时无副作用）。

    首次调用时自动初始化，后续直接返回缓存实例。
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
    """
    获取全局 LoggerManager 单例（线程安全懒加载）。

    典型用法（FastAPI lifespan）：
      lm = get_logger_manager()
      lm.start_config_watcher()
      yield
      lm.stop_config_watcher()
      lm.stop_queue_listener()
    """
    global _logger_manager
    if _logger_manager is not None:
        return _logger_manager
    with _init_lock:
        if _logger_manager is not None:
            return _logger_manager
        _bootstrap_singletons()
    return _logger_manager  # type: ignore[return-value]


# ══════════════════════════════════════════════════════════════════
# 向后兼容：模块级 logger / set_log_context 直接导入
# ══════════════════════════════════════════════════════════════════
# 现有代码 `from logger.logger_manager import logger, set_log_context` 无需任何修改

class _LazyProxy:
    """将属性访问代理到 get_logger()，实现 logger 的懒加载。"""

    def __getattr__(self, name: str):
        return getattr(get_logger(), name)

    def __repr__(self) -> str:
        return repr(get_logger())


logger             = _LazyProxy()
set_log_context    = set_log_context        # noqa: F811  显式重绑定，保证 __all__ 可导出
set_extra_context  = set_extra_context      # noqa: F811
clear_extra_context = clear_extra_context   # noqa: F811
wrap_for_executor  = wrap_for_executor      # noqa: F811
get_logger_manager = get_logger_manager     # noqa: F811
