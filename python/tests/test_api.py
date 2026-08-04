from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from omarag_bridge.app import create_app
from omarag_bridge.config import Settings
from omarag_bridge.models.domain import ModelCategory


async def test_meta_health_and_openapi(client: httpx2.AsyncClient) -> None:
    health = await client.get("/v1/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    meta = await client.get("/v1/meta")
    assert meta.status_code == 200
    assert meta.json()["api_version"] == "1.0"
    assert meta.json()["capabilities"]["event_replay"] is True

    schema = (await client.get("/openapi.json")).json()
    assert schema["openapi"].startswith("3.1")
    assert "/v1/workspaces/{workspace_id}/documents/ingest" in schema["paths"]


async def test_search_and_ingest_policies_reject_unknown_values(
    client: httpx2.AsyncClient, workspace: dict[str, object]
) -> None:
    workspace_id = str(workspace["id"])
    invalid_policy = await client.post(
        f"/v1/workspaces/{workspace_id}/search",
        json={"query": "Beton", "document_policy": "curent-only"},
    )
    assert invalid_policy.status_code == 422

    unknown_filter = await client.post(
        f"/v1/workspaces/{workspace_id}/search",
        json={"query": "Beton", "filters": {"edtion_number": 8}},
    )
    assert unknown_filter.status_code == 422

    invalid_profile = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/ingest",
        headers={"Idempotency-Key": "invalid-profile"},
        json={
            "sources": [{"type": "file", "path": "/tmp/book.pdf"}],
            "processing_profile": "techncial",
        },
    )
    assert invalid_profile.status_code == 422


