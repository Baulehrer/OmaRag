from __future__ import annotations

from types import SimpleNamespace

import pytest

from omarag_bridge.adapters.haiku_v070 import (
    HaikuV070Adapter,
    _anchors_for_refs,
    _deduplicate_citations,
    document_filter_for_ids,
)
from omarag_bridge.models.domain import Citation


@pytest.fixture
def docling_document():
    docling = pytest.importorskip("docling_core.types.doc.document")
    DoclingDocument = docling.DoclingDocument
    PictureItem = docling.PictureItem
    TextItem = docling.TextItem
    ProvenanceItem = docling.ProvenanceItem
    BoundingBox = docling.BoundingBox
    CoordOrigin = docling.CoordOrigin
    Size = docling.Size
    DocItemLabel = docling.DocItemLabel
    document = DoclingDocument(name="provenance")
    item = TextItem(
        self_ref="#/texts/0",
        parent=None,
        children=[],
        label=DocItemLabel.PARAGRAPH,
        text="Grounded paragraph",
        orig="Grounded paragraph",
        prov=[
            ProvenanceItem(
                page_no=2,
                bbox=BoundingBox(
                    l=10,
                    t=180,
                    r=110,
                    b=80,
                    coord_origin=CoordOrigin.BOTTOMLEFT,
                ),
                charspan=(0, 19),
            )
        ],
    )
    document.texts.append(item)
    document.pictures.append(
        PictureItem(
            self_ref="#/pictures/0",
            label=DocItemLabel.CHART,
            prov=[
                ProvenanceItem(
                    page_no=2,
                    bbox=BoundingBox(
                        l=20,
                        t=170,
                        r=180,
                        b=30,
                        coord_origin=CoordOrigin.BOTTOMLEFT,
                    ),
                    charspan=(0, 0),
                )
            ],
        )
    )
    document.pages[2] = SimpleNamespace(size=Size(width=200, height=200))
    return document


def test_docling_provenance_is_normalized_to_top_left(docling_document) -> None:
    anchors = _anchors_for_refs(docling_document, ["#/texts/0"], page_offset=100)

    assert len(anchors) == 1
    anchor = anchors[0]
    assert anchor.page == 102
    assert anchor.doc_item_ref == "#/texts/0"
    assert anchor.element_type == "paragraph"
    assert anchor.x0 == pytest.approx(0.05)
    assert anchor.x1 == pytest.approx(0.55)
    assert anchor.y0 == pytest.approx(0.10)
    assert anchor.y1 == pytest.approx(0.60)


def test_missing_refs_degrade_without_error(docling_document) -> None:
    assert _anchors_for_refs(docling_document, ["#/texts/999"]) == []


def test_picture_provenance_has_page_region_and_type(docling_document) -> None:
    anchors = _anchors_for_refs(docling_document, ["#/pictures/0"], page_offset=100)

    assert len(anchors) == 1
    assert anchors[0].page == 102
    assert anchors[0].element_type == "chart"
    assert anchors[0].doc_item_ref == "#/pictures/0"


async def test_adapter_keeps_rich_citation_metadata(docling_document) -> None:
    class StoredDocument:
        def get_docling_document(self):
            return docling_document

    class Rag:
        async def get_chunk_by_id(self, _chunk_id):
            return SimpleNamespace(metadata={"doc_item_refs": ["#/texts/0"]})

        async def get_document_by_id(self, _document_id):
            return StoredDocument()

    cite = SimpleNamespace(
        chunk_id="chunk-1",
        chunk_ids=["chunk-1", "chunk-2"],
        document_id="segment-4",
        document_uri="file:///tmp/Fachbuch%20Betonbau.pdf",
        document_title="Fachbuch Betonbau",
        document_meta={"logical_document_id": "book-1", "page_offset": 100},
        page_numbers=[2],
        headings=["Betonbau", "Querkraft"],
        labels=["paragraph"],
        content="Grounded paragraph",
        doc_item_refs=["#/texts/0"],
        picture_refs=[],
    )

    result = await HaikuV070Adapter()._citation(Rag(), cite, 0)

    assert result.logical_document_id == "book-1"
    assert result.source_uri == "file:///tmp/Fachbuch%20Betonbau.pdf"
    assert result.pages == [102]
    assert result.headings == ["Betonbau", "Querkraft"]
    assert result.element_types == ["paragraph"]
    assert result.primary_anchors[0].page == 102
    assert result.context_anchors == []


def test_overlapping_segment_citations_are_deduplicated() -> None:
    citations = [
        Citation(
            chunk_id=f"chunk-{index}",
            logical_document_id="book-1",
            pages=[25],
            excerpt="same boundary paragraph",
        )
        for index in range(2)
    ]

    result = _deduplicate_citations(citations)

    assert len(result) == 1
    assert result[0].retrieval_rank == 1


def test_document_filter_targets_haiku_document_ids_and_escapes_quotes() -> None:
    assert document_filter_for_ids(None) is None
    assert document_filter_for_ids([]) == "id = '__omarag_no_document__'"
    assert document_filter_for_ids(["doc-1", "doc'2"]) == "id IN ('doc-1', 'doc''2')"
