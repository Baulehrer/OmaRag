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
from typing import Literal, Protocol

from ..models.domain import BookMetadata


class QueryComplexity(StrEnum):
    SIMPLE = "simple"
    STANDARD = "standard"
    COMPLEX = "complex"


class EvidenceKind(StrEnum):
    """Typed evidence classes used by retrieval and answer selection."""

    PROSE = "prose"
    TABLE = "table"
    FORMULA = "formula"
    FIGURE = "figure"
    OCR = "ocr"
    NAVIGATION = "navigation"
    UNKNOWN = "unknown"


class ProvenanceKind(StrEnum):
    ELEMENT = "element"
    PAGE_FALLBACK = "page-fallback"
    SYNTHETIC = "synthetic"
    LEGACY = "legacy"
    UNKNOWN = "unknown"


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

RETRIEVAL_POLICY_DIGEST = "retrieval-v3-sparse-dense-book-2026.08.1"

# User-facing performance profiles deliberately keep query complexity adaptive.
# They change bounded work, not the semantic classification of the question and
# never imply a model or embedding swap.
PROFILE_QUERY_BUDGETS: Mapping[str, Mapping[QueryComplexity, QueryBudget]] = MappingProxyType(
    {
        "fast": MappingProxyType(
            {
                QueryComplexity.SIMPLE: QueryBudget(16, 1, 4, 1, 320, 256, 15_000),
                QueryComplexity.STANDARD: QueryBudget(28, 2, 6, 2, 800, 320, 15_000),
                QueryComplexity.COMPLEX: QueryBudget(48, 3, 10, 4, 1_600, 448, 15_000),
            }
        ),
        "normal": MappingProxyType(
            {
                QueryComplexity.SIMPLE: QueryBudget(24, 1, 5, 1, 420, 256, 15_000),
                QueryComplexity.STANDARD: QueryBudget(40, 2, 8, 2, 1_400, 384, 25_000),
                QueryComplexity.COMPLEX: QueryBudget(72, 4, 14, 4, 2_800, 512, 35_000),
            }
        ),
        "quality": MappingProxyType(
            {
                QueryComplexity.SIMPLE: QueryBudget(32, 2, 6, 1, 640, 320, 20_000),
                QueryComplexity.STANDARD: QueryBudget(56, 3, 10, 2, 2_200, 512, 30_000),
                QueryComplexity.COMPLEX: QueryBudget(96, 5, 18, 4, 4_500, 768, 35_000),
            }
        ),
    }
)


def canonical_performance_profile(value: str | None) -> str:
    """Map the v1.0 wire names onto the three v1.1 user-facing profiles."""

    normalized = (value or "normal").strip().casefold()
    return {
        "auto": "normal",
        "balanced": "normal",
        "deep": "quality",
        "fast": "fast",
        "normal": "normal",
        "quality": "quality",
    }.get(normalized, "normal")


def performance_budget(complexity: QueryComplexity, profile: str | None) -> QueryBudget:
    return PROFILE_QUERY_BUDGETS[canonical_performance_profile(profile)][complexity]


def performance_context_tokens(complexity: QueryComplexity, profile: str | None) -> int:
    """Adaptive generator context target, independent of model capacity ceiling."""

    targets = {
        "fast": {
            QueryComplexity.SIMPLE: 4096,
            QueryComplexity.STANDARD: 6144,
            QueryComplexity.COMPLEX: 8192,
        },
        "normal": {
            QueryComplexity.SIMPLE: 6144,
            QueryComplexity.STANDARD: 8192,
            QueryComplexity.COMPLEX: 12288,
        },
        "quality": {
            QueryComplexity.SIMPLE: 8192,
            QueryComplexity.STANDARD: 16384,
            QueryComplexity.COMPLEX: 24576,
        },
    }
    return targets[canonical_performance_profile(profile)][complexity]


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


