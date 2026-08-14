from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omarag_bridge.adapters.base import SearchManyItem, SearchManyResult, SearchManyStats
from omarag_bridge.models.domain import EvidenceMode, SearchHit
from omarag_bridge.services import query_orchestrator as module
from omarag_bridge.services import run_service as run_module
from omarag_bridge.services.ollama_stream import OllamaModelIdentity, OllamaStreamEvent
from omarag_bridge.services.query_orchestrator import QueryOrchestrator
from omarag_bridge.services.query_v2 import ClaimVerification, EvidenceKind, QueryComplexity
from omarag_bridge.services.run_service import RunService


class FakeStore:
    def recent_completed_session_runs(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    def route_book_knowledge(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []


class FakeAdapter:
    stable_evidence = False

    async def search(self, *_args: Any, **_kwargs: Any) -> list[SearchHit]:
        metadata: dict[str, Any] = {"headings": ["Bemessung", "Grenzwerte"]}
        if self.stable_evidence:
            metadata["evidence_id"] = "ev-stable"
            metadata["generation_id"] = "gen-stable"
        return [
            SearchHit(
                chunk_id="chunk-42",
                content="Der Grenzwert beträgt 42 mm.",
                pages=[7],
                document_id="book-1",
                metadata=metadata,
            )
        ]

    async def get_chunks(self, *_args: Any, **_kwargs: Any) -> list[SearchHit]:
        return []

    async def rerank(
        self, _database: Path, _question: str, candidates: list[SearchHit]
    ) -> list[float]:
        return [3.0] * len(candidates)


class FakeOllama:
    blocks: tuple[str, ...] = ()
    last_messages: list[dict[str, Any]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> FakeOllama:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def stream_chat(self, **kwargs: Any):
        assert kwargs["resolved_identity"].digest == "generator-digest"
        type(self).last_messages = kwargs["messages"]
        for index, block in enumerate(self.blocks):
            done = index == len(self.blocks) - 1
            yield OllamaStreamEvent(
                model="qwen3.5:4b",
                model_digest="generator-digest",
                content=block,
                done=done,
                done_reason="stop" if done else None,
                prompt_eval_count=120 if done else None,
                eval_count=18 if done else None,
                eval_duration_ns=900_000_000 if done else None,
            )


def test_workspace_profile_resolves_auto_and_caps_adaptive_context() -> None:
    service = RunService.__new__(RunService)
    service.workspace_profile = lambda _workspace_id: "quality"
    service.workspace_context_tokens = lambda _workspace_id: 12_288

    request = service._effective_request(
        "ws-1",
        {"question": "Vergleiche A und B.", "options": {"profile": "auto"}},
    )
    assert request["options"]["profile"] == "quality"
    assert request["options"]["_model_context_tokens"] == 12_288
    budget = QueryOrchestrator._bounded_budget(
        module.QueryComplexity.COMPLEX,
        request["options"],
    )
    assert budget["context_tokens"] == 12_288

    low_tier = QueryOrchestrator._bounded_budget(
        QueryComplexity.COMPLEX,
        {"profile": "quality", "_model_context_tokens": 4_096},
    )
    assert low_tier["evidence_tokens"] + low_tier["answer_tokens"] <= (
        low_tier["context_tokens"] - 1_536
    )

    explicit = service._effective_request(
        "ws-1",
        {"question": "Was ist A?", "options": {"profile": "fast"}},
    )
    assert explicit["options"]["profile"] == "fast"


@pytest.mark.asyncio
async def test_outer_deadline_includes_register_complexity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DeadlineStore:
        @staticmethod
        def get_run_request(_run_id: str) -> dict[str, Any]:
            return {"question": "Erläutere Kriechen.", "mode": "rag", "options": {}}

        @staticmethod
        def get_run(_run_id: str) -> Any:
            return SimpleNamespace(workspace_id="ws-1", session_id="session-1", status="running")

        @staticmethod
        def route_book_knowledge(*_args: Any, **_kwargs: Any) -> list[dict[str, str]]:
            return [{"term_id": "term-1"}, {"term_id": "term-2"}]

    service = RunService.__new__(RunService)
    service.store = DeadlineStore()
    service.adapter = SimpleNamespace(capabilities=SimpleNamespace(adaptive_retrieval=True))
    service.query = SimpleNamespace(
        standalone_question=lambda *_args, **_kwargs: ("Erläutere Kriechen.", False)
    )

    async def execute_inner(_run_id: str) -> None:
        return None

    service._execute_inner = execute_inner
    deadlines: list[float] = []

    class NoopTimeout:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_args: Any) -> None:
            return None

    def timeout_at(deadline: float) -> NoopTimeout:
        deadlines.append(deadline)
        return NoopTimeout()

    monkeypatch.setattr(run_module.asyncio, "timeout_at", timeout_at)
    started = run_module.asyncio.get_running_loop().time()
    await service._execute("run-deadline")

    assert len(deadlines) == 1
    assert deadlines[0] - started >= 24.9


@pytest.mark.asyncio
async def test_orchestrator_prefers_batched_search_many_when_adapter_exposes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BatchedAdapter(FakeAdapter):
        def __init__(self) -> None:
            self.batch_calls: list[tuple[object, ...]] = []

        async def search_many(self, _database, requests, **_kwargs):
            self.batch_calls.append(tuple(requests))
            return SearchManyResult(
                items=[
                    SearchManyItem(
                        key=request.key,
                        hits=await self.search(None, request.query),
                    )
                    for request in requests
                ],
                hydrated_chunks=[],
                stats=SearchManyStats(
                    search_requests=len(requests),
                    successful_searches=len(requests),
                ),
            )

    FakeOllama.blocks = (
        '<claim>{"id":"C1","text":"Der Grenzwert beträgt 42 mm.",'
        '"evidence_ids":["E1"],"facet_id":"F1","status":"supported"}</claim>',
    )
    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)
    adapter = BatchedAdapter()

    await QueryOrchestrator(
        adapter=adapter, store=FakeStore(), ollama_url="http://ollama.invalid"
    ).answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-batch",
        session_id="session-1",
        question="Was ist der Grenzwert?",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={},
        model="qwen3.5:4b",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
    )

    assert len(adapter.batch_calls) == 1
    requests = adapter.batch_calls[0]
    assert [request.key for request in requests] == ["F1:fts", "F1:vector"]
    assert {request.query for request in requests} == {"Was ist der Grenzwert?"}
    assert {request.search_type for request in requests} == {"fts", "vector"}
    assert all(request.limit >= 4 for request in requests)
    assert all(request.rerank is False for request in requests)


