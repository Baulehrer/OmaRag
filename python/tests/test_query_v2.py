from __future__ import annotations

import math
from dataclasses import replace

import pytest

from omarag_bridge.services.query_v2 import (
    ClaimBlock,
    ClaimBlockParser,
    ClaimParseError,
    ClaimVerification,
    EvidenceKind,
    PlattCalibrator,
    ProgressiveRetrievalPolicy,
    ProvenanceKind,
    QueryComplexity,
    RerankedCandidate,
    RetrievalCandidate,
    SelectiveVerifierPolicy,
    adaptive_select,
    canonical_performance_profile,
    classify_query,
    extract_evidence_window,
    pack_evidence_windows,
    performance_budget,
    performance_context_tokens,
    validate_claim,
    weighted_rrf,
)


def candidate(
    chunk_id: str,
    content: str,
    *,
    document: str = "book-1",
    section: str | None = None,
    facets: tuple[str, ...] = (),
) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk_id=chunk_id,
        content=content,
        logical_document_id=document,
        section_id=section,
        pages=(7,),
        headings=("Kapitel",),
        facet_ids=facets,
    )


def test_classifier_assigns_fixed_budgets_and_bounded_facets() -> None:
    direct = classify_query("Was ist der Elastizitätsmodul?")
    assert direct.complexity is QueryComplexity.SIMPLE
    assert direct.score == -1
    assert direct.budget.candidate_cap == 24
    assert [facet.query for facet in direct.facets] == [direct.question]

    comparison = classify_query(
        "Vergleiche Beton und Stahl und erkläre, wie die Wahl die Tragfähigkeit beeinflusst."
    )
    assert comparison.complexity is QueryComplexity.COMPLEX
    assert comparison.budget.final_max == 14
    assert 2 <= len(comparison.facets) <= 4
    assert any("Beton" in facet.query for facet in comparison.facets)
    assert any("Stahl" in facet.query for facet in comparison.facets)


def test_v11_performance_profiles_keep_complexity_but_change_bounded_work() -> None:
    assert canonical_performance_profile("balanced") == "normal"
    assert canonical_performance_profile("deep") == "quality"
    assert canonical_performance_profile("auto") == "normal"

    assert performance_budget(QueryComplexity.SIMPLE, "fast").candidate_cap == 16
    assert performance_budget(QueryComplexity.STANDARD, "normal").final_max == 8
    quality = performance_budget(QueryComplexity.COMPLEX, "quality")
    assert quality.candidate_cap == 96
    assert quality.final_min == 5
    assert quality.final_max == 18
    assert quality.evidence_tokens == 4_500
    assert performance_context_tokens(QueryComplexity.SIMPLE, "fast") == 4_096
    assert performance_context_tokens(QueryComplexity.STANDARD, "normal") == 8_192
    assert performance_context_tokens(QueryComplexity.COMPLEX, "quality") == 24_576


def test_progressive_policy_escalates_for_missing_explicit_evidence_kind() -> None:
    policy = ProgressiveRetrievalPolicy()
    prose = RerankedCandidate(candidate("prose", "Der Wert wird erläutert."), 2.0, 0.88)
    missing = policy.missing_evidence_requirements(
        "Welcher Wert steht in der Tabelle?",
        (prose,),
    )
    assert missing == ("table-evidence",)
    assert policy.should_escalate(
        selected_count=1,
        missing_facets=(),
        top_relevance=0.88,
        second_relevance=None,
        optional_facets_available=True,
        missing_evidence_requirements=missing,
    )

    table_candidate = replace(
        candidate("table", "| Kennwert | 42 |\n|---|---|"),
        evidence_kind=EvidenceKind.TABLE,
    )
    table = RerankedCandidate(table_candidate, 2.1, 0.89)
    assert (
        policy.missing_evidence_requirements("Welcher Wert steht in der Tabelle?", (table,)) == ()
    )


def test_classifier_accounts_for_discovered_session_and_corpus_scope() -> None:
    plan = classify_query(
        "Und wie gilt das dort?",
        has_session_reference=True,
        register_entity_count=2,
        edition_scope_count=2,
    )
    assert plan.complexity is QueryComplexity.COMPLEX
    assert {"session_reference", "multiple_entities", "multiple_editions"} <= set(plan.reasons)


def test_classifier_keeps_two_direct_subquestions_out_of_simple_path() -> None:
    plan = classify_query("Was ist Kriechen und wie wird Kriechen berücksichtigt?")
    assert plan.complexity is QueryComplexity.STANDARD
    assert len(plan.facets) == 2


