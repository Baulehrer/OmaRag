from __future__ import annotations

import hashlib
import math
import re
import statistics
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..models.book import (
    BookBookmark,
    BookLine,
    BookPage,
    BookStructure,
    BookStructureNode,
    GlossaryEntry,
    HeadingCandidate,
    IndexEntry,
    NavigationRegion,
    PageLocator,
    TocEntry,
)

_ROLE_HEADINGS: dict[str, tuple[str, ...]] = {
    "toc": ("inhaltsverzeichnis", "inhalt", "contents", "table of contents"),
    "index": (
        "sachwortverzeichnis",
        "stichwortverzeichnis",
        "sachregister",
        "subject index",
        "index",
    ),
    "glossary": ("glossar", "glossary"),
    "abbreviations": (
        "abkurzungsverzeichnis",
        "abbreviations",
        "list of abbreviations",
    ),
    "symbols": ("symbolverzeichnis", "nomenklatur", "nomenclature", "list of symbols"),
    "figures": ("abbildungsverzeichnis", "list of figures"),
    "tables": ("tabellenverzeichnis", "list of tables"),
    "formulas": ("formelverzeichnis", "gleichungsverzeichnis", "list of equations"),
}
_NAVIGATION_HEADINGS = frozenset(item for values in _ROLE_HEADINGS.values() for item in values)
_ROMAN_RE = r"[ivxlcdm]+"
_LABEL_RE = rf"(?:[a-z]{{1,4}}[-–]?\d+|\d+[a-z]?|{_ROMAN_RE})"
_TOC_LINE_RE = re.compile(
    rf"^(?P<title>.+?)(?:\s*\.{{2,}}\s*|\t+|\s{{2,}})(?P<label>{_LABEL_RE})\s*$",
    re.IGNORECASE,
)
_LOCATOR_AT_END_RE = re.compile(
    rf"(?P<locators>{_LABEL_RE}(?:\s*[-–—]\s*{_LABEL_RE})?(?:\s*f{{1,2}}\.?)?"
    rf"(?:\s*,\s*{_LABEL_RE}(?:\s*[-–—]\s*{_LABEL_RE})?(?:\s*f{{1,2}}\.?)?)*)\s*$",
    re.IGNORECASE,
)
_LOCATOR_LIST_RE = re.compile(rf"^{_LOCATOR_AT_END_RE.pattern.rstrip('$')}$$", re.IGNORECASE)
_LOCATOR_TOKEN_RE = re.compile(
    rf"^(?P<start>{_LABEL_RE})(?:\s*[-–—]\s*(?P<end>{_LABEL_RE}))?"
    r"(?:\s*(?P<suffix>ff?|FF?)\.?)?$",
    re.IGNORECASE,
)
_SEE_RE = re.compile(
    r"^(?P<term>.+?)[,;]?\s+(?P<relation>siehe\s+auch|see\s+also|siehe|see)\s+"
    r"(?P<related>.+?)\s*$",
    re.IGNORECASE,
)
_GLOSSARY_RE = re.compile(r"^(?P<term>[^:–—\t]{1,100}?)\s*(?:[:–—]|\t)\s*(?P<definition>\S.+)$")
_NUMBERING_RE = re.compile(
    r"^\s*(?:(?:chapter|kapitel|teil|part|article|artikel)\s+)?"
    r"(?P<number>(?:\d+|[IVXLCDM]+)(?:\.\d+)*)(?:[.):]?\s+)",
    re.IGNORECASE,
)


def normalize_book_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().replace("ß", "ss")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value).split())


def _title_for_matching(value: str) -> str:
    value = _NUMBERING_RE.sub("", value.strip())
    return normalize_book_text(value)


def _heading_role(text: str) -> str | None:
    normalized = normalize_book_text(text)
    for role, names in _ROLE_HEADINGS.items():
        if normalized in names:
            return role
    return None


def _is_toc_entry(text: str) -> bool:
    return _TOC_LINE_RE.match(text.strip()) is not None


def _is_index_entry(text: str) -> bool:
    stripped = text.strip()
    if _SEE_RE.match(stripped):
        return True
    return _split_index_locators(stripped) is not None


