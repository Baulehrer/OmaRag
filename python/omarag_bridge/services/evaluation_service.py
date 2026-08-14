from __future__ import annotations

import hashlib
import json
import math
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..adapters.base import HaikuAdapter
from ..models.domain import EvaluationCase, EvaluationReport
from ..models.errors import ConflictError
from ..store import StateStore
from .workspace_service import WorkspaceService


class EvaluationService:
    """Deterministic Silver retrieval evaluation built from Haiku chunk provenance."""

    def __init__(
        self,
        store: StateStore,
        workspaces: WorkspaceService,
        adapter: HaikuAdapter,
    ) -> None:
        self.store = store
        self.workspaces = workspaces
        self.adapter = adapter
        self.content_egress_guard: Callable[[str, str], None] | None = None

    def generate(self, workspace_id: str, limit: int) -> EvaluationReport:
        self.workspaces.get(workspace_id)
        cases: list[EvaluationCase] = []
        headings_seen: set[tuple[str, str]] = set()
        for chunk in self.store.chunk_manifest(workspace_id):
            headings = [str(item).strip() for item in chunk["headings"] if str(item).strip()]
            if not chunk["chunk_id"] or not headings:
                continue
            heading = headings[-1]
            identity = (chunk["logical_document_id"], heading.casefold())
            if identity in headings_seen:
                continue
            headings_seen.add(identity)
            labels = {str(item).casefold() for item in chunk["labels"]}
            category = "table-location" if "table" in labels else "section-location"
            prompt = (
                f"In welchem Abschnitt und auf welcher Seite steht die Tabelle „{heading}“?"
                if category == "table-location"
                else f"Wo wird das Thema „{heading}“ im Lehrbuch erklärt?"
            )
            cases.append(
                EvaluationCase(
                    id=f"case-{uuid4().hex[:12]}",
                    question=prompt,
                    category=category,
                    expected_chunk_id=chunk["chunk_id"],
                    expected_document_id=chunk["segment_document_id"],
                    expected_pages=chunk["pages"],
                )
            )
            if len(cases) >= limit:
                break
        if not cases:
            raise ConflictError(
                "No structure-grounded Silver cases can be generated yet; index a book first"
            )
        report = EvaluationReport(
            id=f"eval-{uuid4().hex[:12]}", workspace_id=workspace_id, cases=cases
        )
        self._save(report)
        return report

    def import_gold(
        self,
        workspace_id: str,
        cases: list[EvaluationCase],
        *,
        evaluation_id: str | None = None,
        baseline_id: str | None = None,
        require_reviewed: bool = True,
    ) -> EvaluationReport:
        """Persist a user-owned, human-reviewed V2 evaluation set locally."""

        self.workspaces.get(workspace_id)
        if require_reviewed and any(not case.reviewed for case in cases):
            raise ConflictError("Every imported gold case must be marked reviewed")
        if len({case.id for case in cases}) != len(cases):
            raise ConflictError("Evaluation case ids must be unique")
        canonical = [
            case.model_dump(mode="json") for case in sorted(cases, key=lambda item: item.id)
        ]
        dataset_digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        report = EvaluationReport(
            id=evaluation_id or f"eval-{uuid4().hex[:12]}",
            workspace_id=workspace_id,
            cases=cases,
            dataset_digest=dataset_digest,
            baseline_id=baseline_id,
        )
        self._save(report)
        return report

    async def run(
        self,
        workspace_id: str,
        evaluation_id: str | None,
        variants: list[str],
        top_k: int,
    ) -> EvaluationReport:
        if self.content_egress_guard is not None:
            self.content_egress_guard(workspace_id, self.workspaces.ollama_url)
        if evaluation_id:
            report = EvaluationReport.model_validate(
                self.store.evaluation(workspace_id, evaluation_id)
            )
        else:
            latest = self.store.latest_evaluation(workspace_id)
            report = (
                EvaluationReport.model_validate(latest)
                if latest and latest.get("cases")
                else self.generate(workspace_id, 30)
            )
        measured: dict[str, dict[str, float]] = {}
        gold_cases = sum(case.reviewed or case.origin == "gold" for case in report.cases)
        for variant in dict.fromkeys(variants):
            ranks: list[int | None] = []
            page_hits = 0
            rerank_ranks: list[int | None] = []
            rerank_page_hits = 0
            rerank_measured_count = 0
            for case in report.cases:
                hits = await self.adapter.search(
                    self.workspaces.database_path(workspace_id),
                    case.question,
                    top_k,
                    search_type=variant,
                )
                rank = self._case_rank(case, hits)
                if rank is not None and any(
                    set(case.expected_pages) & set(hit.pages) for hit in hits[:rank]
                ):
                    page_hits += 1
                ranks.append(rank)
                rerank_scores: list[float] = []
                try:
                    rerank_scores = await self.adapter.rerank(
                        self.workspaces.database_path(workspace_id),
                        case.question,
                        hits,
                    )
                except (AttributeError, NotImplementedError, RuntimeError):
                    rerank_scores = []
                if len(rerank_scores) == len(hits) and hits:
                    rerank_measured_count += 1
                    reranked_hits = [
                        hit
                        for _score, hit in sorted(
                            zip(rerank_scores, hits, strict=True),
                            key=lambda item: (-float(item[0]), item[1].chunk_id),
                        )
                    ]
                    rerank_rank = self._case_rank(case, reranked_hits)
                    if rerank_rank is not None and any(
                        set(case.expected_pages) & set(hit.pages)
                        for hit in reranked_hits[:rerank_rank]
                    ):
                        rerank_page_hits += 1
                    rerank_ranks.append(rerank_rank)
                else:
                    rerank_ranks.append(None)
            total = max(len(ranks), 1)
            metrics = {
                "recall_at_5": sum(rank is not None and rank <= 5 for rank in ranks) / total,
                "recall_at_10": sum(rank is not None and rank <= 10 for rank in ranks) / total,
                "mrr": sum(1 / rank for rank in ranks if rank is not None) / total,
                "ndcg_at_10": sum(
                    1 / math.log2(rank + 1) for rank in ranks if rank is not None and rank <= 10
                )
                / total,
                "page_hit_rate": page_hits / total,
                "gold_case_fraction": gold_cases / total,
            }
            if rerank_measured_count:
                metrics.update(
                    {
                        "rerank_recall_at_5": sum(
                            rank is not None and rank <= 5 for rank in rerank_ranks
                        )
                        / total,
                        "rerank_recall_at_10": sum(
                            rank is not None and rank <= 10 for rank in rerank_ranks
                        )
                        / total,
                        "rerank_mrr": sum(1 / rank for rank in rerank_ranks if rank is not None)
                        / total,
                        "rerank_page_hit_rate": rerank_page_hits / total,
                        "rerank_measured_fraction": rerank_measured_count / total,
                    }
                )
            measured[variant] = metrics
        report = report.model_copy(update={"variants": measured})
        self._save(report)
        return report

    @staticmethod
    def _case_rank(case: EvaluationCase, hits: list[Any]) -> int | None:
        if not case.answerable:
            return None
        allowed = [set(group) for group in case.allowed_evidence_sets if group]
        if not allowed and case.expected_chunk_id:
            allowed = [{case.expected_chunk_id}]
        seen: set[str] = set()
        for rank, hit in enumerate(hits, start=1):
            seen.add(hit.chunk_id)
            seen.update(str(item) for item in hit.metadata.get("chunk_ids", []))
            if hit.metadata.get("evidence_id"):
                seen.add(str(hit.metadata["evidence_id"]))
            if any(group <= seen for group in allowed):
                return rank
        return None

    def _save(self, report: EvaluationReport) -> None:
        payload = report.model_dump(mode="json")
        self.store.save_evaluation(report.id, report.workspace_id, payload)
        workspace = self.workspaces.get(report.workspace_id)
        path = Path(workspace.path) / "evaluations" / "history" / f"{report.id}.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{report.id}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                temporary = Path(stream.name)
            temporary.chmod(0o600)
            temporary.replace(path)
            path.chmod(0o600)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
