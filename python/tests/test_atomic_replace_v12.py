from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from omarag_bridge.models.api import IngestRequest
from omarag_bridge.models.domain import JobStatus
from omarag_bridge.services.source_fetcher import DownloadedSource


def _generation(
    source: Path,
    *,
    generation_id: str,
    segment_id: str,
    superseded: list[str] | None = None,
) -> dict[str, Any]:
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    return {
        "source": str(source),
        "original_source": str(source),
        "managed_source": str(source),
        "document_id": "book-atomic",
        "logical_document_id": "book-atomic",
        "generation_id": generation_id,
        "fingerprint": fingerprint,
        "segment_document_ids": [segment_id],
        "superseded_segment_document_ids": list(superseded or []),
        "segments": [
            {
                "document_id": segment_id,
                "segment_index": 0,
                "page_start": 1,
                "page_end": 8,
                "fingerprint": fingerprint,
                "generation_id": generation_id,
                "status": "committed",
                "metadata": {},
            }
        ],
        "chunk_manifest": [],
        "pipeline_version": "book-index-v3",
    }


async def _admit_without_spawning(
    app: FastAPI,
    workspace_id: str,
    source: Path | str,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    *,
    source_type: str = "file",
    duplicate_policy: str = "replace",
):
    jobs = app.state.services.jobs
    monkeypatch.setattr(jobs, "_spawn", lambda _job_id: None)
    job, _ = await jobs.start_ingest(
        workspace_id,
        IngestRequest(
            sources=[{"type": source_type, "path": str(source)}],
            duplicate_policy=duplicate_policy,
        ),
        key,
    )
    return job