def _split_index_locators(text: str) -> tuple[str, str] | None:
    # Index terms often end in a digit themselves ("Beton C30, 42"). Search
    # comma boundaries from left to right and only accept a complete locator suffix.
    for match in re.finditer(r",", text):
        term = text[: match.start()].strip(" ,.;")
        locators = text[match.end() :].strip()
        if term and _LOCATOR_LIST_RE.fullmatch(locators):
            return term, locators
    columns = re.split(r"\s{2,}", text, maxsplit=1)
    if len(columns) == 2 and columns[0].strip() and _LOCATOR_LIST_RE.fullmatch(columns[1].strip()):
        return columns[0].strip(" ,.;"), columns[1].strip()
    return None


def _is_glossary_entry(text: str) -> bool:
    return _GLOSSARY_RE.match(text.strip()) is not None


def _line_matches_role(line: BookLine, role: str) -> bool:
    if _heading_role(line.text) is not None:
        return False
    if role in {"toc", "figures", "tables", "formulas"}:
        return _is_toc_entry(line.text)
    if role == "index":
        return _is_index_entry(line.text)
    if role == "glossary":
        return _is_glossary_entry(line.text)
    if role in {"abbreviations", "symbols"}:
        return _is_glossary_entry(line.text) or bool(re.search(r"\s{2,}|\t", line.text))
    return False


def _entry_ratio(page: BookPage, role: str) -> float:
    content = [
        line for line in page.lines if _heading_role(line.text) is None and line.text.strip()
    ]
    if not content:
        return 0.0
    return sum(_line_matches_role(line, role) for line in content) / len(content)


def _locator_number(text: str) -> int | None:
    stripped = text.strip()
    index_parts = _split_index_locators(stripped)
    match = _TOC_LINE_RE.match(stripped)
    if match is None and index_parts is not None:
        match = _LOCATOR_AT_END_RE.search(index_parts[1])
    if match is None:
        return None
    label = match.groupdict().get("label")
    if label is None:
        locator_text = match.groupdict().get("locators", "").split(",", 1)[0].strip()
        locator_match = _LOCATOR_TOKEN_RE.match(locator_text)
        label = locator_match.group("start") if locator_match else None
    if label is None:
        return None
    if label.isdigit():
        return int(label)
    return _roman_to_int(label)


def _ordered_signal(lines: Sequence[BookLine], role: str) -> float:
    if role in {"toc", "figures", "tables", "formulas", "index"}:
        values = [number for line in lines if (number := _locator_number(line.text)) is not None]
        if len(values) < 3:
            return 0.0
        if role in {"toc", "figures", "tables", "formulas"}:
            return (
                1.0
                if all(left <= right for left, right in zip(values, values[1:], strict=False))
                else 0.0
            )
    terms = [normalize_book_text(line.text.split(",", 1)[0]) for line in lines]
    terms = [term for term in terms if term]
    if len(terms) < 3:
        return 0.0
    ordered_pairs = sum(left <= right for left, right in zip(terms, terms[1:], strict=False))
    return ordered_pairs / (len(terms) - 1)