async def test_hardware_aware_model_catalog_roles_profiles_and_runtime(
    client: httpx2.AsyncClient,
) -> None:
    for category in ("chat", "vl", "embedding", "rerank"):
        response = await client.get(
            "/v1/models/catalog",
            params={
                "source": "hugging-face",
                "category": category,
                "quantization": "Q3_K_M",
                "context_tokens": 4096,
                "profile": "eco",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["entries"][0]["category"] == category
        assert payload["entries"][0]["recommended_rank"] == 1
        assert payload["entries"][0]["fit"] == "comfortable"
        assert payload["scanned"] == 500
        assert payload["truncated"] is True

    runtime = await client.get("/v1/models/runtime")
    assert runtime.status_code == 200
    assert runtime.json()["models"][0]["name"] == "test/chat-2b"

    loaded = await client.post(
        "/v1/models/load",
        json={"model": "test/chat-2b", "context_tokens": 4096, "keep_alive": "5m"},
    )
    assert loaded.status_code == 200
    assert loaded.json()["operation"] == "load"

    unloaded = await client.post("/v1/models/unload", json={"model": "test/chat-2b"})
    assert unloaded.status_code == 200
    assert unloaded.json()["operation"] == "unload"

    pulled = await client.post("/v1/models/pull", json={"model": "test/chat-2b"})
    assert pulled.status_code == 200
    assert '"status":"success"' in pulled.text

    wrong_confirmation = await client.request(
        "DELETE",
        "/v1/models",
        json={"model": "test/remove-2b", "confirm": "wrong"},
    )
    assert wrong_confirmation.status_code == 422

    loaded_delete = await client.request(
        "DELETE",
        "/v1/models",
        json={"model": "test/chat-2b", "confirm": "test/chat-2b"},
    )
    assert loaded_delete.status_code == 409

    deleted = await client.request(
        "DELETE",
        "/v1/models",
        json={"model": "test/remove-2b", "confirm": "test/remove-2b"},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {
        "model": "test/remove-2b",
        "operation": "delete",
        "status": "ok",
    }


async def test_model_defaults_are_preflighted_applied_atomically_and_reported(
    client: httpx2.AsyncClient, workspace: dict[str, object]
) -> None:
    workspace_id = str(workspace["id"])
    config = await client.get(f"/v1/workspaces/{workspace_id}/config")
    commented = config.json()["content"].replace("qa:\n", "# user model preference\nqa:\n")
    saved = await client.put(
        f"/v1/workspaces/{workspace_id}/config",
        headers={"If-Match": config.headers["etag"]},
        json={"content": commented},
    )
    assert saved.status_code == 200
    payload = {
        "chat": "test/chat-2b",
        "vl": "test/chat-2b",
        "embedding": "qwen3-embedding:0.6b",
        "rerank": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        "embedding_provider": "ollama",
        "rerank_provider": "cross-encoder",
        "vector_dim": 1024,
    }
    preflight = await client.post(
        f"/v1/workspaces/{workspace_id}/model-defaults/preflight", json=payload
    )
    assert preflight.status_code == 200
    assert preflight.json()["requires_reindex"] is False
    assert "chat" in preflight.json()["changes"]

    applied = await client.post(
        f"/v1/workspaces/{workspace_id}/model-defaults/apply",
        headers={"If-Match": saved.headers["etag"]},
        json=payload,
    )
    assert applied.status_code == 200
    assert "# user model preference" in applied.json()["content"]
    assert "chat: test/chat-2b" in applied.json()["content"]

    runtime = await client.get("/v1/models/runtime", params={"workspace_id": workspace_id})
    assert runtime.status_code == 200
    roles = {item["role"]: item for item in runtime.json()["roles"]}
    assert roles["chat"]["model"] == "test/chat-2b"
    assert roles["chat"]["residency"] == "loaded"
    assert roles["vl"]["shared_with"] == ["chat"]

    stale = await client.post(
        f"/v1/workspaces/{workspace_id}/model-defaults/apply",
        headers={"If-Match": saved.headers["etag"]},
        json={**payload, "chat": "test/other"},
    )
    assert stale.status_code == 409


async def test_gguf_import_validates_early_streams_and_removes_temporary_files(
    client: httpx2.AsyncClient, app: FastAPI
) -> None:
    invalid = await client.post(
        "/v1/models/import/gguf",
        data={"model": "local/bad", "category": "chat"},
        files={"file": ("bad.gguf", b"NOPE", "application/octet-stream")},
    )
    assert invalid.status_code == 409

    wrong_role = await client.post(
        "/v1/models/import/gguf",
        data={"model": "local/rerank", "category": "rerank"},
        files={"file": ("rank.gguf", b"GGUFdata", "application/octet-stream")},
    )
    assert wrong_role.status_code == 409

    imported = await client.post(
        "/v1/models/import/gguf",
        data={"model": "local/textbook", "category": "chat"},
        files={"file": ("textbook.gguf", b"GGUFtest-model", "application/octet-stream")},
    )
    assert imported.status_code == 200
    assert '"status":"success"' in imported.text
    service = app.state.services.models
    assert service.imported_gguf is not None
    assert service.imported_gguf[0:3] == (
        "textbook.gguf",
        "local/textbook",
        ModelCategory.CHAT,
    )
    imports = app.state.services.settings.data_dir / "cache" / "model-imports"
    assert list(imports.glob("*.part")) == []


async def test_configured_model_cannot_be_deleted(
    client: httpx2.AsyncClient, workspace: dict[str, object]
) -> None:
    response = await client.request(
        "DELETE",
        "/v1/models",
        json={
            "model": "qwen3-embedding:0.6b",
            "confirm": "qwen3-embedding:0.6b",
        },
    )
    assert response.status_code == 409
    assert str(workspace["name"]) in response.json()["error"]["message"]


async def test_auth_is_secure_by_default(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", auth_enabled=True, bearer_token="secret"))
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as protected:
            assert (await protected.get("/v1/health")).status_code == 200
            assert (await protected.get("/v1/meta")).status_code == 401
            response = await protected.get("/v1/meta", headers={"Authorization": "Bearer secret"})
            assert response.status_code == 200


async def test_parser_catalog_exposes_docling_provenance(client: httpx2.AsyncClient) -> None:
    response = await client.get("/v1/parsers")

    assert response.status_code == 200
    parsers = {item["id"]: item for item in response.json()}
    assert set(parsers) == {"auto", "docling"}
    assert parsers["docling"]["provenance"] is True
    assert parsers["docling"]["structured_chunking"] is True


async def test_workspace_lifecycle_etag_clone_and_physical_delete(
    client: httpx2.AsyncClient, workspace: dict[str, object]
) -> None:
    workspace_id = str(workspace["id"])
    workspace_path = Path(str(workspace["path"]))
    assert (workspace_path / "workspace.toml").exists()
    assert (workspace_path / "database").is_dir()
    config = (workspace_path / "haiku.rag.yaml").read_text(encoding="utf-8")
    assert "qwen3-embedding:0.6b" in config
    assert "chunk_size: 384" in config
    assert "chunking_use_markdown_tables: true" in config
    assert "split_pages: 25" in config
    assert "mmarco-mMiniLMv2-L12-H384-v1" in config
    assert "qwen3.5:4b-q4_K_M" in config
    assert "vision: true" in config

    fetched = await client.get(f"/v1/workspaces/{workspace_id}")
    assert fetched.headers["etag"] == f'"{workspace["etag"]}"'

    stale = await client.patch(
        f"/v1/workspaces/{workspace_id}",
        headers={"If-Match": '"stale"'},
        json={"name": "Neu"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "ETAG_CONFLICT"

    changed = await client.patch(
        f"/v1/workspaces/{workspace_id}",
        headers={"If-Match": fetched.headers["etag"]},
        json={"name": "Baustoffkunde 2"},
    )
    assert changed.status_code == 200
    assert changed.json()["name"] == "Baustoffkunde 2"

    clone = await client.post(f"/v1/workspaces/{workspace_id}/clone", json={"name": "Klon"})
    assert clone.status_code == 201
    clone_path = Path(clone.json()["path"])
    deleted = await client.request(
        "DELETE",
        f"/v1/workspaces/{clone.json()['id']}",
        json={"confirm": "DELETE", "mode": "physical"},
    )
    assert deleted.status_code == 204
    assert not clone_path.exists()


async def test_ingest_is_idempotent_and_events_replay(
    client: httpx2.AsyncClient, app: FastAPI, workspace: dict[str, object]
) -> None:
    workspace_id = str(workspace["id"])
    payload = {
        "sources": [{"type": "file", "path": "/tmp/beton.pdf"}],
        "tags": ["Beton"],
    }
    headers = {"Idempotency-Key": "ingest-beton-1"}
    first = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/ingest",
        json=payload,
        headers=headers,
    )
    second = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/ingest",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json() == {"id": first.json()["id"], "reused": True}

    conflict = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/ingest",
        json={"sources": [{"type": "file", "path": "/tmp/anders.pdf"}]},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    job_id = first.json()["id"]
    for _ in range(50):
        job = (await client.get(f"/v1/jobs/{job_id}")).json()
        if job["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert job["status"] == "completed"
    assert job["progress"] == 1.0

    store = app.state.services.store
    events = store.events_after(0, job_id=job_id)
    assert [event.type for event in events] == [
        "job.queued",
        "job.started",
        "job.progress",
        "job.completed",
    ]
    replay = store.events_after(events[1].event_id, job_id=job_id)
    assert [event.event_id for event in replay] == [
        events[2].event_id,
        events[3].event_id,
    ]


async def test_search_and_run_have_stable_domain_models(
    client: httpx2.AsyncClient, workspace: dict[str, object]
) -> None:
    workspace_id = str(workspace["id"])
    search = await client.post(
        f"/v1/workspaces/{workspace_id}/search",
        json={"query": "XC4", "limit": 5},
    )
    assert search.status_code == 200
    assert search.json()[0]["chunk_id"] == "chunk-1"

    explanation = await client.post(
        f"/v1/workspaces/{workspace_id}/search/explain",
        json={"query": "XC4", "limit": 5},
    )
    assert explanation.status_code == 200
    assert explanation.json()["ranked"][0]["chunk_id"] == "chunk-1"

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/runs",
        json={"question": "Was bedeutet XC4?", "evidence_mode": "strict"},
    )
    assert response.status_code == 202
    run_id = response.json()["id"]
    for _ in range(50):
        run = (await client.get(f"/v1/runs/{run_id}")).json()
        if run["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert run["answer"] == "Antwort auf: Was bedeutet XC4?"
    assert run["citations"][0]["pages"] == [1]

    analysis_response = await client.post(
        f"/v1/workspaces/{workspace_id}/runs",
        json={"question": "Pruefe XC4", "mode": "analysis"},
    )
    analysis_id = analysis_response.json()["id"]
    for _ in range(50):
        analysis = (await client.get(f"/v1/runs/{analysis_id}")).json()
        if analysis["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert analysis["answer"] == "Analyse von: Pruefe XC4"
    assert analysis["citations"][0]["pages"] == [2]


async def test_exact_answers_are_cached_per_generation_and_sessions_get_receipts(
    client: httpx2.AsyncClient, workspace: dict[str, object], app: FastAPI
) -> None:
    workspace_id = str(workspace["id"])
    adapter = app.state.services.adapter

    async def run_question() -> dict[str, object]:
        response = await client.post(
            f"/v1/workspaces/{workspace_id}/runs",
            json={
                "question": "  Was   bedeutet XC4?  ",
                "evidence_mode": "strict",
                "session_id": "conversation-cache-test",
            },
        )
        assert response.status_code == 202
        run_id = response.json()["id"]
        for _ in range(50):
            run = (await client.get(f"/v1/runs/{run_id}")).json()
            if run["status"] == "completed":
                return run
            await asyncio.sleep(0.01)
        pytest.fail("run did not complete")

    first = await run_question()
    second = await run_question()

    assert adapter.ask_calls == 1
    assert first["session_id"] == "conversation-cache-test"
    assert first["receipt"]["turn"] == 1
    assert first["receipt"]["cache_status"] == "miss"
    assert first["receipt"]["new_source_count"] == 1
    assert second["receipt"]["turn"] == 2
    assert second["receipt"]["cache_status"] == "hit"
    assert second["receipt"]["reused_source_count"] == 1
    assert second["receipt"]["new_source_count"] == 0

    events = app.state.services.store.events_after(0, run_id=str(second["id"]))
    completed = next(event for event in events if event.type == "run.completed")
    assert completed.payload["receipt"]["cache_status"] == "hit"

    config = await client.get(f"/v1/workspaces/{workspace_id}/config")
    changed = await client.put(
        f"/v1/workspaces/{workspace_id}/config",
        headers={"If-Match": config.headers["etag"]},
        json={"content": config.json()["content"] + "\n# cache generation changed\n"},
    )
    assert changed.status_code == 200

    third = await run_question()
    assert adapter.ask_calls == 2
    assert third["receipt"]["turn"] == 3
    assert third["receipt"]["cache_status"] == "miss"


async def test_workspace_feature_vertical_slices(
    client: httpx2.AsyncClient, workspace: dict[str, object], tmp_path: Path
) -> None:
    workspace_id = str(workspace["id"])
    source_pdf = tmp_path / "regelwerk.pdf"
    source_pdf.write_bytes(b"test fixture")
    ingest = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/ingest",
        json={"sources": [{"type": "file", "path": str(source_pdf)}]},
        headers={"Idempotency-Key": "feature-document"},
    )
    for _ in range(50):
        job = (await client.get(f"/v1/jobs/{ingest.json()['id']}")).json()
        if job["status"] == "completed":
            break
        await asyncio.sleep(0.01)

    documents = await client.get(f"/v1/workspaces/{workspace_id}/documents")
    assert documents.status_code == 200
    assert documents.json()[0]["title"] == "regelwerk.pdf"
    document_id = documents.json()[0]["id"]
    removed = await client.delete(f"/v1/workspaces/{workspace_id}/documents/{document_id}")
    assert removed.status_code == 204
    assert (await client.get(f"/v1/workspaces/{workspace_id}/documents")).json() == []
    restored_document = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/{document_id}/restore"
    )
    assert restored_document.status_code == 204
    restored_documents = await client.get(f"/v1/workspaces/{workspace_id}/documents")
    assert restored_documents.json()[0]["id"] == document_id

    source = await client.post(
        f"/v1/workspaces/{workspace_id}/sources",
        json={"name": "Regelwerk", "type": "file", "location": "/tmp/regelwerk.pdf"},
    )
    assert source.status_code == 201
    sources = await client.get(f"/v1/workspaces/{workspace_id}/sources")
    assert [item["name"] for item in sources.json()] == ["Regelwerk"]

    quality = await client.get(f"/v1/workspaces/{workspace_id}/quality")
    assert quality.json()["document_count"] == 1
    assert quality.json()["status"] == "ok"

    config = await client.get(f"/v1/workspaces/{workspace_id}/config")
    assert config.status_code == 200
    updated_content = config.json()["content"].replace("temperature: 0.1", "temperature: 0.3")
    updated = await client.put(
        f"/v1/workspaces/{workspace_id}/config",
        headers={"If-Match": config.headers["etag"]},
        json={"content": updated_content},
    )
    assert updated.status_code == 200
    assert "temperature: 0.3" in updated.json()["content"]

    backup = await client.post(f"/v1/workspaces/{workspace_id}/backups")
    assert backup.status_code == 201
    assert backup.json()["verified"] is True
    verified = await client.post(
        f"/v1/workspaces/{workspace_id}/backups/{backup.json()['id']}/verify"
    )
    assert verified.json()["verified"] is True

    workspace_path = Path(str(workspace["path"]))
    marker = workspace_path / "after-backup.txt"
    marker.write_text("wird beim Restore entfernt", encoding="utf-8")
    restored = await client.post(
        f"/v1/workspaces/{workspace_id}/backups/{backup.json()['id']}/restore",
        json={"confirm": "RESTORE"},
    )
    assert restored.status_code == 200
    assert restored.json()["id"] == backup.json()["id"]
    assert not marker.exists()
    assert len((await client.get(f"/v1/workspaces/{workspace_id}/backups")).json()) == 2


async def test_duplicate_replace_rebuilds_the_existing_document(
    client: httpx2.AsyncClient,
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
) -> None:
    workspace_id = str(workspace["id"])
    source = tmp_path / "mixed-table.pdf"
    source.write_bytes(b"same textbook content")

    async def ingest(key: str, policy: str) -> dict[str, object]:
        started = await client.post(
            f"/v1/workspaces/{workspace_id}/documents/ingest",
            headers={"Idempotency-Key": key},
            json={
                "sources": [{"type": "file", "path": str(source)}],
                "duplicate_policy": policy,
            },
        )
        assert started.status_code == 202
        for _ in range(50):
            job = (await client.get(f"/v1/jobs/{started.json()['id']}")).json()
            if job["status"] in {"completed", "failed"}:
                return job
            await asyncio.sleep(0.01)
        pytest.fail("ingest did not complete")

    assert (await ingest("initial-table-import", "review"))["status"] == "completed"
    assert (await ingest("replacement-table-import", "replace"))["status"] == "completed"
    assert app.state.services.adapter.ingest_calls == 2
