"""单进程模型配置：原子持久化、乐观版本控制及请求级不可变快照。"""
import json
import os
import tempfile
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    syncModel: str = Field(min_length=1, max_length=200)
    asyncModel: str = Field(min_length=1, max_length=200)
    fallbackModels: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("syncModel", "asyncModel")
    @classmethod
    def model_id(cls, value):
        if value != value.strip() or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("模型 ID 不得含空白或控制字符")
        return value

    @field_validator("fallbackModels")
    @classmethod
    def fallback_ids(cls, values):
        for value in values:
            if not 1 <= len(value) <= 200:
                raise ValueError("备用模型 ID 长度应为 1～200")
            cls.model_id(value)
        if len(values) != len(set(values)):
            raise ValueError("备用模型不可重复")
        return values


class ModelSnapshot(ModelSelection):
    version: int = Field(default=0, ge=0)
    updatedAt: str = ""
    source: str = "environment"


class ModelConfigState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    current: ModelSnapshot
    previous: ModelSelection | None = None


class ConfigConflict(ValueError):
    pass


class ModelConfigStore:
    def __init__(self, path: Path, defaults: ModelSelection):
        self.path = path
        self._lock = RLock()
        # 损坏文件必须阻止启动，不能静默切回旧环境默认值。
        self._state = (ModelConfigState.model_validate_json(path.read_text()) if path.exists()
                       else ModelConfigState(current=ModelSnapshot(**defaults.model_dump())))

    def state(self) -> ModelConfigState:
        with self._lock:
            return self._state

    def apply(self, selection: ModelSelection | None, expected_version: int) -> ModelConfigState:
        with self._lock:
            old = self._state
            if old.current.version != expected_version:
                raise ConfigConflict("配置已更新，请刷新后重试")
            if selection is None:
                selection = old.previous
                if selection is None:
                    raise ConfigConflict("没有可回退的版本")
            previous = ModelSelection(**old.current.model_dump(include=set(ModelSelection.model_fields)))
            new = ModelConfigState(
                current=ModelSnapshot(**selection.model_dump(), version=old.current.version + 1,
                                      updatedAt=datetime.now(timezone.utc).isoformat(), source="runtime"),
                previous=previous,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".model-config-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(new.model_dump_json(indent=2) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                # 当前和上一版在同一个文件原子提交；失败时不改变内存。
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            self._state = new
            return new


_store = None
_store_lock = RLock()
request_model_snapshot: ContextVar[ModelSnapshot | None] = ContextVar("model_config_snapshot", default=None)


def get_model_store() -> ModelConfigStore:
    global _store
    with _store_lock:
        if _store is None:
            from config import MODEL_CONFIG_FILE, SYNC_MODEL_NAME, ASYNC_MODEL_NAME, FALLBACK_MODELS
            _store = ModelConfigStore(Path(MODEL_CONFIG_FILE), ModelSelection(
                syncModel=SYNC_MODEL_NAME, asyncModel=ASYNC_MODEL_NAME, fallbackModels=tuple(FALLBACK_MODELS)))
        return _store


def get_runtime_model(lane: str) -> str:
    snapshot = request_model_snapshot.get() or get_model_store().state().current
    return snapshot.syncModel if lane == "sync" else snapshot.asyncModel


class ModelSnapshotMiddleware:
    """在 HTTP 请求进入时固定整批配置；asyncio 任务及线程池继承同一快照。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        token = request_model_snapshot.set(get_model_store().state().current)
        try:
            await self.app(scope, receive, send)
        finally:
            request_model_snapshot.reset(token)
