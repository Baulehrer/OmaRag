"""Recovering navigation pages that the table model swallowed.

Docling labels a printed subject index `document_index` -- it knows what the
page is.  But it delivers it as a TableItem, and TableFormer imposes a grid on
a three-column index, scrambling reading order into cells like
"121 plasticizer 43 Bettungstypen bedding".  The structure signals only read
text items, so those pages arrive with a single line (the heading) or none, and
no navigation region is ever detected.

Measured on "Tabellenbuch Bau": zero index entries from the document, versus
678 entries when the same pages are read as plain text.
"""

from __future__ import annotations

from pathlib import Path

from omarag_bridge.adapters import book_v2


class _Prov:
    def __init__(self, page_no: int) -> None:
        self.page_no = page_no
        self.bbox = None


class _Item:
    def __init__(self, label: str, pages: list[int], text: str = "") -> None:
        self.label = label
        self.prov = [_Prov(page) for page in pages]
        self.text = text
        self.self_ref = f"#/{label}/0"


class _Document:
    def __init__(self, items: list[_Item]) -> None:
        self._items = items
        self.tables = [item for item in items if item.label == "document_index"]

    def iterate_items(self):
        return ((item, 0) for item in self._items)


def test_pages_carrying_a_document_index_table_are_recognised() -> None:
    document = _Document(
        [
            _Item("text", [1], "Mauerwerk"),
            _Item("document_index", [223, 224, 225]),
        ]
    )

    assert book_v2._navigation_table_pages(document) == {223, 224, 225}


def test_a_book_without_an_index_table_needs_no_recovery() -> None:
    document = _Document([_Item("text", [1], "Mauerwerk")])

    assert book_v2._navigation_table_pages(document) == set()


def test_index_pages_are_refilled_from_the_source_text(monkeypatch) -> None:
    """The heading survives as a text item; the entries must be added back."""

    document = _Document(
        [
            _Item("section_header", [223], "SACHWORTVERZEICHNIS"),
            _Item("document_index", [223]),
        ]
    )
    monkeypatch.setattr(
        book_v2,
        "_pdf_text_lines",
        lambda source, page_numbers: {
            223: ["SACHWORTVERZEICHNIS", "Redoxvorgang 262", "Regelfuge 112"]
        },
    )

    pages, _headings = book_v2.collect_docling_book_signals(
        document,
        (223, 223),
        page_labels={},
        scanned_pages=[False] * 300,
        source=Path("book.pdf"),
    )

    texts = [line.text for page in pages for line in page.lines]
    assert "Redoxvorgang 262" in texts
    assert "Regelfuge 112" in texts
    assert texts.count("SACHWORTVERZEICHNIS") == 1, "the heading must not be duplicated"


def test_recovery_is_skipped_when_docling_already_delivered_the_text(monkeypatch) -> None:
    """A single-column index arrives as text items; do not read the PDF twice."""

    document = _Document(
        [
            _Item("text", [223], "Redoxvorgang 262\nRegelfuge 112\nRohdichte 247, 363"),
            _Item("document_index", [223]),
        ]
    )
    calls: list[object] = []

    def _spy(source, page_numbers):
        calls.append(page_numbers)
        return {}

    monkeypatch.setattr(book_v2, "_pdf_text_lines", _spy)

    book_v2.collect_docling_book_signals(
        document,
        (223, 223),
        page_labels={},
        scanned_pages=[False] * 300,
        source=Path("book.pdf"),
    )

    assert calls == [], "docling already provided the lines"


def test_running_headers_do_not_count_as_page_content(monkeypatch) -> None:
    """A table-of-contents page carries a footer and nothing else.

    Docling uses `document_index` for both the table of contents and the
    subject index.  Those pages still yield the running footer as a text item,
    so counting raw lines makes an empty page look supplied.
    """

    document = _Document(
        [
            _Item("page_footer", [8], "handwerk-technik.de"),
            _Item("page_footer", [8], "VII"),
            _Item("document_index", [8]),
        ]
    )
    monkeypatch.setattr(
        book_v2,
        "_pdf_text_lines",
        lambda source, page_numbers: {8: ["INHALTSVERZEICHNIS", "1 Grundlagen 12"]},
    )

    pages, _headings = book_v2.collect_docling_book_signals(
        document,
        (8, 8),
        page_labels={},
        scanned_pages=[False] * 300,
        source=Path("book.pdf"),
    )

    texts = [line.text for page in pages for line in page.lines]
    assert "1 Grundlagen 12" in texts