# The frame a German question is wrapped in: an interrogative, an optional
# auxiliary, an optional article. None of it says which page holds the answer.
_INTERROGATIVE_FRAME = re.compile(
    r"""(?ix)^\s*
    (?:was|wer|wen|wem|wie|wo|wohin|woher|wann|warum|weshalb|wieso|wodurch|womit|wofür
       |welche[rsnm]?|what|which|how|where|when|why)
    \b\s*
    (?:ist|sind|war|waren|wird|werden|bedeutet|heißt|nennt\s+man|versteht\s+man\s+unter
       |gibt\s+es|macht|kann|können|muss|müssen|darf|dürfen|is|are|does|do)?
    \s*
    (?:\b(?:der|die|das|den|dem|des|ein|eine|einen|einem|einer|eines|the|a|an)\b)?
    \s*
    """
)
# ... and the tail some of them end with, which is frame too.
_INTERROGATIVE_TAIL = re.compile(
    r"(?i)\s*\b(?:gibt\s+es|gibt'?s|kennt\s+man|unterscheidet\s+man|are\s+there)\s*\??\s*$"
)


def retrieval_query(question: str) -> str:
    """The part of a question that names what to look for.

    A cross-encoder scores the query against a passage as one string, so the
    interrogative frame competes with the subject for the match. For "Was ist
    das Ausbreitmaß?" a passage opening "Das erhitzte Kältemittel wird nun an
    den Heizkreislauf ..." scored 9.91 against 9.43 for the page that defines
    the term — the frame matched, the subject did not — and the calibrated
    threshold then discarded the lot. Asked as "Ausbreitmaß", the same index
    put the right page first with a wide margin.

    Only a leading frame and a trailing auxiliary are removed, and only when
    something substantive is left; otherwise the question is returned as it
    came.
    """
    head, separator, tail = question.partition("\n")
    stripped = _INTERROGATIVE_TAIL.sub("", _INTERROGATIVE_FRAME.sub("", head)).strip(" \t?!.,;:")
    # Two characters is not a subject; better the whole question than a stub.
    if len(stripped) < 3 or not any(character.isalnum() for character in stripped):
        return question
    return stripped + separator + tail


