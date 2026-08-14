from __future__ import annotations

import asyncio
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omarag_bridge.adapters import book_v2
from omarag_bridge.adapters.book_v2 import (
    BOOK_V2_PIPELINE,
    HeadingManifest,
    PdfBookPreflight,
    RangeCache,
    _plan_ranges,
    ingest_pdf_book_v2,
)
from omarag_bridge.adapters.haiku_v070 import VanillaHaikuAdapter
from omarag_bridge.adapters.isolated import IsolatedHaikuAdapter
from omarag_bridge.models.book import BookBookmark
from omarag_bridge.models.domain import SearchHit
from omarag_bridge.models.errors import ConflictError


class FakeChunk:
    def __init__(self, content: str, metadata: dict[str, Any], order: int = 0) -> None:
        self.id: str | None = None
        self.content = content
        self.metadata = metadata
        self.order = order
        self.embedding: list[float] | None = None


class FakeDocument:
    def __init__(self, page_range: tuple[int, int], heading: str = "Kapitel") -> None:
        start, _end = page_range
        self.page_range = page_range
        self.heading = SimpleNamespace(
            self_ref=f"#/texts/h-{start}",
            label="section_header",
            level=1,
            text=heading,
            prov=[SimpleNamespace(page_no=start)],
        )
        self.paragraph = SimpleNamespace(
            self_ref=f"#/texts/p-{start}",
            label="paragraph",
            text="Rohe Evidenz",
            prov=[SimpleNamespace(page_no=start)],
        )

    def iterate_items(self):
        yield self.heading, 0
        yield self.paragraph, 1

    def export_to_dict(self) -> dict[str, Any]:
        # Deliberately not a valid DoclingDocument; adapter contracts retain
        # this in-memory fake while production documents round-trip the cache.
        return {"invalid_fake_document": True}


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        processing=SimpleNamespace(
            pictures="none",
            conversion_options=SimpleNamespace(
                do_ocr=True,
                force_ocr=False,
                ocr_engine="auto",
                ocr_lang=["de", "en"],
                do_table_structure=True,
                table_mode="accurate",
                table_cell_matching=True,
                images_scale=1.0,
                generate_page_images=False,
            ),
        ),
        reranking=SimpleNamespace(model="configured"),
    )


def test_range_planner_uses_absolute_non_overlapping_ranges() -> None:
    ranges = _plan_ranges(
        [False, False, False, True, True, False],
        "default",
        lambda _preferred, scanned: 1 if scanned else 2,
    )

    assert ranges == [(1, 2), (3, 3), (4, 4), (5, 5), (6, 6)]
    assert [page for start, end in ranges for page in range(start, end + 1)] == list(range(1, 7))


def test_range_cache_key_includes_absolute_page_range(tmp_path: Path) -> None:
    cache = RangeCache(tmp_path)
    signature = {"docling": "2.119.0"}

    assert cache.key("sha", (1, 10), signature) != cache.key("sha", (11, 20), signature)


def test_heading_manifest_carries_global_path_and_keeps_raw_content() -> None:
    manifest = HeadingManifest("book-1")
    first = FakeDocument((1, 1), heading="Inhaltsverzeichnis")
    first_chunk = FakeChunk(
        "Grundlagen ........ 7\nBaustoffe ........ 12\nNormen ........ 18",
        {"doc_item_refs": ["#/texts/p-1"], "page_numbers": [1], "labels": ["text"]},
    )

    manifest.patch(first, [first_chunk], (1, 1))
    second = SimpleNamespace(iterate_items=lambda: iter(()))
    second_chunk = FakeChunk(
        "Fortsetzung der Navigation",
        {"doc_item_refs": [], "page_numbers": [2], "labels": ["text"]},
    )
    manifest.patch(second, [second_chunk], (2, 2))

    assert first_chunk.content.startswith("Grundlagen")
    assert first_chunk.metadata["headings"] == ["Inhaltsverzeichnis"]
    assert second_chunk.metadata["headings"] == ["Inhaltsverzeichnis"]
    assert first_chunk.metadata["navigation_role"] == "toc"
    assert second_chunk.metadata["navigation_role"] == "toc"
    assert first_chunk.metadata["evidence_role"] == "raw"


