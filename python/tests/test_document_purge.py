from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI

from omarag_bridge.models.domain import JobStatus
from omarag_bridge.models.errors import NotFoundError


def _publish_book(app: FastAPI, workspace: dict[str, object]) -> Path:
    workspace_id = str(workspace["id"])
    workspace_path = Path(str(workspace["path"]))
    fingerprint = "a" * 64
    original = workspace_path / "sources" / "originals" / f"{fingerprint}.pdf"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"%PDF-purge-me")
    app.state.services.store.upsert_document(
        workspace_id,
        "/private/user/source.pdf",
        fingerprint,
        {
            "logical_document_id": "book-purge",
            "document_id": "book-purge",
            "generation_id": "gen-purge",
            "fingerprint": fingerprint,
            "original_source": "/private/user/source.pdf",
            "managed_source": str(original),
            "pipeline_version": "book-index-v3",
            "segments": [
                {
                    "document_id": "segment-purge-1",
                    "page_start": 1,
                    "page_end": 4,
                    "core_start": 1,
                    "core_end": 4,
                }
            ],
        },
    )
    return original


def test_job_history_persists_compact_document_results(
    app: FastAPI,
    workspace: dict[str, object],
) -> None:
    workspace_id = str(workspace["id"])
    store = app.state.services.store
    job, _ = store.create_job_idempotent(
        job_id="job-compact-result",
        workspace_id=workspace_id,
        kind="ingest",
        payload={"sources": [{"type": "file", "path": "/books/compact.pdf"}]},
        idempotency_key="compact-result",
    )
    document = {
        "logical_document_id": "book-compact",
        "generation_id": "gen-compact",
        "fingerprint": "c" * 64,
        "quality": {"score": 0.9},
        "segment_document_ids": ["segment-compact"],
        "segments": [{"document_id": "segment-compact", "raw": "private-raw-content"}],
        "chunk_manifest": [{"content": "private-raw-content"}],
        "book_structure": {"raw": "private-raw-content"},
        "book_knowledge_snapshot": {"evidence": [{"raw_content": "private-raw-content"}]},
    }
    store.update_job(job.id, status=JobStatus.COMPLETED, result={"documents": [document]})
    store.checkpoint(job.id, "source-result-0", document)
    event = store.append_event(
        event_type="job.completed",
        correlation_id=job.id,
        workspace_id=workspace_id,
        job_id=job.id,
        payload={"documents": [document], "overall_progress": 1.0},
    )

    compact = store.get_job(job.id).result["documents"][0]
    assert compact["segment_document_ids"] == ["segment-compact"]
    assert compact["quality"] == {"score": 0.9}
    for field in ("segments", "chunk_manifest", "book_structure", "book_knowledge_snapshot"):
        assert field not in compact
        assert field not in store.checkpoint_data(job.id, "source-result-0")
    assert "private-raw-content" not in json.dumps(compact)
    assert "private-raw-content" not in json.dumps(store.checkpoint_data(job.id, "source-result-0"))
    assert event.payload == {
        "status": "completed",
        "document_count": 1,
        "document_ids": ["book-compact"],
    }


@pytest.mark.asyncio
async def test_document_purge_is_pinned_two_step_and_removes_backups(
    app: FastAPI,
    client: httpx2.AsyncClient,
    workspace: dict[str, object],
) -> None:
    workspace_id = str(workspace["id"])
    original = _publish_book(app, workspace)
    backup = await client.post(f"/v1/workspaces/{workspace_id}/backups")
    assert backup.status_code == 201

    preflight = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/book-purge/purge/preflight"
    )
    assert preflight.status_code == 200
    plan = preflight.json()
    assert plan["generation_id"] == "gen-purge"
    assert plan["requires_backup_confirmation"] is True
    assert plan["can_purge"] is True

    denied = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/book-purge/purge",
        json={"plan_id": plan["plan_id"], "confirm": "PURGE_DOCUMENT"},
    )
    assert denied.status_code == 409

    replacement = (
        await client.post(f"/v1/workspaces/{workspace_id}/documents/book-purge/purge/preflight")
    ).json()
    purged = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/book-purge/purge",
        json={
            "plan_id": replacement["plan_id"],
            "confirm": "PURGE_DOCUMENT",
            "backup_confirm": "PURGE_BACKUPS",
        },
    )
    assert purged.status_code == 200, purged.text
    assert purged.json()["removed_segments"] == 1
    assert purged.json()["removed_backups"] == 1
    assert purged.json()["original_removed"] is True
    assert not original.exists()
    assert (await client.get(f"/v1/workspaces/{workspace_id}/backups")).json() == []
    with pytest.raises(NotFoundError):
        app.state.services.store.book_record(workspace_id, "book-purge")


