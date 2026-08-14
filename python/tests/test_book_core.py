from __future__ import annotations

from omarag_bridge.models.book import (
    BookBookmark,
    BookLine,
    BookPage,
    EvidenceAnchor,
    EvidenceRecord,
    HeadingCandidate,
)
from omarag_bridge.services.book_knowledge_service import build_bookrag_lite
from omarag_bridge.services.book_snapshot_service import (
    build_book_knowledge_snapshot,
    stable_evidence_id,
)
from omarag_bridge.services.book_structure_service import (
    detect_navigation_regions,
    parse_glossary,
    parse_reference_list,
    parse_subject_index,
    parse_table_of_contents,
    reconcile_book_structure,
)


def _page(page_no: int, *lines: tuple[str, float], label: str | None = None) -> BookPage:
    return BookPage(
        page_no=page_no,
        page_label=label or str(page_no),
        lines=[
            BookLine(page_no=page_no, text=text, x0=x0, source_ref=f"#/texts/{index}")
            for index, (text, x0) in enumerate(lines)
        ],
    )


def test_navigation_regions_and_entries_use_printed_book_structure() -> None:
    pages = [
        _page(
            3,
            ("Inhaltsverzeichnis", 40),
            ("1 Grundlagen ........................ 1", 40),
            ("1.1 Zement und Wasser ............... 4", 55),
            ("1.2 Gesteinskörnung ................ 11", 55),
            ("2 Frischbeton ...................... 23", 40),
            ("2.1 Konsistenz ..................... 25", 55),
            ("2.2 Verdichtung .................... 31", 55),
            ("3 Festbeton ........................ 45", 40),
            ("4 Dauerhaftigkeit .................. 72", 40),
            ("5 Bemessung ........................ 91", 40),
            label="iii",
        ),
        _page(4, ("1 Grundlagen", 40), label="1"),
        _page(7, ("1.1 Zement und Wasser", 40), label="4"),
        _page(14, ("1.2 Gesteinskörnung", 40), label="11"),
        _page(
            198,
            ("Sachwortverzeichnis", 40),
            *[(f"Begriff {index}, {10 + index}", 40) for index in range(10)],
            label="195",
        ),
        _page(
            199,
            *[(f"Material {index}, {30 + index}", 40) for index in range(10)],
            label="196",
        ),
        _page(
            200,
            ("Glossar", 40),
            ("Adhäsion – Haftung zwischen unterschiedlichen Stoffen.", 40),
            ("Hydratation – Reaktion von Zement mit Wasser.", 40),
            ("Porosität – Verhältnis von Porenvolumen zu Gesamtvolumen.", 40),
            label="197",
        ),
    ]

    regions = detect_navigation_regions(pages, total_pages=200)
    by_role = {region.role: region for region in regions if region.accepted}
    assert set(by_role) == {"toc", "index", "glossary"}
    assert (by_role["index"].page_start, by_role["index"].page_end) == (198, 199)

    page_labels = {page.page_label: page.page_no for page in pages}
    toc = parse_table_of_contents(pages, by_role["toc"], page_labels)
    assert [(entry.title, entry.depth, entry.target_pages) for entry in toc[:3]] == [
        ("1 Grundlagen", 0, [4]),
        ("1.1 Zement und Wasser", 1, [7]),
        ("1.2 Gesteinskörnung", 1, [14]),
    ]

    index_entries = parse_subject_index(pages, by_role["index"], page_labels)
    assert len(index_entries) == 20
    assert index_entries[0].term == "Begriff 0"

    glossary = parse_glossary(pages, by_role["glossary"])
    assert glossary[1].term == "Hydratation"
    assert glossary[1].definition == "Reaktion von Zement mit Wasser."


def test_false_positive_index_chapter_is_not_a_navigation_region() -> None:
    pages = [
        _page(50, ("Index", 40), ("Ein Index beschleunigt Datenbankabfragen.", 40)),
        _page(51, ("B-Bäume und Selektivität", 40), ("Der Suchbaum bleibt balanciert.", 40)),
    ]
    assert not [
        region for region in detect_navigation_regions(pages, total_pages=300) if region.accepted
    ]


def test_technical_reference_lists_support_prefixed_page_labels() -> None:
    page = _page(
        5,
        ("Abbildungsverzeichnis", 40),
        *[(f"Abb. {index} Bauteil ........ A-{index}", 40) for index in range(1, 9)],
    )
    region = detect_navigation_regions([page], total_pages=100)[0]
    assert region.role == "figures"
    assert region.accepted is True
    parsed = parse_reference_list(
        [page], region, {f"A-{index}": 60 + index for index in range(1, 9)}
    )
    assert parsed[0].title == "Abb. 1 Bauteil"
    assert parsed[0].target_pages == [61]


def test_reconciliation_uses_full_book_bookmark_depth_and_has_window_fallback() -> None:
    bookmarks = [
        BookBookmark(title="Grundlagen", depth=0, page_no=4),
        BookBookmark(title="Zement und Wasser", depth=1, page_no=7),
        BookBookmark(title="Festbeton", depth=0, page_no=48),
    ]
    headings = [
        HeadingCandidate(title="1 Grundlagen", page_no=4, level=1, source_ref="#/texts/1"),
        HeadingCandidate(title="1.1 Zement und Wasser", page_no=7, level=1, source_ref="#/texts/2"),
        HeadingCandidate(title="3 Festbeton", page_no=48, level=1, source_ref="#/texts/3"),
    ]
    structure = reconcile_book_structure(
        "book-concrete", total_pages=100, bookmarks=bookmarks, headings=headings
    )
    assert [(node.title, node.depth, node.parent_id is not None) for node in structure.nodes] == [
        ("1 Grundlagen", 0, False),
        ("1.1 Zement und Wasser", 1, True),
        ("3 Festbeton", 0, False),
    ]
    assert structure.nodes[0].page_end == 47
    assert structure.nodes[1].page_end == 47

    fallback = reconcile_book_structure(
        "book-flat",
        total_pages=24,
        headings=[HeadingCandidate(title="Vorwort", page_no=1, level=1)],
    )
    assert fallback.mode == "window-fallback"
    assert [(node.page_start, node.page_end) for node in fallback.nodes] == [
        (1, 8),
        (9, 16),
        (17, 24),
    ]


