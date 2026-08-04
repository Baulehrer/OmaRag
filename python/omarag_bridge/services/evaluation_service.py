from __future__ import annotations

import json
import math
from pathlib import Path
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

    async def run(
        self,
        workspace_id: str,
        evaluation_id: str | None,
        variants: list[str],
        top_k: int,
    ) -> EvaluationReport:
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
        for variant in dict.fromkeys(variants):
            ranks: list[int | None] = []
            page_hits = 0
            for case in report.cases:
                hits = await self.adapter.search(
                    self.workspaces.database_path(workspace_id),
                    case.question,
                    top_k,
                    search_type=variant,
                )
                rank = None
                for index, hit in enumerate(hits, start=1):
                    ids = {hit.chunk_id, *hit.metadata.get("chunk_ids", [])}
                    if case.expected_chunk_id in ids:
                        rank = index
                        if set(case.expected_pages) & set(hit.pages):
                            page_hits += 1
                        break
                ranks.append(rank)
            total = max(len(ranks), 1)
            measured[variant] = {
                "recall_at_5": sum(rank is not None and rank <= 5 for rank in ranks) / total,
                "recall_at_10": sum(rank is not None and rank <= 10 for rank in ranks) / total,
                "mrr": sum(1 / rank for rank in ranks if rank is not None) / total,
                "ndcg_at_10": sum(
                    1 / math.log2(rank + 1) for rank in ranks if rank is not None and rank <= 10
                )
                / total,
                "page_hit_rate": page_hits / total,
            }
        report = report.model_copy(update={"variants": measured})
        self._save(report)
        return report

    def _save(self, report: EvaluationReport) -> None:
        payload = report.model_dump(mode="json")
        self.store.save_evaluation(report.id, report.workspace_id, payload)
        workspace = self.workspaces.get(report.workspace_id)
        path = Path(workspace.path) / "evaluations" / "history" / f"{report.id}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
