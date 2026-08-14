from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from ..models.book import (
    BookKnowledgeSnapshot,
    BookRagGraph,
    BookStructure,
    EvidenceAnchor,
    EvidenceRecord,
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def stable_evidence_id(
    book_fingerprint: str,
    pipeline_config_hash: str,
    anchors: Sequence[EvidenceAnchor],
    raw_content: str,
) -> str:
    """Hash exact evidence identity independently from Haiku's generated chunk id."""

    if not anchors:
        raise ValueError("Evidence requires at least one provenance anchor")
    canonical_anchors = sorted(
        [
            {
                "page_no": anchor.page_no,
                "source_ref": anchor.source_ref,
                "bbox": (
                    [round(value, 6) for value in anchor.bbox] if anchor.bbox is not None else None
                ),
                "label": anchor.label,
            }
            for anchor in anchors
        ],
        key=lambda item: (
            item["page_no"],
            item["source_ref"],
            item["bbox"] or [],
            item["label"] or "",
        ),
    )
    content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()
    material = {
        "schema": "omarag-evidence-v2",
        "book_fingerprint": book_fingerprint,
        "pipeline_config_hash": pipeline_config_hash,
        "anchors": canonical_anchors,
        "content_hash": content_hash,
    }
    return f"ev-{hashlib.sha256(_canonical_json(material)).hexdigest()}"


def build_book_knowledge_snapshot(
    *,
    logical_document_id: str,
    generation_id: str,
    fingerprint: str,
    config_hash: str,
    structure: BookStructure,
    evidence: Sequence[EvidenceRecord],
    graph: BookRagGraph,
) -> BookKnowledgeSnapshot:
    """Validate and freeze the complete deterministic Core sidecar as v2 JSON."""

    if structure.logical_document_id != logical_document_id:
        raise ValueError("Structure belongs to a different logical document")
    node_by_id = {node.node_id: node for node in structure.nodes}
    if len(node_by_id) != len(structure.nodes):
        raise ValueError("Structure contains duplicate node ids")
    if len({node.ordinal for node in structure.nodes}) != len(structure.nodes):
        raise ValueError("Structure contains duplicate ordinals")
    node_ids = set(node_by_id)
    for node in structure.nodes:
        if node.page_start > node.page_end or node.page_end > structure.total_pages:
            raise ValueError(f"Structure node {node.node_id} has an invalid page range")
        if node.parent_id is None:
            continue
        parent = node_by_id.get(node.parent_id)
        if parent is None:
            raise ValueError(f"Structure node {node.node_id} has an unknown parent")
        if parent.depth >= node.depth:
            raise ValueError(f"Structure node {node.node_id} has an invalid parent depth")
        if parent.page_start > node.page_start or parent.page_end < node.page_end:
            raise ValueError(f"Structure node {node.node_id} is outside its parent range")
    for node in structure.nodes:
        visited: set[str] = set()
        current = node
        while current.parent_id is not None:
            if current.node_id in visited:
                raise ValueError(f"Structure contains a parent cycle at {current.node_id}")
            visited.add(current.node_id)
            current = node_by_id[current.parent_id]
    evidence_ids: set[str] = set()
    for record in evidence:
        if record.evidence_id in evidence_ids:
            raise ValueError(f"Duplicate evidence id: {record.evidence_id}")
        evidence_ids.add(record.evidence_id)
        if record.section_node_id not in node_ids:
            raise ValueError(f"Unknown evidence section: {record.section_node_id}")
        if not record.anchors:
            raise ValueError(f"Evidence {record.evidence_id} has no provenance")
        if record.page_start > record.page_end:
            raise ValueError(f"Evidence {record.evidence_id} has an invalid page range")
        if any(
            anchor.page_no < record.page_start or anchor.page_no > record.page_end
            for anchor in record.anchors
        ):
            raise ValueError(f"Evidence {record.evidence_id} anchor is outside its page range")
    for record in evidence:
        for neighbor in (record.previous_evidence_id, record.next_evidence_id):
            if neighbor is not None and neighbor not in evidence_ids:
                raise ValueError(f"Evidence {record.evidence_id} has an unknown neighbor")
            if neighbor == record.evidence_id:
                raise ValueError(f"Evidence {record.evidence_id} links to itself")
    term_ids = {term.term_id for term in graph.terms}
    if len(term_ids) != len(graph.terms):
        raise ValueError("Graph contains duplicate term ids")
    if len({term.normalized for term in graph.terms}) != len(graph.terms):
        raise ValueError("Graph contains duplicate normalized terms")
    for alias in graph.aliases:
        if alias.term_id not in term_ids:
            raise ValueError(f"Alias points to unknown term: {alias.term_id}")
    for target in graph.targets:
        if target.term_id not in term_ids:
            raise ValueError(f"Target points to unknown term: {target.term_id}")
        if target.node_id is not None and target.node_id not in node_ids:
            raise ValueError(f"Target points to unknown section: {target.node_id}")
        if target.evidence_id is not None and target.evidence_id not in evidence_ids:
            raise ValueError(f"Target points to unknown evidence: {target.evidence_id}")
        if target.page_start is not None and target.page_start > structure.total_pages:
            raise ValueError(f"Target page is outside the book: {target.term_id}")
        if (
            target.page_start is not None
            and target.page_end is not None
            and target.page_start > target.page_end
        ):
            raise ValueError(f"Target has an invalid page range: {target.term_id}")
        if target.node_id is not None and target.evidence_id is not None:
            record = next(item for item in evidence if item.evidence_id == target.evidence_id)
            if record.section_node_id != target.node_id:
                raise ValueError(f"Target section disagrees with its evidence: {target.term_id}")
    graph_ids = node_ids | term_ids
    edge_ids: set[str] = set()
    for edge in graph.edges:
        if edge.edge_id in edge_ids:
            raise ValueError(f"Duplicate graph edge id: {edge.edge_id}")
        edge_ids.add(edge.edge_id)
        if edge.source_id not in graph_ids or edge.target_id not in graph_ids:
            raise ValueError(f"Graph edge {edge.edge_id} has an unknown endpoint")
        if any(item not in evidence_ids for item in edge.evidence_ids):
            raise ValueError(f"Graph edge {edge.edge_id} has unknown evidence")
        expected_ids = node_ids if edge.relation in {"parent_of", "next_section"} else term_ids
        if edge.source_id not in expected_ids or edge.target_id not in expected_ids:
            raise ValueError(f"Graph edge {edge.edge_id} has invalid endpoint kinds")
    payload = {
        "schema_version": "2",
        "logical_document_id": logical_document_id,
        "generation_id": generation_id,
        "fingerprint": fingerprint,
        "config_hash": config_hash,
        "structure": structure.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "graph": graph.model_dump(mode="json"),
    }
    content_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return BookKnowledgeSnapshot(
        logical_document_id=logical_document_id,
        generation_id=generation_id,
        fingerprint=fingerprint,
        config_hash=config_hash,
        content_hash=content_hash,
        structure=structure,
        evidence=list(evidence),
        graph=graph,
        stats={
            "structure_nodes": len(structure.nodes),
            "evidence_chunks": len(evidence),
            "terms": len(graph.terms),
            "aliases": len(graph.aliases),
            "targets": len(graph.targets),
            "edges": len(graph.edges),
        },
    )
