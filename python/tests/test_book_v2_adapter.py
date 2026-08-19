from __future__ import annotations

import asyncio
import copy
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omarag_bridge.adapters import book_v2
from omarag_bridge.adapters.book_v2 import (
    BOOK_V3_PIPELINE,
    HeadingManifest,
    PdfBookPreflight,
    RangeCache,
    _parse_navigation,
    _plan_ranges,
    build_evidence_record,
    ingest_pdf_book_v2,
)
from omarag_bridge.adapters.haiku_v070 import VanillaHaikuAdapter
from omarag_bridge.adapters.isolated import IsolatedHaikuAdapter
from omarag_bridge.models.book import (
    BookBookmark,
    BookLine,
    BookPage,
    BookStructure,
    BookStructureNode,
    NavigationRegion,
)
from omarag_bridge.models.domain import SearchHit
from omarag_bridge.models.errors import ConflictError
from omarag_bridge.services.structure_fallback_service import (
    StructureFallbackRequest,
    StructureRouteSelection,
)


class FakeChunk:
    def __init__(self, content: str, metadata: dict[str, Any], order: int = 0) -> None:
        self.id: str | None = None
        self.content = content
        self.metadata = metadata
        self.order = order
        self.embedding: list[float] | None = None


@pytest.mark.asyncio
async def test_visual_dense_fails_closed_until_media_gold_gate(tmp_path: Path) -> None:
    with pytest.raises(ConflictError, match="media quality gate") as captured:
        await ingest_pdf_book_v2(
            database=tmp_path / "database",
            source=tmp_path / "not-read.pdf",
            config=SimpleNamespace(),
            client_factory=lambda *_args, **_kwargs: None,
            haiku_version="0.74.0",
            indexing_options={"pipeline": "book-v3", "visual_dense": "on"},
        )
    assert captured.value.details["fallback"] == "caption-page-graph"


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


def test_range_cache_v3_is_compressed_and_keeps_stable_key_namespace(
    tmp_path: Path,
) -> None:
    cache = RangeCache(tmp_path)
    signature = {"docling": "2.119.0"}
    key = cache.key("sha", (1, 2), signature)

    cache.store(key, (1, 2), FakeDocument((1, 2)))

    assert cache.path(key).suffixes == [".json", ".gz"]
    with gzip.open(cache.path(key), mode="rt", encoding="utf-8") as source:
        payload = json.load(source)
    assert payload["schema"] == 3
    assert payload["page_range"] == [1, 2]


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


def test_evidence_record_exposes_typed_raw_provenance() -> None:
    document = FakeDocument((5, 5))
    chunk = FakeChunk(
        "| Bauteil | Wert |\n|---|---|\n| Wand | 24 cm |",
        {
            "doc_item_refs": ["#/texts/p-5"],
            "page_numbers": [5],
            "labels": ["table"],
            "headings": ["Bemessung"],
        },
    )
    structure = BookStructure(
        logical_document_id="book-1",
        mode="body-headings",
        confidence=0.9,
        total_pages=5,
        nodes=[
            BookStructureNode(
                node_id="section-1",
                depth=0,
                ordinal=0,
                title="Bemessung",
                normalized_title="bemessung",
                page_start=1,
                page_end=5,
                source_kind="body-heading",
                confidence=0.9,
            )
        ],
    )

    record = build_evidence_record(
        document=document,
        chunk=chunk,
        structure=structure,
        fingerprint="f" * 64,
        config_hash="c" * 64,
        previous_evidence_id=None,
    )

    assert record.evidence_kind == "table"
    assert record.provenance_kind == "element"
    assert chunk.metadata["evidence_kind"] == "table"
    assert chunk.metadata["provenance_kind"] == "element"