def test_weighted_rrf_deduplicates_each_path_and_merges_facets() -> None:
    first = candidate("a", "Alpha", facets=("F1",))
    second_view = candidate("a", "Alpha", facets=("F2",))
    other = candidate("b", "Beta")
    fused = weighted_rrf(
        {"hybrid": [first, first, other], "register": [second_view]},
        {"hybrid": 1.0, "register": 1.2},
    )
    assert [item.candidate.chunk_id for item in fused] == ["a", "b"]
    assert fused[0].candidate.facet_ids == ("F1", "F2")
    assert fused[0].fused_score == pytest.approx(2.2 / 61)
    assert fused[0].ranks == (("hybrid", 1), ("register", 1))
    assert fused[1].ranks == (("hybrid", 2),)
    with pytest.raises(ValueError, match="finite"):
        weighted_rrf({"hybrid": [first]}, {"hybrid": float("nan")})


def test_weighted_rrf_uses_route_specific_defaults() -> None:
    hybrid = candidate("hybrid", "Semantischer Treffer")
    exact = candidate("exact", "Exakter Registerbegriff")
    fused = weighted_rrf({"hybrid": [hybrid], "fts": [exact]})
    assert [item.candidate.chunk_id for item in fused] == ["exact", "hybrid"]


def test_adaptive_selection_uses_calibration_gap_facets_and_exact_dedup() -> None:
    calibrator = PlattCalibrator(scale=1.0, bias=0.0, digest="test")
    rows = [
        (candidate("a", "Alpha Nachweis 42 mm.", facets=("F1",)), 3.0),
        (candidate("a-copy", "Alpha Nachweis 42 mm.", facets=("F1",)), 2.9),
        (candidate("b", "Beta Nachweis 51 mm.", facets=("F2",)), 2.0),
        (candidate("c", "Noch ein ausreichend relevanter Treffer."), 0.6),
        (candidate("d", "Sehr schwacher Treffer."), -1.0),
    ]
    result = adaptive_select(
        rows,
        complexity=QueryComplexity.STANDARD,
        required_facets=("F1", "F2", "F3"),
        calibrator=calibrator,
    )
    assert [item.candidate.chunk_id for item in result.selected] == ["a", "b"]
    assert result.missing_facets == ("F3",)
    assert result.cutoff_reason == "score_gap"
    assert result.eligible_count == 3
    assert result.cutoff_score == pytest.approx(1 / (1 + math.exp(-2)))


def test_platt_fit_is_empirical_and_digest_bound() -> None:
    calibrator = PlattCalibrator.fit(
        model_digest="reranker-sha256",
        dataset_digest="gold-v1",
        policy_digest="retrieval-v3-sparse-dense-book-2026.08.1",
        scores=(-3.0, -1.0, 0.0, 1.0, 3.0, 4.0),
        labels=(0, 0, 0, 1, 1, 1),
    )

    assert calibrator.model_digest == "reranker-sha256"
    assert calibrator.dataset_digest == "gold-v1"
    assert calibrator.policy_digest == "retrieval-v3-sparse-dense-book-2026.08.1"
    assert calibrator.digest not in {"identity-logit-v1", "test"}
    assert calibrator.scale > 0
    assert calibrator.probability(-3.0) < 0.5
    assert calibrator.probability(3.0) > 0.5
    assert calibrator.probability(3.0) > calibrator.probability(1.0)


def test_selection_fails_closed_on_calibrator_digest_mismatch() -> None:
    calibrator = PlattCalibrator.fit(
        model_digest="reranker-a",
        dataset_digest="gold-v1",
        policy_digest="retrieval-v3-sparse-dense-book-2026.08.1",
        scores=(-2.0, -1.0, 1.0, 2.0),
        labels=(0, 0, 1, 1),
    )
    result = adaptive_select(
        [(candidate("a", "Beleg"), 2.0)],
        complexity=QueryComplexity.SIMPLE,
        calibrator=calibrator,
        reranker_digest="reranker-b",
    )

    assert result.selected == ()
    assert result.cutoff_reason == "calibration_mismatch"