@pytest.mark.asyncio
async def test_replace_publishes_complete_generation_before_retiring_old_rows(
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = app.state.services
    workspace_id = str(workspace["id"])
    source = tmp_path / "atomic.pdf"
    source.write_bytes(b"same immutable textbook")
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    old = _generation(source, generation_id="gen-old", segment_id="segment-old")
    services.store.upsert_document(workspace_id, str(source), fingerprint, old)

    job = await _admit_without_spawning(app, workspace_id, source, monkeypatch, "atomic-replace")
    staged = asyncio.Event()
    reader_acquired = asyncio.Event()
    release_reader = asyncio.Event()
    operations: list[str] = []

    async def ingest(_database: Path, _source: str, **options: Any) -> dict[str, Any]:
        assert options["indexing_options"]["_defer_previous_generation_retirement"] is True
        result = _generation(
            source,
            generation_id=str(options["generation_id"]),
            segment_id="segment-new",
            superseded=["segment-old"],
        )
        await options["on_segment"](result["segments"][0])
        staged.set()
        return result

    async def delete_document(_database: Path, document_id: str) -> bool:
        operations.append(f"delete:{document_id}")
        return True

    original_upsert = services.store.upsert_document

    def observed_upsert(*args: Any, **kwargs: Any) -> None:
        operations.append("publish")
        original_upsert(*args, **kwargs)

    monkeypatch.setattr(services.adapter, "ingest", ingest)
    monkeypatch.setattr(services.adapter, "delete_document", delete_document)
    monkeypatch.setattr(services.store, "upsert_document", observed_upsert)

    async def old_generation_reader() -> list[str] | None:
        async with services.resources.chat():
            visible = services.store.resolve_segment_ids(workspace_id, {}, "current-only")
            reader_acquired.set()
            await release_reader.wait()
            return visible

    reader = asyncio.create_task(old_generation_reader())
    await reader_acquired.wait()
    runner = asyncio.create_task(services.jobs._run_ingest(job.id))
    staged_waiter = asyncio.create_task(staged.wait())
    done, _pending = await asyncio.wait(
        {runner, staged_waiter}, return_when=asyncio.FIRST_COMPLETED
    )
    if runner in done and not staged.is_set():
        await runner
        pytest.fail(f"replacement stopped before staging: {services.store.get_job(job.id)}")
    await staged_waiter

    # The fully staged provider rows remain unpublished while a reader owns the
    # old generation. The writer cannot delete that reader's source underneath it.
    assert services.store.resolve_segment_ids(workspace_id, {}, "current-only") == ["segment-old"]
    assert operations == []
    release_reader.set()
    assert await reader == ["segment-old"]
    await runner

    assert services.store.get_job(job.id).status == JobStatus.COMPLETED
    assert operations == ["publish", "delete:segment-old"]
    assert services.store.resolve_segment_ids(workspace_id, {}, "current-only") == ["segment-new"]
    publication = services.store.checkpoint_data(job.id, "source-published-0")
    assert publication is not None
    assert (
        publication["generation_id"]
        == services.store.book_record(workspace_id, "book-atomic")["generation_id"]
    )


@pytest.mark.asyncio
async def test_publication_marker_failure_rolls_back_catalog_swap(
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
) -> None:
    services = app.state.services
    workspace_id = str(workspace["id"])
    source = tmp_path / "transaction.pdf"
    source.write_bytes(b"transactional textbook")
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    services.store.upsert_document(
        workspace_id,
        str(source),
        fingerprint,
        _generation(source, generation_id="gen-old", segment_id="segment-old"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        services.store.upsert_document(
            workspace_id,
            str(source),
            fingerprint,
            _generation(source, generation_id="gen-new", segment_id="segment-new"),
            publication_checkpoint=("missing-job", 0),
        )

    assert services.store.resolve_segment_ids(workspace_id, {}, "current-only") == ["segment-old"]
    assert services.store.book_record(workspace_id, "book-atomic")["generation_id"] == "gen-old"


@pytest.mark.asyncio
async def test_published_replace_resumes_only_retirement_after_delete_failure(
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = app.state.services
    workspace_id = str(workspace["id"])
    source = tmp_path / "recovery.pdf"
    source.write_bytes(b"recoverable textbook")
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    services.store.upsert_document(
        workspace_id,
        str(source),
        fingerprint,
        _generation(source, generation_id="gen-old", segment_id="segment-old"),
    )
    job = await _admit_without_spawning(app, workspace_id, source, monkeypatch, "atomic-recovery")
    ingest_calls = 0
    delete_calls = 0

    async def ingest(_database: Path, _source: str, **options: Any) -> dict[str, Any]:
        nonlocal ingest_calls
        ingest_calls += 1
        result = _generation(
            source,
            generation_id=str(options["generation_id"]),
            segment_id="segment-new",
            superseded=["segment-old"],
        )
        await options["on_segment"](result["segments"][0])
        return result

    async def delete_document(_database: Path, _document_id: str) -> bool:
        nonlocal delete_calls
        delete_calls += 1
        if delete_calls == 1:
            raise RuntimeError("simulated provider cleanup failure")
        return True

    monkeypatch.setattr(services.adapter, "ingest", ingest)
    monkeypatch.setattr(services.adapter, "delete_document", delete_document)

    await services.jobs._run_ingest(job.id)
    assert services.store.get_job(job.id).status == JobStatus.FAILED
    assert services.store.resolve_segment_ids(workspace_id, {}, "current-only") == ["segment-new"]
    assert services.store.checkpoint_data(job.id, "source-published-0") is not None
    assert services.store.checkpoint_data(job.id, "source-result-0") is None

    # Restart recovery finds the transactionally published generation and does
    # not stage/embed it a second time; it only repeats idempotent retirement.
    services.store.update_job(job.id, status=JobStatus.RUNNING, error=None)
    await services.jobs._run_ingest(job.id)
    assert services.store.get_job(job.id).status == JobStatus.COMPLETED
    assert ingest_calls == 1
    assert delete_calls == 2
    assert services.store.checkpoint_data(job.id, "source-result-0") is not None


@pytest.mark.asyncio
async def test_staging_failure_keeps_old_generation_published(
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = app.state.services
    workspace_id = str(workspace["id"])
    source = tmp_path / "failed-stage.pdf"
    source.write_bytes(b"failed replacement")
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    services.store.upsert_document(
        workspace_id,
        str(source),
        fingerprint,
        _generation(source, generation_id="gen-old", segment_id="segment-old"),
    )
    job = await _admit_without_spawning(
        app, workspace_id, source, monkeypatch, "atomic-stage-failure"
    )

    async def ingest(_database: Path, _source: str, **options: Any) -> dict[str, Any]:
        staged_segment = _generation(
            source,
            generation_id=str(options["generation_id"]),
            segment_id="segment-partial",
        )["segments"][0]
        await options["on_segment"](staged_segment)
        raise RuntimeError("simulated staging failure")

    monkeypatch.setattr(services.adapter, "ingest", ingest)
    await services.jobs._run_ingest(job.id)

    assert services.store.get_job(job.id).status == JobStatus.FAILED
    assert services.store.resolve_segment_ids(workspace_id, {}, "current-only") == ["segment-old"]
    assert services.store.checkpoint_data(job.id, "source-published-0") is None
    assert services.store.list_segments(job.id, 0)[0]["document_id"] == "segment-partial"


@pytest.mark.asyncio
async def test_url_ingest_persists_only_opaque_source_and_removes_work_file(
    app: FastAPI,
    workspace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = app.state.services
    workspace_id = str(workspace["id"])
    raw_url = "https://private.example.invalid/manual.pdf?customer=secret"
    opaque = "omarag://imports/sha256/opaque-url"
    body = b"downloaded textbook"
    fingerprint = hashlib.sha256(body).hexdigest()
    authorized: list[str] = []
    observed_work_file: Path | None = None

    services.jobs.url_source_guard = lambda _workspace_id, url: authorized.append(url)

    async def download(
        url: str, directory: Path, *, authorize, **_options: Any
    ) -> DownloadedSource:
        nonlocal observed_work_file
        assert url == raw_url
        authorize(url)
        authorize("https://cdn.example.invalid/manual.pdf")
        observed_work_file = directory / ".url-import-test.pdf"
        observed_work_file.write_bytes(body)
        observed_work_file.chmod(0o600)
        return DownloadedSource(
            path=observed_work_file,
            fingerprint=fingerprint,
            size_bytes=len(body),
            final_reference=opaque,
        )

    async def ingest(_database: Path, managed_source: str, **options: Any) -> dict[str, Any]:
        assert Path(managed_source).is_file()
        assert options["original_source"] == opaque
        generation_id = str(options["generation_id"])
        return {
            "document_id": "book-url",
            "logical_document_id": "book-url",
            "generation_id": generation_id,
            "segments": [],
            "segment_document_ids": [],
        }

    monkeypatch.setattr("omarag_bridge.services.job_service.download_url_source", download)
    monkeypatch.setattr(services.adapter, "ingest", ingest)
    services.store.save_import_preflight(
        "preflight-url-secret",
        workspace_id,
        {"candidates": [{"source": raw_url}]},
    )
    job = await _admit_without_spawning(
        app,
        workspace_id,
        raw_url,
        monkeypatch,
        "opaque-url-import",
        source_type="url",
    )

    await services.jobs._run_ingest(job.id)

    stored_job = services.store.get_job(job.id)
    assert stored_job.status == JobStatus.COMPLETED
    assert stored_job.payload["sources"][0]["path"] == opaque
    assert stored_job.payload["sources"][0]["type"] == "file"
    assert raw_url not in str(stored_job.payload)
    recovery = services.store.checkpoint_data(job.id, "url-source-0")
    assert recovery["original_source"] == opaque
    assert Path(recovery["managed_source"]).is_file()
    assert raw_url not in str(
        services.store.get_import_preflight("preflight-url-secret", workspace_id)
    )
    indexed = services.store.document_by_fingerprint(workspace_id, fingerprint)
    assert indexed is not None
    assert indexed["source_path"] == opaque
    assert indexed["result"]["original_source"] == opaque
    assert raw_url not in str(indexed["result"])
    assert authorized == [
        raw_url,
        "https://cdn.example.invalid/manual.pdf",
    ]
    assert observed_work_file is not None
    assert not observed_work_file.exists()


@pytest.mark.asyncio
async def test_url_duplicate_skip_redacts_secret_and_keeps_recovery_copy(
    app: FastAPI,
    workspace: dict[str, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = app.state.services
    workspace_id = str(workspace["id"])
    raw_url = "https://private.example.invalid/manual.pdf?token=do-not-store"
    opaque = "omarag://imports/sha256/duplicate-opaque"
    body = b"already indexed textbook"
    fingerprint = hashlib.sha256(body).hexdigest()
    existing = tmp_path / "existing.pdf"
    existing.write_bytes(body)
    services.store.upsert_document(
        workspace_id,
        str(existing),
        fingerprint,
        _generation(existing, generation_id="gen-existing", segment_id="segment-existing"),
    )
    observed_work_file: Path | None = None
    services.jobs.url_source_guard = lambda _workspace_id, _url: None

    async def download(
        _url: str, directory: Path, *, authorize, **_options: Any
    ) -> DownloadedSource:
        nonlocal observed_work_file
        authorize(raw_url)
        observed_work_file = directory / ".url-import-duplicate.pdf"
        observed_work_file.write_bytes(body)
        observed_work_file.chmod(0o600)
        return DownloadedSource(
            path=observed_work_file,
            fingerprint=fingerprint,
            size_bytes=len(body),
            final_reference=opaque,
        )

    async def unexpected_ingest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("duplicate_policy=skip must not invoke the indexer")

    monkeypatch.setattr("omarag_bridge.services.job_service.download_url_source", download)
    monkeypatch.setattr(services.adapter, "ingest", unexpected_ingest)
    services.store.save_import_preflight(
        "preflight-url-duplicate",
        workspace_id,
        {"candidates": [{"source": raw_url}]},
    )
    job = await _admit_without_spawning(
        app,
        workspace_id,
        raw_url,
        monkeypatch,
        "opaque-url-duplicate-skip",
        source_type="url",
        duplicate_policy="skip",
    )

    await services.jobs._run_ingest(job.id)

    stored_job = services.store.get_job(job.id)
    assert stored_job.status == JobStatus.COMPLETED
    assert stored_job.payload["sources"][0]["path"] == opaque
    assert stored_job.payload["sources"][0]["type"] == "file"
    assert raw_url not in str(stored_job.payload)
    recovery = services.store.checkpoint_data(job.id, "url-source-0")
    assert recovery["original_source"] == opaque
    assert Path(recovery["managed_source"]).is_file()
    assert raw_url not in str(
        services.store.get_import_preflight("preflight-url-duplicate", workspace_id)
    )
    assert services.store.checkpoint_data(job.id, "source-result-0")["duplicate"] is True
    assert observed_work_file is not None
    assert not observed_work_file.exists()