@pytest.mark.asyncio
async def test_pinned_run_blocks_document_purge(
    app: FastAPI,
    client: httpx2.AsyncClient,
    workspace: dict[str, object],
) -> None:
    workspace_id = str(workspace["id"])
    _publish_book(app, workspace)
    store = app.state.services.store
    store.create_run(
        "run-pinned-purge",
        workspace_id,
        {
            "session_id": "session-purge",
            "question": "private",
            "evidence_mode": "strict",
        },
    )
    store.update_run(
        "run-pinned-purge",
        pinned=True,
        citations=[
            {
                "chunk_id": "chunk-purge",
                "logical_document_id": "book-purge",
                "excerpt": "private evidence",
            }
        ],
    )

    response = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/book-purge/purge/preflight"
    )
    assert response.status_code == 200
    assert response.json()["can_purge"] is False
    assert response.json()["pinned_run_ids"] == ["run-pinned-purge"]


@pytest.mark.asyncio
async def test_document_purge_scrubs_only_target_from_shared_job_history(
    app: FastAPI,
    client: httpx2.AsyncClient,
    workspace: dict[str, object],
) -> None:
    workspace_id = str(workspace["id"])
    _publish_book(app, workspace)
    store = app.state.services.store
    other_source = "/books/public/other.pdf"
    other_result = {
        "logical_document_id": "book-other",
        "document_id": "book-other",
        "generation_id": "gen-other",
        "fingerprint": "b" * 64,
        "segment_document_ids": ["segment-other-1"],
        "page_count": 12,
    }
    store.upsert_document(
        workspace_id,
        other_source,
        "b" * 64,
        {
            **other_result,
            "pipeline_version": "book-index-v3",
            "segments": [
                {
                    "document_id": "segment-other-1",
                    "page_start": 1,
                    "page_end": 12,
                }
            ],
        },
    )
    target_result = {
        "logical_document_id": "book-purge",
        "document_id": "book-purge",
        "generation_id": "gen-purge",
        "fingerprint": "a" * 64,
        "original_source": "/private/user/source.pdf",
        "managed_source": str(
            Path(str(workspace["path"])) / "sources" / "originals" / f"{'a' * 64}.pdf"
        ),
        "segment_document_ids": ["segment-purge-1"],
        "raw_snapshot": {"private": "target-only-content"},
    }
    job, _ = store.create_job_idempotent(
        job_id="job-shared-purge",
        workspace_id=workspace_id,
        kind="ingest",
        payload={
            "sources": [
                {
                    "type": "file",
                    "path": "/private/user/source.pdf",
                    "metadata": {"title": "target-only-content"},
                },
                {"type": "file", "path": other_source},
            ]
        },
        idempotency_key="shared-purge",
    )
    store.update_job(
        job.id,
        status=JobStatus.COMPLETED,
        phase="completed",
        progress=1.0,
        checkpoint="book-purge",
        error={"source": "/private/user/source.pdf"},
        progress_detail={"current_document": "/private/user/source.pdf"},
        result={"documents": [target_result, other_result]},
    )
    store.save_import_preflight(
        "preflight-target-purge",
        workspace_id,
        {"candidates": [{"source": "/private/user/source.pdf"}]},
    )
    for name, data in (
        ("source-init-0", {"source": "/private/user/source.pdf", **target_result}),
        ("source-result-0", target_result),
        ("source-published-0", target_result),
        ("source-0", {"source": {"path": "/private/user/source.pdf"}}),
        ("source-result-1", other_result),
        ("source-1", {"source": {"path": other_source}}),
    ):
        store.checkpoint(job.id, name, data)
    store.record_segment(
        job.id,
        0,
        {
            "fingerprint": "a" * 64,
            "generation_id": "gen-purge",
            "segment_index": 0,
            "page_start": 1,
            "page_end": 4,
            "document_id": "segment-purge-1",
            "metadata": {"source": "/private/user/source.pdf"},
        },
    )
    store.record_segment(
        job.id,
        1,
        {
            "fingerprint": "b" * 64,
            "generation_id": "gen-other",
            "segment_index": 0,
            "page_start": 1,
            "page_end": 12,
            "document_id": "segment-other-1",
            "metadata": {"source": other_source},
        },
    )
    store.append_event(
        event_type="job.completed",
        correlation_id=job.id,
        workspace_id=workspace_id,
        job_id=job.id,
        payload={"documents": [target_result, other_result]},
    )

    for run_id, logical_id, secret in (
        ("run-target-purge", "book-purge", "target-only-content"),
        ("run-other-purge", "book-other", "safe-other-content"),
    ):
        store.create_run(
            run_id,
            workspace_id,
            {
                "session_id": f"session-{run_id}",
                "question": secret,
                "evidence_mode": "strict",
            },
        )
        store.update_run(
            run_id,
            status=JobStatus.COMPLETED,
            answer=secret,
            receipt={
                "session_id": f"session-{run_id}",
                "turn": 1,
                "cache_status": "miss",
                "total_ms": 1.0,
                "source_count": 1,
                "reused_source_count": 0,
                "new_source_count": 1,
                "source_check": "reviewed",
                "fallbacks": [logical_id],
            },
            error={"logical_document_id": logical_id},
            citations=[
                {
                    "chunk_id": f"chunk-{logical_id}",
                    "logical_document_id": logical_id,
                    "excerpt": secret,
                }
            ],
        )
        store.append_event(
            event_type="run.completed",
            correlation_id=run_id,
            workspace_id=workspace_id,
            run_id=run_id,
            payload={"answer": secret, "logical_document_id": logical_id},
        )

    plan = (
        await client.post(f"/v1/workspaces/{workspace_id}/documents/book-purge/purge/preflight")
    ).json()
    purged = await client.post(
        f"/v1/workspaces/{workspace_id}/documents/book-purge/purge",
        json={"plan_id": plan["plan_id"], "confirm": "PURGE_DOCUMENT"},
    )

    assert purged.status_code == 200, purged.text
    documents = (await client.get(f"/v1/workspaces/{workspace_id}/documents")).json()
    assert [item["id"] for item in documents] == ["book-other"]
    retained_job = (await client.get(f"/v1/jobs/{job.id}")).json()
    assert retained_job["status"] == "completed"
    assert retained_job["payload"]["sources"] == [{"type": "file", "path": other_source}]
    assert retained_job["result"]["documents"] == [other_result]
    serialized_job = json.dumps(retained_job, sort_keys=True)
    assert "book-purge" not in serialized_job
    assert "/private/user/source.pdf" not in serialized_job
    assert "target-only-content" not in serialized_job
    assert retained_job["checkpoint"] == "document-purged"

    with pytest.raises(NotFoundError):
        store.get_run("run-target-purge")
    assert (await client.get("/v1/runs/run-target-purge")).status_code == 404
    assert (await client.get("/v1/runs/run-other-purge")).status_code == 200
    assert store.checkpoint_data(job.id, "source-result-0") is None
    assert store.checkpoint_data(job.id, "source-published-0") is None
    assert store.checkpoint_data(job.id, "source-init-0") is None
    assert store.checkpoint_data(job.id, "source-0") is None
    assert store.checkpoint_data(job.id, "source-result-1") == other_result
    assert store.list_segments(job.id, 0) == []
    assert store.list_segments(job.id, 1)[0]["document_id"] == "segment-other-1"
    events = store.events_after(0, workspace_id=workspace_id)
    assert not any(event.run_id == "run-target-purge" for event in events)
    retained_job_event = next(event for event in events if event.job_id == job.id)
    assert retained_job_event.payload == {
        "status": "completed",
        "document_count": 2,
        "document_ids": ["book-other"],
    }
    serialized_events = json.dumps(
        [event.model_dump(mode="json") for event in events], sort_keys=True
    )
    assert "book-purge" not in serialized_events
    assert "target-only-content" not in serialized_events
    try:
        retained_preflight = store.get_import_preflight("preflight-target-purge", workspace_id)
    except NotFoundError:
        retained_preflight = {}
    assert "/private/user/source.pdf" not in json.dumps(retained_preflight)