def test_typed_evidence_packing_skips_navigation_and_returns_support_spans() -> None:
    navigation = candidate("nav", "Inhaltsverzeichnis: Grenzwert", facets=("F1",))
    navigation = replace(navigation, evidence_kind=EvidenceKind.NAVIGATION)
    table = candidate(
        "table",
        "Tabelle 4: Grenzwerte in mm\n| Klasse | Grenzwert |\n|---|---|\n| A | 42 mm |",
        facets=("F1",),
    )
    table = replace(table, evidence_kind=EvidenceKind.TABLE)

    windows = pack_evidence_windows(
        [navigation, table],
        "Grenzwert Klasse A",
        total_token_budget=64,
        per_window_tokens=32,
    )
    assert [window.chunk_id for window in windows] == ["table"]
    assert windows[0].evidence_kind is EvidenceKind.TABLE
    assert windows[0].text.startswith("Tabelle 4")
    assert "| Klasse | Grenzwert |" in windows[0].text

    validation = validate_claim(
        ClaimBlock("C1", "Der Grenzwert beträgt 42 mm.", ("E1",), "F1"),
        {"E1": windows[0]},
        allowed_facets=("F1",),
    )
    assert validation.valid
    assert validation.support_spans
    assert validation.support_spans[0].evidence_id == "E1"


def test_formula_window_keeps_adjacent_variable_definition_and_drops_synthetic() -> None:
    synthetic = replace(
        candidate("summary", "Generierte Zusammenfassung"),
        provenance_kind=ProvenanceKind.SYNTHETIC,
    )
    formula = replace(
        candidate(
            "formula",
            "Dabei ist E der Elastizitätsmodul.\nσ = E · ε\nε bezeichnet die Dehnung.",
        ),
        evidence_kind=EvidenceKind.FORMULA,
        provenance_kind=ProvenanceKind.ELEMENT,
    )

    windows = pack_evidence_windows(
        [synthetic, formula],
        "Wie lautet die Formel für Spannung und Dehnung?",
        total_token_budget=80,
        per_window_tokens=48,
    )

    assert [window.chunk_id for window in windows] == ["formula"]
    assert "Elastizitätsmodul" in windows[0].text
    assert "σ = E · ε" in windows[0].text
    assert "Dehnung" in windows[0].text


def test_progressive_policy_escalates_only_for_missing_facets_or_low_margin() -> None:
    policy = ProgressiveRetrievalPolicy()
    assert (
        policy.should_escalate(
            selected_count=2,
            missing_facets=(),
            top_relevance=0.91,
            second_relevance=0.70,
            optional_facets_available=True,
        )
        is False
    )
    assert (
        policy.should_escalate(
            selected_count=2,
            missing_facets=("F2",),
            top_relevance=0.91,
            second_relevance=0.70,
            optional_facets_available=True,
        )
        is True
    )
    assert (
        policy.should_escalate(
            selected_count=1,
            missing_facets=(),
            top_relevance=0.61,
            second_relevance=0.59,
            optional_facets_available=True,
        )
        is True
    )


def test_selective_verifier_policy_is_typed_and_fail_closed() -> None:
    policy = SelectiveVerifierPolicy()
    evidence = extract_evidence_window(
        candidate("formula", "f = m · a = 42 N"),
        "Formel",
        evidence_id="E1",
    )
    evidence = replace(evidence, evidence_kind=EvidenceKind.FORMULA)
    assert policy.should_verify(
        ClaimBlock("C1", "Die Formel ergibt 42 N.", ("E1",), "F1"),
        (evidence,),
    )
    assert policy.fail_closed(None) == ClaimVerification("unknown", "verifier-unavailable")


def test_adaptive_selection_never_fills_below_evidence_threshold() -> None:
    result = adaptive_select(
        [(candidate("weak", "Unpassend"), -5.0)],
        complexity=QueryComplexity.COMPLEX,
        required_facets=("F1",),
    )
    assert result.selected == ()
    assert result.missing_facets == ("F1",)
    assert result.cutoff_reason == "threshold"
    with pytest.raises(ValueError, match="finite"):
        adaptive_select(
            [(candidate("bad", "Ungültiger Score"), float("nan"))],
            complexity=QueryComplexity.SIMPLE,
        )


def test_adaptive_selection_reserves_an_eligible_required_facet_beyond_global_cut() -> None:
    rows = [
        (candidate(f"f1-{index}", f"F1 Treffer {index}", facets=("F1",)), 4 - index * 0.1)
        for index in range(9)
    ]
    rows.append((candidate("f2", "Relevanter F2 Treffer", facets=("F2",)), 2.5))
    result = adaptive_select(
        rows,
        complexity=QueryComplexity.STANDARD,
        evidence_mode="strict",
        required_facets=("F1", "F2"),
    )
    assert "f2" in {item.candidate.chunk_id for item in result.selected}
    assert result.missing_facets == ()


