from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omarag_bridge.models.domain import EvaluationCase, EvaluationReport, SearchHit
from omarag_bridge.services.evaluation_service import EvaluationService


class StubAdapter:
    async def search(self, _database: Path, _question: str, _limit: int, **_kwargs: Any):
        return [
            SearchHit(chunk_id="wrong", content="other", pages=[2]),
            SearchHit(chunk_id="expected", content="answer", pages=[7]),
        ]

    async def rerank(self, _database: Path, _question: str, hits: list[SearchHit]):
        return [0.1 if hit.chunk_id == "wrong" else 0.9 for hit in hits]


@pytest.mark.asyncio
async def test_run_reports_gold_and_rerank_metrics_even_when_gold_misses() -> None:
    report = EvaluationReport(
        id="eval-1",
        workspace_id="ws-1",
        cases=[
            EvaluationCase(
                id="case-1",
                question="first",
                expected_chunk_id="expected",
                expected_document_id="doc-1",
                expected_pages=[7],
                reviewed=True,
                origin="gold",
            ),
            EvaluationCase(
                id="case-2",
                question="second",
                expected_chunk_id="missing",
                expected_document_id="doc-1",
                expected_pages=[9],
            ),
        ],
    )

    class Store:
        def evaluation(self, _workspace_id: str, _evaluation_id: str) -> dict[str, Any]:
            return report.model_dump(mode="json")

    service = EvaluationService(
        Store(),  # type: ignore[arg-type]
        SimpleNamespace(database_path=lambda _workspace_id: Path("/tmp/db")),
        StubAdapter(),  # type: ignore[arg-type]
    )
    saved: list[EvaluationReport] = []
    service._save = saved.append  # type: ignore[method-assign]

    result = await service.run("ws-1", "eval-1", ["hybrid"], top_k=5)
    metrics = result.variants["hybrid"]

    assert metrics["gold_case_fraction"] == pytest.approx(0.5)
    assert metrics["rerank_recall_at_5"] == pytest.approx(0.5)
    assert metrics["rerank_mrr"] == pytest.approx(0.5)
    assert metrics["rerank_measured_fraction"] == pytest.approx(1.0)
    assert saved == [result]


@pytest.mark.asyncio
async def test_v2_case_accepts_any_complete_allowed_evidence_set() -> None:
    report = EvaluationReport(
        id="eval-v2",
        workspace_id="ws-1",
        cases=[
            EvaluationCase(
                id="case-v2",
                question="Vergleiche beide Abschnitte.",
                category="multi-hop",
                expected_document_id="doc-1",
                expected_pages=[2, 7],
                allowed_evidence_sets=[["first", "expected"], ["alternative"]],
                required_facets=["F1", "F2"],
                reviewed=True,
                origin="gold",
                split="test",
                book_group="book-a",
            )
        ],
    )

    class Store:
        def evaluation(self, _workspace_id: str, _evaluation_id: str) -> dict[str, Any]:
            return report.model_dump(mode="json")

    class MultiAdapter(StubAdapter):
        async def search(self, *_args: Any, **_kwargs: Any):
            return [
                SearchHit(chunk_id="first", content="one", pages=[2]),
                SearchHit(chunk_id="expected", content="two", pages=[7]),
            ]

    service = EvaluationService(
        Store(),  # type: ignore[arg-type]
        SimpleNamespace(database_path=lambda _workspace_id: Path("/tmp/db")),
        MultiAdapter(),  # type: ignore[arg-type]
    )
    service._save = lambda _report: None  # type: ignore[method-assign]

    result = await service.run("ws-1", "eval-v2", ["hybrid"], top_k=5)

    assert result.variants["hybrid"]["recall_at_5"] == pytest.approx(1.0)
    assert result.variants["hybrid"]["mrr"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_evaluation_rechecks_content_egress_before_private_gold_queries() -> None:
    report = EvaluationReport(id="eval-egress", workspace_id="ws-1", cases=[])

    class Store:
        def evaluation(self, _workspace_id: str, _evaluation_id: str) -> dict[str, Any]:
            return report.model_dump(mode="json")

    adapter = StubAdapter()
    service = EvaluationService(
        Store(),  # type: ignore[arg-type]
        SimpleNamespace(
            database_path=lambda _workspace_id: Path("/tmp/db"),
            ollama_url="https://remote-model.example.test",
        ),
        adapter,  # type: ignore[arg-type]
    )

    def denied(_workspace_id: str, _url: str) -> None:
        raise PermissionError("blocked before retrieval")

    service.content_egress_guard = denied
    with pytest.raises(PermissionError, match="blocked before retrieval"):
        await service.run("ws-1", "eval-egress", ["hybrid"], top_k=5)


def test_imported_goldset_gets_content_digest() -> None:
    saved: list[EvaluationReport] = []
    service = EvaluationService(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(get=lambda _workspace_id: object()),
        StubAdapter(),  # type: ignore[arg-type]
    )
    service._save = saved.append  # type: ignore[method-assign]
    case = EvaluationCase(
        id="gold-1",
        question="Wo steht der Wert?",
        expected_chunk_id="expected",
        expected_document_id="doc-1",
        reviewed=True,
        origin="gold",
        split="calibration",
        book_group="book-a",
    )

    result = service.import_gold("ws-1", [case], evaluation_id="eval-private")

    assert result.dataset_digest is not None
    assert len(result.dataset_digest) == 64
    assert result.schema_version == 2
    assert saved == [result]