def detect_navigation_regions(
    pages: Sequence[BookPage], *, total_pages: int | None = None
) -> list[NavigationRegion]:
    """Detect printed TOC/index/glossary-like regions without an LLM.

    Low-confidence candidates (0.55–0.75) are retained with ``accepted=False`` so
    the indexing orchestrator can selectively ask a local model. A bare chapter
    named "Index" has keyword signal only and is therefore rejected.
    """

    if not pages:
        return []
    total_pages = total_pages or max(page.page_no for page in pages)
    by_number = {page.page_no: page for page in pages}
    regions: list[NavigationRegion] = []
    consumed: set[tuple[str, int]] = set()
    for page in sorted(pages, key=lambda item: item.page_no):
        for heading in page.lines:
            role = _heading_role(heading.text)
            if role is None or (role, page.page_no) in consumed:
                continue
            selected = [page]
            gap_used = False
            next_page_no = page.page_no + 1
            while (next_page := by_number.get(next_page_no)) is not None:
                next_roles = {
                    detected
                    for line in next_page.lines
                    if (detected := _heading_role(line.text)) is not None
                }
                if any(detected != role for detected in next_roles):
                    break
                ratio = _entry_ratio(next_page, role)
                if ratio >= 0.25:
                    selected.append(next_page)
                    gap_used = False
                elif not gap_used and next_page_no < total_pages:
                    gap_used = True
                else:
                    break
                next_page_no += 1
            entry_lines = [
                line
                for selected_page in selected
                for line in selected_page.lines
                if _line_matches_role(line, role)
            ]
            content_lines = [
                line
                for selected_page in selected
                for line in selected_page.lines
                if line.text.strip() and _heading_role(line.text) is None
            ]
            entry_count = len(entry_lines)
            entry_ratio = entry_count / max(len(content_lines), 1)
            count_floor = (
                3 if role == "glossary" else 5 if role in {"symbols", "abbreviations"} else 8
            )
            count_signal = 1.0 if entry_count >= count_floor else entry_count / count_floor
            ordered = _ordered_signal(entry_lines, role)
            front_limit = max(12, math.ceil(total_pages * 0.15))
            expected_position = (
                selected[0].page_no <= front_limit
                if role in {"toc", "figures", "tables", "formulas"}
                else selected[-1].page_no >= math.floor(total_pages * 0.65)
            )
            score = min(
                1.0,
                0.35
                + 0.25 * count_signal
                + 0.20 * min(1.0, entry_ratio / 0.70)
                + 0.10 * ordered
                + 0.10 * float(expected_position),
            )
            if score < 0.55:
                continue
            source_refs = [
                line.source_ref
                for selected_page in selected
                for line in selected_page.lines
                if line.source_ref is not None
            ]
            region = NavigationRegion(
                role=role,
                page_start=selected[0].page_no,
                page_end=selected[-1].page_no,
                score=round(score, 6),
                accepted=score >= 0.75,
                source_refs=source_refs,
                metrics={
                    "entry_count": float(entry_count),
                    "entry_ratio": round(entry_ratio, 6),
                    "ordered": round(ordered, 6),
                    "expected_position": float(expected_position),
                },
            )
            regions.append(region)
            consumed.update(
                (role, number) for number in range(region.page_start, region.page_end + 1)
            )
    return sorted(regions, key=lambda item: (item.page_start, item.role))


def _roman_to_int(value: str) -> int | None:
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    normalized = value.casefold()
    if not normalized or any(char not in values for char in normalized):
        return None
    total = 0
    previous = 0
    for char in reversed(normalized):
        current = values[char]
        total += -current if current < previous else current
        previous = max(previous, current)
    return total


def _resolve_label(label: str, page_labels: Mapping[str, int]) -> int | None:
    direct = page_labels.get(label)
    if direct is not None:
        return direct
    normalized = normalize_book_text(label)
    for candidate, page_no in page_labels.items():
        if normalize_book_text(candidate) == normalized:
            return page_no
    return None


def parse_page_locator(raw: str, page_labels: Mapping[str, int]) -> PageLocator | None:
    match = _LOCATOR_TOKEN_RE.match(raw.strip())
    if match is None:
        return None
    start_label = match.group("start")
    end_label = match.group("end")
    suffix_raw = (match.group("suffix") or "").casefold()
    suffix = "ff" if suffix_raw.startswith("ff") else "f" if suffix_raw else None
    start = _resolve_label(start_label, page_labels)
    end = _resolve_label(end_label, page_labels) if end_label else None
    resolved: list[int] = []
    if start is not None:
        resolved = [start]
        if end is not None and end >= start and end - start <= 500:
            resolved = list(range(start, end + 1))
        elif suffix == "f":
            resolved.append(start + 1)
    return PageLocator(
        raw=raw.strip(),
        start_label=start_label,
        end_label=end_label,
        suffix=suffix,
        resolved_pages=resolved,
    )


def _region_lines(pages: Sequence[BookPage], region: NavigationRegion) -> list[BookLine]:
    return [
        line
        for page in sorted(pages, key=lambda item: item.page_no)
        if region.page_start <= page.page_no <= region.page_end
        for line in page.lines
    ]


def _numbering_depth(title: str) -> int | None:
    match = _NUMBERING_RE.match(title)
    if match is None:
        return None
    number = match.group("number")
    return number.count(".") if number[0].isdigit() else 0