def test_extractive_prose_window_is_an_exact_chunk_slice_with_neighbors() -> None:
    content = (
        "Die Vorbemerkung betrifft andere Baustoffe. "
        "Der Bemessungswert beträgt 42 mm. "
        "Dieser Wert gilt für Bauteilklasse B. "
        "Danach folgt ein anderes Thema."
    )
    window = extract_evidence_window(
        candidate("c1", content), "Welcher Bemessungswert gilt für Bauteilklasse B?"
    )
    assert window.text == content[window.char_start : window.char_end]
    assert "42 mm" in window.text
    assert "Bauteilklasse B" in window.text
    assert window.content_hash == candidate("c1", content).stable_content_hash()


def test_extractive_table_window_keeps_header_separator_and_matching_row() -> None:
    content = (
        "Messwerte:\n| Klasse | Grenzwert |\n|---|---|\n| A | 42 mm |\n| B | 51 mm |\nNachsatz."
    )
    window = extract_evidence_window(candidate("table", content), "Grenzwert Klasse B")
    assert window.text == content[window.char_start : window.char_end]
    assert "| Klasse | Grenzwert |" in window.text
    assert "| B | 51 mm |" in window.text


def test_evidence_packer_obeys_total_budget_and_numbers_ids() -> None:
    windows = pack_evidence_windows(
        [
            candidate("a", "Aussage Alpha. Der Wert ist 42 mm. Weiterer Satz."),
            candidate("b", "Aussage Beta. Der Wert ist 51 mm. Weiterer Satz."),
        ],
        "Welcher Wert?",
        total_token_budget=32,
        per_window_tokens=16,
    )
    assert [window.evidence_id for window in windows] == ["E1", "E2"]
    assert sum(window.estimated_tokens for window in windows) <= 32


def test_claim_parser_handles_fragmented_blocks_and_rejects_trailing_partial_data() -> None:
    parser = ClaimBlockParser()
    assert parser.feed("ignored<cla") == []
    claims = parser.feed(
        'im>{"id":"C1","text":"Der Wert beträgt 42 mm.",'
        '"evidence_ids":["E1"],"facet_id":"F1"}</claim>'
    )
    assert claims == [ClaimBlock("C1", "Der Wert beträgt 42 mm.", ("E1",), "F1", "supported")]
    parser.finish()

    incomplete = ClaimBlockParser()
    incomplete.feed('<claim>{"id":"C1"}')
    with pytest.raises(ClaimParseError, match="incomplete"):
        incomplete.finish()


def test_draft_text_shows_prose_while_the_claim_is_still_arriving() -> None:
    """The user must see words, not JSON, while the model is still writing.

    The model streams `<claim>{...}</claim>` blocks, so the raw token stream is
    unreadable. `draft_text` decodes the prose as far as it has arrived; without
    it the interface stays blank until a whole claim is complete.
    """
    stream = (
        '<claim>{"id":"C1","facet_id":"F1","evidence_ids":["E1"],'
        '"text":"Beton h\\u00e4rtet \\"langsam\\" aus.\\nDaher ..."}</claim>'
    )
    parser = ClaimBlockParser()
    drafts = []
    for character in stream:
        parser.feed(character)
        drafts.append(parser.draft_text())

    longest = max(drafts, key=len)
    # Escapes are decoded, so the reader never sees \u00e4 or \".
    assert longest == 'Beton härtet "langsam" aus.\nDaher ...'
    # And no JSON structure leaks into the visible draft.
    for leak in ('"id"', "{", "evidence_ids", "<claim>", "\\u"):
        assert all(leak not in draft for draft in drafts), leak
    # Prose appears well before the block is complete — that is the whole point.
    first_visible = next(index for index, draft in enumerate(drafts) if draft)
    assert first_visible < len(stream) * 0.7
    # Once the block completes the draft is spent; the committed claim takes over.
    assert drafts[-1] == ""


def test_draft_text_is_empty_until_the_text_field_starts() -> None:
    parser = ClaimBlockParser()
    parser.feed('<claim>{"id":"C1","facet_id":"F1",')
    assert parser.draft_text() == ""
    parser.feed('"evidence_ids":["E1"],"te')
    assert parser.draft_text() == ""
    parser.feed('xt":"Anfang')
    assert parser.draft_text() == "Anfang"


