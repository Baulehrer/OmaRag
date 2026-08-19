from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from .domain import StrictModel
from .media import BookMediaSnapshot

NavigationRole = Literal[
    "toc",
    "index",
    "glossary",
    "abbreviations",
    "symbols",
    "figures",
    "tables",
    "formulas",
]
StructureMode = Literal[
    "bookmarks",
    "printed-toc",
    "body-headings",
    "reconciled",
    "window-fallback",
]


class BookLine(StrictModel):
    """One reading-order line used by deterministic book-structure analysis."""

    page_no: int = Field(ge=1)
    text: str
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0
    source_ref: str | None = None


class BookPage(StrictModel):
    page_no: int = Field(ge=1)
    page_label: str
    lines: list[BookLine] = Field(default_factory=list)
    scanned: bool = False

    @model_validator(mode="after")
    def line_pages_match(self) -> BookPage:
        if any(line.page_no != self.page_no for line in self.lines):
            raise ValueError("Every line must belong to its containing page")
        return self


class NavigationRegion(StrictModel):
    role: NavigationRole
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    score: float = Field(ge=0.0, le=1.0)
    accepted: bool
    source_refs: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class PageLocator(StrictModel):
    raw: str
    start_label: str
    end_label: str | None = None
    suffix: Literal["f", "ff"] | None = None
    resolved_pages: list[int] = Field(default_factory=list)


class TocEntry(StrictModel):
    title: str
    depth: int = Field(ge=0, le=99)
    locator: PageLocator
    target_pages: list[int] = Field(default_factory=list)
    source_page: int = Field(ge=1)
    source_ref: str | None = None
    confidence: float = Field(default=0.92, ge=0.0, le=1.0)


class IndexEntry(StrictModel):
    term: str
    subterm: str | None = None
    locators: list[PageLocator] = Field(default_factory=list)
    relation: Literal["located", "see", "see_also"] = "located"
    related_term: str | None = None
    source_page: int = Field(ge=1)
    source_ref: str | None = None
    confidence: float = Field(default=0.95, ge=0.0, le=1.0)


class GlossaryEntry(StrictModel):
    term: str
    definition: str
    source_page: int = Field(ge=1)
    source_ref: str | None = None
    confidence: float = Field(default=0.95, ge=0.0, le=1.0)


class BookBookmark(StrictModel):
    title: str
    depth: int = Field(ge=0, le=99)
    page_no: int = Field(ge=1)
    source_ref: str | None = None


class HeadingCandidate(StrictModel):
    title: str
    page_no: int = Field(ge=1)
    level: int | None = Field(default=None, ge=0, le=100)
    x0: float = 0.0
    y0: float = 0.0
    source_ref: str | None = None
    confidence: float = Field(default=0.65, ge=0.0, le=1.0)


class BookStructureNode(StrictModel):
    node_id: str
    parent_id: str | None = None
    kind: Literal["section", "window"] = "section"
    depth: int = Field(ge=0, le=99)
    ordinal: int = Field(ge=0)
    title: str
    normalized_title: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    source_kind: Literal["bookmark", "printed-toc", "body-heading", "window", "mixed"]
    confidence: float = Field(ge=0.0, le=1.0)
    source_refs: list[str] = Field(default_factory=list)


class BookStructure(StrictModel):
    logical_document_id: str
    mode: StructureMode
    confidence: float = Field(ge=0.0, le=1.0)
    total_pages: int = Field(ge=1)
    nodes: list[BookStructureNode]
    page_labels: dict[str, int] = Field(default_factory=dict)
    regions: list[NavigationRegion] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)


class EvidenceAnchor(StrictModel):
    page_no: int = Field(ge=1)
    source_ref: str
    bbox: tuple[float, float, float, float] | None = None
    label: str | None = None


class EvidenceRecord(StrictModel):
    evidence_id: str
    raw_content: str
    content_hash: str
    anchors: list[EvidenceAnchor]
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    section_node_id: str
    headings: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    # Regex word/punctuation count, not the chunker's tokenizer: it runs about
    # 20 % above the Qwen count that `processing.chunk_size` budgets.
    raw_tokens: int | None = Field(default=None, ge=0)
    context_hash: str | None = None
    previous_evidence_id: str | None = None
    next_evidence_id: str | None = None
    evidence_kind: Literal[
        "prose",
        "table",
        "formula",
        "figure",
        "navigation",
        "ocr",
        "unknown",
    ] = "unknown"
    provenance_kind: Literal["element", "page-fallback", "synthetic", "legacy"] = "legacy"
    quality_flags: list[str] = Field(default_factory=list)


class KnowledgeTerm(StrictModel):
    term_id: str
    canonical: str
    normalized: str
    kind: Literal["index", "glossary", "heading", "caption", "keyphrase"]
    source_page: int | None = Field(default=None, ge=1)
    source_ref: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TermAlias(StrictModel):
    term_id: str
    alias: str
    normalized_alias: str
    relation: Literal["alias", "see", "see_also", "acronym"] = "alias"


class TermTarget(StrictModel):
    term_id: str
    node_id: str | None = None
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    evidence_id: str | None = None
    relation: Literal["located_in", "defined_in", "described_in"]
    confidence: float = Field(ge=0.0, le=1.0)


class KnowledgeEdge(StrictModel):
    edge_id: str
    source_id: str
    target_id: str
    relation: Literal[
        "parent_of",
        "next_section",
        "alias_of",
        "see_also",
        "co_occurs",
    ]
    weight: float = Field(ge=0.0)
    evidence_ids: list[str] = Field(default_factory=list)


class BookRagGraph(StrictModel):
    terms: list[KnowledgeTerm] = Field(default_factory=list)
    aliases: list[TermAlias] = Field(default_factory=list)
    targets: list[TermTarget] = Field(default_factory=list)
    edges: list[KnowledgeEdge] = Field(default_factory=list)


class BookKnowledgeSnapshot(StrictModel):
    """Portable deterministic Core sidecar.

    Schema v3 adds first-class media. Pydantic defaults let existing v2 JSON
    load unchanged with an empty media block.
    """

    schema_version: Literal["2", "3"] = "2"
    logical_document_id: str
    generation_id: str
    fingerprint: str
    config_hash: str
    content_hash: str
    structure: BookStructure
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    graph: BookRagGraph = Field(default_factory=BookRagGraph)
    media: BookMediaSnapshot = Field(default_factory=BookMediaSnapshot)
    stats: dict[str, Any] = Field(default_factory=dict)