def test_heading_hook_cannot_rewrite_evidence() -> None:
    manifest = HeadingManifest("book-1")
    document = FakeDocument((5, 5))
    chunk = FakeChunk(
        "Original",
        {"doc_item_refs": ["#/texts/p-5"], "page_numbers": [5]},
    )

    def corrupt(_context, chunks):
        chunks[0].content = "Halluzination"

    with pytest.raises(ConflictError, match="raw evidence"):
        manifest.patch(document, [chunk], (5, 5), corrupt)


@pytest.mark.asyncio
async def test_pdf_v2_uses_one_converter_original_ranges_and_public_haiku_primitives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "book.pdf"
    original = b"%PDF immutable original"
    source.write_bytes(original)
    converter_calls: list[tuple[Path, tuple[int, int]]] = []
    converter_instances: list[object] = []

    class Converter:
        def convert(self, path: Path, *, page_range: tuple[int, int]):
            converter_calls.append((path, page_range))
            heading = "Inhaltsverzeichnis" if page_range[0] == 1 else "Kapitel 1"
            return SimpleNamespace(document=FakeDocument(page_range, heading))

    def converter_factory(_config, _profile):
        converter = Converter()
        converter_instances.append(converter)
        return converter

    monkeypatch.setattr(book_v2, "build_docling_converter", converter_factory)
    monkeypatch.setattr(
        book_v2,
        "pdf_preflight",
        lambda _path: PdfBookPreflight(
            total_pages=4,
            scanned_pages=[False] * 4,
            page_labels={"i": 1, "ii": 2, "1": 3, "2": 4},
            bookmarks=[
                BookBookmark(title="Inhaltsverzeichnis", depth=0, page_no=1),
                BookBookmark(title="Kapitel 1", depth=0, page_no=3),
            ],
        ),
    )

    class Rag:
        embedder = object()

        def __init__(self) -> None:
            self.imported: list[tuple[Any, list[FakeChunk], dict[str, Any]]] = []

        async def list_documents(self):
            return []

        async def chunk(self, document: FakeDocument):
            page = document.page_range[0]
            return [
                FakeChunk(
                    f"Rohe Evidenz Seite {page}",
                    {
                        "doc_item_refs": [f"#/texts/p-{page}"],
                        "page_numbers": [page],
                        "labels": ["paragraph"],
                    },
                )
            ]

        async def import_document(self, document, chunks, **kwargs):
            for index, chunk in enumerate(chunks):
                chunk.id = f"chunk-{len(self.imported)}-{index}"
            self.imported.append((document, chunks, kwargs))
            return SimpleNamespace(id=f"range-{len(self.imported)}")

        async def delete_document(self, _document_id):
            return True

    rag = Rag()

    class ClientContext:
        async def __aenter__(self):
            return rag

        async def __aexit__(self, *_args):
            return False

    client_calls = 0

    def client_factory(_database, *, config):
        nonlocal client_calls
        assert config is configuration
        client_calls += 1
        return ClientContext()

    async def embed_chunks(chunks, embedder, config):
        assert embedder is rag.embedder
        assert config is configuration
        for chunk in chunks:
            chunk.embedding = [0.1, 0.2]
        return chunks

    configuration = _config()
    result = await ingest_pdf_book_v2(
        database=tmp_path / "database" / "knowledge.lancedb",
        source=source,
        config=configuration,
        client_factory=client_factory,
        haiku_version="0.74.0",
        document_fingerprint=hashlib.sha256(original).hexdigest(),
        segment_sizer=lambda _preferred, _scanned: 2,
        embed_chunks_fn=embed_chunks,
    )

    assert source.read_bytes() == original
    assert len(converter_instances) == 1
    assert client_calls == 1
    assert converter_calls == [(source.resolve(), (1, 2)), (source.resolve(), (3, 4))]
    assert [item[1][0].content for item in rag.imported] == [
        "Rohe Evidenz Seite 1",
        "Rohe Evidenz Seite 3",
    ]
    assert all(item[1][0].embedding for item in rag.imported)
    assert result["pipeline_version"] == BOOK_V2_PIPELINE
    assert result["pipeline_stats"]["pdf_original_unchanged"] is True
    assert result["pipeline_stats"]["absolute_page_ranges"] is True
    assert result["pipeline_stats"]["two_pass_structure"] is True
    assert result["book_structure"]["nodes"]
    assert result["book_knowledge_snapshot"]["schema_version"] == "2"
    assert len(result["book_knowledge_snapshot"]["evidence"]) == 2
    assert result["quality"]["structure_mode"] == "bookmarks"
    assert [chunk["pages"] for chunk in result["chunk_manifest"]] == [[1], [3]]
    assert all(chunk["evidence_id"].startswith("ev-") for chunk in result["chunk_manifest"])
    assert (
        result["chunk_manifest"][0]["next_evidence_id"]
        == result["chunk_manifest"][1]["evidence_id"]
    )


