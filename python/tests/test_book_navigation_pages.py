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


class _Bbox:
    def __init__(self, left: float, top: float) -> None:
        self.l = left  # noqa: E741 - docling's field name
        self.t = top
        self.r = left + 10
        self.b = top - 10


class _PicProv:
    def __init__(self, page_no: int, left: float = 0.0, top: float = 0.0) -> None:
        self.page_no = page_no
        self.bbox = _Bbox(left, top)


class _Child:
    def __init__(self, ref: str, text: str, page: int, left: float, top: float) -> None:
        self.self_ref = ref
        self.text = text
        self.label = "text"
        self.prov = [_PicProv(page, left, top)]
        self.parent = type("Ref", (), {"cref": "#/pictures/0"})()


class _Picture:
    def __init__(self, page: int) -> None:
        self.self_ref = "#/pictures/0"
        self.label = "picture"
        self.prov = [_PicProv(page)]


class _FigureDocument:
    def __init__(self, children: list[_Child], picture: _Picture) -> None:
        self.texts = children
        self.pictures = [picture]
        self.tables = []


def test_text_drawn_inside_a_figure_is_recovered() -> None:
    """Docling parents text that sits inside a drawing to the picture item, and
    ``iterate_items`` does not descend there -- so the chunker never sees it.

    Measured on "Tabellenbuch Bau": page 104 carries 140 text items, 138 of
    them under ``#/pictures/5``, and the chunker produced zero chunks for the
    page.  The words are already extracted and perfectly readable
    ("Ermittlung des Abminderungsfaktors"); no vision model is needed to get
    them back, only a decision to stop discarding them.
    """

    picture = _Picture(104)
    document = _FigureDocument(
        [
            _Child("#/texts/1", "Ermittlung des", 104, left=10, top=200),
            _Child("#/texts/2", "Abminderungsfaktors", 104, left=60, top=200),
            _Child("#/texts/3", "Nur oben und unten gehalten", 104, left=10, top=150),
        ],
        picture,
    )

    recovered = book_v2._picture_text_chunks(document, uncovered_pages={104})

    assert len(recovered) == 1
    chunk = recovered[0]
    assert "Ermittlung des Abminderungsfaktors" in chunk.content
    assert "Nur oben und unten gehalten" in chunk.content
    assert chunk.metadata["page_numbers"] == [104]
    assert "#/pictures/0" in chunk.metadata["doc_item_refs"]


def test_figure_text_on_a_page_that_already_has_chunks_is_left_alone() -> None:
    """Only pages the chunker dropped entirely are recovered, so ordinary
    figure captions are not indexed twice."""

    document = _FigureDocument(
        [_Child("#/texts/1", "Bild 3: Läuferverband", 105, left=10, top=200)],
        _Picture(105),
    )

    assert book_v2._picture_text_chunks(document, uncovered_pages=set()) == []


def test_a_figure_holding_only_stray_glyphs_is_not_indexed() -> None:
    """Axis labels like "ρ" and "n" carry no retrievable meaning on their own."""

    document = _FigureDocument(
        [
            _Child("#/texts/1", "ρ", 104, left=10, top=200),
            _Child("#/texts/2", "n", 104, left=20, top=200),
        ],
        _Picture(104),
    )

    assert book_v2._picture_text_chunks(document, uncovered_pages={104}) == []
