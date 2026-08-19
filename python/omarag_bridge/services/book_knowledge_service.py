from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Sequence

from ..models.book import (
    BookRagGraph,
    BookStructure,
    BookStructureNode,
    EvidenceRecord,
    GlossaryEntry,
    IndexEntry,
    KnowledgeEdge,
    KnowledgeTerm,
    TermAlias,
    TermTarget,
)
from .book_structure_service import normalize_book_text

_STOPWORDS = frozenset(
    {
        "aber",
        "als",
        "and",
        "auch",
        "auf",
        "aus",
        "bei",
        "das",
        "dem",
        "den",
        "der",
        "des",
        "die",
        "durch",
        "ein",
        "eine",
        "einer",
        "eines",
        "for",
        "from",
        "für",
        "ist",
        "mit",
        "oder",
        "of",
        "the",
        "und",
        "von",
        "with",
        "wird",
        "werden",
        "zu",
        "zur",
        "zum",
    }
)


def _stable_id(prefix: str, *values: object) -> str:
    material = "\0".join(str(value) for value in values).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:24]}"


def _section_for_page(structure: BookStructure, page_no: int) -> BookStructureNode | None:
    candidates = [node for node in structure.nodes if node.page_start <= page_no <= node.page_end]
    return max(candidates, key=lambda node: (node.depth, node.page_start), default=None)


def _term_tokens(value: str) -> list[str]:
    return [
        token
        for token in normalize_book_text(value).split()
        if len(token) >= 3 and token not in _STOPWORDS and not token.isdigit()
    ]


def _fallback_keyphrases(
    evidence: Sequence[EvidenceRecord], *, cap: int
) -> list[tuple[str, float, list[str]]]:
    """Extract bounded deterministic 1–5 gram phrases without another model."""

    phrase_chunks: dict[str, set[str]] = defaultdict(set)
    display: dict[str, str] = {}
    for record in evidence:
        tokens = _term_tokens(record.raw_content)
        for width in range(1, min(5, len(tokens)) + 1):
            for index in range(len(tokens) - width + 1):
                gram = tokens[index : index + width]
                normalized = " ".join(gram)
                if not normalized or all(token in _STOPWORDS for token in gram):
                    continue
                phrase_chunks[normalized].add(record.evidence_id)
                display.setdefault(normalized, normalized)
    candidates: list[tuple[float, str, list[str]]] = []
    for normalized, chunk_ids in phrase_chunks.items():
        if len(chunk_ids) < 2:
            continue
        width = len(normalized.split())
        # A small C-value-like preference for specific, repeated multi-word terms.
        score = (1.0 + 0.25 * (width - 1)) * len(chunk_ids)
        candidates.append((score, normalized, sorted(chunk_ids)))
    candidates.sort(key=lambda item: (-item[0], -len(item[1]), item[1]))
    return [
        (display[normalized], min(0.7, 0.45 + score / 100), chunk_ids)
        for score, normalized, chunk_ids in candidates[:cap]
    ]