def test_model_route_depth_never_overwrites_deterministic_high_confidence_entry() -> None:
    pages = [
        BookPage(
            page_no=1,
            page_label="i",
            lines=[
                BookLine(page_no=1, text="Inhaltsverzeichnis", source_ref="#/h"),
                BookLine(page_no=1, text="Kapitel Alpha ........ 12", source_ref="#/e"),
            ],
        )
    ]
    deterministic = NavigationRegion(
        role="toc",
        page_start=1,
        page_end=1,
        score=0.9,
        accepted=True,
    )
    selection = StructureRouteSelection(
        candidate_id="route-1",
        page_no=1,
        source_ref="#/e",
        substring="Kapitel Alpha",
        locator="12",
        role="toc",
        level=2,
        parent_id=None,
        objective=1.0,
    )

    _, deterministic_entries, *_ = _parse_navigation(
        pages,
        {"12": 12},
        total_pages=20,
        detected_regions=[deterministic],
        route_selections=[selection],
    )
    assisted_region = deterministic.model_copy(
        update={
            "score": 1.0,
            "metrics": {
                "llm_objective_baseline": 0.7,
                "llm_objective_gain": 0.3,
            },
        }
    )
    _, assisted_entries, *_ = _parse_navigation(
        pages,
        {"12": 12},
        total_pages=20,
        detected_regions=[assisted_region],
        route_selections=[selection],
    )

    assert deterministic_entries[0].depth == 0
    assert deterministic_entries[0].confidence == 0.92
    assert assisted_entries[0].depth == 2
    assert assisted_entries[0].confidence == 0.81
    assert assisted_entries[0].title == deterministic_entries[0].title
    assert assisted_entries[0].locator == deterministic_entries[0].locator


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
            document = FakeDocument(page_range, heading)
            core_page = 1 if page_range[0] == 1 else 3
            document.paragraph.self_ref = f"#/texts/p-{core_page}"
            document.paragraph.prov = [SimpleNamespace(page_no=core_page)]
            document.core_page = core_page
            return SimpleNamespace(document=document)

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
            page = document.core_page
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
        indexing_options={"pipeline": "book-v3"},
    )

    assert source.read_bytes() == original
    assert len(converter_instances) == 1
    assert client_calls == 1
    assert converter_calls == [(source.resolve(), (1, 3)), (source.resolve(), (2, 4))]
    assert [item[1][0].content for item in rag.imported] == [
        "Rohe Evidenz Seite 1",
        "Rohe Evidenz Seite 3",
    ]
    assert [(item["conversion_start"], item["conversion_end"]) for item in result["segments"]] == [
        (1, 3),
        (2, 4),
    ]
    assert all(item[1][0].embedding for item in rag.imported)
    assert result["pipeline_version"] == BOOK_V3_PIPELINE
    assert result["source_uri"].startswith("omarag://documents/book-")
    assert str(source.resolve()) not in result["source_uri"]
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
async def test_pdf_v2_wires_bounded_local_structure_fallback_as_routing_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "uncertain-index.pdf"
    source.write_bytes(b"%PDF local structure fallback")

    class IndexDocument(FakeDocument):
        def __init__(self, page_range: tuple[int, int]) -> None:
            super().__init__(page_range, "Sachwortverzeichnis")
            self.paragraph.text = "Alpha, 12\nBeta, 18\nGamma, 24"
            self.paragraph.prov = [SimpleNamespace(page_no=1)]

    class Converter:
        def convert(self, _path: Path, *, page_range: tuple[int, int]):
            return SimpleNamespace(document=IndexDocument(page_range))

    monkeypatch.setattr(
        book_v2,
        "build_docling_converter",
        lambda _config, _profile: Converter(),
    )
    monkeypatch.setattr(
        book_v2,
        "pdf_preflight",
        lambda _path: PdfBookPreflight(
            total_pages=100,
            scanned_pages=[False] * 100,
            page_labels={str(page): page for page in range(1, 101)},
            bookmarks=[BookBookmark(title="Kapitel 1", depth=0, page_no=2)],
        ),
    )

    class Rag:
        embedder = object()

        async def list_documents(self):
            return []

        async def chunk(self, _document: IndexDocument):
            return [
                FakeChunk(
                    "Alpha, 12\nBeta, 18\nGamma, 24",
                    {
                        "doc_item_refs": ["#/texts/p-1"],
                        "page_numbers": [1],
                        "labels": ["paragraph"],
                    },
                )
            ]

        async def import_document(self, _document, chunks, **_kwargs):
            for index, chunk in enumerate(chunks):
                chunk.id = f"chunk-{index}"
            return SimpleNamespace(id="range-1")

        async def delete_document(self, _document_id):
            return True

    rag = Rag()

    class ClientContext:
        async def __aenter__(self):
            return rag

        async def __aexit__(self, *_args):
            return False

    class LocalRunner:
        def __init__(self) -> None:
            self.requests: list[StructureFallbackRequest] = []

        async def run(self, request: StructureFallbackRequest) -> dict[str, Any]:
            self.requests.append(request)
            candidates = request.payload["candidates"][:3]
            return {
                "selections": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "substring": candidate["allowed_substrings"][-1],
                        "locator": candidate["allowed_locators"][0],
                        "role": "index",
                        "level": 0,
                        "parent_id": None,
                    }
                    for candidate in candidates
                ]
            }

    async def embed(chunks, _embedder, _config):
        for chunk in chunks:
            chunk.embedding = [0.25]
        return chunks

    configuration = _config()
    configuration.oracle = SimpleNamespace(
        model_defaults=SimpleNamespace(structure="local-structure:latest")
    )
    runner = LocalRunner()
    result = await ingest_pdf_book_v2(
        database=tmp_path / "database" / "knowledge.lancedb",
        source=source,
        config=configuration,
        client_factory=lambda *_args, **_kwargs: ClientContext(),
        haiku_version="0.74.0",
        segment_sizer=lambda _preferred, _scanned: 100,
        embed_chunks_fn=embed,
        indexing_options={"pipeline": "book-v3", "llm_fallback": "auto"},
        llm_url="http://127.0.0.1:11434",
        structure_fallback_runner=runner,
    )

    assert len(runner.requests) == 1
    assert result["quality"]["llm_fallback_used"] is True
    assert result["pipeline_stats"]["llm_fallback_calls"] == 1
    assert result["pipeline_stats"]["llm_fallback_applied_regions"] == 1
    assert result["pipeline_stats"]["llm_fallback_model"] == "local-structure:latest"
    assert result["chunk_manifest"][0]["navigation_role"] == "index"
    assert result["book_knowledge_snapshot"]["graph"]["terms"]
    assert result["book_knowledge_snapshot"]["evidence"][0]["raw_content"] == (
        "Alpha, 12\nBeta, 18\nGamma, 24"
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
            core_page = 1 if page_range[0] == 1 else 5
            document.paragraph.self_ref = f"#/texts/p-{core_page}"
            document.paragraph.prov = [SimpleNamespace(page_no=core_page)]
            document.core_page = core_page
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
            page = document.core_page
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
        indexing_options={"pipeline": "book-v3"},
    )

    assert converter_instances == 1
    assert converter_calls == [(1, 8), (1, 5), (4, 8)]
    assert [(item["core_start"], item["core_end"]) for item in result["segments"]] == [
        (1, 4),
        (5, 8),
    ]
    assert [(item["conversion_start"], item["conversion_end"]) for item in result["segments"]] == [
        (1, 5),
        (4, 8),
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
async def test_book_v3_halo_imports_cross_boundary_evidence_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "boundary.pdf"
    source.write_bytes(b"%PDF boundary")

    class BoundaryDocument:
        def __init__(self, page_range: tuple[int, int]) -> None:
            self.page_range = page_range
            self.items = [
                SimpleNamespace(
                    self_ref=f"#/texts/p-{page}",
                    label="paragraph",
                    text=f"P{page}",
                    prov=[SimpleNamespace(page_no=page)],
                )
                for page in range(page_range[0], page_range[1] + 1)
            ]

        def iterate_items(self):
            for item in self.items:
                yield item, 0

        def export_to_dict(self) -> dict[str, Any]:
            return {"invalid_fake_document": True}

    class Converter:
        def convert(self, _path: Path, *, page_range: tuple[int, int]):
            return SimpleNamespace(document=BoundaryDocument(page_range))

    monkeypatch.setattr(book_v2, "build_docling_converter", lambda *_args: Converter())
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

    class Rag:
        embedder = object()

        async def list_documents(self):
            return []

        async def chunk(self, document: BoundaryDocument):
            if document.page_range == (1, 3):
                return [
                    FakeChunk("A", {"doc_item_refs": ["#/texts/p-1"], "page_numbers": [1]}),
                    FakeChunk(
                        "Cross",
                        {
                            "doc_item_refs": ["#/texts/p-2", "#/texts/p-3"],
                            "page_numbers": [2, 3],
                        },
                    ),
                    FakeChunk("Halo", {"doc_item_refs": ["#/texts/p-3"], "page_numbers": [3]}),
                ]
            return [
                FakeChunk(
                    "Cross",
                    {
                        "doc_item_refs": ["#/texts/p-2", "#/texts/p-3"],
                        "page_numbers": [2, 3],
                    },
                ),
                FakeChunk("B", {"doc_item_refs": ["#/texts/p-4"], "page_numbers": [4]}),
            ]

        async def import_document(self, _document, chunks, **_kwargs):
            for index, chunk in enumerate(chunks):
                chunk.id = f"chunk-{id(chunks)}-{index}"
            return SimpleNamespace(id=f"range-{id(chunks)}")

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
        document_fingerprint=hashlib.sha256(source.read_bytes()).hexdigest(),
        segment_sizer=lambda _preferred, _scanned: 2,
        embed_chunks_fn=embed,
        indexing_options={"pipeline": "book-v3"},
    )

    assert [item["raw_content"] for item in result["book_knowledge_snapshot"]["evidence"]] == [
        "A",
        "Cross",
        "B",
    ]
    assert result["quality"]["exact_duplicate_count"] == 0
    assert result["pipeline_stats"]["core_owned_evidence"] is True


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

    published_document_id = rebuilt["segments"][0]["document_id"]
    deleted_before_deferred = list(rag.deleted)
    deferred = await ingest_pdf_book_v2(
        **(common | {"generation_id": "generation-next"}),
        indexing_options={"_defer_previous_generation_retirement": True},
    )

    assert deferred["superseded_segment_document_ids"] == [published_document_id]
    assert published_document_id in rag.documents
    assert rag.deleted == deleted_before_deferred


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
            "source_uri": "omarag://documents/book-1/generations/gen-1",
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
                "source_uri": "omarag://documents/book-1/generations/gen-1",
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


