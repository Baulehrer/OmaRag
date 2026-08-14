from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from fastapi import FastAPI

from omarag_bridge.models.api import IngestRequest, ModelProfileApplyAndReindexRequest
from omarag_bridge.models.domain import CatalogRole, JobStatus
from omarag_bridge.models.errors import ConflictError
from omarag_bridge.services import job_service as job_service_module


def _old_book(source: Path) -> dict[str, Any]:
    return {
        "logical_document_id": "book-old",
        "document_id": "book-old",
        "generation_id": "generation-old",
        "original_source": str(source),
        "managed_source": str(source),
        "pipeline_version": "book-index-v3",
        "book_metadata": {"title": "Old book"},
        "quality": {},
        "segments": [
            {
                "segment_index": 0,
                "document_id": "segment-old",
                "page_start": 1,
                "page_end": 1,
            }
        ],
    }


def test_apply_and_reindex_contract_requires_exact_consent() -> None:
    with pytest.raises(ValueError):
        ModelProfileApplyAndReindexRequest.model_validate(
            {"preflight_id": "rec-1", "confirm": "APPLY"}
        )
    request = ModelProfileApplyAndReindexRequest.model_validate(
        {"preflight_id": "rec-1", "confirm": "APPLY_AND_REINDEX"}
    )
    assert request.indexing.pipeline == "book-v3"


@pytest.mark.asyncio
async def test_http_source_cannot_hide_behind_file_type_and_config_cannot_redirect_ollama(
    client: httpx2.AsyncClient,
    workspace: dict[str, object],
) -> None:
    workspace_id = str(workspace["id"])
    disguised = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/ingest",
        json={"sources": [{"type": "file", "path": "https://example.test/book.pdf"}]},
        headers={"Idempotency-Key": "disguised-url"},
    )
    assert disguised.status_code == 422

    config = await client.get(f"/v1/workspaces/{workspace_id}/config")
    redirected = config.json()["content"].replace(
        "base_url: http://127.0.0.1:11434",
        "base_url: https://remote.example.test",
    )
    rejected = await client.put(
        f"/v1/workspaces/{workspace_id}/config",
        headers={"If-Match": config.headers["etag"]},
        json={"content": redirected},
    )
    assert rejected.status_code == 409
    unchanged = await client.get(f"/v1/workspaces/{workspace_id}/config")
    assert unchanged.json()["etag"] == config.json()["etag"]