@pytest.mark.asyncio
async def test_pdf_v2_splits_oom_ranges_and_covers_frontmatter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "large-book.pdf"
    source.write_bytes(b"%PDF immutable")
    converter_calls: list[tuple[int, int]] = []
    converter_instances = 0

    class Converter:
        def convert(self, path: Path, *, page_range: tuple[int, int]):
            assert path == source.resolve()
            converter_calls.append(page_range)
            if page_range == (1, 8):
                raise MemoryError("simulated Docling OOM")
            document = FakeDocument(
                page_range,
                "Kapitel 1" if page_range[0] == 1 else "Kapitel 2 Fortsetzung",
            )
            if page_range[0] == 1:
                document.heading.label = "paragraph"
            return SimpleNamespace(document=document)

    def converter_factory(_config, _profile):
        nonlocal converter_instances
        converter_instances += 1
        return Converter()

    monkeypatch.setattr(book_v2, "build_docling_converter", converter_factory)
    monkeypatch.setattr(
        book_v2,
        "pdf_preflight",
        lambda _path: PdfBookPreflight(
            total_pages=8,
            scanned_pages=[False] * 8,
            page_labels={str(page): page for page in range(1, 9)},
            bookmarks=[BookBookmark(title="Kapitel 1", depth=0, page_no=3)],
        ),
    )

    class Rag:
        embedder = object()

        def __init__(self) -> None:
            self.imported: list[Any] = []

        async def list_documents(self):
            return []

        async def chunk(self, document: FakeDocument):
            page = document.page_range[0]
            return [
                FakeChunk(
                    f"Evidenz {page}",
                    {
                        "doc_item_refs": [f"#/texts/p-{page}"],
                        "page_numbers": [page],
                        "labels": ["paragraph"],
                    },
                )
            ]

        async def import_document(self, _document, chunks, **_kwargs):
            for index, chunk in enumerate(chunks):
                chunk.id = f"chunk-{len(self.imported)}-{index}"
            self.imported.append(chunks)
            return SimpleNamespace(id=f"range-{len(self.imported)}")

        async def delete_document(self, _document_id):
            return True

    rag = Rag()

    class ClientContext:
        async def __aenter__(self):
            return rag

        async def __aexit__(self, *_args):
            return False

    async def embed(chunks, _embedder, _config):
        for chunk in chunks:
            chunk.embedding = [0.5]
        return chunks

    result = await ingest_pdf_book_v2(
        database=tmp_path / "db" / "knowledge.lancedb",
        source=source,
        config=_config(),
        client_factory=lambda *_args, **_kwargs: ClientContext(),
        haiku_version="0.74.0",
        generation_id="gen-oom",
        document_fingerprint=hashlib.sha256(source.read_bytes()).hexdigest(),
        segment_sizer=lambda _preferred, _scanned: 8,
        embed_chunks_fn=embed,
    )

    assert converter_instances == 1
    assert converter_calls == [(1, 8), (1, 4), (5, 8)]
    assert [(item["core_start"], item["core_end"]) for item in result["segments"]] == [
        (1, 4),
        (5, 8),
    ]
    frontmatter = result["book_structure"]["nodes"][0]
    assert (frontmatter["kind"], frontmatter["page_start"], frontmatter["page_end"]) == (
        "window",
        1,
        2,
    )
    assert (
        result["book_knowledge_snapshot"]["evidence"][0]["section_node_id"]
        == frontmatter["node_id"]
    )


