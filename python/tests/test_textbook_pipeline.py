from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from PIL import Image

from omarag_bridge.models.errors import ConflictError
from omarag_bridge.services import textbook_service


async def test_import_preflight_confirms_metadata_and_archives_original(
    client: httpx2.AsyncClient,
    workspace: dict[str, object],
    tmp_path: Path,
) -> None:
    source = tmp_path / "Beton-Handbuch.pdf"
    Image.new("RGB", (64, 64), "white").save(source, "PDF")
    workspace_id = str(workspace["id"])

    inspected = await client.post(
        f"/v1/workspaces/{workspace_id}/imports/preflight",
        json={"sources": [{"type": "file", "path": str(source)}]},
    )
    assert inspected.status_code == 200
    batch = inspected.json()
    candidate = batch["candidates"][0]
    assert candidate["fingerprint"]
    assert candidate["metadata"]["confirmed"] is False

    candidate["metadata"]["confirmed"] = True
    committed = await client.post(
        f"/v1/workspaces/{workspace_id}/imports/commit",
        headers={"Idempotency-Key": "confirmed-book"},
        json={
            "preflight_id": batch["id"],
            "sources": [
                {
                    "type": "file",
                    "path": candidate["source"],
                    "candidate_id": candidate["id"],
                    "fingerprint": candidate["fingerprint"],
                    "metadata": candidate["metadata"],
                }
            ],
        },
    )
    assert committed.status_code == 202
    for _ in range(100):
        job = (await client.get(f"/v1/jobs/{committed.json()['id']}")).json()
        if job["status"] in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)
    assert job["status"] == "completed"

    documents = (await client.get(f"/v1/workspaces/{workspace_id}/documents")).json()
    assert documents[0]["book"]["confirmed"] is True
    assert documents[0]["archive_mode"] in {"reflink", "copy", "existing"}
    assert Path(documents[0]["managed_source"]).is_file()
    assert Path(documents[0]["managed_source"]).parent.name == "originals"


def test_managed_original_fallback_is_independent_and_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    source = tmp_path / "book.pdf"
    original = b"stable textbook evidence"
    source.write_bytes(original)
    fingerprint = textbook_service.file_sha256(source)
    monkeypatch.setattr(textbook_service, "_reflink_and_hash", lambda _source, _target: None)

    archived, verified, mode = textbook_service.archive_source(workspace, source, fingerprint)
    assert verified == fingerprint
    assert mode == "copy"
    assert archived.read_bytes() == original
    assert archived.stat().st_mode & 0o222 == 0

    source.write_bytes(b"the user's file changed later")
    assert archived.read_bytes() == original
    with pytest.raises(ConflictError, match="size"):
        textbook_service.archive_source(workspace, source, fingerprint)
    source.write_bytes(original)
    reused, reused_fingerprint, reused_mode = textbook_service.archive_source(
        workspace, source, fingerprint
    )
    assert reused == archived
    assert reused_fingerprint == fingerprint
    assert reused_mode == "existing"


async def test_silver_retrieval_evaluation_compares_public_search_variants(
    client: httpx2.AsyncClient,
    app: FastAPI,
    workspace: dict[str, object],
) -> None:
    workspace_id = str(workspace["id"])
    app.state.services.store.upsert_document(
        workspace_id,
        "/tmp/book.pdf",
        "abc123",
        {
            "document_id": "book-1",
            "logical_document_id": "book-1",
            "generation_id": "gen-1",
            "segments": [
                {
                    "document_id": "segment-1",
                    "segment_index": 0,
                    "page_start": 1,
                    "page_end": 10,
                }
            ],
            "chunk_manifest": [
                {
                    "chunk_id": "chunk-1",
                    "segment_index": 0,
                    "chunk_order": 0,
                    "content_hash": "hash",
                    "pages": [4],
                    "headings": ["Expositionsklassen"],
                    "labels": ["text"],
                    "doc_item_refs": ["#/texts/1"],
                }
            ],
        },
    )
    generated = await client.post(
        f"/v1/workspaces/{workspace_id}/evaluations/generate", json={"limit": 5}
    )
    assert generated.status_code == 200
    report = generated.json()
    assert report["cases"][0]["expected_pages"] == [4]

    measured = await client.post(
        f"/v1/workspaces/{workspace_id}/evaluations/run",
        json={"evaluation_id": report["id"], "variants": ["hybrid"], "top_k": 5},
    )
    assert measured.status_code == 200
    assert measured.json()["variants"]["hybrid"]["recall_at_5"] == 1.0
