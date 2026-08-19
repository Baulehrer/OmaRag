"""Parsing of printed subject indexes (Sachwortverzeichnis).

A textbook's back-of-book index is expert-curated term-to-page knowledge, and
OmaRag already routes it into retrieval via ``build_bookrag_lite`` and
``route_book_knowledge``.  It only ever received a fraction of it: measured
against the real index pages of two German construction textbooks, the parser
recognised 7 % of lines, because it required a comma or a double space to
separate a term from its locators.  The ordinary German entry -- term, one
space, one page number -- has neither.
"""

from __future__ import annotations

import pytest

from omarag_bridge.services.book_structure_service import (
    _is_index_entry,
    _split_index_locators,
)

# Verbatim lines from "Lernfeld Bautechnik - Grundstufe", pages 386-389.
SINGLE_LOCATOR = [
    ("Redoxvorgang 262", "Redoxvorgang", "262"),
    ("Reduktionsmittel 262", "Reduktionsmittel", "262"),
    ("Regelfuge 112", "Regelfuge", "112"),
    ("Regelsieblinien 162", "Regelsieblinien", "162"),
    ("Renaissance 13", "Renaissance", "13"),
    ("Rezeptmörtel 98", "Rezeptmörtel", "98"),
    ("Richtmaße 101", "Richtmaße", "101"),
]

# Verbatim lines from "Tabellenbuch Bau", pages 224-230 (bilingual index).
BILINGUAL = [
    ("Beton concrete 38, 40, 50", "Beton concrete", "38, 40, 50"),
    ("Bodenkenngrößen soil parameters 27", "Bodenkenngrößen soil parameters", "27"),
]


@pytest.mark.parametrize(("line", "term", "locators"), SINGLE_LOCATOR)
def test_a_term_with_one_page_number_is_an_index_entry(line: str, term: str, locators: str) -> None:
    assert _split_index_locators(line) == (term, locators)
    assert _is_index_entry(line)


@pytest.mark.parametrize(("line", "term", "locators"), BILINGUAL)
def test_a_bilingual_entry_keeps_both_terms(line: str, term: str, locators: str) -> None:
    assert _split_index_locators(line) == (term, locators)


def test_entries_with_several_locators_still_parse() -> None:
    assert _split_index_locators("Reduktion 262, 271") == ("Reduktion", "262, 271")
    assert _split_index_locators("Rohdichte 247, 363") == ("Rohdichte", "247, 363")


def test_a_term_that_ends_in_a_number_keeps_that_number() -> None:
    """The comment on the original code names this case: "Beton C30, 42"."""

    assert _split_index_locators("Beton C30, 42") == ("Beton C30", "42")
    assert _split_index_locators("Beton C30 42") == ("Beton C30", "42")


@pytest.mark.parametrize(
    "line",
    [
        "SACHWORTVERZEICHNIS",
        "Regel",
        "Böden soils, subsoils",
        "–, Klassifizierung building material,",
        "",
        "   ",
    ],
)
def test_lines_without_a_locator_are_not_index_entries(line: str) -> None:
    assert _split_index_locators(line) is None
    assert not _is_index_entry(line)


def test_a_long_prose_sentence_is_not_mistaken_for_an_entry() -> None:
    """Body pages must not look like index pages just because a line ends in a
    number -- region growth is driven by the share of matching lines."""

    prose = (
        "Der Beton erreicht seine Nennfestigkeit in der Regel nach "
        "achtundzwanzig Tagen unter normgerechter Lagerung 25"
    )
    assert _split_index_locators(prose) is None


def _index_region(lines: list[tuple[str, float]], page_no: int = 200):
    from omarag_bridge.models.book import BookLine, BookPage, NavigationRegion

    page = BookPage(
        page_no=page_no,
        page_label=str(page_no),
        lines=[
            BookLine(page_no=page_no, text=text, x0=x0, source_ref=f"#/texts/{i}")
            for i, (text, x0) in enumerate(lines)
        ],
    )
    region = NavigationRegion(
        role="index", page_start=page_no, page_end=page_no, score=0.9, accepted=True
    )
    return [page], region


def test_a_wrapped_entry_is_joined_instead_of_yielding_its_tail_as_a_term() -> None:
    """Two-column indexes wrap long entries across lines.

    Verbatim from "Tabellenbuch Bau" page 224.  Without joining, the parser
    silently indexes "classification" and "material" as terms pointing at
    page 127 -- English fragments that no reader would ever search for, while
    the real term is lost.
    """

    from omarag_bridge.services.book_structure_service import parse_subject_index

    pages, region = _index_region(
        [
            ("Baustoff building material 120", 40.0),
            ("–, Klassifizierung building material,", 50.0),
            ("classification 127", 50.0),
        ]
    )

    entries = parse_subject_index(pages, region, {"200": 200})
    terms = [entry.subterm or entry.term for entry in entries]

    assert "classification" not in terms
    assert any("Klassifizierung" in term for term in terms)
    wrapped = next(entry for entry in entries if "Klassifizierung" in (entry.subterm or ""))
    assert [locator.start_label for locator in wrapped.locators] == ["127"]


def test_a_line_without_a_locator_is_dropped_when_the_next_one_starts_a_new_entry() -> None:
    """A dangling line must not swallow the entry that follows it."""

    from omarag_bridge.services.book_structure_service import parse_subject_index

    pages, region = _index_region(
        [
            ("Bodenkenngrößen soil parameters 27", 40.0),
            ("Böden soils, subsoils", 40.0),
            ("Beton concrete 38, 40", 40.0),
        ]
    )

    entries = parse_subject_index(pages, region, {"200": 200})
    terms = [entry.term for entry in entries]

    assert "Beton concrete" in terms
    assert not any(term.startswith("Böden soils, subsoils Beton") for term in terms)


def test_spaced_dot_leaders_are_a_table_of_contents_entry() -> None:
    """Typeset leaders are dot-space pairs, not runs of dots.

    Verbatim from the contents pages of "Tabellenbuch Bau".  The pattern
    required two consecutive dots, which this style never produces, so not one
    contents line in the book was recognised.
    """

    from omarag_bridge.services.book_structure_service import _is_toc_entry

    assert _is_toc_entry("Berechnung von Mauerwerk. . . . . . . . . . . . . . . 93")
    assert _is_toc_entry("Sortierkriterien für Kanthölzer. . . . . . . . . . . . . . 144")
    assert _is_toc_entry("1 Grundlagen ....... 12")
    assert _is_toc_entry("Anhang\t\tIV")


def test_a_wrapped_contents_line_without_a_page_number_is_not_an_entry() -> None:
    from omarag_bridge.services.book_structure_service import _is_toc_entry

    assert not _is_toc_entry("Charakteristische Druckfestigkeit fk von")
    assert not _is_toc_entry("Einsteinmauerwerk. . . . . . . . . . . . . . . . . . .")