@pytest.mark.asyncio
async def test_pdf_v2_resume_is_strict_and_never_deletes_a_foreign_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "resume-book.pdf"
    source.write_bytes(b"%PDF immutable")
    monkeypatch.setattr(
        book_v2,
        "pdf_preflight",
        lambda _path: PdfBookPreflight(
            total_pages=2,
            scanned_pages=[False, False],
            page_labels={"1": 1, "2": 2},
            bookmarks=[BookBookmark(title="Kapitel", depth=0, page_no=1)],
        ),
    )

    class Converter:
        def convert(self, _path: Path, *, page_range: tuple[int, int]):
            return SimpleNamespace(document=FakeDocument(page_range))

    monkeypatch.setattr(book_v2, "build_docling_converter", lambda *_args: Converter())

    class Rag:
        embedder = object()

        def __init__(self) -> None:
            self.documents: dict[str, Any] = {}
            self.chunks: dict[str, FakeChunk] = {}
            self.import_count = 0
            self.deleted: list[str] = []

        async def list_documents(self):
            return list(self.documents.values())

        async def chunk(self, document: FakeDocument):
            page = document.page_range[0]
            return [
                FakeChunk(
                    "Stabile Evidenz",
                    {
                        "doc_item_refs": [f"#/texts/p-{page}"],
                        "page_numbers": [page],
                        "labels": ["paragraph"],
                    },
                )
            ]

        async def import_document(self, _document, chunks, **kwargs):
            self.import_count += 1
            document_id = f"range-{self.import_count}"
            for index, chunk in enumerate(chunks):
                chunk.id = f"chunk-{self.import_count}-{index}"
                chunk.document_id = document_id
                self.chunks[chunk.id] = chunk
            self.documents[document_id] = SimpleNamespace(
                id=document_id,
                metadata=dict(kwargs["metadata"]),
            )
            return SimpleNamespace(id=document_id)

        async def get_chunk_by_id(self, chunk_id):
            return self.chunks.get(chunk_id)

        async def delete_document(self, document_id):
            self.deleted.append(document_id)
            self.documents.pop(document_id, None)
            self.chunks = {
                chunk_id: chunk
                for chunk_id, chunk in self.chunks.items()
                if getattr(chunk, "document_id", None) != document_id
            }
            return True

    rag = Rag()

    class ClientContext:
        async def __aenter__(self):
            return rag

        async def __aexit__(self, *_args):
            return False

    embed_count = 0

    async def embed(chunks, _embedder, _config):
        nonlocal embed_count
        embed_count += 1
        for chunk in chunks:
            chunk.embedding = [0.25]
        return chunks

    common = {
        "database": tmp_path / "db" / "knowledge.lancedb",
        "source": source,
        "config": _config(),
        "client_factory": lambda *_args, **_kwargs: ClientContext(),
        "haiku_version": "0.74.0",
        "generation_id": "generation-shared",
        "document_fingerprint": hashlib.sha256(source.read_bytes()).hexdigest(),
        "segment_sizer": lambda _preferred, _scanned: 2,
        "embed_chunks_fn": embed,
    }
    first = await ingest_pdf_book_v2(**common)
    original_document_id = first["segments"][0]["document_id"]
    rag.documents["foreign-book"] = SimpleNamespace(
        id="foreign-book",
        metadata={
            "logical_document_id": "book-foreign",
            "generation_id": "generation-shared",
        },
    )

    resumed = await ingest_pdf_book_v2(
        **common,
        resume_segments=first["segments"],
    )

    assert rag.import_count == 1
    assert embed_count == 1
    assert resumed["pipeline_stats"]["resumed_ranges"] == 1
    assert resumed["segments"][0]["document_id"] == original_document_id
    assert "foreign-book" not in rag.deleted

    stale_segments = copy.deepcopy(resumed["segments"])
    stale_segments[0]["metadata"]["chunk_manifest"][0]["metadata_hash"] = "stale"
    rebuilt = await ingest_pdf_book_v2(
        **common,
        resume_segments=stale_segments,
    )

    assert original_document_id in rag.deleted
    assert "foreign-book" not in rag.deleted
    assert rag.import_count == 2
    assert embed_count == 2
    assert rebuilt["pipeline_stats"]["resumed_ranges"] == 0