def build_bookrag_lite(
    structure: BookStructure,
    evidence: Sequence[EvidenceRecord],
    *,
    index_entries: Sequence[IndexEntry] = (),
    glossary_entries: Sequence[GlossaryEntry] = (),
    max_term_postings: int = 64,
) -> BookRagGraph:
    """Create a small provenance-bound graph; no generated factual triples."""

    terms: dict[str, KnowledgeTerm] = {}
    aliases: dict[tuple[str, str, str], TermAlias] = {}
    targets: dict[tuple[object, ...], TermTarget] = {}
    edges: dict[str, KnowledgeEdge] = {}
    priority = {"glossary": 5, "index": 4, "heading": 3, "caption": 2, "keyphrase": 1}

    def ensure_term(
        canonical: str,
        kind: str,
        confidence: float,
        *,
        source_page: int | None = None,
        source_ref: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> KnowledgeTerm | None:
        normalized = normalize_book_text(canonical)
        if not normalized:
            return None
        existing = terms.get(normalized)
        candidate = KnowledgeTerm(
            term_id=_stable_id("term", structure.logical_document_id, normalized),
            canonical=canonical.strip(),
            normalized=normalized,
            kind=kind,  # type: ignore[arg-type]
            source_page=source_page,
            source_ref=source_ref,
            confidence=confidence,
            metadata=dict(metadata or {}),
        )
        if existing is None or priority[kind] > priority[existing.kind]:
            terms[normalized] = candidate.model_copy(
                update={"term_id": existing.term_id if existing else candidate.term_id}
            )
        return terms[normalized]

    def add_target(target: TermTarget) -> None:
        key = (
            target.term_id,
            target.node_id,
            target.page_start,
            target.page_end,
            target.evidence_id,
            target.relation,
        )
        current = targets.get(key)
        if current is None or target.confidence > current.confidence:
            targets[key] = target

    # The structure itself is the graph backbone.
    for node in structure.nodes:
        term = ensure_term(
            node.title,
            "heading",
            node.confidence,
            source_page=node.page_start,
            source_ref=node.source_refs[0] if node.source_refs else None,
        )
        if term is not None:
            # Section routing must resolve to Haiku-owned chunks, not merely a
            # sidecar page hint. Keep a small, deterministic set of entry
            # evidence for the section (including its descendants).
            representatives = [
                record
                for record in evidence
                if node.page_start <= record.page_start <= node.page_end
            ][:4]
            if not representatives:
                add_target(
                    TermTarget(
                        term_id=term.term_id,
                        node_id=node.node_id,
                        page_start=node.page_start,
                        page_end=node.page_end,
                        relation="located_in",
                        confidence=node.confidence,
                    )
                )
            for record in representatives:
                add_target(
                    TermTarget(
                        term_id=term.term_id,
                        node_id=record.section_node_id,
                        page_start=record.page_start,
                        page_end=record.page_end,
                        evidence_id=record.evidence_id,
                        relation="located_in",
                        confidence=node.confidence,
                    )
                )
        if node.parent_id:
            edge_id = _stable_id("edge", node.parent_id, node.node_id, "parent_of")
            edges[edge_id] = KnowledgeEdge(
                edge_id=edge_id,
                source_id=node.parent_id,
                target_id=node.node_id,
                relation="parent_of",
                weight=1.0,
            )
    for left, right in zip(structure.nodes, structure.nodes[1:], strict=False):
        edge_id = _stable_id("edge", left.node_id, right.node_id, "next_section")
        edges[edge_id] = KnowledgeEdge(
            edge_id=edge_id,
            source_id=left.node_id,
            target_id=right.node_id,
            relation="next_section",
            weight=1.0,
        )

    for entry in index_entries:
        canonical = entry.subterm or entry.term
        term = ensure_term(
            canonical,
            "index",
            entry.confidence,
            source_page=entry.source_page,
            source_ref=entry.source_ref,
            metadata={"parent_term": entry.term} if entry.subterm else None,
        )
        if term is None:
            continue
        if entry.subterm:
            alias_key = (term.term_id, normalize_book_text(entry.term), "alias")
            aliases[alias_key] = TermAlias(
                term_id=term.term_id,
                alias=entry.term,
                normalized_alias=normalize_book_text(entry.term),
            )
        if entry.relation in {"see", "see_also"} and entry.related_term:
            related = ensure_term(entry.related_term, "index", entry.confidence)
            if related is not None:
                relation = entry.relation
                alias_key = (related.term_id, term.normalized, relation)
                aliases[alias_key] = TermAlias(
                    term_id=related.term_id,
                    alias=term.canonical,
                    normalized_alias=term.normalized,
                    relation=relation,
                )
                edge_relation = "see_also" if relation == "see_also" else "alias_of"
                edge_id = _stable_id("edge", term.term_id, related.term_id, edge_relation)
                edges[edge_id] = KnowledgeEdge(
                    edge_id=edge_id,
                    source_id=term.term_id,
                    target_id=related.term_id,
                    relation=edge_relation,
                    weight=entry.confidence,
                )
        postings = 0
        for locator in entry.locators:
            for page_no in locator.resolved_pages:
                if postings >= max_term_postings:
                    break
                node = _section_for_page(structure, page_no)
                records = [
                    record for record in evidence if record.page_start <= page_no <= record.page_end
                ]
                if not records:
                    add_target(
                        TermTarget(
                            term_id=term.term_id,
                            node_id=node.node_id if node else None,
                            page_start=page_no,
                            page_end=page_no,
                            relation="located_in",
                            confidence=entry.confidence,
                        )
                    )
                    postings += 1
                    continue
                for record in records:
                    if postings >= max_term_postings:
                        break
                    add_target(
                        TermTarget(
                            term_id=term.term_id,
                            node_id=record.section_node_id,
                            page_start=record.page_start,
                            page_end=record.page_end,
                            evidence_id=record.evidence_id,
                            relation="located_in",
                            confidence=entry.confidence,
                        )
                    )
                    postings += 1

    for entry in glossary_entries:
        term = ensure_term(
            entry.term,
            "glossary",
            entry.confidence,
            source_page=entry.source_page,
            source_ref=entry.source_ref,
            metadata={"definition": entry.definition},
        )
        if term is None:
            continue
        node = _section_for_page(structure, entry.source_page)
        same_page = next(
            (
                record
                for record in evidence
                if record.page_start <= entry.source_page <= record.page_end
            ),
            None,
        )
        add_target(
            TermTarget(
                term_id=term.term_id,
                node_id=node.node_id if node else None,
                page_start=entry.source_page,
                page_end=entry.source_page,
                evidence_id=same_page.evidence_id if same_page else None,
                relation="defined_in",
                confidence=entry.confidence,
            )
        )

    for record in evidence:
        if "caption" not in {label.casefold() for label in record.labels}:
            continue
        caption = " ".join(record.raw_content.split())
        if not caption:
            continue
        canonical = " ".join(caption.split()[:8]).rstrip(".,:;")
        term = ensure_term(
            canonical,
            "caption",
            0.8,
            source_page=record.page_start,
            source_ref=record.anchors[0].source_ref if record.anchors else None,
        )
        if term is not None:
            add_target(
                TermTarget(
                    term_id=term.term_id,
                    node_id=record.section_node_id,
                    page_start=record.page_start,
                    page_end=record.page_end,
                    evidence_id=record.evidence_id,
                    relation="described_in",
                    confidence=0.8,
                )
            )

    # Headings are not a term index: every book has them, and counting them
    # here meant the safety net never deployed for a book whose printed index
    # could not be read.  Only curated terms -- index, glossary, captions --
    # make extracted keyphrases unnecessary.
    curated_kinds = {"index", "glossary", "caption"}
    explicit_count = sum(term.kind in curated_kinds for term in terms.values())
    keyphrase_cap = max(0, min(2000, 2 * structure.total_pages) - len(terms))
    if explicit_count < 8 and keyphrase_cap:
        for canonical, confidence, evidence_ids in _fallback_keyphrases(
            evidence, cap=keyphrase_cap
        ):
            term = ensure_term(canonical, "keyphrase", confidence)
            if term is None:
                continue
            for evidence_id in evidence_ids[:max_term_postings]:
                record = next(item for item in evidence if item.evidence_id == evidence_id)
                add_target(
                    TermTarget(
                        term_id=term.term_id,
                        node_id=record.section_node_id,
                        page_start=record.page_start,
                        page_end=record.page_end,
                        evidence_id=evidence_id,
                        relation="located_in",
                        confidence=confidence,
                    )
                )

    # Provenance-bound co-occurrence: at least two distinct evidence chunks.
    occurrences: dict[str, set[str]] = defaultdict(set)
    searchable_terms = sorted(
        terms.values(),
        key=lambda item: (
            item.kind == "keyphrase",
            -item.confidence,
            -len(item.normalized),
            item.normalized,
        ),
    )
    evidence_terms: dict[str, list[str]] = {}
    for record in evidence:
        content = f" {normalize_book_text(record.raw_content)} "
        # A dense glossary page can mention hundreds of terms. Pairing all of
        # them is quadratic and adds little routing value; preserve the 32 most
        # explicit/specific matches per evidence unit.
        matched = [term.term_id for term in searchable_terms if f" {term.normalized} " in content][
            :32
        ]
        evidence_terms[record.evidence_id] = matched
        for term_id in matched:
            occurrences[term_id].add(record.evidence_id)
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_evidence: dict[tuple[str, str], set[str]] = defaultdict(set)
    for record in evidence:
        present = sorted(evidence_terms.get(record.evidence_id, []))
        for index, left in enumerate(present):
            for right in present[index + 1 :]:
                pair_counts[(left, right)] += 1
                pair_evidence[(left, right)].add(record.evidence_id)
    neighbors: Counter[str] = Counter()
    for (left, right), count in sorted(pair_counts.items(), key=lambda item: (-item[1], item[0])):
        if count < 2 or neighbors[left] >= 5 or neighbors[right] >= 5:
            continue
        edge_id = _stable_id("edge", left, right, "co_occurs")
        edges[edge_id] = KnowledgeEdge(
            edge_id=edge_id,
            source_id=left,
            target_id=right,
            relation="co_occurs",
            weight=float(count),
            evidence_ids=sorted(pair_evidence[(left, right)]),
        )
        neighbors[left] += 1
        neighbors[right] += 1

    return BookRagGraph(
        terms=sorted(terms.values(), key=lambda item: (item.normalized, item.term_id)),
        aliases=sorted(aliases.values(), key=lambda item: (item.normalized_alias, item.term_id)),
        targets=sorted(
            targets.values(),
            key=lambda item: (
                item.term_id,
                item.page_start or 0,
                item.node_id or "",
                item.evidence_id or "",
                item.relation,
            ),
        ),
        edges=sorted(edges.values(), key=lambda item: item.edge_id),
    )
