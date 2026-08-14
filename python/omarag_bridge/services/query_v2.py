from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import partial
from types import MappingProxyType
from typing import Literal

from ..models.domain import BookMetadata


class QueryComplexity(StrEnum):
    SIMPLE = "simple"
    STANDARD = "standard"
    COMPLEX = "complex"


@dataclass(frozen=True, slots=True)
class QueryBudget:
    candidate_cap: int
    final_min: int
    final_max: int
    max_facets: int
    evidence_tokens: int
    answer_tokens: int
    deadline_ms: int


QUERY_BUDGETS: Mapping[QueryComplexity, QueryBudget] = MappingProxyType(
    {
        QueryComplexity.SIMPLE: QueryBudget(24, 1, 5, 1, 320, 256, 15_000),
        QueryComplexity.STANDARD: QueryBudget(40, 2, 8, 2, 1_200, 384, 25_000),
        QueryComplexity.COMPLEX: QueryBudget(72, 4, 14, 4, 2_400, 512, 35_000),
    }
)

DEFAULT_RRF_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "hybrid": 1.0,
        "facet_hybrid": 0.9,
        "fts": 1.2,
        "tree": 1.1,
        "register": 1.1,
        "kg": 0.8,
    }
)


@dataclass(frozen=True, slots=True)
class QueryFacet:
    id: str
    query: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class QueryPlan:
    question: str
    complexity: QueryComplexity
    score: int
    reasons: tuple[str, ...]
    facets: tuple[QueryFacet, ...]
    budget: QueryBudget


_COMPARE = re.compile(
    r"(?is)\b(?:vergleiche|vergleich(?:e)?|unterschied(?:e)?\s+zwischen|compare)\s+"
    r"(?P<left>.+?)\s+(?:und|mit|gegen(?:über)?|vs\.?)\s+(?P<right>.+?)(?:[?.;]|$)"
)
_COMPARISON_SIGNAL = re.compile(
    r"(?i)\b(?:vergleiche|vergleich|unterschied|gegenüber|versus|vs\.?|compare|difference)\b"
)
_GLOBAL_SIGNAL = re.compile(
    r"(?i)\b(?:im gesamten (?:buch|werk)|kapitelübergreifend|über alle|gesamtüberblick|"
    r"globale?|entwick(?:lung|le)|zusammenfass(?:en|ung)|fass(?:e|en).{0,40}zusammen|"
    r"across (?:the )?book|overall)\b"
)
_MULTIHOP_SIGNAL = re.compile(
    r"(?i)\b(?:ursache.{0,40}wirkung|warum.{0,80}(?:dadurch|deshalb|folg)|"
    r"wie.{0,60}(?:beeinflusst|führt|wirkt)|cause.{0,40}effect|how.{0,60}affect)\b"
)
_CALCULATION_SIGNAL = re.compile(
    r"(?i)\b(?:berechne|ermittle|bestimme|rechne|calculate|compute|derive|herleite)\b"
)
_DIRECT_SIGNAL = re.compile(
    r"(?i)^\s*(?:was (?:ist|bedeutet)|definiere|definition|wo (?:steht|finde)|"
    r"welche seite|wie lautet (?:die )?(?:formel|definition)|what is|define|where is)\b"
)
_QUESTION_WORD = re.compile(
    r"(?i)\b(?:was|wie|warum|wann|wo|welche[rmns]?|wer|wodurch|what|how|why|where|which|who)\b"
)
_CLAUSE_SPLIT = re.compile(
    r"[;?]+|\b(?:sowie|außerdem|zusätzlich|and additionally)\b|"
    r"\b(?:und|and)\s+(?=(?:was|wie|warum|wann|wo|welche[rmns]?|wer|wodurch|"
    r"what|how|why|where|which|who)\b)",
    re.IGNORECASE,
)