@pytest.mark.asyncio
async def test_orchestrator_commits_only_complete_validated_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOllama.blocks = (
        '<claim>{"id":"C1","text":"Der Grenzwert ',
        'beträgt 42 mm.","evidence_ids":["E1"],"facet_id":"F1","status":"supported"}</claim>',
    )
    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)
    committed: list[tuple[str, list[str]]] = []

    async def emit(claim, citations) -> None:
        committed.append((claim.text, [item.evidence_id for item in citations]))

    answer = await QueryOrchestrator(FakeStore(), FakeAdapter(), "http://ollama.invalid").answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-1",
        session_id="session-1",
        question="Was ist der Grenzwert?",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={"verifier": "off"},
        model="qwen3.5:4b",
        expected_model_digest="generator-digest",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
        emit_claim=emit,
    )

    assert answer.answer == "Der Grenzwert beträgt 42 mm."
    assert committed == [("Der Grenzwert beträgt 42 mm.", ["E1"])]
    assert answer.rejected_claims == 0
    assert answer.rerank_status == "applied"
    assert answer.citations[0].claim_ids == ["C1"]
    assert answer.citations[0].relevance_score is not None
    assert answer.citations[0].rerank_score == 3.0
    assert answer.prompt_tokens == 120
    assert answer.output_tokens == 18
    assert answer.tokens_per_second == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_orchestrator_fail_closes_typed_claims_when_local_verifier_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOllama.blocks = (
        '<claim>{"id":"C1","text":"Der Grenzwert beträgt 42 mm.",'
        '"evidence_ids":["E1"],"facet_id":"F1","status":"supported"}</claim>',
    )
    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)

    class TypedAdapter(FakeAdapter):
        async def search(self, *_args: Any, **_kwargs: Any) -> list[SearchHit]:
            return [
                SearchHit(
                    chunk_id="chunk-42",
                    content="| Klasse | Grenzwert |\n|---|---|\n| A | 42 mm |",
                    pages=[7],
                    document_id="book-1",
                    metadata={
                        "headings": ["Grenzwerte"],
                        "evidence_kind": EvidenceKind.TABLE.value,
                    },
                )
            ]

    class RejectingVerifier:
        def __init__(self) -> None:
            self.calls = 0

        async def verify(self, _claim, _evidence) -> ClaimVerification:
            self.calls += 1
            return ClaimVerification("contradicted", "fixture")

    verifier = RejectingVerifier()
    answer = await QueryOrchestrator(
        FakeStore(), TypedAdapter(), "http://ollama.invalid", claim_verifier=verifier
    ).answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-typed-rejected",
        session_id="session-1",
        question="Was ist der Grenzwert?",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={},
        model="qwen3.5:4b",
        expected_model_digest="generator-digest",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
    )

    assert verifier.calls == 1
    assert answer.abstention == "full"
    assert answer.citations == ()
    assert any(item.startswith("claim_verifier_") for item in answer.fallbacks)