@pytest.mark.asyncio
async def test_pdf_v2_rechecks_cancellation_under_guard_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "cancel-book.pdf"
    source.write_bytes(b"%PDF immutable")
    monkeypatch.setattr(
        book_v2,
        "pdf_preflight",
        lambda _path: PdfBookPreflight(
            total_pages=4,
            scanned_pages=[False] * 4,
            page_labels={str(page): page for page in range(1, 5)},
            bookmarks=[BookBookmark(title="Kapitel", depth=0, page_no=1)],
        ),
    )

    class Converter:
        def convert(self, _path: Path, *, page_range: tuple[int, int]):
            return SimpleNamespace(document=FakeDocument(page_range))

    monkeypatch.setattr(book_v2, "build_docling_converter", lambda *_args: Converter())

    class Rag:
        embedder = object()

        def __init__(self) -> None:
            self.import_count = 0

        async def list_documents(self):
            return []

        async def chunk(self, document: FakeDocument):
            return [
                FakeChunk(
                    "Raw evidence",
                    {
                        "doc_item_refs": ["#/texts/p-1"],
                        "page_numbers": [document.page_range[0]],
                        "labels": ["paragraph"],
                    },
                )
            ]

        async def import_document(self, *_args, **_kwargs):
            self.import_count += 1
            return SimpleNamespace(id="must-not-import")

        async def delete_document(self, _document_id):
            return True

    rag = Rag()

    class ClientContext:
        async def __aenter__(self):
            return rag

        async def __aexit__(self, *_args):
            return False

    guard_entries = 0

    class Guard:
        async def __aenter__(self):
            nonlocal guard_entries
            guard_entries += 1

        async def __aexit__(self, *_args):
            return False

    checkpoints: list[tuple[int, int, int]] = []

    async def before_segment(start: int, end: int, total: int) -> bool:
        checkpoints.append((start, end, total))
        # Pass 1, pass-2 range, and embedding may run. Cancellation at the
        # final gate must prevent the public import call.
        return len(checkpoints) < 4

    phases: list[tuple[str, int, int, int]] = []

    async def on_phase(phase: str, start: int, end: int, total: int) -> None:
        phases.append((phase, start, end, total))

    async def embed(chunks, _embedder, _config):
        for chunk in chunks:
            chunk.embedding = [0.25]
        return chunks

    with pytest.raises(asyncio.CancelledError):
        await ingest_pdf_book_v2(
            database=tmp_path / "db" / "knowledge.lancedb",
            source=source,
            config=_config(),
            client_factory=lambda *_args, **_kwargs: ClientContext(),
            haiku_version="0.74.0",
            document_fingerprint=hashlib.sha256(source.read_bytes()).hexdigest(),
            segment_sizer=lambda _preferred, _scanned: 4,
            segment_guard=lambda: Guard(),
            before_segment=before_segment,
            on_phase=on_phase,
            embed_chunks_fn=embed,
        )

    assert rag.import_count == 0
    assert guard_entries == 3
    assert checkpoints == [(0, 4, 4), (3, 4, 4), (3, 4, 4), (3, 4, 4)]
    visible_progress = [end for _phase, _start, end, _total in phases]
    assert visible_progress == sorted(visible_progress)


@pytest.mark.asyncio
async def test_public_search_can_skip_reranker_and_get_raw_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = _config()
    seen_config: list[Any] = []
    search_kwargs: dict[str, Any] = {}

    class Rag:
        async def search(
            self,
            query,
            limit=None,
            search_type=None,
            filter=None,
            include_images=True,
        ):
            search_kwargs.update(
                query=query,
                limit=limit,
                search_type=search_type,
                filter=filter,
                include_images=include_images,
            )
            return []

        async def get_chunk_by_id(self, chunk_id):
            return SimpleNamespace(
                id=chunk_id,
                content="Unveraenderter Beleg",
                document_id="doc-1",
                document_title="Fachbuch",
                document_uri="file:///book.pdf",
                document_meta={"generation_id": "gen-1"},
                metadata={"page_numbers": [42], "navigation_role": "body"},
            )

        async def get_document_by_id(self, document_id):
            assert document_id == "doc-1"
            return SimpleNamespace(
                id=document_id,
                title="Fachbuch",
                uri="file:///book.pdf",
                metadata={
                    "generation_id": "gen-1",
                    "logical_document_id": "book-1",
                },
            )

    class ClientContext:
        async def __aenter__(self):
            return Rag()

        async def __aexit__(self, *_args):
            return False

    def client(_database, *, config=None, **_kwargs):
        seen_config.append(config)
        return ClientContext()

    adapter = VanillaHaikuAdapter()
    monkeypatch.setattr(adapter, "ensure_database", lambda _database: _async_none())
    monkeypatch.setattr(adapter, "_config", lambda _database: configuration)
    monkeypatch.setattr(adapter, "_client", client)

    await adapter.search(tmp_path / "db", "XC4", 20, rerank=False)
    chunk = await adapter.get_chunk(tmp_path / "db", "chunk-42")

    assert seen_config[0].reranking.model is None
    assert configuration.reranking.model == "configured"
    assert search_kwargs["include_images"] is False
    assert chunk == SearchHit(
        chunk_id="chunk-42",
        content="Unveraenderter Beleg",
        pages=[42],
        document_id="doc-1",
        document_title="Fachbuch",
        metadata={
            "page_numbers": [42],
            "navigation_role": "body",
            "source_uri": "file:///book.pdf",
            "document_meta": {
                "generation_id": "gen-1",
                "logical_document_id": "book-1",
            },
            "logical_document_id": "book-1",
            "generation_id": "gen-1",
            "raw_evidence": True,
        },
        search_type="lookup",
    )


