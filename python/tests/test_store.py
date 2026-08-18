from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from omarag_bridge.models.api import CreateWorkspaceRequest
from omarag_bridge.models.book import (
    BookRagGraph,
    BookStructure,
    BookStructureNode,
    EvidenceAnchor,
    EvidenceRecord,
    KnowledgeTerm,
    TermTarget,
)
from omarag_bridge.models.domain import JobStatus
from omarag_bridge.services.book_snapshot_service import build_book_knowledge_snapshot
from omarag_bridge.services.workspace_service import WorkspaceService
from omarag_bridge.models.errors import NotFoundError
from omarag_bridge.store import StateStore


def test_running_job_becomes_resumable_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = StateStore(database)
    workspaces = WorkspaceService(tmp_path / "workspaces", store)
    workspace = workspaces.create(CreateWorkspaceRequest(name="Recovery"))
    job, reused = store.create_job_idempotent(
        job_id="job-recovery",
        workspace_id=workspace.id,
        kind="ingest",
        payload={"sources": []},
        idempotency_key="recovery-key",
    )
    assert reused is False
    store.update_job(job.id, status=JobStatus.RUNNING, phase="embedding")
    store.close()

    reopened = StateStore(database)
    recovered = reopened.get_job(job.id)
    assert recovered.status == JobStatus.PAUSED
    assert recovered.phase == "interrupted"
    reopened.close()


def test_segment_ledger_and_document_fingerprint_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    store = StateStore(database)
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Long books")
    )
    job, _ = store.create_job_idempotent(
        job_id="job-segments",
        workspace_id=workspace.id,
        kind="ingest",
        payload={"sources": [{"path": "/books/omarag.pdf"}]},
        idempotency_key="segments-key",
    )
    segment = {
        "fingerprint": "abc123",
        "generation_id": "gen-1",
        "segment_index": 0,
        "page_start": 1,
        "page_end": 25,
        "document_id": "haiku-segment-1",
        "metadata": {"cache_hit": True},
    }
    store.record_segment(job.id, 0, segment)
    result = {
        "document_id": "book-1",
        "logical_document_id": "book-1",
        "generation_id": "gen-1",
        "segment_document_ids": ["haiku-segment-1"],
        "runtime_lock": {
            "embedding_provider": "ollama",
            "embedding_model": "embed:1",
            "embedding_digest": "sha256:abc",
        },
    }
    store.upsert_document(workspace.id, "/books/omarag.pdf", "abc123", result)
    store.close()

    reopened = StateStore(database)
    assert reopened.list_segments(job.id, 0)[0]["page_end"] == 25
    indexed = reopened.document_by_fingerprint(workspace.id, "abc123")
    assert indexed is not None
    assert indexed["generation_id"] == "gen-1"
    assert reopened.workspace_index_runtime_locks(workspace.id) == [result["runtime_lock"]]
    reopened.close()


def test_current_document_policy_selects_latest_active_edition(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Editions")
    )
    for edition in (7, 8):
        store.upsert_document(
            workspace.id,
            f"/books/edition-{edition}.pdf",
            f"fingerprint-{edition}",
            {
                "document_id": f"book-{edition}",
                "generation_id": f"gen-{edition}",
                "book_metadata": {
                    "work_id": "work-concrete",
                    "title": "Baustoffkunde",
                    "edition_number": edition,
                    "document_status": "active",
                    "confirmed": True,
                },
                "segments": [
                    {
                        "document_id": f"segment-{edition}",
                        "segment_index": 0,
                        "page_start": 1,
                        "page_end": 10,
                    }
                ],
            },
        )

    assert store.resolve_segment_ids(workspace.id, {}, "current-only") == ["segment-8"]
    assert store.resolve_segment_ids(workspace.id, {"edition_number": 7}, "current-only") == [
        "segment-7"
    ]
    with pytest.raises(ValueError, match="Unsupported document filters"):
        store.resolve_segment_ids(workspace.id, {"edtion_number": 7}, "current-only")
    with pytest.raises(ValueError, match="Unsupported document policy"):
        store.resolve_segment_ids(workspace.id, {}, "curent-only")
    store.close()


