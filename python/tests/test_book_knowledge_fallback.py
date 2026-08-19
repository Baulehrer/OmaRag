"""The keyphrase safety net for books whose printed index cannot be read.

``build_bookrag_lite`` extracts keyphrases only when a book yields too few
explicit terms.  Headings counted towards "explicit", and every book has
headings, so the net never deployed: a textbook with no readable subject index
got no compensating term index at all and had to be found by chunk text alone.
"""

from __future__ import annotations

from omarag_bridge.models.book import BookStructure, BookStructureNode, EvidenceRecord
from omarag_bridge.services.book_knowledge_service import build_bookrag_lite

SENTENCES = [
    "Zementmoertel erreicht seine Endfestigkeit nach achtundzwanzig Tagen.",
    "Der Blockverband setzt Laeuferschichten und Binderschichten abwechselnd.",
    "Die Ueberbindelaenge betraegt mindestens ein Viertel der Steinlaenge.",
    "Porenbetonsteine werden mit Duennbettmoertel vermauert.",
    "Bewehrungsstahl schuetzt Betonbauteile gegen Zugbeanspruchung.",
    "Die Schlankheit einer Wand begrenzt ihre Tragfaehigkeit.",
]


def _book(section_count: int) -> tuple[BookStructure, list[EvidenceRecord]]:
    nodes = [
        BookStructureNode(
            node_id=f"node-{index}",
            depth=0,
            ordinal=index,
            title=f"Kapitel {index} Mauerwerksbau",
            normalized_title=f"kapitel {index} mauerwerksbau",
            page_start=index + 1,
            page_end=index + 1,
            source_kind="body-heading",
            confidence=0.7,
        )
        for index in range(section_count)
    ]
    structure = BookStructure(
        logical_document_id="book-test",
        mode="body-headings",
        confidence=0.7,
        total_pages=section_count + 1,
        nodes=nodes,
    )
    evidence = [
        EvidenceRecord(
            evidence_id=f"ev-{index}",
            raw_content=" ".join(SENTENCES),
            content_hash=f"hash-{index}",
            anchors=[],
            page_start=index + 1,
            page_end=index + 1,
            section_node_id=nodes[index].node_id,
            headings=[nodes[index].title],
            labels=["paragraph"],
        )
        for index in range(section_count)
    ]
    return structure, evidence


def test_headings_alone_do_not_count_as_a_term_index() -> None:
    """A book with many headings and no index must still get keyphrases."""

    structure, evidence = _book(section_count=12)

    graph = build_bookrag_lite(structure, evidence)

    kinds = {term.kind for term in graph.terms}
    assert "heading" in kinds
    assert "keyphrase" in kinds, "the safety net must deploy when nothing else does"


def test_a_readable_index_suppresses_the_keyphrase_net() -> None:
    """Curated terms are better than extracted ones; do not dilute them."""

    from omarag_bridge.models.book import IndexEntry, PageLocator

    structure, evidence = _book(section_count=12)
    index_entries = [
        IndexEntry(
            term=f"Fachbegriff {index}",
            locators=[
                PageLocator(
                    raw=str(index + 1),
                    start_label=str(index + 1),
                    resolved_pages=[index + 1],
                )
            ],
            source_page=structure.total_pages,
        )
        for index in range(10)
    ]

    graph = build_bookrag_lite(structure, evidence, index_entries=index_entries)

    kinds = {term.kind for term in graph.terms}
    assert "index" in kinds
    assert "keyphrase" not in kinds