def test_reconciliation_keeps_unmatched_outline_nodes_and_resolves_unlabelled_toc() -> None:
    toc_page = _page(
        2,
        ("Inhaltsverzeichnis", 40),
        ("1 Grundlagen ........ 1", 40),
        ("1.1 Stoffe ........... 5", 55),
    )
    toc_region = detect_navigation_regions(
        [
            toc_page,
            _page(
                3,
                ("2 Planung ............ 20", 40),
                ("2.1 Entwurf .......... 25", 55),
                ("3 Ausfuehrung ........ 40", 40),
                ("4 Betrieb ............ 60", 40),
                ("5 Rueckbau ........... 80", 40),
                ("6 Anhang ............. 95", 40),
            ),
        ],
        total_pages=100,
    )[0]
    toc = parse_table_of_contents([toc_page], toc_region, {})
    headings = [
        HeadingCandidate(title="1 Grundlagen", page_no=4, level=1),
        HeadingCandidate(title="1.1 Stoffe", page_no=8, level=1),
    ]
    structure = reconcile_book_structure(
        "book-unlabelled", total_pages=100, toc_entries=toc, headings=headings
    )
    assert [(node.page_start, node.depth) for node in structure.nodes] == [(4, 0), (8, 1)]

    unresolved = reconcile_book_structure("book-unresolved", total_pages=100, toc_entries=toc)
    assert unresolved.mode == "window-fallback"
    assert unresolved.nodes[0].page_start == 1

    bookmark_only = reconcile_book_structure(
        "book-bookmarks",
        total_pages=80,
        bookmarks=[
            BookBookmark(title="Teil A", depth=0, page_no=1),
            BookBookmark(title="Kapitel A.1", depth=1, page_no=12),
            BookBookmark(title="Teil B", depth=0, page_no=40),
        ],
    )
    assert bookmark_only.mode == "bookmarks"
    assert [(node.title, node.depth) for node in bookmark_only.nodes] == [
        ("Teil A", 0),
        ("Kapitel A.1", 1),
        ("Teil B", 0),
    ]


def test_evidence_ids_snapshot_and_bookrag_lite_are_deterministic() -> None:
    anchors = [
        EvidenceAnchor(
            page_no=7,
            source_ref="#/texts/12",
            bbox=(0.1, 0.2, 0.8, 0.3),
            label="paragraph",
        ),
        EvidenceAnchor(page_no=7, source_ref="#/texts/11", label="section_header"),
    ]
    first = stable_evidence_id("pdf-sha", "config-sha", anchors, "Wasserzementwert 0,50")
    second = stable_evidence_id("pdf-sha", "config-sha", anchors, "Wasserzementwert 0,50")
    changed = stable_evidence_id("pdf-sha", "config-sha", anchors, "Wasserzementwert 0,55")
    assert first == second
    assert first == stable_evidence_id(
        "pdf-sha", "config-sha", list(reversed(anchors)), "Wasserzementwert 0,50"
    )
    assert first != changed
    assert first.startswith("ev-")

    structure = reconcile_book_structure(
        "book-concrete",
        total_pages=16,
        headings=[
            HeadingCandidate(title="1 Grundlagen", page_no=1, level=1),
            HeadingCandidate(title="1.1 Hydratation", page_no=5, level=2),
            HeadingCandidate(title="2 Festigkeit", page_no=12, level=1),
        ],
    )
    evidence = [
        EvidenceRecord(
            evidence_id=first,
            raw_content="Hydratation beschreibt die Reaktion von Zement und Wasser.",
            content_hash="content-sha",
            anchors=anchors,
            page_start=7,
            page_end=7,
            section_node_id=structure.nodes[1].node_id,
            headings=["1 Grundlagen", "1.1 Hydratation"],
            labels=["paragraph"],
        )
    ]
    graph = build_bookrag_lite(
        structure,
        evidence,
        glossary_entries=parse_glossary(
            [_page(16, ("Glossar", 40), ("Hydratation – Reaktion mit Wasser.", 40))],
            detect_navigation_regions(
                [_page(16, ("Glossar", 40), ("Hydratation – Reaktion mit Wasser.", 40))],
                total_pages=16,
            )[0],
        ),
    )
    assert any(term.canonical == "Hydratation" for term in graph.terms)
    assert any(edge.relation == "parent_of" for edge in graph.edges)
    assert any(target.relation == "defined_in" for target in graph.targets)

    snapshot = build_book_knowledge_snapshot(
        logical_document_id="book-concrete",
        generation_id="gen-v2",
        fingerprint="pdf-sha",
        config_hash="config-sha",
        structure=structure,
        evidence=evidence,
        graph=graph,
    )
    assert snapshot.schema_version == "2"
    assert (
        snapshot.content_hash
        == build_book_knowledge_snapshot(
            logical_document_id="book-concrete",
            generation_id="gen-v2",
            fingerprint="pdf-sha",
            config_hash="config-sha",
            structure=structure,
            evidence=evidence,
            graph=graph,
        ).content_hash
    )
