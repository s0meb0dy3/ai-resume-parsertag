"""运行时模型配置与 HTTP 管理接口回归，不访问真实模型网关。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core import model_config as mc
from api import routes_model_config as routes
from config import wrap_for_executor, set_current_model_name, reset_current_model_name


@pytest.fixture
def store(tmp_path, monkeypatch):
    store = mc.ModelConfigStore(tmp_path / "models.json", mc.ModelSelection(
        syncModel="sync-old", asyncModel="async-old", fallbackModels=("backup-old",)))
    monkeypatch.setattr(mc, "_store", store)
    return store


def new_selection():
    return mc.ModelSelection(syncModel="sync-new", asyncModel="async-new", fallbackModels=("backup-new",))


def test_defaults_persistence_rollback_and_conflict(store):
    assert store.state().current.source == "environment"
    store.apply(new_selection(), 0)
    reloaded = mc.ModelConfigStore(store.path, new_selection())
    assert reloaded.state() == store.state()
    with pytest.raises(mc.ConfigConflict):
        store.apply(new_selection(), 0)
    state = store.apply(None, 1)
    assert state.current.syncModel == "sync-old"
    assert state.current.version == 2
    assert state.previous == new_selection()


def test_write_failure_preserves_memory_and_disk(store, monkeypatch):
    store.apply(new_selection(), 0)
    before, content = store.state(), store.path.read_bytes()
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(mc.os, "replace", fail)
    with pytest.raises(OSError):
        store.apply(None, 1)
    assert store.state() == before
    assert store.path.read_bytes() == content
    assert not list(store.path.parent.glob(".model-config-*"))


def test_corrupt_file_is_not_silently_ignored(store):
    store.path.write_text("broken")
    with pytest.raises(ValidationError):
        mc.ModelConfigStore(store.path, new_selection())


@pytest.mark.parametrize("patch", [
    {"syncModel": " "}, {"asyncModel": "model\nname"},
    {"fallbackModels": ["same", "same"]}, {"fallbackModels": [""]},
    {"fallbackModels": [str(i) for i in range(9)]},
])
def test_invalid_selection(patch):
    with pytest.raises(ValidationError):
        mc.ModelSelection(**(new_selection().model_dump() | patch))


def test_queued_async_tasks_and_threads_keep_snapshot(store):
    async def run():
        ready = asyncio.Event()
        token = mc.request_model_snapshot.set(store.state().current)
        async def queued():
            await ready.wait()
            with ThreadPoolExecutor(1) as pool:
                return await asyncio.get_running_loop().run_in_executor(pool, wrap_for_executor(
                    lambda: (mc.get_runtime_model("async"), mc.request_model_snapshot.get().fallbackModels)))
        task = asyncio.create_task(queued())
        mc.request_model_snapshot.reset(token)
        store.apply(new_selection(), 0)
        ready.set()
        assert await task == ("async-old", ("backup-old",))
        assert mc.get_runtime_model("async") == "async-new"
    asyncio.run(run())


def test_fallback_uses_pinned_snapshot(store, monkeypatch):
    from core import parser
    calls = []
    def fake(messages, model, **kwargs):
        calls.append(model)
        if len(calls) == 1:
            raise ValueError("main failed")
        return "OK"
    monkeypatch.setattr(parser, "_call_llm_model_once", fake)
    token = mc.request_model_snapshot.set(store.state().current)
    model_token = set_current_model_name("sync-old")
    try:
        store.apply(new_selection(), 0)
        assert parser._call_llm_with_fallback([]) == "OK"
        assert calls == ["sync-old", "backup-old"]
    finally:
        mc.request_model_snapshot.reset(token)
        reset_current_model_name(model_token)


@pytest.fixture
def http(store, monkeypatch):
    monkeypatch.setattr(routes, "MODEL_ADMIN_TOKEN", "test-admin")
    app = FastAPI()
    app.include_router(routes.router)
    app.add_middleware(mc.ModelSnapshotMiddleware)
    with TestClient(app) as client:
        yield client


AUTH = {"Authorization": "Bearer test-admin"}
BASE = "/api/admin/models"


def test_management_auth_and_updates(http, store, monkeypatch):
    assert http.get("/admin/models").status_code == 200
    for method, path, body in [
        ("GET", "/config", None), ("GET", "/catalog", None),
        ("PUT", "/config", new_selection().model_dump() | {"expectedVersion": 0}),
        ("POST", "/rollback", {"expectedVersion": 0}),
        ("POST", "/test", new_selection().model_dump() | {"kind": "text"}),
    ]:
        assert http.request(method, BASE + path, json=body).status_code == 401
    payload = new_selection().model_dump() | {"expectedVersion": 0}
    assert http.put(BASE + "/config", json=payload, headers=AUTH).status_code == 200
    assert http.put(BASE + "/config", json=payload, headers=AUTH).status_code == 409
    assert http.post(BASE + "/rollback", json={"expectedVersion": 1}, headers=AUTH).status_code == 200
    assert store.state().current.syncModel == "sync-old"
    monkeypatch.setattr(routes, "MODEL_ADMIN_TOKEN", "")
    assert http.get(BASE + "/config", headers=AUTH).status_code == 503


def test_page_serves_only_local_compiled_assets(http):
    page = http.get("/admin/models")
    assert '/admin/model-assets/model-config.css' in page.text
    assert '/admin/model-assets/model-config.js' in page.text
    for filename, content_type in [("model-config.css", "text/css"), ("model-config.js", "javascript")]:
        response = http.get("/admin/model-assets/" + filename)
        assert response.status_code == 200
        assert content_type in response.headers["content-type"]
        assert response.headers["cache-control"] == "no-store"
    for filename in ("package.json", "styles.css", "model-config.html", ".env"):
        assert http.get("/admin/model-assets/" + filename).status_code == 404


def test_probe_does_not_fallback_or_apply(http, store, monkeypatch):
    calls = []
    def fail(messages, model, **kwargs):
        calls.append(model)
        raise RuntimeError("secret upstream error")
    monkeypatch.setattr(routes, "_call_llm_model_once", fail)
    response = http.post(BASE + "/test", headers=AUTH,
                         json=new_selection().model_dump() | {"kind": "text"})
    assert response.status_code == 200
    assert calls == ["sync-new", "async-new", "backup-new"]
    assert all(not r["passed"] for r in response.json()["results"])
    assert "secret upstream" not in response.text
    assert store.state().current.version == 0


def test_vision_probe_uses_image_only_expected_value(http, monkeypatch):
    monkeypatch.setattr(routes.secrets, "randbelow", lambda n: 1234)
    def fake(messages, model, **kwargs):
        content = messages[0]["content"]
        assert "2234" not in content[0]["text"]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return "2234"
    monkeypatch.setattr(routes, "_call_llm_model_once", fake)
    response = http.post(BASE + "/test", headers=AUTH,
                         json=new_selection().model_dump() | {"kind": "vision"})
    assert response.status_code == 200
    assert all(r["passed"] for r in response.json()["results"])


def test_sync_helpers_use_same_pinned_model_after_apply(store, monkeypatch):
    from api import routes_openai as sync
    from config import get_current_model_name
    seen = []
    monkeypatch.setattr(sync, "parse_sync_resume_with_logging", lambda *args: seen.append(get_current_model_name()) or {})
    monkeypatch.setattr(sync, "run_tagging", lambda *args, **kwargs: seen.append(get_current_model_name()) or {})
    token = mc.request_model_snapshot.set(store.state().current)
    try:
        sync.parse_sync_document_with_model(b"", "test.pdf")
        store.apply(new_selection(), 0)
        sync.tag_sync_resume_with_model({}, {})
        assert seen == ["sync-old", "sync-old"]
    finally:
        mc.request_model_snapshot.reset(token)
    sync.tag_sync_resume_with_model({}, {})
    assert seen[-1] == "sync-new"


def test_concurrent_writers_only_one_commits(store):
    def write():
        try:
            store.apply(new_selection(), 0)
            return True
        except mc.ConfigConflict:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda _: write(), range(2))) == [False, True]
    assert store.state().current.version == 1


def test_real_app_requests_pin_independent_snapshots(store):
    from app import app
    # 独立测试路由验证实际 app 中间件，不启动全局 executor 的关闭生命周期。
    async def echo():
        return {"model": mc.get_runtime_model("sync"), "version": mc.request_model_snapshot.get().version}
    from fastapi.routing import APIRoute
    route = APIRoute("/_test_model_snapshot", echo)
    app.router.routes.append(route)
    try:
        client = TestClient(app)
        assert client.get("/_test_model_snapshot").json() == {"model": "sync-old", "version": 0}
        store.apply(new_selection(), 0)
        assert client.get("/_test_model_snapshot").json() == {"model": "sync-new", "version": 1}
    finally:
        app.router.routes.remove(route)