def parse_table_of_contents(
    pages: Sequence[BookPage],
    region: NavigationRegion,
    page_labels: Mapping[str, int],
) -> list[TocEntry]:
    if region.role != "toc":
        raise ValueError("Expected a table-of-contents region")
    parsed: list[tuple[BookLine, str, str]] = []
    pending: BookLine | None = None
    for line in _region_lines(pages, region):
        if _heading_role(line.text) is not None:
            continue
        match = _TOC_LINE_RE.match(line.text.strip())
        if match is None:
            if line.text.strip() and _LOCATOR_AT_END_RE.search(line.text.strip()) is None:
                pending = line
            continue
        title = match.group("title").strip(" .")
        if pending is not None and abs(pending.x0 - line.x0) <= 12:
            title = f"{pending.text.strip()} {title}"
        pending = None
        parsed.append((line, title, match.group("label")))
    if not parsed:
        return []
    base_indent = min(line.x0 for line, _, _ in parsed)
    entries: list[TocEntry] = []
    for line, title, raw_locator in parsed:
        depth = _numbering_depth(title)
        if depth is None:
            depth = max(0, min(6, round((line.x0 - base_indent) / 15)))
        locator = parse_page_locator(raw_locator, page_labels)
        if locator is None:
            continue
        entries.append(
            TocEntry(
                title=title,
                depth=depth,
                locator=locator,
                target_pages=locator.resolved_pages,
                source_page=line.page_no,
                source_ref=line.source_ref,
            )
        )
    return entries


def parse_reference_list(
    pages: Sequence[BookPage],
    region: NavigationRegion,
    page_labels: Mapping[str, int],
) -> list[TocEntry]:
    """Parse a list of figures/tables/equations into provenance-bound locators."""

    if region.role not in {"figures", "tables", "formulas"}:
        raise ValueError("Expected a figure, table, or formula list")
    return parse_table_of_contents(
        pages,
        region.model_copy(update={"role": "toc"}),
        page_labels,
    )


def parse_subject_index(
    pages: Sequence[BookPage],
    region: NavigationRegion,
    page_labels: Mapping[str, int],
) -> list[IndexEntry]:
    if region.role != "index":
        raise ValueError("Expected a subject-index region")
    entries: list[IndexEntry] = []
    parent_term: str | None = None
    parent_indent: float | None = None
    for line in _region_lines(pages, region):
        stripped = line.text.strip()
        if not stripped or _heading_role(stripped) is not None:
            continue
        see = _SEE_RE.match(stripped)
        if see is not None:
            relation_text = normalize_book_text(see.group("relation"))
            relation = "see_also" if "auch" in relation_text or "also" in relation_text else "see"
            term = see.group("term").strip(" ,;")
            entries.append(
                IndexEntry(
                    term=term,
                    relation=relation,
                    related_term=see.group("related").strip(" ,;"),
                    source_page=line.page_no,
                    source_ref=line.source_ref,
                )
            )
            continue
        locator_parts = _split_index_locators(stripped)
        if locator_parts is None:
            continue
        term, locator_text = locator_parts
        if not term:
            continue
        subterm: str | None = None
        if parent_indent is not None and line.x0 >= parent_indent + 10 and parent_term:
            subterm = term
            term = parent_term
        else:
            parent_term = term
            parent_indent = line.x0
        locators = [
            locator
            for raw in re.split(r"\s*,\s*", locator_text)
            if (locator := parse_page_locator(raw, page_labels)) is not None
        ]
        entries.append(
            IndexEntry(
                term=term,
                subterm=subterm,
                locators=locators,
                source_page=line.page_no,
                source_ref=line.source_ref,
            )
        )
    return entries


def parse_glossary(pages: Sequence[BookPage], region: NavigationRegion) -> list[GlossaryEntry]:
    if region.role not in {"glossary", "abbreviations", "symbols"}:
        raise ValueError("Expected a glossary-like region")
    entries: list[GlossaryEntry] = []
    for line in _region_lines(pages, region):
        if _heading_role(line.text) is not None:
            continue
        match = _GLOSSARY_RE.match(line.text.strip())
        if match is None:
            continue
        entries.append(
            GlossaryEntry(
                term=match.group("term").strip(),
                definition=match.group("definition").strip(),
                source_page=line.page_no,
                source_ref=line.source_ref,
            )
        )
    return entries