def test_answer_cache_is_bounded_and_document_generations_invalidate_it(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Cache")
    )
    for index in range(3):
        store.cache_answer(
            cache_key=f"key-{index}",
            workspace_id=workspace.id,
            index_fingerprint="empty",
            config_fingerprint="config",
            request={"question": f"Question {index}"},
            answer=f"Answer {index}",
            citations=[],
            max_entries=2,
        )
    assert store.answer_cache_size(workspace.id) == 2
    assert store.cached_answer("key-0") is None
    cached = store.cached_answer("key-2")
    assert cached is not None
    assert cached["claims"] == []
    assert cached["metadata"] == {}

    before = store.workspace_index_fingerprint(workspace.id)
    store.upsert_document(
        workspace.id,
        "/books/cache.pdf",
        "fingerprint-cache",
        {
            "document_id": "book-cache",
            "logical_document_id": "book-cache",
            "generation_id": "generation-cache",
            "segments": [],
        },
    )
    assert store.workspace_index_fingerprint(workspace.id) != before
    assert store.answer_cache_size(workspace.id) == 0
    store.close()


def test_answer_cache_is_bounded_by_encoded_payload_bytes(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Byte Cache")
    )
    for index in range(2):
        store.cache_answer(
            cache_key=f"large-{index}",
            workspace_id=workspace.id,
            index_fingerprint="index",
            config_fingerprint="config",
            request={"question": str(index)},
            answer="x" * 600_000,
            citations=[],
            max_entries=64,
            max_bytes=1024**2,
        )
    assert store.answer_cache_size(workspace.id) == 1
    assert store.cached_answer("large-1") is not None

    store.cache_answer(
        cache_key="too-large",
        workspace_id=workspace.id,
        index_fingerprint="index",
        config_fingerprint="config",
        request={"question": "oversized"},
        answer="y" * (2 * 1024**2),
        citations=[],
        max_entries=64,
        max_bytes=1024**2,
    )
    assert store.cached_answer("too-large") is None
    store.close()