@pytest.mark.asyncio
async def test_public_search_hydrates_chunk_metadata_on_document_meta_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Rag:
        async def search(self, _query, **_kwargs):
            return [
                SimpleNamespace(
                    chunk_id="chunk-7",
                    content="Raw evidence",
                    score=0.91,
                    document_id="segment-1",
                    document_uri="file:///book.pdf",
                    document_title="Fachbuch",
                    document_meta={
                        "logical_document_id": "book-1",
                        "generation_id": "gen-1",
                        "page_number_mode": "absolute",
                    },
                    page_numbers=[7],
                    headings=["Search heading"],
                )
            ]

        async def get_chunk_by_id(self, chunk_id):
            assert chunk_id == "chunk-7"
            return SimpleNamespace(
                id=chunk_id,
                document_id="segment-1",
                metadata={
                    "page_numbers": [7],
                    "citation_headings": ["Canonical heading"],
                    "navigation_role": "body",
                    "evidence_id": "ev-7",
                },
            )

        async def get_document_by_id(self, _document_id):
            raise AssertionError("SearchResult.document_meta must stay on the fast path")

    class ClientContext:
        async def __aenter__(self):
            return Rag()

        async def __aexit__(self, *_args):
            return False

    adapter = VanillaHaikuAdapter()
    monkeypatch.setattr(adapter, "ensure_database", lambda _database: _async_none())
    monkeypatch.setattr(adapter, "_client", lambda *_args, **_kwargs: ClientContext())

    hits = await adapter.search(tmp_path / "db", "question", 10)

    assert hits == [
        SearchHit(
            chunk_id="chunk-7",
            content="Raw evidence",
            score=0.91,
            pages=[7],
            document_id="segment-1",
            document_title="Fachbuch",
            metadata={
                "page_numbers": [7],
                "citation_headings": ["Canonical heading"],
                "navigation_role": "body",
                "evidence_id": "ev-7",
                "source_uri": "file:///book.pdf",
                "headings": ["Canonical heading"],
                "labels": [],
                "doc_item_refs": [],
                "chunk_ids": [],
                "document_meta": {
                    "logical_document_id": "book-1",
                    "generation_id": "gen-1",
                    "page_number_mode": "absolute",
                },
                "logical_document_id": "book-1",
                "generation_id": "gen-1",
                "raw_evidence": True,
            },
            search_type="hybrid",
        )
    ]


@pytest.mark.asyncio
async def test_isolated_adapter_forwards_candidate_and_chunk_lookup(monkeypatch) -> None:
    adapter = object.__new__(IsolatedHaikuAdapter)
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def call(operation, *args, **kwargs):
        calls.append((operation, args, kwargs))
        return None if operation == "get_chunk" else []

    monkeypatch.setattr(adapter, "_call", call)
    database = Path("/tmp/book-v2.lancedb")

    assert await adapter.search(database, "query", 30, rerank=False) == []
    assert await adapter.get_chunk(database, "chunk-1") is None
    assert calls == [
        (
            "search",
            (database, "query", 30),
            {"document_filter": None, "search_type": "hybrid", "rerank": False},
        ),
        ("get_chunk", (database, "chunk-1"), {}),
    ]


async def _async_none() -> None:
    return None