def _token_similarity(left: str, right: str) -> float:
    left_tokens = set(_title_for_matching(left).split())
    right_tokens = set(_title_for_matching(right).split())
    if not left_tokens or not right_tokens:
        return 0.0
    if left_tokens == right_tokens:
        return 1.0
    overlap = len(left_tokens & right_tokens)
    return (2 * overlap) / (len(left_tokens) + len(right_tokens))


@dataclass(frozen=True)
class _OutlineAnchor:
    title: str
    depth: int
    page_no: int
    source_kind: str
    confidence: float
    source_ref: str | None


def _pair_score(anchor: _OutlineAnchor, heading: HeadingCandidate) -> float:
    title = _token_similarity(anchor.title, heading.title)
    page_distance = abs(anchor.page_no - heading.page_no)
    page = (
        1.0
        if page_distance == 0
        else 0.8
        if page_distance <= 2
        else 0.4
        if page_distance <= 5
        else 0.0
    )
    inferred = _numbering_depth(heading.title)
    numbering = 1.0 if inferred is None or inferred == anchor.depth else 0.4
    level_depth = max(0, (heading.level or 1) - 1)
    hierarchy = 1.0 if heading.level is None or level_depth == anchor.depth else 0.5
    return 0.55 * title + 0.25 * page + 0.10 * numbering + 0.10 * hierarchy


def _monotone_matches(
    anchors: Sequence[_OutlineAnchor], headings: Sequence[HeadingCandidate]
) -> dict[int, tuple[_OutlineAnchor, float]]:
    rows, columns = len(anchors), len(headings)
    # Backtracking needs one byte per cell; scores need only the previous row.
    # This keeps large textbook outlines well below the 13 GiB deployment cap.
    actions = [bytearray(columns + 1) for _ in range(rows + 1)]
    previous = [0.0] * (columns + 1)
    for row in range(1, rows + 1):
        current = [0.0] * (columns + 1)
        for column in range(1, columns + 1):
            pair = _pair_score(anchors[row - 1], headings[column - 1])
            score = previous[column]
            action = 1  # skip anchor
            if current[column - 1] > score:
                score = current[column - 1]
                action = 2  # skip heading
            if pair >= 0.68:
                matched = previous[column - 1] + pair
                if matched > score:
                    score = matched
                    action = 3
            current[column] = score
            actions[row][column] = action
        previous = current
    matches: dict[int, tuple[_OutlineAnchor, float]] = {}
    row, column = rows, columns
    while row and column:
        action = actions[row][column]
        if action == 3:
            anchor = anchors[row - 1]
            matches[column - 1] = (anchor, _pair_score(anchor, headings[column - 1]))
            row -= 1
            column -= 1
        elif action == 1:
            row -= 1
        else:
            column -= 1
    return matches


def _stable_node_id(logical_document_id: str, title: str, page_no: int, depth: int) -> str:
    material = f"{logical_document_id}\0{normalize_book_text(title)}\0{page_no}\0{depth}".encode()
    return f"sec-{hashlib.sha256(material).hexdigest()[:24]}"


def _window_fallback(
    logical_document_id: str,
    total_pages: int,
    scanned_pages: Iterable[int] = (),
) -> BookStructure:
    scanned = set(scanned_pages)
    nodes: list[BookStructureNode] = []
    start = 1
    while start <= total_pages:
        is_scan = start in scanned
        window = 4 if is_scan else 8
        end = min(total_pages, start + window - 1)
        for page_no in range(start + 1, end + 1):
            if (page_no in scanned) != is_scan:
                end = page_no - 1
                break
        title = f"Seiten {start}–{end}"
        nodes.append(
            BookStructureNode(
                node_id=_stable_node_id(logical_document_id, title, start, 0),
                depth=0,
                ordinal=len(nodes),
                title=title,
                normalized_title=normalize_book_text(title),
                page_start=start,
                page_end=end,
                source_kind="window",
                confidence=0.45,
            )
        )
        start = end + 1
    return BookStructure(
        logical_document_id=logical_document_id,
        mode="window-fallback",
        confidence=0.45,
        total_pages=total_pages,
        nodes=nodes,
        stats={"fallback_window_count": len(nodes)},
    )