def test_book_v2_store_round_trip_and_generation_gate(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Book v2")
    )
    store.begin_index_generation(
        workspace.id,
        "gen-v2",
        "book-index-v2",
        "config-v2",
    )
    assert store.workspace_index_generation(workspace.id)["status"] == "maintenance"

    structure = BookStructure(
        logical_document_id="book-v2",
        mode="body-headings",
        confidence=0.9,
        total_pages=10,
        nodes=[
            BookStructureNode(
                node_id="sec-1",
                depth=0,
                ordinal=0,
                title="Grundlagen",
                normalized_title="grundlagen",
                page_start=1,
                page_end=10,
                source_kind="body-heading",
                confidence=0.9,
            )
        ],
    )
    evidence = EvidenceRecord(
        evidence_id="ev-one",
        raw_content="Beleg",
        content_hash="raw-hash",
        anchors=[EvidenceAnchor(page_no=2, source_ref="#/texts/1")],
        page_start=2,
        page_end=2,
        section_node_id="sec-1",
    )
    snapshot = build_book_knowledge_snapshot(
        logical_document_id="book-v2",
        generation_id="gen-v2",
        fingerprint="pdf-sha",
        config_hash="config-v2",
        structure=structure,
        evidence=[evidence],
        graph=BookRagGraph(
            terms=[
                KnowledgeTerm(
                    term_id="term-beleg",
                    canonical="Belegbegriff",
                    normalized="belegbegriff",
                    kind="index",
                    confidence=0.95,
                )
            ],
            targets=[
                TermTarget(
                    term_id="term-beleg",
                    node_id="sec-1",
                    page_start=2,
                    page_end=2,
                    evidence_id="ev-one",
                    relation="located_in",
                    confidence=0.95,
                )
            ],
        ),
    )
    store.upsert_document(
        workspace.id,
        "/books/v2.pdf",
        "pdf-sha",
        {
            "logical_document_id": "book-v2",
            "generation_id": "gen-v2",
            "pipeline_version": "book-index-v2",
            "book_knowledge_snapshot": snapshot.model_dump(mode="json"),
            "segments": [
                {
                    "document_id": "haiku-v2-segment",
                    "segment_index": 0,
                    "page_start": 1,
                    "page_end": 10,
                    "core_start": 1,
                    "core_end": 10,
                    "conversion_start": 1,
                    "conversion_end": 10,
                    "role": "body",
                }
            ],
            "chunk_manifest": [
                {
                    "chunk_id": "haiku-chunk",
                    "segment_index": 0,
                    "chunk_order": 0,
                    "content_hash": "raw-hash",
                    "pages": [2],
                    "headings": ["Grundlagen"],
                    "labels": ["paragraph"],
                    "doc_item_refs": ["#/texts/1"],
                    "generation_id": "gen-v2",
                    "evidence_id": "ev-one",
                    "global_order": 0,
                    "anchor_page": 2,
                    "page_labels": ["2"],
                    "section_node_id": "sec-1",
                    "raw_tokens": 2,
                    "context_hash": "ctx",
                }
            ],
        },
    )
    assert store.book_structure(workspace.id, "book-v2")["nodes"][0]["title"] == "Grundlagen"
    restored = store.book_knowledge_snapshot(workspace.id, "book-v2")
    assert restored["schema_version"] == "2"
    assert restored["evidence"][0]["evidence_id"] == "ev-one"
    assert restored["evidence"][0]["raw_content"] == ""
    indexed_result = store.document_by_fingerprint(workspace.id, "pdf-sha")["result"]
    assert "book_knowledge_snapshot" not in indexed_result
    assert "chunk_manifest" not in indexed_result
    assert "segments" not in indexed_result

    routed = store.route_book_knowledge(workspace.id, "Grundlagen", limit=4)
    assert routed[0]["section_node_id"] == "sec-1"
    assert routed[0]["retrieval_path"] == "book-section"
    routed_evidence = store.route_book_knowledge(workspace.id, "Belegbegriff", limit=4)
    assert routed_evidence[0]["evidence_id"] == "ev-one"
    assert routed_evidence[0]["chunk_id"] == "haiku-chunk"

    report = store.validate_index_generation(workspace.id, "gen-v2")
    assert report["valid"] is True
    store.update_index_generation(workspace.id, "gen-v2", status="ready")
    assert store.workspace_index_generation(workspace.id)["status"] == "ready"

    store.clear_workspace_index(workspace.id, preserve_books=True)
    assert store.book_records(workspace.id)
    assert store.chunk_manifest(workspace.id) == []
    assert store.book_structure(workspace.id, "book-v2") is None
    store.close()


def test_book_record_returns_the_same_row_as_scanning_every_book(tmp_path: Path) -> None:
    """`book_record` is a direct lookup; it must still answer like the full scan.

    It is called per document while building visual evidence and per citation
    preview, so it used to load and JSON-parse the whole library for one row.
    """
    database = tmp_path / "records.sqlite3"
    store = StateStore(database)
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Records")
    )
    now = "2026-01-01T00:00:00Z"
    with store._lock:
        for index in range(5):
            logical_id = f"book-{index}"
            store._db.execute(
                """INSERT INTO book_records(
                       workspace_id, logical_document_id, original_source, managed_source,
                       fingerprint, generation_id, metadata_json, quality_json,
                       pipeline_version, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    workspace.id,
                    logical_id,
                    f"/books/{logical_id}.pdf",
                    None,
                    f"fingerprint-{index}",
                    f"gen-{index}",
                    json.dumps({"title": f"Book {index}"}),
                    json.dumps({"score": index}),
                    "textbook-v1",
                    now,
                ),
            )
            for segment in range(3):
                store._db.execute(
                    """INSERT INTO document_segments(
                           workspace_id, logical_document_id, generation_id,
                           segment_document_id, page_start, page_end
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        workspace.id,
                        logical_id,
                        f"gen-{index}",
                        f"{logical_id}-s{segment}",
                        segment * 10 + 1,
                        segment * 10 + 10,
                    ),
                )

    for index in range(5):
        logical_id = f"book-{index}"
        scanned = next(
            record
            for record in store.book_records(workspace.id)
            if record["logical_document_id"] == logical_id
        )
        assert store.book_record(workspace.id, logical_id) == scanned

    with pytest.raises(NotFoundError):
        store.book_record(workspace.id, "book-missing")
    store.close()


