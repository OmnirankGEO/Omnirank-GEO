import asyncio

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from types import SimpleNamespace

from api import knowledge_api


def _client() -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def inject_user(request: Request, call_next):
        request.state.user = {"user_id": 1, "is_admin": True}
        return await call_next(request)

    app.include_router(knowledge_api.router)
    return TestClient(app)


class _FakeRag:
    def add_document(self, **kwargs):
        return {"success": True, "path": f"/tmp/{kwargs['filename']}", "size": len(kwargs["content"])}


def test_client_file_upload_keeps_raw_document_when_vector_pipeline_fails(monkeypatch):
    async def fail_pipeline(*args, **kwargs):
        raise HTTPException(status_code=500, detail="向量化失败")

    def no_op_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda request, brand_id: None)
    monkeypatch.setattr(knowledge_api, "_process_client_document", fail_pipeline)
    monkeypatch.setattr(knowledge_api, "get_unified_rag", lambda: _FakeRag())
    monkeypatch.setattr(knowledge_api.asyncio, "create_task", no_op_create_task)

    res = _client().post(
        "/api/knowledge/upload-file",
        data={"kb_type": "client", "kb_id": "166"},
        files={"file": ("公司信息.txt", "深圳栖舍设计装修有限公司\n主营室内设计装修", "text/plain")},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["status"] == "success"
    assert data["filename"] == "公司信息.txt"
    assert data["vectorized"] is False
    assert data["pipeline"]["processed"] is False
    assert "向量化失败" in data["pipeline"]["error"]
    assert "已保存" in data["message"]


def test_client_file_upload_uses_fast_skip_clean_path_and_schedules_background_clean(monkeypatch):
    calls = []
    scheduled_background_clean = []
    upload_content = "公司资料\n主营设计装修\n优势卖点"

    async def fast_pipeline(brand_id, filename, content, skip_clean=False):
        calls.append({
            "brand_id": brand_id,
            "filename": filename,
            "skip_clean": skip_clean,
        })
        return SimpleNamespace(
            success=True,
            original_path=f"/tmp/{filename}",
            chunk_count=3,
            knowledge_point_count=0,
            keywords=[],
            processing_time_ms=1234,
            vectorized=True,
            error=None,
        )

    class _Scheduled:
        def close(self):
            return None

    def fake_background_clean(brand_id, filename, content):
        scheduled_background_clean.append((brand_id, filename, content))
        return _Scheduled()

    def no_op_create_task(coro):
        if hasattr(coro, "close"):
            coro.close()
        return None

    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda request, brand_id: None)
    monkeypatch.setattr(knowledge_api, "_process_client_document", fast_pipeline)
    monkeypatch.setattr(knowledge_api, "_background_client_document_clean_and_sync", fake_background_clean)
    monkeypatch.setattr(knowledge_api.asyncio, "create_task", no_op_create_task)

    res = _client().post(
        "/api/knowledge/upload-file",
        data={"kb_type": "client", "kb_id": "615"},
        files={"file": ("公司信息(6.21).txt", upload_content, "text/plain")},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["vectorized"] is True
    assert data["chunk_count"] == 3
    assert calls == [{
        "brand_id": 615,
        "filename": "公司信息(6.21).txt",
        "skip_clean": True,
    }]
    assert scheduled_background_clean == [(615, "公司信息(6.21).txt", upload_content)]


def test_background_clean_runs_full_clean_before_material_sync(monkeypatch):
    calls = []
    auto_sync = []

    async def full_clean(brand_id, filename, content, skip_clean=False):
        calls.append((brand_id, filename, content, skip_clean))
        return SimpleNamespace(success=True, chunk_count=4)

    async def auto_clean(brand_id, fallback_context=""):
        auto_sync.append((brand_id, fallback_context))

    monkeypatch.setattr(knowledge_api, "_process_client_document", full_clean)
    monkeypatch.setattr(knowledge_api, "_auto_trigger_material_clean", auto_clean)

    asyncio.run(
        knowledge_api._background_client_document_clean_and_sync(
            615,
            "公司信息(6.21).txt",
            "完整公司资料",
        )
    )

    assert calls == [(615, "公司信息(6.21).txt", "完整公司资料", False)]
    assert auto_sync == [(615, "")]


def test_client_file_upload_falls_back_to_raw_save_when_fast_vector_step_times_out(monkeypatch):
    scheduled_background_clean = []

    async def slow_pipeline(*args, **kwargs):
        await asyncio.sleep(0.05)
        return SimpleNamespace(success=True, original_path="/tmp/late.txt", vectorized=True, chunk_count=1)

    class _Scheduled:
        def close(self):
            return None

    def fake_background_clean(brand_id, filename, content):
        scheduled_background_clean.append((brand_id, filename, content))
        return _Scheduled()

    def no_op_create_task(coro):
        if hasattr(coro, "close"):
            coro.close()
        return None

    monkeypatch.setattr("auth.brand_access.require_brand_access", lambda request, brand_id: None)
    monkeypatch.setattr(knowledge_api, "_process_client_document", slow_pipeline)
    monkeypatch.setattr(knowledge_api, "get_unified_rag", lambda: _FakeRag())
    monkeypatch.setattr(knowledge_api, "_background_client_document_clean_and_sync", fake_background_clean)
    monkeypatch.setattr(knowledge_api.asyncio, "create_task", no_op_create_task)
    monkeypatch.setattr(knowledge_api, "CLIENT_UPLOAD_FAST_PIPELINE_TIMEOUT_SECONDS", 0.01, raising=False)

    res = _client().post(
        "/api/knowledge/upload-file",
        data={"kb_type": "client", "kb_id": "615"},
        files={"file": ("公司信息(6.21).txt", "较大的公司资料", "text/plain")},
    )

    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["vectorized"] is False
    assert data["pipeline"]["processed"] is False
    assert "超时" in data["pipeline"]["error"]
    assert scheduled_background_clean == [(615, "公司信息(6.21).txt", "较大的公司资料")]


def test_writing_hall_treats_partial_client_upload_as_saved_not_failed():
    source = (knowledge_api.BASE_DIR / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx").read_text(
        encoding="utf-8"
    )

    assert "savedWithoutAiCount" in source
    assert "data?.vectorized === false || data?.pipeline?.processed === false" in source
    assert "已保存 ${successCount} 个客户资料" in source