def _deterministic_facets(
    question: str, complexity: QueryComplexity, max_facets: int
) -> tuple[QueryFacet, ...]:
    if complexity is QueryComplexity.SIMPLE:
        # Deliberately the whole question. Cutting it down to the subject was
        # measured and made retrieval worse, not better: asked for
        # "Ausbreitmaß" alone the sparse and dense channels stopped returning
        # the page that defines it at all, while the full question still had it
        # among the candidates. The frame hurts the cross-encoder, not the
        # index — see `retrieval_query` and where it is actually used.
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
    return tuple(
        QueryFacet(
            f"F{index}",
            query,
            required=not (
                complexity is QueryComplexity.COMPLEX and query.casefold() == question.casefold()
            ),
        )
        for index, query in enumerate(unique, 1)
    )


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
    generation_id: str | None = None
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
    evidence_kind: EvidenceKind = EvidenceKind.UNKNOWN
    provenance_kind: ProvenanceKind = ProvenanceKind.UNKNOWN
    quality_flags: tuple[str, ...] = ()

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
    """Digest-bound logistic calibration for one reranker score convention.

    ``scale=1,bias=0`` was used by early query-v2 builds as a convenient
    placeholder.  It is intentionally no longer the default: production
    callers must use a profile fitted from labelled pairs and bound to both
    model and calibration-set digests.  The legacy constructor remains valid
    for callers that explicitly provide a digest (including old tests and
    compatibility integrations).
    """

    scale: float
    bias: float
    digest: str
    model_digest: str | None = None
    dataset_digest: str | None = None
    policy_digest: str | None = None
    version: str = "platt-v2"

    def __post_init__(self) -> None:
        if not math.isfinite(self.scale) or not math.isfinite(self.bias):
            raise ValueError("calibrator parameters must be finite")
        if not self.digest:
            raise ValueError("calibrator digest must not be empty")
        if not self.version:
            raise ValueError("calibrator version must not be empty")

    @classmethod
    def fit(
        cls,
        *,
        model_digest: str,
        dataset_digest: str,
        policy_digest: str,
        scores: Sequence[float],
        labels: Sequence[int | bool],
        iterations: int = 250,
    ) -> PlattCalibrator:
        """Fit a deterministic Platt model from labelled reranker pairs.

        The small gradient solver avoids adding a numerical dependency to the
        query worker.  Both classes are required and all values are checked;
        a profile cannot accidentally be emitted as an unbound identity map.
        """

        if not model_digest or not dataset_digest or not policy_digest:
            raise ValueError("model, dataset and retrieval-policy digests are required")
        if len(scores) != len(labels) or len(scores) < 4:
            raise ValueError("calibration requires at least four score/label pairs")
        if not {int(label) for label in labels} >= {0, 1}:
            raise ValueError("calibration labels must contain both classes")
        if iterations < 1 or iterations > 10_000:
            raise ValueError("iterations must be between 1 and 10000")
        values = [float(score) for score in scores]
        targets = [int(label) for label in labels]
        if not all(math.isfinite(score) for score in values):
            raise ValueError("calibration scores must be finite")

        scale, bias = 1.0, 0.0
        # A conservative learning rate plus mild L2 regularization keeps the
        # fit stable for tiny per-workspace gold sets.
        learning_rate = 0.08 / max(1.0, math.sqrt(len(values)))
        for _ in range(iterations):
            gradient_scale = 0.0
            gradient_bias = 0.0
            for score, target in zip(values, targets, strict=True):
                probability = _sigmoid(scale * score + bias)
                error = probability - target
                gradient_scale += error * score
                gradient_bias += error
            gradient_scale = gradient_scale / len(values) + 0.01 * scale
            gradient_bias /= len(values)
            scale -= learning_rate * gradient_scale
            bias -= learning_rate * gradient_bias
            scale = max(0.01, min(scale, 100.0))
            bias = max(-100.0, min(bias, 100.0))
        digest = hashlib.sha256(
            json.dumps(
                {
                    "version": "platt-v2",
                    "model_digest": model_digest,
                    "dataset_digest": dataset_digest,
                    "policy_digest": policy_digest,
                    "scale": round(scale, 12),
                    "bias": round(bias, 12),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return cls(
            scale=scale,
            bias=bias,
            digest=digest,
            model_digest=model_digest,
            dataset_digest=dataset_digest,
            policy_digest=policy_digest,
        )

    @property
    def bound(self) -> bool:
        return bool(self.model_digest and self.dataset_digest and self.policy_digest)

    def probability(self, score: float) -> float:
        if not math.isfinite(score):
            raise ValueError("reranker score must be finite")
        return _sigmoid(self.scale * float(score) + self.bias)


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-min(value, 709.0)))
    exp_value = math.exp(max(value, -709.0))
    return exp_value / (1.0 + exp_value)


# This bootstrap profile keeps V1.1 workspaces usable until the owner imports a
# reviewed private gold set. It is deliberately labelled as bootstrap in every
# receipt and is not a V1.2 release-quality calibration claim.
DEFAULT_RERANKER_CALIBRATOR = PlattCalibrator(
    scale=1.35,
    bias=-0.55,
    digest="bundled-bootstrap-platt-v2-mmarco-2026.08.1",
    model_digest="e99c92466f2a65f1d63e4529027613cdb62ca8f69071b278e0aa542134e204d5",
    dataset_digest="bootstrap-silver-v1-not-release-gold",
    policy_digest=RETRIEVAL_POLICY_DIGEST,
)


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
    reranker_digest: str | None = None,
    policy: SelectionPolicy | None = None,
    max_candidates: int | None = None,
    budget: QueryBudget | None = None,
) -> SelectionResult:
    """Calibrate, threshold, score-gap cut and diversify reranker raw scores.

    Each score must use the same raw-score convention as the supplied, digest-pinned
    calibrator. It must not be a provider score from hybrid/vector/FTS retrieval.
    """

    calibrator = calibrator or DEFAULT_RERANKER_CALIBRATOR
    if reranker_digest is not None and calibrator.model_digest != reranker_digest:
        return SelectionResult(
            (),
            tuple(dict.fromkeys(required_facets)),
            "calibration_mismatch",
            None,
            0,
        )
    if reranker_digest is not None and calibrator.policy_digest != RETRIEVAL_POLICY_DIGEST:
        return SelectionResult(
            (),
            tuple(dict.fromkeys(required_facets)),
            "calibration_mismatch",
            None,
            0,
        )
    policy = policy or SelectionPolicy()
    budget = budget or QUERY_BUDGETS[complexity]
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
    evidence_kind: EvidenceKind = EvidenceKind.UNKNOWN
    provenance_kind: ProvenanceKind = ProvenanceKind.UNKNOWN
    quality_flags: tuple[str, ...] = ()

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
    formula = _best_formula_span(content, terms, max_chars)
    prose = _best_prose_span(content, terms, max_chars)
    if candidate.evidence_kind is EvidenceKind.TABLE and table is not None:
        span = table
    elif candidate.evidence_kind is EvidenceKind.FORMULA and formula is not None:
        span = formula
    else:
        alternatives = [prose, *(item for item in (table, formula) if item is not None)]
        span = max(alternatives, key=lambda item: item.score)
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
        evidence_kind=candidate.evidence_kind,
        provenance_kind=candidate.provenance_kind,
        quality_flags=candidate.quality_flags,
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
        # Navigation chunks are routing aids, never answer evidence. Unknown
        # is retained for V1.1 indexes and upgraded after a full reindex.
        if (
            candidate.evidence_kind is EvidenceKind.NAVIGATION
            or candidate.provenance_kind is ProvenanceKind.SYNTHETIC
        ):
            continue
        if remaining < 8:
            break
        kind_multiplier = (
            1.5
            if candidate.evidence_kind is EvidenceKind.TABLE
            else 1.25
            if candidate.evidence_kind is EvidenceKind.FORMULA
            else 1.0
        )
        window = extract_evidence_window(
            candidate,
            question,
            evidence_id=f"E{len(windows) + 1}",
            max_tokens=min(max(8, round(per_window_tokens * kind_multiplier)), remaining),
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
        start = group[start_index][0]
        if start_index == 0:
            group_line = next(
                (index for index, line in enumerate(lines) if line[0] == group[0][0]),
                None,
            )
            if group_line is not None and group_line > 0:
                caption_start, _caption_end, caption = lines[group_line - 1]
                if caption.strip() and group[end_index][1] - caption_start <= max_chars:
                    start = caption_start
        span = _Span(start, group[end_index][1], row_score + 0.25)
        if best is None or span.score > best.score:
            best = span
    return best


def _best_formula_span(content: str, terms: set[str], max_chars: int) -> _Span | None:
    lines = _line_spans(content)
    candidates = [
        (index, line)
        for index, line in enumerate(lines)
        if re.search(r"(?:=|≤|≥|≈|∑|√|[α-ωΑ-Ω])", line[2])
    ]
    if not candidates:
        return None
    best_index, best_line = max(
        candidates,
        key=lambda item: (
            _overlap_score(item[1][2], terms),
            len(extract_technical_literals(item[1][2])),
            -item[0],
        ),
    )
    start_index = max(0, best_index - 2)
    end_index = min(len(lines) - 1, best_index + 2)
    while lines[end_index][1] - lines[start_index][0] > max_chars:
        if best_index - start_index >= end_index - best_index and start_index < best_index:
            start_index += 1
        elif end_index > best_index:
            end_index -= 1
        else:
            break
    return _Span(
        lines[start_index][0],
        lines[end_index][1],
        _overlap_score(best_line[2], terms) + 0.2,
    )


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

    def draft_text(self) -> str:
        """Human-readable prose from the block still being received.

        The model streams JSON inside `<claim>` tags, so the raw token stream is
        unreadable. This decodes the `text` field as far as it has arrived, which
        is what lets the interface show something the moment the model starts
        writing instead of waiting for a whole validated claim.

        Returns an empty string until the field begins.
        """
        return _partial_text_field(self._buffer)

    def finish(self) -> None:
        if self._buffer.strip():
            raise ClaimParseError("claim stream ended with an incomplete block")


def _partial_text_field(buffer: str) -> str:
    """Decode the `"text"` value of a partially received claim block.

    Stops at the closing quote, or at the end of what has arrived. Trailing
    incomplete escapes are dropped rather than shown as backslashes.
    """
    marker = buffer.find('"text"')
    if marker < 0:
        return ""
    cursor = buffer.find(":", marker + len('"text"'))
    if cursor < 0:
        return ""
    cursor += 1
    while cursor < len(buffer) and buffer[cursor] in " \t\r\n":
        cursor += 1
    if cursor >= len(buffer) or buffer[cursor] != '"':
        return ""
    cursor += 1

    out: list[str] = []
    while cursor < len(buffer):
        char = buffer[cursor]
        if char == '"':
            break
        if char != "\\":
            out.append(char)
            cursor += 1
            continue
        # An escape that has not fully arrived yet contributes nothing.
        if cursor + 1 >= len(buffer):
            break
        code = buffer[cursor + 1]
        simple = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if code in simple:
            out.append(simple[code])
            cursor += 2
            continue
        if code == "u":
            if cursor + 6 > len(buffer):
                break
            try:
                out.append(chr(int(buffer[cursor + 2 : cursor + 6], 16)))
            except ValueError:
                return "".join(out)
            cursor += 6
            continue
        return "".join(out)
    return "".join(out)


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
class SupportSpan:
    evidence_id: str
    start: int
    end: int
    kind: str = "literal"


@dataclass(frozen=True, slots=True)
class ClaimValidation:
    valid: bool
    errors: tuple[str, ...]
    technical_literals: tuple[str, ...]
    support_spans: tuple[SupportSpan, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimVerification:
    verdict: Literal["entailed", "contradicted", "unknown"]
    reason: str = ""
    score: float | None = None


class ClaimVerifier(Protocol):
    async def verify(
        self, claim: ClaimBlock, evidence: Sequence[EvidenceWindow]
    ) -> ClaimVerification: ...


@dataclass(frozen=True, slots=True)
class SelectiveVerifierPolicy:
    """Trigger policy for a local verifier; never broadens evidence access."""

    max_claims: int = 2
    evidence_kinds: tuple[EvidenceKind, ...] = (
        EvidenceKind.TABLE,
        EvidenceKind.FORMULA,
    )

    def __post_init__(self) -> None:
        if not 1 <= self.max_claims <= 8:
            raise ValueError("max_claims must be between 1 and 8")

    def should_verify(self, claim: ClaimBlock, evidence: Sequence[EvidenceWindow]) -> bool:
        if claim.status != "supported":
            return False
        if set(self.evidence_kinds) & {item.evidence_kind for item in evidence}:
            return True
        if extract_technical_literals(claim.text) or len(evidence) > 1:
            return True
        terms = _search_terms(claim.text)
        if terms & _NEGATION_TERMS:
            return True
        return bool(
            _COMPARISON_SIGNAL.search(claim.text)
            or _MULTIHOP_SIGNAL.search(claim.text)
            or re.search(
                r"(?i)\b(?:mindestens|höchstens|weniger als|mehr als|über|unter|"
                r"at least|at most|less than|more than|above|below)\b",
                claim.text,
            )
        )

    @staticmethod
    def fail_closed(verifier: ClaimVerifier | None) -> ClaimVerification:
        if verifier is None:
            return ClaimVerification("unknown", "verifier-unavailable")
        return ClaimVerification("unknown", "verifier-error")


@dataclass(frozen=True, slots=True)
class ProgressiveRetrievalPolicy:
    minimum_relevance: float = 0.62
    minimum_margin: float = 0.08

    def __post_init__(self) -> None:
        if not 0.0 <= self.minimum_relevance <= 1.0:
            raise ValueError("minimum_relevance must be between 0 and 1")
        if not 0.0 <= self.minimum_margin <= 1.0:
            raise ValueError("minimum_margin must be between 0 and 1")

    def should_escalate(
        self,
        *,
        selected_count: int,
        missing_facets: Sequence[str],
        top_relevance: float | None,
        second_relevance: float | None,
        optional_facets_available: bool,
        missing_evidence_requirements: Sequence[str] = (),
    ) -> bool:
        if not optional_facets_available:
            return False
        if (
            missing_facets
            or missing_evidence_requirements
            or selected_count < 1
            or top_relevance is None
        ):
            return True
        if top_relevance < self.minimum_relevance:
            return True
        return second_relevance is not None and (
            top_relevance - second_relevance < self.minimum_margin
        )

    @staticmethod
    def missing_evidence_requirements(
        query: str,
        selected: Sequence[RerankedCandidate],
    ) -> tuple[str, ...]:
        """Detect query-explicit evidence kinds absent from Stage A."""

        query_terms = _search_terms(query)
        wants_table = bool(
            query_terms
            & {
                "tabelle",
                "tabellarisch",
                "zeile",
                "spalte",
                "matrix",
                "table",
                "row",
                "column",
            }
        )
        wants_formula = bool(
            query_terms
            & {
                "formel",
                "gleichung",
                "variable",
                "symbol",
                "berechne",
                "formula",
                "equation",
                "calculate",
            }
        )
        wants_negation = bool(query_terms & _NEGATION_TERMS)
        kinds = {item.candidate.evidence_kind for item in selected}
        contents = "\n".join(item.candidate.content for item in selected)
        content_terms = _search_terms(contents)
        has_table = EvidenceKind.TABLE in kinds or _best_table_span(contents, query_terms, 512)
        has_formula = EvidenceKind.FORMULA in kinds or _best_formula_span(
            contents, query_terms, 512
        )
        missing: list[str] = []
        if wants_table and not has_table:
            missing.append("table-evidence")
        if wants_formula and not has_formula:
            missing.append("formula-evidence")
        if wants_negation and not (content_terms & _NEGATION_TERMS):
            missing.append("negation-evidence")
        return tuple(missing)


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
    support_spans: list[SupportSpan] = []
    if claim.status == "supported":
        if not claim.evidence_ids:
            errors.append("missing_evidence")
        cited_text = "\n".join(
            evidence[item].text for item in claim.evidence_ids if item in evidence
        )
        supported = extract_technical_literals(cited_text)
        if not literals <= supported:
            errors.append("unsupported_technical_literal")
        for evidence_id in claim.evidence_ids:
            window = evidence.get(evidence_id)
            if window is None:
                continue
            for literal in literals:
                match = next(
                    (
                        item
                        for item in _TECHNICAL_LITERAL.finditer(window.text)
                        if _normalize_literal(item.group(0)) == literal
                    ),
                    None,
                )
                if match is not None:
                    support_spans.append(SupportSpan(evidence_id, match.start(), match.end()))
        if not support_spans:
            lexical = _best_claim_support_span(claim.text, evidence, claim.evidence_ids)
            if lexical is not None:
                support_spans.append(lexical)
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

    return ClaimValidation(
        not errors,
        tuple(dict.fromkeys(errors)),
        tuple(sorted(literals)),
        tuple(support_spans),
    )


def _best_claim_support_span(
    claim_text: str,
    evidence: Mapping[str, EvidenceWindow],
    evidence_ids: Sequence[str],
) -> SupportSpan | None:
    """Return the strongest exact sentence span without manufacturing text."""

    claim_terms = _search_terms(claim_text)
    if not claim_terms:
        return None
    best: tuple[int, float, str, int, int] | None = None
    for evidence_id in evidence_ids:
        window = evidence.get(evidence_id)
        if window is None:
            continue
        for span in _sentence_spans(window.text) or [_Span(0, len(window.text))]:
            terms = _search_terms(window.text[span.start : span.end])
            overlap = len(claim_terms & terms)
            ratio = overlap / len(claim_terms)
            candidate = (overlap, ratio, evidence_id, span.start, span.end)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        return None
    overlap, _ratio, evidence_id, start, end = best
    if overlap < min(2, len(claim_terms)):
        return None
    return SupportSpan(evidence_id, start, end, "semantic")