@pytest.mark.asyncio
async def test_orchestrator_rejects_unsupported_technical_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOllama.blocks = (
        '<claim>{"id":"C1","text":"Der Grenzwert beträgt 51 mm.",'
        '"evidence_ids":["E1"],"facet_id":"F1","status":"supported"}</claim>',
    )
    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)
    committed: list[str] = []

    async def emit(claim, _citations) -> None:
        committed.append(claim.text)

    answer = await QueryOrchestrator(FakeStore(), FakeAdapter(), "http://ollama.invalid").answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-2",
        session_id="session-1",
        question="Was ist der Grenzwert?",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={"verifier": "off"},
        model="qwen3.5:4b",
        expected_model_digest="generator-digest",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
        emit_claim=emit,
    )

    assert answer.abstention == "full"
    assert answer.answer == "In den bereitgestellten Quellen nicht ausreichend belegt."
    assert answer.rejected_claims == 1
    assert answer.citations == ()
    assert committed == []


@pytest.mark.asyncio
async def test_orchestrator_fails_closed_for_unbound_custom_reranker() -> None:
    answer = await QueryOrchestrator(FakeStore(), FakeAdapter(), "http://ollama.invalid").answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-custom-reranker",
        session_id="session-1",
        question="Was ist der Grenzwert?",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={"verifier": "off"},
        model="qwen3.5:4b",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
        reranker_digest="custom-reranker-without-gold-calibration",
    )

    assert answer.abstention == "full"
    assert "calibration_mismatch" in answer.fallbacks


@pytest.mark.asyncio
async def test_risky_claim_fails_closed_when_verifier_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOllama.blocks = (
        '<claim>{"id":"C1","text":"Der Grenzwert beträgt 42 mm.",'
        '"evidence_ids":["E1"],"facet_id":"F1","status":"supported"}</claim>',
    )
    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)

    answer = await QueryOrchestrator(FakeStore(), FakeAdapter(), "http://ollama.invalid").answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-no-verifier",
        session_id="session-1",
        question="Was ist der Grenzwert?",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={},
        model="qwen3.5:4b",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
    )

    assert answer.abstention == "full"
    assert "claim_verifier_unavailable" in answer.fallbacks
    assert "claim_verifier_verifier-unavailable" in answer.fallbacks