@pytest.mark.asyncio
async def test_backup_restore_swaps_files_and_generation_catalogue_together(
    app: FastAPI,
    client: httpx2.AsyncClient,
    workspace: dict[str, object],
) -> None:
    workspace_id = str(workspace["id"])
    _publish_book(app, workspace)
    backup = (await client.post(f"/v1/workspaces/{workspace_id}/backups")).json()
    store = app.state.services.store
    current = store.book_record(workspace_id, "book-purge")
    store.upsert_document(
        workspace_id,
        "/private/user/source.pdf",
        "b" * 64,
        {
            "logical_document_id": "book-purge",
            "document_id": "book-purge",
            "generation_id": "gen-newer",
            "managed_source": current["managed_source"],
            "pipeline_version": "book-index-v3",
            "segments": [
                {
                    "document_id": "segment-newer",
                    "page_start": 1,
                    "page_end": 4,
                }
            ],
        },
    )
    assert store.book_record(workspace_id, "book-purge")["generation_id"] == "gen-newer"

    restored = await client.post(
        f"/v1/workspaces/{workspace_id}/backups/{backup['id']}/restore",
        json={"confirm": "RESTORE"},
    )

    assert restored.status_code == 200, restored.text
    record = store.book_record(workspace_id, "book-purge")
    assert record["generation_id"] == "gen-purge"
    assert record["segments"][0]["segment_document_id"] == "segment-purge-1"
    assert not (Path(str(workspace["path"])) / ".omarag" / "index-state.json").exists()