@pytest.mark.asyncio
async def test_profile_embedding_change_is_staged_then_fails_closed_and_replays(
    client: httpx2.AsyncClient,
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = app.state.services
    workspace_id = str(workspace["id"])
    source = tmp_path / "book.txt"
    source.write_text("stable archived original", encoding="utf-8")
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    services.store.upsert_document(workspace_id, str(source), fingerprint, _old_book(source))

    original_profile_preflight = services.models.profile_preflight

    async def changed_embedding(*args: Any, **kwargs: Any):
        result = await original_profile_preflight(*args, **kwargs)
        assignments = [
            item.model_copy(
                update={
                    "model": "test/new-embedding:1",
                    "digest": "sha256:new-embedding-digest",
                    "installed_digest": "sha256:new-embedding-digest",
                }
            )
            if item.role == CatalogRole.EMBEDDING
            else item
            for item in result.recommendation.assignments
        ]
        recommendation = result.recommendation.model_copy(update={"assignments": assignments})
        return result.model_copy(
            update={
                "recommendation": recommendation,
                "requires_reindex": True,
                "downloads": [],
                "can_apply": True,
            }
        )

    monkeypatch.setattr(services.models, "profile_preflight", changed_embedding)

    def frozen_runtime_lock(
        workspace_path: Path,
        _ollama_url: str,
        pipeline: str = "book-index-v2",
        *,
        config_bytes: bytes | None = None,
    ) -> dict[str, str]:
        raw = config_bytes or (workspace_path / "haiku.rag.yaml").read_bytes()
        config = yaml.safe_load(raw) or {}
        embedding = (config.get("embeddings") or {}).get("model") or {}
        reranker = (config.get("reranking") or {}).get("model") or {}
        model = str(embedding.get("name") or "")
        digest = (
            "sha256:new-embedding-digest"
            if model == "test/new-embedding:1"
            else "sha256:old-embedding-digest"
        )
        return {
            "pipeline": pipeline,
            "haiku": "0.70.0",
            "docling": "2.58.0",
            "workspace_config_sha256": hashlib.sha256(raw).hexdigest(),
            "embedding_provider": str(embedding.get("provider") or "ollama"),
            "embedding_model": model,
            "embedding_digest": digest,
            "reranker_model": str(reranker.get("name") or ""),
        }

    monkeypatch.setattr(job_service_module, "_rebuild_runtime_lock", frozen_runtime_lock)
    monkeypatch.setattr(services.jobs, "spawn_profile_reindex", lambda _job_id: None)

    preflight = await client.post(
        f"/v1/workspaces/{workspace_id}/model-profile/preflight",
        json={"performance_profile": "quality"},
    )
    assert preflight.status_code == 200
    recommendation_id = preflight.json()["recommendation"]["recommendation_id"]
    assert preflight.json()["requires_reindex"] is True
    before = services.features.config(workspace_id)

    request = {
        "preflight_id": recommendation_id,
        "confirm": "APPLY_AND_REINDEX",
    }
    headers = {"Idempotency-Key": "profile-reindex-once"}
    queued = await client.post(
        f"/v1/workspaces/{workspace_id}/model-profile/apply-and-reindex",
        json=request,
        headers=headers,
    )
    assert queued.status_code == 202
    assert queued.json()["reused"] is False
    job_id = queued.json()["id"]
    job = services.store.get_job(job_id)
    transition = job.payload["profile_transition"]
    assert job.status == JobStatus.QUEUED
    assert services.features.config(workspace_id).etag == before.etag
    stage = Path(str(workspace["path"])) / transition["staged_config"]
    assert stage.is_file()
    assert stage.stat().st_mode & 0o777 == 0o600
    assert "test/new-embedding:1" in stage.read_text(encoding="utf-8")

    replay = await client.post(
        f"/v1/workspaces/{workspace_id}/model-profile/apply-and-reindex",
        json=request,
        headers=headers,
    )
    assert replay.status_code == 202
    assert replay.json() == {"id": job_id, "reused": True}

    await services.jobs._run_reindex(job_id)
    failed = services.store.get_job(job_id)
    generation = services.store.workspace_index_generation(workspace_id)
    assert failed.status == JobStatus.FAILED
    assert generation is not None
    assert generation["status"] == "maintenance_failed"
    assert services.features.config(workspace_id).etag == transition["target_config_etag"]
    assert stage.is_file(), "failed staged rebuild must remain resumable"
    blocked = await client.post(
        f"/v1/workspaces/{workspace_id}/search",
        json={"query": "test"},
    )
    assert blocked.status_code == 503
    assert blocked.json()["error"]["code"] == "INDEX_NOT_READY"

    completed_replay = await client.post(
        f"/v1/workspaces/{workspace_id}/model-profile/apply-and-reindex",
        json=request,
        headers=headers,
    )
    assert completed_replay.status_code == 202
    assert completed_replay.json() == {"id": job_id, "reused": True}


@pytest.mark.asyncio
async def test_queued_pause_and_resume_wait_for_the_old_runner(
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = app.state.services
    workspace_id = str(workspace["id"])
    source = tmp_path / "pause.txt"
    source.write_text("pause-safe", encoding="utf-8")
    request = IngestRequest(sources=[{"type": "file", "path": str(source)}])

    original_spawn = services.jobs._spawn
    monkeypatch.setattr(services.jobs, "_spawn", lambda _job_id: None)
    job, _ = await services.jobs.start_ingest(workspace_id, request, "pause-race")
    await services.jobs.pause(job.id)
    await services.jobs._run_ingest(job.id)
    assert services.store.get_job(job.id).status == JobStatus.PAUSED
    assert services.adapter.ingest_calls == 0

    with pytest.raises(ConflictError, match="queued, running, or paused"):
        async with services.jobs.writer(fail_if_active=True):
            pass

    monkeypatch.setattr(services.jobs, "_spawn", original_spawn)

    async def old_runner_unwind() -> None:
        await asyncio.sleep(0.02)

    services.jobs._tasks[job.id] = asyncio.create_task(old_runner_unwind())
    resumed = await services.jobs.resume(job.id)
    assert resumed.status == JobStatus.RUNNING
    for _ in range(100):
        current = services.store.get_job(job.id)
        if current.status in {JobStatus.COMPLETED, JobStatus.FAILED}:
            break
        await asyncio.sleep(0.01)
    assert current.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_hidden_book_never_falls_back_to_unfiltered_search(
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
) -> None:
    services = app.state.services
    workspace_id = str(workspace["id"])
    source = tmp_path / "hidden.pdf"
    source.write_bytes(b"hidden")
    result = _old_book(source)
    services.store.upsert_document(
        workspace_id,
        str(source),
        hashlib.sha256(source.read_bytes()).hexdigest(),
        result,
    )
    assert services.store.resolve_segment_ids(workspace_id, {}, "current-only") == ["segment-old"]
    services.store.replace_hidden_documents(workspace_id, {"book-old"})
    assert services.store.resolve_segment_ids(workspace_id, {}, "current-only") == []
    services.store.replace_hidden_documents(workspace_id, set())
    assert services.store.resolve_segment_ids(workspace_id, {}, "current-only") == ["segment-old"]