@pytest.mark.asyncio
async def test_stable_evidence_id_is_joinable_while_prompt_id_stays_internal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOllama.blocks = (
        '<claim>{"id":"C1","text":"Der Grenzwert beträgt 42 mm.",'
        '"evidence_ids":["E1"],"facet_id":"F1","status":"supported"}</claim>',
    )
    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)
    adapter = FakeAdapter()
    adapter.stable_evidence = True
    emitted: list[tuple[list[str], list[str]]] = []

    async def emit(claim, citations) -> None:
        emitted.append((claim.evidence_ids, [item.evidence_id for item in citations]))

    answer = await QueryOrchestrator(FakeStore(), adapter, "http://ollama.invalid").answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-stable",
        session_id="session-1",
        question="Was ist der Grenzwert?",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={"verifier": "off"},
        model="qwen3.5:4b",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
        emit_claim=emit,
    )

    assert emitted == [(["ev-stable"], ["ev-stable"])]
    assert answer.claims[0].evidence_ids == ["ev-stable"]
    assert answer.citations[0].evidence_id == "ev-stable"
    assert answer.citations[0].generation_id == "gen-stable"
    assert answer.citations[0].prompt_evidence_id == "E1"
    assert answer.claims[0].support_spans
    support = answer.claims[0].support_spans[0]
    assert support.evidence_id == "ev-stable"
    assert support.char_end > support.char_start
    assert support.content_hash == answer.citations[0].chunk_content_hash


@pytest.mark.asyncio
async def test_orchestrator_does_not_route_filtered_book_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RoutedStore(FakeStore):
        def __init__(self) -> None:
            self.route_options: list[dict[str, Any]] = []

        def route_book_knowledge(self, *_args: Any, **_kwargs: Any):
            self.route_options.append(_kwargs)
            return [
                {
                    "term": "Grenzwert",
                    "chunk_id": "secret-chunk",
                    "logical_document_id": "secret-book",
                    "retrieval_path": "book-located_in",
                }
            ]

    class RoutedAdapter(FakeAdapter):
        async def get_chunks(self, *_args: Any, **_kwargs: Any) -> list[SearchHit]:
            return [
                SearchHit(
                    chunk_id="secret-chunk",
                    content="Geheime Evidenz: 99 mm.",
                    pages=[99],
                    document_id="secret-segment",
                )
            ]

    FakeOllama.blocks = (
        '<claim>{"id":"C1","text":"Der Grenzwert beträgt 42 mm.",'
        '"evidence_ids":["E1"],"facet_id":"F1","status":"supported"}</claim>',
    )
    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)
    store = RoutedStore()
    answer = await QueryOrchestrator(store, RoutedAdapter(), "http://ollama.invalid").answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-filter",
        session_id="session-1",
        question="Was ist der Grenzwert?",
        evidence_mode=EvidenceMode.STRICT,
        document_filter="id IN ('book-1')",
        options={"verifier": "off"},
        model="qwen3.5:4b",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
        allowed_document_ids={"book-1"},
    )

    assert [item.chunk_id for item in answer.citations] == ["chunk-42"]
    assert all("secret" not in path for item in answer.citations for path in item.retrieval_paths)
    assert all(options["allowed_segment_ids"] == {"book-1"} for options in store.route_options)
    assert store.route_options[-1]["expand_sections"] is True


def test_memory_off_never_uses_session_history() -> None:
    class ExplodingHistoryStore(FakeStore):
        def recent_completed_session_runs(self, *_args: Any, **_kwargs: Any):
            raise AssertionError("history must not be loaded")

    question, referenced = QueryOrchestrator(
        ExplodingHistoryStore(), FakeAdapter(), "http://ollama.invalid"
    ).standalone_question("ws-1", "session-1", "run-1", "Und wie gilt das?", memory_enabled=False)
    assert question == "Und wie gilt das?"
    assert referenced is False


@pytest.mark.asyncio
async def test_empty_fusion_is_reported_as_empty_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyAdapter(FakeAdapter):
        async def search(self, *_args: Any, **_kwargs: Any) -> list[SearchHit]:
            return []

        async def rerank(self, *_args: Any, **_kwargs: Any) -> list[float]:
            raise AssertionError("empty retrieval must not invoke the reranker")

    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)
    answer = await QueryOrchestrator(FakeStore(), EmptyAdapter(), "http://ollama.invalid").answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-empty",
        session_id="session-1",
        question="Was ist der Grenzwert?",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={"verifier": "off"},
        model="qwen3.5:4b",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
    )

    assert answer.abstention == "full"
    assert "retrieval_empty" in answer.fallbacks
    assert "relevance_threshold" not in answer.fallbacks