def test_claim_validation_is_scoped_to_cited_evidence_and_technical_literals() -> None:
    source = candidate("c1", "Der Grenzwert beträgt 42 mm nach DIN EN 1234-5.")
    evidence = extract_evidence_window(source, "Grenzwert", evidence_id="E1")
    valid = validate_claim(
        ClaimBlock("C1", "Der Grenzwert beträgt 42 mm nach DIN EN 1234-5.", ("E1",), "F1"),
        {"E1": evidence},
        allowed_facets=("F1",),
    )
    assert valid.valid

    unsupported = validate_claim(
        ClaimBlock("C2", "Der Grenzwert beträgt 51 mm.", ("E1",), "F1"),
        {"E1": evidence},
        allowed_facets=("F1",),
    )
    assert not unsupported.valid
    assert "unsupported_technical_literal" in unsupported.errors

    wrong_id = validate_claim(
        ClaimBlock("C3", "Eine allgemeine Aussage.", ("E9",), "F2"),
        {"E1": evidence},
        allowed_facets=("F1",),
    )
    assert set(wrong_id.errors) == {"unknown_facet_id", "unknown_evidence_id"}


def test_insufficient_claims_must_not_smuggle_values_or_citations() -> None:
    result = validate_claim(
        ClaimBlock("C1", "Für 42 mm fehlen Belege.", (), "F1", "insufficient"),
        {},
        allowed_facets=("F1",),
    )
    assert not result.valid
    assert result.errors == ("insufficient_claim_has_technical_literal",)


def test_supported_claims_need_semantic_overlap_not_only_a_known_evidence_id() -> None:
    evidence = extract_evidence_window(
        candidate("concrete", "Beton ist grau und wird als Baustoff eingesetzt."),
        "Beton",
        evidence_id="E1",
    )
    result = validate_claim(
        ClaimBlock("C1", "Beton ist radioaktiv.", ("E1",), "F1"),
        {"E1": evidence},
        allowed_facets=("F1",),
    )
    assert not result.valid
    assert "unsupported_semantic_terms" in result.errors


@pytest.mark.parametrize(
    ("evidence_text", "claim_text"),
    [
        ("Der Baustoff ist brennbar.", "Der Baustoff ist nicht brennbar."),
        ("Der Baustoff ist nicht brennbar.", "Der Baustoff ist brennbar."),
        ("Der Einsatz des Baustoffs ist zulässig.", "Der Einsatz ist verboten."),
    ],
)
def test_claim_validation_rejects_negation_and_obvious_antonym_conflicts(
    evidence_text: str, claim_text: str
) -> None:
    evidence = extract_evidence_window(
        candidate("polarity", evidence_text),
        "Baustoff Einsatz",
        evidence_id="E1",
    )
    result = validate_claim(
        ClaimBlock("C1", claim_text, ("E1",), "F1"),
        {"E1": evidence},
        allowed_facets=("F1",),
    )

    assert not result.valid
    assert "semantic_contradiction" in result.errors


@pytest.mark.parametrize(
    "evidence_text",
    [
        "Der Baustoff ist brennbar und der Einsatz ist nicht zulässig.",
        "Nicht nur der Baustoff ist brennbar.",
    ],
)
def test_negation_guard_does_not_leak_polarity_between_clauses(
    evidence_text: str,
) -> None:
    evidence = extract_evidence_window(
        candidate("scoped-polarity", evidence_text),
        "Baustoff brennbar",
        evidence_id="E1",
    )
    result = validate_claim(
        ClaimBlock("C1", "Der Baustoff ist brennbar.", ("E1",), "F1"),
        {"E1": evidence},
        allowed_facets=("F1",),
    )

    assert result.valid


def test_technical_literal_validation_preserves_sign_and_scientific_notation() -> None:
    evidence = extract_evidence_window(
        candidate("temperature", "Die Temperatur beträgt -5 °C bei 1,2e-3 MPa."),
        "Temperatur und Druck",
        evidence_id="E1",
    )
    result = validate_claim(
        ClaimBlock("C1", "Die Temperatur beträgt +5 °C bei 1,2e-3 MPa.", ("E1",), "F1"),
        {"E1": evidence},
        allowed_facets=("F1",),
    )
    assert not result.valid
    assert "unsupported_technical_literal" in result.errors