def test_heading_detection_does_not_fall_back_on_font_style() -> None:
    """Style-based heading detection shreds textbook prose into fragments.

    Docling's HierarchicalChunker emits one chunk per document item and merges
    neighbours only while their heading path is identical, so every heading it
    invents ends a merge run.  In a two-column textbook, where bold and larger
    type mark emphasis rather than structure, style detection invents a great
    many of them.

    Measured over pages 1-8 of a construction textbook, chunked with the
    workspace configuration:

        use_style=True    118 chunks, median  11 words, 76 % under 20 words
        use_style=False    41 chunks, median  78 words,  7 % under 20 words

    Bookmarks and numbering stay on: both are explicit signals an author put
    there on purpose, and neither produced this failure.
    """

    from omarag_bridge.adapters.book_v2 import _conversion_signature

    class _Options:
        do_ocr = True
        force_ocr = False
        ocr_engine = "auto"
        ocr_lang = ["de"]
        do_table_structure = True
        table_mode = "accurate"
        table_cell_matching = True
        images_scale = 1.0
        generate_page_images = False

    class _Processing:
        conversion_options = _Options()
        pictures = "none"

    class _Config:
        processing = _Processing()

    heading = _conversion_signature(_Config(), "default")["heading_hierarchy"]

    assert heading["use_style"] is False
    assert heading["use_bookmarks"] is True
    assert heading["use_numbering"] is True
    assert heading["enabled"] is True
