from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx2
import pypdfium2 as pdfium
from fastapi import FastAPI

from omarag_bridge.models.domain import Citation, CitationAnchor
from omarag_bridge.preview import render_citation_preview


async def test_retrieval_inspector_uses_public_ranked_search(
    client: httpx2.AsyncClient, workspace: dict[str, object]
) -> None:
    response = await client.post(
        f"/v1/workspaces/{workspace['id']}/search/explain",
        json={"query": "Betondeckung", "limit": 3},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ranked"][0]["chunk_id"] == "chunk-1"
    assert payload["candidates"] == []
    assert payload["timing"]["total_ms"] >= payload["timing"]["search_ms"]
    assert "public API" in payload["provider_notes"][0]


async def test_citation_preview_renders_grounded_pdf_region(tmp_path: Path) -> None:
    pdf_path = tmp_path / "evidence.pdf"
    document = pdfium.PdfDocument.new()
    page = document.new_page(600, 800)
    page.close()
    document.save(pdf_path)
    document.close()
    citation = Citation(
        chunk_id="chunk-1",
        source_uri=pdf_path.as_uri(),
        pages=[1],
        excerpt="visual evidence",
        primary_anchors=[
            CitationAnchor(
                page=1,
                doc_item_ref="#/texts/0",
                element_type="text",
                x0=0.2,
                y0=0.25,
                x1=0.8,
                y1=0.5,
            )
        ],
    )

    payload = await render_citation_preview(citation, tmp_path / "cache", 800)

    assert payload.startswith(b"\x89PNG")
    assert list((tmp_path / "cache").glob("*.png"))


async def test_failed_import_resumes_after_last_committed_segment(
    client: httpx2.AsyncClient,
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
) -> None:
    source = tmp_path / "long-book.pdf"
    source.write_bytes(b"stable content")
    adapter = app.state.services.adapter
    attempts = 0
    resumed_with: list[dict[str, Any]] = []

    async def ingest(_database: Path, path: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await kwargs["on_segment"](
                {
                    "fingerprint": kwargs["document_fingerprint"],
                    "generation_id": kwargs["generation_id"],
                    "segment_index": 0,
                    "page_start": 1,
                    "page_end": 25,
                    "document_id": "segment-1",
                    "metadata": {"cache_hit": False},
                }
            )
            raise RuntimeError("simulated daemon interruption")
        resumed_with.extend(kwargs["resume_segments"])
        return {
            "source": path,
            "document_id": "book-1",
            "logical_document_id": "book-1",
            "generation_id": kwargs["generation_id"],
            "segment_document_ids": ["segment-1", "segment-2"],
            "page_count": 40,
        }

    adapter.ingest = ingest
    accepted = await client.post(
        f"/v1/workspaces/{workspace['id']}/documents/ingest",
        json={"sources": [{"type": "file", "path": str(source)}]},
        headers={"Idempotency-Key": "resume-v06"},
    )
    job_id = accepted.json()["id"]
    for _ in range(100):
        failed = (await client.get(f"/v1/jobs/{job_id}")).json()
        if failed["status"] == "failed":
            break
        await asyncio.sleep(0.01)
    assert failed["status"] == "failed"

    response = await client.post(f"/v1/jobs/{job_id}/resume")
    assert response.status_code == 200
    for _ in range(100):
        completed = (await client.get(f"/v1/jobs/{job_id}")).json()
        if completed["status"] == "completed":
            break
        await asyncio.sleep(0.01)

    assert completed["status"] == "completed"
    assert resumed_with[0]["document_id"] == "segment-1"
    assert attempts == 2