def test_legacy_store_migrates_v2_columns_and_session_claims(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite3"
    store = StateStore(database)
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Session claims")
    )
    first = store.create_run(
        "run-first",
        workspace.id,
        {
            "session_id": "session-one",
            "question": "Was ist Beton?",
            "evidence_mode": "strict",
        },
    )
    store.update_run(
        first.id,
        status=JobStatus.COMPLETED,
        answer="Ein Baustoff.",
        claims=[
            {
                "id": "claim-one",
                "text": "Beton ist ein Baustoff.",
                "evidence_ids": [],
                "status": "supported",
            }
        ],
    )
    current = store.create_run(
        "run-current",
        workspace.id,
        {
            "session_id": "session-one",
            "question": "Und woraus?",
            "evidence_mode": "strict",
        },
    )
    recent = store.recent_completed_session_runs(workspace.id, "session-one", current.id, limit=4)
    assert [run.id for run in recent] == [first.id]
    assert recent[0].claims[0].status == "supported"
    store._db.execute(
        "UPDATE runs SET created_at = datetime('now', '-25 hours') WHERE id = ?", (first.id,)
    )
    assert not store.recent_completed_session_runs(
        workspace.id, "session-one", current.id, max_age_hours=24
    )
    store.close()

    reopened = StateStore(database)
    columns = {
        row["name"] for row in reopened._db.execute("PRAGMA table_info(chunk_manifest)").fetchall()
    }
    assert {"evidence_id", "section_node_id", "context_hash"} <= columns
    assert "claims_json" in {
        row["name"] for row in reopened._db.execute("PRAGMA table_info(runs)").fetchall()
    }
    assert {"claims_json", "metadata_json"} <= {
        row["name"] for row in reopened._db.execute("PRAGMA table_info(answer_cache)").fetchall()
    }
    reopened.close()


def test_actual_legacy_tables_receive_additive_v2_migration(tmp_path: Path) -> None:
    database = tmp_path / "pre-v2.sqlite3"
    legacy = sqlite3.connect(database)
    legacy.executescript(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, session_id TEXT NOT NULL,
            status TEXT NOT NULL, question TEXT NOT NULL, evidence_mode TEXT NOT NULL,
            answer TEXT NOT NULL DEFAULT '', citations_json TEXT NOT NULL DEFAULT '[]',
            receipt_json TEXT, error_json TEXT, request_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_event_id INTEGER
        );
        CREATE TABLE document_segments (
            workspace_id TEXT NOT NULL, logical_document_id TEXT NOT NULL,
            generation_id TEXT NOT NULL, segment_document_id TEXT NOT NULL,
            page_start INTEGER NOT NULL, page_end INTEGER NOT NULL,
            PRIMARY KEY(workspace_id, segment_document_id)
        );
        CREATE TABLE chunk_manifest (
            workspace_id TEXT NOT NULL, logical_document_id TEXT NOT NULL,
            segment_document_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
            chunk_order INTEGER NOT NULL, content_hash TEXT NOT NULL,
            pages_json TEXT NOT NULL DEFAULT '[]', headings_json TEXT NOT NULL DEFAULT '[]',
            labels_json TEXT NOT NULL DEFAULT '[]', refs_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(workspace_id, chunk_id)
        );
        """
    )
    legacy.close()

    migrated = StateStore(database)
    assert "claims_json" in {
        row["name"] for row in migrated._db.execute("PRAGMA table_info(runs)").fetchall()
    }
    assert {
        "core_start",
        "core_end",
        "conversion_start",
        "conversion_end",
        "page_number_mode",
    } <= {
        row["name"]
        for row in migrated._db.execute("PRAGMA table_info(document_segments)").fetchall()
    }
    assert {"evidence_id", "section_node_id", "quality_flags_json"} <= {
        row["name"] for row in migrated._db.execute("PRAGMA table_info(chunk_manifest)").fetchall()
    }
    assert migrated._db.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'book_structures'"
    ).fetchone()
    migrated.close()