def normalize_query(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def classify_query(
    question: str,
    *,
    has_session_reference: bool = False,
    register_entity_count: int = 0,
    edition_scope_count: int = 1,
) -> QueryPlan:
    """Classify a question without an LLM and create bounded search facets."""

    normalized = normalize_query(question)
    if not normalized:
        raise ValueError("question must not be empty")

    score = 0
    reasons: list[str] = []
    for pattern, weight, reason in (
        (_COMPARISON_SIGNAL, 2, "comparison"),
        (_GLOBAL_SIGNAL, 2, "global"),
        (_MULTIHOP_SIGNAL, 2, "multi_hop"),
        (_CALCULATION_SIGNAL, 1, "calculation"),
    ):
        if pattern.search(normalized):
            score += weight
            reasons.append(reason)

    question_clauses = [
        item.strip(" -.,:?") for item in _CLAUSE_SPLIT.split(normalized) if item.strip(" -.,:?")
    ]
    interrogative_clauses = sum(bool(_QUESTION_WORD.search(item)) for item in question_clauses)
    if interrogative_clauses >= 2:
        score += 1
        reasons.append("multiple_questions")
    if has_session_reference:
        score += 1
        reasons.append("session_reference")
    if register_entity_count >= 2:
        score += 1
        reasons.append("multiple_entities")
    if edition_scope_count >= 2:
        score += 1
        reasons.append("multiple_editions")
    if _DIRECT_SIGNAL.search(normalized) and interrogative_clauses < 2:
        score -= 1
        reasons.append("direct_lookup")

    complexity = (
        QueryComplexity.SIMPLE
        if score <= 0
        else QueryComplexity.STANDARD
        if score <= 2
        else QueryComplexity.COMPLEX
    )
    budget = QUERY_BUDGETS[complexity]
    facets = _deterministic_facets(normalized, complexity, budget.max_facets)
    return QueryPlan(
        question=normalized,
        complexity=complexity,
        score=score,
        reasons=tuple(reasons),
        facets=facets,
        budget=budget,
    )


def _deterministic_facets(
    question: str, complexity: QueryComplexity, max_facets: int
) -> tuple[QueryFacet, ...]:
    if complexity is QueryComplexity.SIMPLE:
        return (QueryFacet("F1", question),)

    queries: list[str] = []
    comparison = _COMPARE.search(question)
    if comparison:
        left = _clean_facet(comparison.group("left"))
        right = _clean_facet(comparison.group("right"))
        if left:
            queries.append(left)
        if right:
            queries.append(right)

    clauses = _CLAUSE_SPLIT.split(question)
    for clause in clauses:
        cleaned = _clean_facet(clause)
        if cleaned and (_QUESTION_WORD.search(cleaned) or len(queries) < 2):
            queries.append(cleaned)

    if complexity is QueryComplexity.COMPLEX:
        queries.append(question)

    unique: list[str] = []
    seen: set[str] = set()
    for query in queries or [question]:
        key = query.casefold()
        if key not in seen:
            unique.append(query)
            seen.add(key)
        if len(unique) == max_facets:
            break
    return tuple(QueryFacet(f"F{index}", query) for index, query in enumerate(unique, 1))


def _clean_facet(value: str) -> str:
    value = value.strip(" \t\r\n-–—,.;:?")
    value = re.sub(
        r"(?i)^(?:bitte\s+)?(?:vergleiche|vergleich(?:e)?|erläutere|erkläre|nenne)\s+",
        "",
        value,
    )
    return normalize_query(value)


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    chunk_id: str
    content: str
    document_id: str | None = None
    document_title: str | None = None
    source_uri: str | None = None
    logical_document_id: str | None = None
    section_id: str | None = None
    pages: tuple[int, ...] = ()
    headings: tuple[str, ...] = ()
    facet_ids: tuple[str, ...] = ()
    content_hash: str | None = None
    evidence_id: str | None = None
    element_types: tuple[str, ...] = ()
    doc_item_refs: tuple[str, ...] = ()
    book: BookMetadata | None = None

    def __post_init__(self) -> None:
        if not self.chunk_id:
            raise ValueError("chunk_id must not be empty")

    def stable_content_hash(self) -> str:
        return self.content_hash or hashlib.sha256(self.content.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    candidate: RetrievalCandidate
    fused_score: float
    ranks: tuple[tuple[str, int], ...]
    retrieval_paths: tuple[str, ...]


def weighted_rrf(
    rankings: Mapping[str, Sequence[RetrievalCandidate]],
    weights: Mapping[str, float] | None = None,
    *,
    rank_constant: int = 60,
    limit: int | None = None,
) -> list[FusedCandidate]:
    """Fuse heterogeneous rank lists without comparing their provider scores."""

    if rank_constant < 1:
        raise ValueError("rank_constant must be positive")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")

    active_weights = DEFAULT_RRF_WEIGHTS if weights is None else weights

    totals: dict[tuple[str, str], float] = {}
    representatives: dict[tuple[str, str], RetrievalCandidate] = {}
    paths: dict[tuple[str, str], list[str]] = {}
    ranks: dict[tuple[str, str], list[tuple[str, int]]] = {}
    facets: dict[tuple[str, str], list[str]] = {}
    for path, candidates in rankings.items():
        weight = float(active_weights.get(path, 1.0))
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("RRF weights must be finite and non-negative")
        seen_in_path: set[tuple[str, str]] = set()
        effective_rank = 0
        for candidate in candidates:
            identity = _candidate_identity(candidate)
            if identity in seen_in_path:
                continue
            seen_in_path.add(identity)
            effective_rank += 1
            representatives.setdefault(identity, candidate)
            totals[identity] = totals.get(identity, 0.0) + weight / (rank_constant + effective_rank)
            paths.setdefault(identity, []).append(path)
            ranks.setdefault(identity, []).append((path, effective_rank))
            current_facets = facets.setdefault(identity, [])
            for facet in candidate.facet_ids:
                if facet not in current_facets:
                    current_facets.append(facet)

    fused: list[FusedCandidate] = []
    for identity, score in totals.items():
        candidate = representatives[identity]
        merged_facets = tuple(dict.fromkeys((*candidate.facet_ids, *facets[identity])))
        if merged_facets != candidate.facet_ids:
            candidate = replace(candidate, facet_ids=merged_facets)
        fused.append(
            FusedCandidate(
                candidate=candidate,
                fused_score=score,
                ranks=tuple(ranks[identity]),
                retrieval_paths=tuple(paths[identity]),
            )
        )
    fused.sort(key=lambda item: (-item.fused_score, item.candidate.chunk_id))
    return fused[:limit]


@dataclass(frozen=True, slots=True)
class PlattCalibrator:
    scale: float = 1.0
    bias: float = 0.0
    digest: str = "identity-logit-v1"

    def __post_init__(self) -> None:
        if not math.isfinite(self.scale) or not math.isfinite(self.bias):
            raise ValueError("calibrator parameters must be finite")
        if not self.digest:
            raise ValueError("calibrator digest must not be empty")

    def probability(self, score: float) -> float:
        if not math.isfinite(score):
            raise ValueError("reranker score must be finite")
        value = self.scale * float(score) + self.bias
        if value >= 0:
            return 1.0 / (1.0 + math.exp(-min(value, 709.0)))
        exp_value = math.exp(max(value, -709.0))
        return exp_value / (1.0 + exp_value)


@dataclass(frozen=True, slots=True)
class RerankedCandidate:
    candidate: RetrievalCandidate
    raw_score: float
    relevance: float


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    candidate_floor: float = 0.42
    strict_threshold: float = 0.62
    normal_threshold: float = 0.52
    explore_threshold: float = 0.45
    score_gap: float = 0.08
    top_delta: float = 0.15
    mmr_lambda: float = 0.80

    def __post_init__(self) -> None:
        probabilities = (
            self.candidate_floor,
            self.strict_threshold,
            self.normal_threshold,
            self.explore_threshold,
            self.score_gap,
            self.top_delta,
            self.mmr_lambda,
        )
        if not all(math.isfinite(item) and 0.0 <= item <= 1.0 for item in probabilities):
            raise ValueError("selection policy values must be probabilities")

    def threshold(self, evidence_mode: str) -> float:
        try:
            return {
                "strict": self.strict_threshold,
                "normal": self.normal_threshold,
                "explore": self.explore_threshold,
            }[evidence_mode]
        except KeyError as exc:
            raise ValueError(f"unsupported evidence mode: {evidence_mode}") from exc


@dataclass(frozen=True, slots=True)
class SelectionResult:
    selected: tuple[RerankedCandidate, ...]
    missing_facets: tuple[str, ...]
    cutoff_reason: str
    cutoff_score: float | None
    eligible_count: int


def adaptive_select(
    candidates: Sequence[tuple[RetrievalCandidate, float]],
    *,
    complexity: QueryComplexity,
    evidence_mode: Literal["strict", "normal", "explore"] = "strict",
    required_facets: Iterable[str] = (),
    calibrator: PlattCalibrator | None = None,
    policy: SelectionPolicy | None = None,
    max_candidates: int | None = None,
) -> SelectionResult:
    """Calibrate, threshold, score-gap cut and diversify reranker raw scores.

    Each score must use the same raw-score convention as the supplied, digest-pinned
    calibrator. It must not be a provider score from hybrid/vector/FTS retrieval.
    """

    calibrator = calibrator or PlattCalibrator()
    policy = policy or SelectionPolicy()
    budget = QUERY_BUDGETS[complexity]
    if max_candidates is not None:
        if max_candidates < 1:
            raise ValueError("max_candidates must be positive")
        budget = replace(
            budget,
            final_max=min(budget.final_max, max_candidates),
            final_min=min(budget.final_min, max_candidates),
        )
    threshold = max(policy.candidate_floor, policy.threshold(evidence_mode))

    deduplicated: dict[tuple[str, str], RerankedCandidate] = {}
    for candidate, raw_score in candidates:
        relevance = calibrator.probability(raw_score)
        if relevance < threshold:
            continue
        document = candidate.logical_document_id or candidate.document_id or "unknown"
        key = (document, candidate.stable_content_hash())
        ranked = RerankedCandidate(candidate, float(raw_score), relevance)
        previous = deduplicated.get(key)
        if previous is None or relevance > previous.relevance:
            deduplicated[key] = ranked

    eligible = sorted(
        deduplicated.values(), key=lambda item: (-item.relevance, item.candidate.chunk_id)
    )
    if not eligible:
        return SelectionResult((), tuple(dict.fromkeys(required_facets)), "threshold", None, 0)

    required = tuple(dict.fromkeys(required_facets))
    reserved: list[RerankedCandidate] = []
    reserved_seen: set[tuple[str, str]] = set()
    for facet in required:
        match = next(
            (item for item in eligible if facet in item.candidate.facet_ids),
            None,
        )
        if match is None:
            continue
        identity = _candidate_identity(match.candidate)
        if identity not in reserved_seen:
            reserved.append(match)
            reserved_seen.add(identity)
    reserved_ids = {_candidate_identity(item.candidate) for item in reserved}
    window = list(reserved)
    window.extend(
        item for item in eligible if _candidate_identity(item.candidate) not in reserved_ids
    )
    window = window[: budget.final_max]
    window.sort(key=lambda item: (-item.relevance, item.candidate.chunk_id))
    target, cutoff_reason = _adaptive_target(window, budget, policy)

    selected: list[RerankedCandidate] = []
    selected_ids: set[tuple[str, str]] = set()
    for facet in required:
        match = next(
            (
                item
                for item in window
                if facet in item.candidate.facet_ids
                and _candidate_identity(item.candidate) not in selected_ids
            ),
            None,
        )
        if match is not None:
            selected.append(match)
            selected_ids.add(_candidate_identity(match.candidate))

    target = min(budget.final_max, max(target, len(selected)))
    while len(selected) < target:
        remaining = [
            item for item in window if _candidate_identity(item.candidate) not in selected_ids
        ]
        if not remaining:
            break
        covered = {facet for item in selected for facet in item.candidate.facet_ids}

        mmr = partial(
            _mmr_key,
            selected=tuple(selected),
            covered=frozenset(covered),
            policy=policy,
        )
        chosen = max(remaining, key=mmr)
        selected.append(chosen)
        selected_ids.add(_candidate_identity(chosen.candidate))

    selected.sort(key=lambda item: (-item.relevance, item.candidate.chunk_id))
    covered = {facet for item in selected for facet in item.candidate.facet_ids}
    missing = tuple(facet for facet in required if facet not in covered)
    cutoff = selected[-1].relevance if selected else None
    return SelectionResult(tuple(selected), missing, cutoff_reason, cutoff, len(eligible))


def _adaptive_target(
    candidates: Sequence[RerankedCandidate], budget: QueryBudget, policy: SelectionPolicy
) -> tuple[int, str]:
    available = min(len(candidates), budget.final_max)
    if available <= budget.final_min:
        return available, "available"

    best_gap = 0.0
    best_target: int | None = None
    for index in range(budget.final_min - 1, available - 1):
        gap = candidates[index].relevance - candidates[index + 1].relevance
        if gap >= policy.score_gap and gap > best_gap:
            best_gap = gap
            best_target = index + 1
    if best_target is not None:
        return best_target, "score_gap"

    floor = candidates[0].relevance - policy.top_delta
    within_delta = sum(item.relevance >= floor for item in candidates[:available])
    return max(budget.final_min, within_delta), "top_delta"


_WORD = re.compile(r"[^\W_]+(?:[-_/][^\W_]+)*", re.UNICODE)
_STOPWORDS = {
    "aber",
    "als",
    "auch",
    "das",
    "der",
    "die",
    "ein",
    "eine",
    "für",
    "ist",
    "mit",
    "oder",
    "sind",
    "und",
    "von",
    "was",
    "welche",
    "wie",
    "wird",
    "the",
    "and",
    "what",
    "which",
}

_NEGATION_TERMS = frozenset(
    {
        "kein",
        "keine",
        "keinem",
        "keinen",
        "keiner",
        "keines",
        "nie",
        "niemals",
        "nicht",
        "ohne",
        "no",
        "not",
        "never",
        "without",
    }
)
_ANTONYM_GROUPS: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (
        frozenset({"erlaubt", "gestattet", "zulässig", "allowed", "permitted"}),
        frozenset({"verboten", "unzulässig", "forbidden", "impermissible", "prohibited"}),
    ),
    (
        frozenset({"möglich", "possible"}),
        frozenset({"unmöglich", "impossible"}),
    ),
    (
        frozenset({"erhöht", "steigt", "zunimmt", "increases", "rises"}),
        frozenset({"fällt", "reduziert", "sinkt", "decreases", "drops"}),
    ),
    (
        frozenset({"größer", "höher", "oberhalb", "greater", "higher", "above"}),
        frozenset({"kleiner", "niedriger", "unterhalb", "less", "lower", "below"}),
    ),
    (
        frozenset({"positiv", "positive"}),
        frozenset({"negativ", "negative"}),
    ),
    (
        frozenset({"heiß", "warm", "hot"}),
        frozenset({"kalt", "cold"}),
    ),
    (
        frozenset({"offen", "open"}),
        frozenset({"geschlossen", "closed"}),
    ),
    (
        frozenset({"aktiv", "active"}),
        frozenset({"inaktiv", "inactive"}),
    ),
    (
        frozenset({"wahr", "true"}),
        frozenset({"falsch", "false"}),
    ),
)
_ANTONYM_TERMS = frozenset(term for group in _ANTONYM_GROUPS for side in group for term in side)
_CLAUSE_BOUNDARY = re.compile(
    r"(?i)(?<=[.!?;])\s+|\n+|\s+\b(?:und|and)\b\s+(?=(?:der|die|das|ein|eine|the)\b)"
)


def _search_terms(value: str) -> set[str]:
    return {
        token.casefold()
        for token in _WORD.findall(normalize_query(value))
        if (len(token) >= 2 or token.isupper() or token.isdigit())
        and token.casefold() not in _STOPWORDS
    }


def _semantic_contradiction(claim_text: str, evidence_text: str) -> bool:
    """Detect only high-precision lexical polarity conflicts.

    This is deliberately narrower than entailment: it catches an otherwise aligned
    proposition whose negation flips, or whose predicate uses an explicit antonym.
    """

    claim_clauses = _semantic_clauses(claim_text)
    evidence_clauses = _semantic_clauses(evidence_text)
    if not claim_clauses or not evidence_clauses:
        return False
    single_pair = len(claim_clauses) == len(evidence_clauses) == 1
    for claim_terms in claim_clauses:
        for evidence_terms in evidence_clauses:
            claim_core = claim_terms - _NEGATION_TERMS
            evidence_core = evidence_terms - _NEGATION_TERMS
            shared_core = claim_core & evidence_core
            required_overlap = min(2, len(claim_core), len(evidence_core))
            aligned = bool(shared_core) and len(shared_core) >= required_overlap
            if aligned and bool(claim_terms & _NEGATION_TERMS) != bool(
                evidence_terms & _NEGATION_TERMS
            ):
                return True

            for positive, negative in _ANTONYM_GROUPS:
                opposed = bool(claim_terms & positive and evidence_terms & negative) or bool(
                    claim_terms & negative and evidence_terms & positive
                )
                if not opposed:
                    continue
                claim_anchors = claim_core - _ANTONYM_TERMS
                evidence_anchors = evidence_core - _ANTONYM_TERMS
                if claim_anchors & evidence_anchors or (
                    single_pair and not claim_anchors and not evidence_anchors
                ):
                    return True
    return False


def _semantic_clauses(value: str) -> tuple[set[str], ...]:
    # "nicht nur" is additive, not a polarity reversal.
    normalized = re.sub(r"(?i)\b(?:nicht\s+nur|not\s+only)\b", "", normalize_query(value))
    return tuple(
        terms for clause in _CLAUSE_BOUNDARY.split(normalized) if (terms := _search_terms(clause))
    )


def _candidate_similarity(left: RetrievalCandidate, right: RetrievalCandidate) -> float:
    left_document = left.logical_document_id or left.document_id
    right_document = right.logical_document_id or right.document_id
    if (
        left_document == right_document
        and left.stable_content_hash() == right.stable_content_hash()
    ):
        return 1.0
    left_terms = _search_terms(left.content)
    right_terms = _search_terms(right.content)
    union = left_terms | right_terms
    lexical = len(left_terms & right_terms) / len(union) if union else 0.0
    if left.section_id and left.section_id == right.section_id:
        lexical = max(lexical, 0.35)
    elif left_document and left_document == right_document:
        lexical = max(lexical, 0.12)
    return lexical


def _candidate_identity(candidate: RetrievalCandidate) -> tuple[str, str]:
    return (
        candidate.logical_document_id or candidate.document_id or "unknown",
        candidate.chunk_id,
    )


def _mmr_key(
    item: RerankedCandidate,
    *,
    selected: Sequence[RerankedCandidate],
    covered: frozenset[str],
    policy: SelectionPolicy,
) -> tuple[float, float, str]:
    similarity = max(
        (_candidate_similarity(item.candidate, prior.candidate) for prior in selected),
        default=0.0,
    )
    uncovered = bool(set(item.candidate.facet_ids) - covered)
    value = (
        policy.mmr_lambda * item.relevance
        + (1.0 - policy.mmr_lambda) * (1.0 - similarity)
        + (0.05 if uncovered else 0.0)
    )
    return value, item.relevance, item.candidate.chunk_id


@dataclass(frozen=True, slots=True)
class EvidenceWindow:
    evidence_id: str
    chunk_id: str
    text: str
    char_start: int
    char_end: int
    content_hash: str
    pages: tuple[int, ...] = ()
    headings: tuple[str, ...] = ()
    facet_ids: tuple[str, ...] = ()

    @property
    def estimated_tokens(self) -> int:
        return max(1, math.ceil(len(self.text) / 4))


@dataclass(frozen=True, slots=True)
class _Span:
    start: int
    end: int
    score: float = 0.0


def extract_evidence_window(
    candidate: RetrievalCandidate,
    question: str,
    *,
    evidence_id: str = "E1",
    max_tokens: int = 160,
) -> EvidenceWindow:
    """Select one contiguous, byte-faithful window from an original chunk."""

    if max_tokens < 8:
        raise ValueError("max_tokens must be at least 8")
    content = candidate.content
    if not content:
        raise ValueError("candidate content must not be empty")
    max_chars = max_tokens * 4
    terms = _search_terms(question)

    table = _best_table_span(content, terms, max_chars)
    prose = _best_prose_span(content, terms, max_chars)
    span = table if table is not None and table.score >= prose.score else prose
    start, end = _trim_span(content, span.start, span.end, max_chars, terms)
    return EvidenceWindow(
        evidence_id=evidence_id,
        chunk_id=candidate.chunk_id,
        text=content[start:end],
        char_start=start,
        char_end=end,
        content_hash=candidate.stable_content_hash(),
        pages=candidate.pages,
        headings=candidate.headings,
        facet_ids=candidate.facet_ids,
    )


def pack_evidence_windows(
    candidates: Sequence[RetrievalCandidate],
    question: str,
    *,
    total_token_budget: int,
    per_window_tokens: int = 160,
) -> tuple[EvidenceWindow, ...]:
    if total_token_budget < 8:
        raise ValueError("total_token_budget must be at least 8")
    if per_window_tokens < 8:
        raise ValueError("per_window_tokens must be at least 8")
    windows: list[EvidenceWindow] = []
    remaining = total_token_budget
    for candidate in candidates:
        if remaining < 8:
            break
        window = extract_evidence_window(
            candidate,
            question,
            evidence_id=f"E{len(windows) + 1}",
            max_tokens=min(per_window_tokens, remaining),
        )
        if window.estimated_tokens > remaining:
            continue
        windows.append(window)
        remaining -= window.estimated_tokens
    return tuple(windows)


def _line_spans(content: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    offset = 0
    for line in content.splitlines(keepends=True):
        end = offset + len(line)
        spans.append((offset, end, line))
        offset = end
    if offset < len(content):
        spans.append((offset, len(content), content[offset:]))
    return spans


def _best_table_span(content: str, terms: set[str], max_chars: int) -> _Span | None:
    lines = _line_spans(content)
    groups: list[list[tuple[int, int, str]]] = []
    current: list[tuple[int, int, str]] = []
    for line in lines:
        if line[2].count("|") >= 2:
            current.append(line)
        elif current:
            if len(current) >= 2:
                groups.append(current)
            current = []
    if len(current) >= 2:
        groups.append(current)

    best: _Span | None = None
    for group in groups:
        separator = next(
            (
                index
                for index, (_, _, line) in enumerate(group)
                if re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", line)
            ),
            None,
        )
        data_start = separator + 1 if separator is not None else 0
        row_indexes = set(range(data_start, len(group))) or set(range(len(group)))
        scored_rows = [
            (_overlap_score(line, terms), index)
            for index, (_, _, line) in enumerate(group)
            if index in row_indexes
            if not re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", line)
        ]
        if not scored_rows:
            continue
        row_score, row_index = max(scored_rows)
        start_index = 0
        end_index = row_index
        while group[end_index][1] - group[start_index][0] > max_chars:
            start_index += 1
            if start_index >= end_index:
                break
        span = _Span(group[start_index][0], group[end_index][1], row_score + 0.25)
        if best is None or span.score > best.score:
            best = span
    return best


def _sentence_spans(content: str) -> list[_Span]:
    boundaries = [0]
    boundaries.extend(match.end() for match in re.finditer(r"(?<=[.!?])(?:\s+|\n+)", content))
    boundaries.append(len(content))
    spans: list[_Span] = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        start, end = _strip_bounds(content, start, end)
        if start < end:
            spans.append(_Span(start, end))
    return spans


def _best_prose_span(content: str, terms: set[str], max_chars: int) -> _Span:
    sentences = _sentence_spans(content) or [_Span(0, len(content))]
    scored = [
        replace(span, score=_overlap_score(content[span.start : span.end], terms))
        for span in sentences
    ]
    best_index = max(range(len(scored)), key=lambda index: scored[index].score)
    start_index = max(0, best_index - 1)
    end_index = min(len(scored) - 1, best_index + 1)
    while scored[end_index].end - scored[start_index].start > max_chars:
        if best_index - start_index > end_index - best_index:
            start_index += 1
        else:
            end_index -= 1
        if start_index == end_index == best_index:
            break
    return _Span(
        scored[start_index].start,
        scored[end_index].end,
        scored[best_index].score,
    )


def _overlap_score(value: str, terms: set[str]) -> float:
    value_terms = _search_terms(value)
    if not terms:
        return 0.0
    lexical = len(value_terms & terms) / len(terms)
    technical = len(extract_technical_literals(value) & extract_technical_literals(" ".join(terms)))
    return lexical + technical * 0.25


def _strip_bounds(content: str, start: int, end: int) -> tuple[int, int]:
    while start < end and content[start].isspace():
        start += 1
    while end > start and content[end - 1].isspace():
        end -= 1
    return start, end


def _trim_span(
    content: str, start: int, end: int, max_chars: int, terms: set[str]
) -> tuple[int, int]:
    start, end = _strip_bounds(content, start, end)
    if end - start <= max_chars:
        return start, end
    segment = content[start:end]
    anchors: list[int] = []
    for term in sorted(terms, key=len, reverse=True):
        match = re.search(rf"(?<!\w){re.escape(term)}(?!\w)", segment, re.IGNORECASE)
        if match is not None:
            anchors.append(start + match.start())
    focus = min(anchors) if anchors else start
    window_start = max(start, focus - max_chars // 3)
    window_end = min(end, window_start + max_chars)
    window_start = max(start, window_end - max_chars)
    if window_start > start:
        whitespace = content.find(" ", window_start, min(window_end, window_start + 40))
        if whitespace >= 0:
            window_start = whitespace + 1
    if window_end < end:
        whitespace = content.rfind(" ", max(window_start, window_end - 40), window_end)
        if whitespace > window_start:
            window_end = whitespace
    return _strip_bounds(content, window_start, window_end)


_TECHNICAL_LITERAL = re.compile(
    r"(?ix)"
    r"(?:\b(?:DIN|EN|ISO|IEC|VDI|ASTM)\s*[A-Z0-9][A-Z0-9 ./:+-]*\d\b)"
    r"|(?:(?<!\w)[+-]?\d+(?:[.,]\d+)?(?:e[+-]?\d+)?"
    r"(?:\s*(?:±|–|-|bis|to)\s*[+-]?\d+(?:[.,]\d+)?(?:e[+-]?\d+)?)?\s*"
    r"(?:%|‰|mm|cm|km|m|µm|nm|mg|kg|g|t|Pa|kPa|MPa|GPa|N|kN|W|kW|MW|V|A|Hz|K|°C|°)\b)"
    r"|(?:(?<!\w)[+-]?\d+(?:[.,]\d+)?(?:e[+-]?\d+)?\b)"
    r"|(?:\b[A-Z][A-Z0-9]*(?:[/.-][A-Z0-9]+)+\b)"
    r"|(?:\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+\b)"
    r"|(?:[α-ωΑ-Ω](?:_[A-Za-z0-9]+)?)"
)
_EVIDENCE_MARKER = re.compile(r"\[(?:E|C)\d+\]", re.IGNORECASE)


def extract_technical_literals(value: str) -> set[str]:
    value = _EVIDENCE_MARKER.sub("", unicodedata.normalize("NFKC", value))
    return {_normalize_literal(match.group(0)) for match in _TECHNICAL_LITERAL.finditer(value)}


def _normalize_literal(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


ClaimStatus = Literal["supported", "insufficient"]


@dataclass(frozen=True, slots=True)
class ClaimBlock:
    id: str
    text: str
    evidence_ids: tuple[str, ...]
    facet_id: str | None = None
    status: ClaimStatus = "supported"


class ClaimParseError(ValueError):
    pass


@dataclass(slots=True)
class ClaimBlockParser:
    max_buffer_chars: int = 64_000
    max_block_chars: int = 8_000
    _buffer: str = field(default="", init=False, repr=False)

    def feed(self, delta: str) -> list[ClaimBlock]:
        self._buffer += delta
        if len(self._buffer) > self.max_buffer_chars:
            raise ClaimParseError("claim stream exceeded its buffer limit")
        claims: list[ClaimBlock] = []
        while True:
            start = self._buffer.find("<claim>")
            if start < 0:
                self._buffer = self._buffer[-(len("<claim>") - 1) :]
                break
            if start:
                self._buffer = self._buffer[start:]
            end = self._buffer.find("</claim>", len("<claim>"))
            if end < 0:
                if len(self._buffer) > self.max_block_chars:
                    raise ClaimParseError("claim block exceeded its size limit")
                break
            raw = self._buffer[len("<claim>") : end]
            self._buffer = self._buffer[end + len("</claim>") :]
            if len(raw) > self.max_block_chars:
                raise ClaimParseError("claim block exceeded its size limit")
            claims.append(_parse_claim_json(raw))
        return claims

    def finish(self) -> None:
        if self._buffer.strip():
            raise ClaimParseError("claim stream ended with an incomplete block")


def _parse_claim_json(raw: str) -> ClaimBlock:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ClaimParseError("claim block is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ClaimParseError("claim block must contain a JSON object")
    allowed = {"id", "text", "evidence_ids", "facet_id", "status"}
    if unknown := set(payload) - allowed:
        raise ClaimParseError(f"claim block contains unknown fields: {sorted(unknown)}")
    claim_id = payload.get("id")
    text = payload.get("text")
    evidence_ids = payload.get("evidence_ids", [])
    facet_id = payload.get("facet_id")
    status = payload.get("status", "supported")
    if not isinstance(claim_id, str) or not re.fullmatch(r"C[1-9]\d*", claim_id):
        raise ClaimParseError("claim id must match C<number>")
    if not isinstance(text, str) or not text.strip() or len(text) > 2_000:
        raise ClaimParseError("claim text must contain 1..2000 characters")
    if (
        not isinstance(evidence_ids, list)
        or not all(
            isinstance(item, str) and re.fullmatch(r"E[1-9]\d*", item) for item in evidence_ids
        )
        or len(set(evidence_ids)) != len(evidence_ids)
    ):
        raise ClaimParseError("evidence_ids must contain unique E<number> identifiers")
    if facet_id is not None and (
        not isinstance(facet_id, str) or not re.fullmatch(r"F[1-9]\d*", facet_id)
    ):
        raise ClaimParseError("facet_id must match F<number>")
    if status not in {"supported", "insufficient"}:
        raise ClaimParseError("unsupported claim status")
    return ClaimBlock(claim_id, text.strip(), tuple(evidence_ids), facet_id, status)


@dataclass(frozen=True, slots=True)
class ClaimValidation:
    valid: bool
    errors: tuple[str, ...]
    technical_literals: tuple[str, ...]


def validate_claim(
    claim: ClaimBlock,
    evidence: Mapping[str, EvidenceWindow],
    *,
    allowed_facets: Iterable[str] | None = None,
    seen_claim_ids: Iterable[str] = (),
) -> ClaimValidation:
    errors: list[str] = []
    if claim.id in set(seen_claim_ids):
        errors.append("duplicate_claim_id")
    if allowed_facets is not None and claim.facet_id not in set(allowed_facets):
        errors.append("unknown_facet_id")
    unknown = [item for item in claim.evidence_ids if item not in evidence]
    if unknown:
        errors.append("unknown_evidence_id")

    literals = extract_technical_literals(claim.text)
    if claim.status == "supported":
        if not claim.evidence_ids:
            errors.append("missing_evidence")
        cited_text = "\n".join(
            evidence[item].text for item in claim.evidence_ids if item in evidence
        )
        supported = extract_technical_literals(cited_text)
        if not literals <= supported:
            errors.append("unsupported_technical_literal")
        if not unknown:
            if _semantic_contradiction(claim.text, cited_text):
                errors.append("semantic_contradiction")
            claim_terms = _search_terms(claim.text)
            evidence_terms = _search_terms(cited_text)
            semantic_terms = claim_terms - evidence_terms
            if claim_terms and (
                len(claim_terms & evidence_terms) < min(2, len(claim_terms))
                or len(semantic_terms) / len(claim_terms) > 0.45
            ):
                errors.append("unsupported_semantic_terms")
    elif claim.evidence_ids:
        errors.append("insufficient_claim_has_evidence")
    elif literals:
        errors.append("insufficient_claim_has_technical_literal")

    return ClaimValidation(not errors, tuple(dict.fromkeys(errors)), tuple(sorted(literals)))