def reconcile_book_structure(
    logical_document_id: str,
    *,
    total_pages: int,
    bookmarks: Sequence[BookBookmark] = (),
    toc_entries: Sequence[TocEntry] = (),
    headings: Sequence[HeadingCandidate] = (),
    page_labels: Mapping[str, int] | None = None,
    regions: Sequence[NavigationRegion] = (),
    scanned_pages: Iterable[int] = (),
) -> BookStructure:
    """Build one canonical full-book hierarchy from independent signals.

    Bookmark depth is always the raw full-book depth supplied by the caller;
    range-local Docling levels only act as a weak fallback signal.
    """

    if total_pages < 1:
        raise ValueError("total_pages must be positive")
    ordered_headings = sorted(headings, key=lambda item: (item.page_no, item.y0, item.x0))

    # Printed page numbers often omit front matter. Match TOC titles against the
    # full-book heading stream and use a robust common offset for entries whose
    # PDF page-label map is merely physical numbering.
    toc_heading_matches: dict[int, HeadingCandidate] = {}
    next_heading = 0
    for toc_index, item in enumerate(toc_entries):
        candidates = [
            (index, heading, _token_similarity(item.title, heading.title))
            for index, heading in enumerate(ordered_headings[next_heading:], next_heading)
        ]
        if not candidates:
            break
        index, heading, score = max(candidates, key=lambda value: (value[2], -value[0]))
        if score >= 0.82:
            toc_heading_matches[toc_index] = heading
            next_heading = index + 1
    offsets = [
        heading.page_no - item.target_pages[0]
        for toc_index, item in enumerate(toc_entries)
        if item.target_pages and (heading := toc_heading_matches.get(toc_index)) is not None
    ]
    calibrated_offset = 0
    if len(offsets) >= 3:
        median = int(round(statistics.median(offsets)))
        if statistics.median(abs(value - median) for value in offsets) <= 1:
            calibrated_offset = median

    anchors: list[_OutlineAnchor] = [
        _OutlineAnchor(
            title=item.title,
            depth=item.depth,
            page_no=item.page_no,
            source_kind="bookmark",
            confidence=0.98,
            source_ref=item.source_ref,
        )
        for item in sorted(bookmarks, key=lambda value: (value.page_no, value.depth))
    ]
    for toc_index, item in enumerate(toc_entries):
        matched_heading = toc_heading_matches.get(toc_index)
        if not item.target_pages and matched_heading is None:
            # The TOC's own page is not a section target. If neither a printed
            # label nor a body-title match resolves it, omit the anchor and let
            # the deterministic window fallback cover those pages.
            continue
        target_page = (
            item.target_pages[0] + calibrated_offset
            if item.target_pages
            else matched_heading.page_no
        )
        if (
            matched_heading is not None
            and _token_similarity(item.title, matched_heading.title) >= 0.9
            and target_page != matched_heading.page_no
        ):
            target_page = matched_heading.page_no
        target_page = min(total_pages, max(1, target_page))
        duplicate = any(
            _token_similarity(anchor.title, item.title) >= 0.9
            and abs(anchor.page_no - target_page) <= 2
            for anchor in anchors
        )
        if not duplicate:
            anchors.append(
                _OutlineAnchor(
                    title=item.title,
                    depth=item.depth,
                    page_no=target_page,
                    source_kind="printed-toc",
                    confidence=0.92,
                    source_ref=item.source_ref,
                )
            )
    anchors.sort(key=lambda item: (item.page_no, item.depth, item.title))
    if not anchors and (not ordered_headings or (total_pages > 20 and len(ordered_headings) < 3)):
        result = _window_fallback(logical_document_id, total_pages, scanned_pages)
        return result.model_copy(
            update={"page_labels": dict(page_labels or {}), "regions": list(regions)}
        )
    matches = _monotone_matches(anchors, ordered_headings)
    provisional: list[dict[str, object]] = []
    heading_by_anchor = {
        id(anchor): (ordered_headings[index], pair_score)
        for index, (anchor, pair_score) in matches.items()
    }
    matched_heading_indexes = set(matches)
    for anchor in anchors:
        matched = heading_by_anchor.get(id(anchor))
        if matched is None:
            title = anchor.title
            page_no = anchor.page_no
            source_kind = anchor.source_kind
            confidence = anchor.confidence
            refs = [anchor.source_ref] if anchor.source_ref else []
        else:
            heading, pair_score = matched
            title = heading.title
            page_no = heading.page_no
            source_kind = "mixed"
            confidence = min(anchor.confidence, pair_score)
            refs = [value for value in (anchor.source_ref, heading.source_ref) if value]
        provisional.append(
            {
                "title": title.strip(),
                "page": page_no,
                "depth": min(6, anchor.depth),
                "source_kind": source_kind,
                "confidence": confidence,
                "refs": refs,
            }
        )
    for index, heading in enumerate(ordered_headings):
        if index in matched_heading_indexes:
            continue
        if any(
            abs(int(item["page"]) - heading.page_no) <= 1
            and _token_similarity(str(item["title"]), heading.title) >= 0.9
            for item in provisional
        ):
            continue
        inferred = _numbering_depth(heading.title)
        provisional.append(
            {
                "title": heading.title.strip(),
                "page": heading.page_no,
                "depth": min(
                    6,
                    inferred if inferred is not None else max(0, (heading.level or 1) - 1),
                ),
                "source_kind": "body-heading",
                "confidence": 0.85 if inferred is not None else heading.confidence,
                "refs": [heading.source_ref] if heading.source_ref else [],
            }
        )
    provisional.sort(key=lambda item: (int(item["page"]), int(item["depth"]), str(item["title"])))
    deduplicated: list[dict[str, object]] = []
    by_identity: dict[tuple[int, int, str], dict[str, object]] = {}
    for item in provisional:
        identity = (
            int(item["page"]),
            int(item["depth"]),
            normalize_book_text(str(item["title"])),
        )
        existing = by_identity.get(identity)
        if existing is None:
            by_identity[identity] = item
            deduplicated.append(item)
            continue
        existing["confidence"] = max(float(existing["confidence"]), float(item["confidence"]))
        existing["refs"] = list(
            dict.fromkeys([*list(existing["refs"]), *list(item["refs"])])  # type: ignore[arg-type]
        )
        if existing["source_kind"] != item["source_kind"]:
            existing["source_kind"] = "mixed"
    provisional = deduplicated
    nodes: list[BookStructureNode] = []
    stack: list[BookStructureNode] = []
    for ordinal, item in enumerate(provisional):
        depth = int(item["depth"])
        while stack and stack[-1].depth >= depth:
            stack.pop()
        if depth > 0 and not stack:
            depth = 0
        elif stack and depth > stack[-1].depth + 1:
            depth = stack[-1].depth + 1
        title = str(item["title"])
        page_start = int(item["page"])
        node = BookStructureNode(
            node_id=_stable_node_id(logical_document_id, title, page_start, depth),
            parent_id=stack[-1].node_id if stack else None,
            depth=depth,
            ordinal=ordinal,
            title=title,
            normalized_title=normalize_book_text(title),
            page_start=page_start,
            page_end=total_pages,
            source_kind=item["source_kind"],  # type: ignore[arg-type]
            confidence=float(item["confidence"]),
            source_refs=list(item["refs"]),  # type: ignore[arg-type]
        )
        nodes.append(node)
        stack.append(node)
    updated: list[BookStructureNode] = []
    for index, node in enumerate(nodes):
        next_boundary = next(
            (
                candidate.page_start
                for candidate in nodes[index + 1 :]
                if candidate.depth <= node.depth and candidate.page_start > node.page_start
            ),
            total_pages + 1,
        )
        updated.append(
            node.model_copy(update={"page_end": max(node.page_start, next_boundary - 1)})
        )
    if bookmarks and toc_entries:
        mode = "reconciled"
    elif bookmarks:
        mode = "bookmarks"
    elif toc_entries:
        mode = "printed-toc"
    else:
        mode = "body-headings"
    confidence = sum(node.confidence for node in updated) / len(updated)
    return BookStructure(
        logical_document_id=logical_document_id,
        mode=mode,
        confidence=round(confidence, 6),
        total_pages=total_pages,
        nodes=updated,
        page_labels=dict(page_labels or {}),
        regions=list(regions),
        stats={
            "bookmark_count": len(bookmarks),
            "toc_entry_count": len(toc_entries),
            "heading_count": len(headings),
            "matched_heading_count": len(matches),
            "printed_page_offset": calibrated_offset,
        },
    )