@pytest.mark.asyncio
async def test_existing_insufficient_facet_is_not_duplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOllama.blocks = (
        '<claim>{"id":"C1","text":"Der Grenzwert beträgt 42 mm.",'
        '"evidence_ids":["E1"],"facet_id":"F1","status":"supported"}</claim>'
        '<claim>{"id":"C2","text":"Dafür fehlt ausreichende Evidenz.",'
        '"evidence_ids":[],"facet_id":"F2","status":"insufficient"}</claim>',
    )
    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)
    answer = await QueryOrchestrator(FakeStore(), FakeAdapter(), "http://ollama.invalid").answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-facets",
        session_id="session-1",
        question="Vergleiche Grenzwert und Bemessung.",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={"verifier": "off"},
        model="qwen3.5:4b",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
    )

    assert [claim.id for claim in answer.claims] == ["C1", "C2"]
    assert [claim.facet_id for claim in answer.claims] == ["F1", "F2"]
    assert answer.abstention == "partial"


@pytest.mark.asyncio
async def test_systemic_claim_id_is_unique_after_sparse_model_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeOllama.blocks = (
        '<claim>{"id":"C9","text":"Der Grenzwert beträgt 42 mm.",'
        '"evidence_ids":["E1"],"facet_id":"F1","status":"supported"}</claim>',
    )
    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)
    answer = await QueryOrchestrator(FakeStore(), FakeAdapter(), "http://ollama.invalid").answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-claim-id",
        session_id="session-1",
        question="Vergleiche Grenzwert und Bemessung.",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={"verifier": "off"},
        model="qwen3.5:4b",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
    )

    assert [claim.id for claim in answer.claims] == ["C9", "C10"]
    assert len({claim.id for claim in answer.claims}) == len(answer.claims)


@pytest.mark.asyncio
async def test_prompt_contract_includes_two_facets_navigation_and_raw_excerpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BookPromptAdapter(FakeAdapter):
        async def search(self, *_args: Any, **_kwargs: Any) -> list[SearchHit]:
            return [
                SearchHit(
                    chunk_id="chunk-42",
                    content="Der Grenzwert beträgt 42 mm.",
                    pages=[7],
                    document_id="book-1",
                    document_title="Handbuch Betonbau",
                    metadata={
                        "headings": ["Bemessung", "Grenzwerte"],
                        "logical_document_id": "work-betonbau",
                        "section_node_id": "section-grenzwerte",
                        "document_meta": {
                            "book_metadata": {
                                "title": "Handbuch Betonbau",
                                "authors": ["Ada Beispiel"],
                                "edition_label": "3. Auflage",
                                "publication_year": 2025,
                                "confirmed": True,
                            }
                        },
                    },
                )
            ]

    FakeOllama.blocks = (
        '<claim>{"id":"C1","text":"Der Grenzwert beträgt 42 mm.",'
        '"evidence_ids":["E1"],"facet_id":"F1","status":"supported"}</claim>',
    )
    FakeOllama.last_messages = []
    monkeypatch.setattr(module, "OllamaStreamClient", FakeOllama)
    await QueryOrchestrator(FakeStore(), BookPromptAdapter(), "http://ollama.invalid").answer(
        workspace_id="ws-1",
        database=Path("/tmp/db"),
        run_id="run-prompt",
        session_id="session-1",
        question="Vergleiche Grenzwert und Bemessung.",
        evidence_mode=EvidenceMode.STRICT,
        document_filter=None,
        options={},
        model="qwen3.5:4b",
        resolved_model_identity=OllamaModelIdentity("qwen3.5:4b", "generator-digest", 1),
    )

    system = FakeOllama.last_messages[0]["content"]
    assert (
        '<facets>[{"id":"F1","query":"Grenzwert"},'
        '{"id":"F2","query":"Bemessung"}]</facets>' in system
    )
    assert '"facet_ids":["F1","F2"]' in system
    assert '"title":"Handbuch Betonbau"' in system
    assert '"chapter_path":["Bemessung","Grenzwerte"]' in system
    assert '"pages":[7]' in system
    raw = "Der Grenzwert beträgt 42 mm."
    marker = f'<raw_excerpt chars="{len(raw)}">'
    raw_start = system.index(marker) + len(marker)
    assert system[raw_start : raw_start + len(raw)] == raw
    assert system[raw_start + len(raw) :].startswith("</raw_excerpt>")
