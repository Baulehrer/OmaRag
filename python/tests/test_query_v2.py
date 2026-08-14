from __future__ import annotations

import math

import pytest

from omarag_bridge.services.query_v2 import (
    ClaimBlock,
    ClaimBlockParser,
    ClaimParseError,
    PlattCalibrator,
    QueryComplexity,
    RetrievalCandidate,
    adaptive_select,
    classify_query,
    extract_evidence_window,
    pack_evidence_windows,
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
