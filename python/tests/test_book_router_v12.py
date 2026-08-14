from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from omarag_bridge.models.api import CreateWorkspaceRequest
from omarag_bridge.models.book import (
    BookRagGraph,
    BookStructure,
    BookStructureNode,
    EvidenceAnchor,
    EvidenceRecord,
    KnowledgeEdge,
    KnowledgeTerm,
    TermTarget,
)
from omarag_bridge.services.book_snapshot_service import build_book_knowledge_snapshot
from omarag_bridge.services.workspace_service import WorkspaceService
from omarag_bridge.store import StateStore


def _node(node_id: str, ordinal: int, *, parent_id: str | None = None) -> BookStructureNode:
    page = ordinal + 1
    return BookStructureNode(
        node_id=node_id,
        parent_id=parent_id,
        depth=1 if parent_id else 0,
        ordinal=ordinal,
        title=f"Section {ordinal:03d}",
        normalized_title=f"section {ordinal:03d}",
        page_start=page,
        page_end=page,
        source_kind="body-heading",
        confidence=0.9,
    )


def _term(term_id: str, canonical: str) -> KnowledgeTerm:
    return KnowledgeTerm(
        term_id=term_id,
        canonical=canonical,
        normalized=canonical.casefold(),
        kind="index",
        confidence=0.95,
    )


def _install_book(
    store: StateStore,
    workspace_id: str,
    *,
    logical_id: str,
    nodes: list[BookStructureNode],
    terms: list[KnowledgeTerm],
    target_specs: Iterable[tuple[str, str, str]],
    edges: list[KnowledgeEdge] | None = None,
) -> dict[str, str]:
    """Install a compact, provenance-backed book and return evidence->segment IDs."""

    targets: list[TermTarget] = []
    evidence_by_id: dict[str, EvidenceRecord] = {}
    node_by_id = {node.node_id: node for node in nodes}
    for term_id, node_id, evidence_id in target_specs:
        node = node_by_id[node_id]
        evidence_by_id.setdefault(
            evidence_id,
            EvidenceRecord(
                evidence_id=evidence_id,
                raw_content=f"Evidence for {evidence_id}",
                content_hash=f"hash-{evidence_id}",
                anchors=[
                    EvidenceAnchor(page_no=node.page_start, source_ref=f"#/texts/{evidence_id}")
                ],
                page_start=node.page_start,
                page_end=node.page_end,
                section_node_id=node_id,
            ),
        )
        targets.append(
            TermTarget(
                term_id=term_id,
                node_id=node_id,
                page_start=node.page_start,
                page_end=node.page_end,
                evidence_id=evidence_id,
                relation="located_in",
                confidence=0.95,
            )
        )

    # Section-only graph neighbours also need representative chunks even when
    # no lexical term targets them.
    for node in nodes:
        evidence_id = f"ev-{node.node_id}"
        evidence_by_id.setdefault(
            evidence_id,
            EvidenceRecord(
                evidence_id=evidence_id,
                raw_content=f"Representative for {node.node_id}",
                content_hash=f"hash-{evidence_id}",
                anchors=[
                    EvidenceAnchor(page_no=node.page_start, source_ref=f"#/texts/{evidence_id}")
                ],
                page_start=node.page_start,
                page_end=node.page_end,
                section_node_id=node.node_id,
            ),
        )

    evidence = list(evidence_by_id.values())
    snapshot = build_book_knowledge_snapshot(
        logical_document_id=logical_id,
        generation_id=f"generation-{logical_id}",
        fingerprint=f"fingerprint-{logical_id}",
        config_hash="router-v12",
        structure=BookStructure(
            logical_document_id=logical_id,
            mode="body-headings",
            confidence=0.9,
            total_pages=max(node.page_end for node in nodes),
            nodes=nodes,
        ),
        evidence=evidence,
        graph=BookRagGraph(terms=terms, targets=targets, edges=edges or []),
    )
    evidence_segments = {
        record.evidence_id: f"{logical_id}-segment-{index}" for index, record in enumerate(evidence)
    }
    store.upsert_document(
        workspace_id,
        f"/books/{logical_id}.pdf",
        f"fingerprint-{logical_id}",
        {
            "logical_document_id": logical_id,
            "generation_id": f"generation-{logical_id}",
            "pipeline_version": "book-index-v3",
            "book_knowledge_snapshot": snapshot.model_dump(mode="json"),
            "segments": [
                {
                    "document_id": evidence_segments[record.evidence_id],
                    "segment_index": index,
                    "page_start": record.page_start,
                    "page_end": record.page_end,
                }
                for index, record in enumerate(evidence)
            ],
            "chunk_manifest": [
                {
                    "chunk_id": f"chunk-{record.evidence_id}",
                    "segment_index": index,
                    "chunk_order": index,
                    "global_order": index,
                    "content_hash": record.content_hash,
                    "pages": [record.page_start],
                    "headings": [node_by_id[record.section_node_id].title],
                    "labels": ["paragraph"],
                    "doc_item_refs": [record.anchors[0].source_ref],
                    "evidence_id": record.evidence_id,
                    "section_node_id": record.section_node_id,
                }
                for index, record in enumerate(evidence)
            ],
        },
    )
    return evidence_segments


def _workspace(tmp_path: Path) -> tuple[StateStore, str]:
    store = StateStore(tmp_path / "state.sqlite3")
    workspace = WorkspaceService(tmp_path / "workspaces", store).create(
        CreateWorkspaceRequest(name="Book router V1.2")
    )
    return store, workspace.id


def test_segment_and_document_filters_apply_before_the_router_limit(tmp_path: Path) -> None:
    store, workspace_id = _workspace(tmp_path)
    nodes = [_node(f"sec-{index}", index) for index in range(7)]
    terms = [_term(f"term-{index}", f"Needle {index:03d}") for index in range(7)]
    segments = _install_book(
        store,
        workspace_id,
        logical_id="allowed-book",
        nodes=nodes,
        terms=terms,
        target_specs=[
            (f"term-{index}", f"sec-{index}", f"ev-target-{index}") for index in range(7)
        ],
    )
    _install_book(
        store,
        workspace_id,
        logical_id="excluded-book",
        nodes=[_node("excluded", 0)],
        terms=[_term("excluded-term", "Needle 999")],
        target_specs=[("excluded-term", "excluded", "ev-excluded")],
    )

    routes = store.route_book_knowledge(
        workspace_id,
        "Needle",
        limit=1,
        allowed_document_ids={"allowed-book"},
        allowed_segment_ids={segments["ev-target-6"]},
    )

    assert [route["term_id"] for route in routes] == ["term-6"]
    assert routes[0]["logical_document_id"] == "allowed-book"
    assert routes[0]["chunk_id"] == "chunk-ev-target-6"
    # An exact target in an excluded segment must not silently fall back to a
    # different representative chunk from the same section.
    assert (
        store.route_book_knowledge(
            workspace_id,
            "Needle",
            allowed_document_ids={"allowed-book"},
            allowed_segment_ids={segments["ev-sec-6"]},
        )
        == []
    )
    assert (
        store.route_book_knowledge(
            workspace_id,
            "Needle",
            allowed_document_ids=set(),
        )
        == []
    )
    store.close()


def test_direct_targets_are_served_round_robin_across_lexical_seeds(tmp_path: Path) -> None:
    store, workspace_id = _workspace(tmp_path)
    nodes = [_node(f"sec-{index}", index) for index in range(5)]
    _install_book(
        store,
        workspace_id,
        logical_id="fair-book",
        nodes=nodes,
        terms=[_term("term-alpha", "Alpha"), _term("term-beta", "Beta")],
        target_specs=[("term-alpha", f"sec-{index}", f"ev-alpha-{index}") for index in range(4)]
        + [("term-beta", "sec-4", "ev-beta")],
    )

    routes = store.route_book_knowledge(workspace_id, "Alpha Beta", limit=2)

    assert {route["term_id"] for route in routes} == {"term-alpha", "term-beta"}
    store.close()


def test_graph_expansion_is_one_hop_capped_deduplicated_and_flag_gated(
    tmp_path: Path,
) -> None:
    store, workspace_id = _workspace(tmp_path)
    node_ids = [
        "root",
        "previous",
        "seed",
        "next",
        "child",
        "alias",
        "see",
        *[f"co-{index}" for index in range(6)],
        "deep",
    ]
    nodes = [
        _node(node_id, index, parent_id="root" if node_id in {"seed", "child"} else None)
        for index, node_id in enumerate(node_ids)
    ]
    nodes[0] = nodes[0].model_copy(update={"page_end": len(node_ids)})
    term_nodes = {
        "term-alpha": "seed",
        "term-alias": "alias",
        "term-see": "see",
        "term-duplicate": "alias",
        "term-deep": "deep",
        **{f"term-co-{index}": f"co-{index}" for index in range(6)},
    }
    terms = [
        _term(term_id, term_id.removeprefix("term-").replace("-", " ").title())
        for term_id in term_nodes
    ]
    targets = [
        (
            term_id,
            node_id,
            "ev-alias-shared" if term_id == "term-duplicate" else f"ev-target-{node_id}",
        )
        for term_id, node_id in term_nodes.items()
    ]
    # Point alias and duplicate at exactly the same raw evidence to exercise
    # global result deduplication across different graph edges.
    targets[1] = ("term-alias", "alias", "ev-alias-shared")
    targets.append(("term-alpha", "alias", "ev-alpha-alias"))
    edges = [
        KnowledgeEdge(
            edge_id="edge-alias",
            source_id="term-alpha",
            target_id="term-alias",
            relation="alias_of",
            weight=1.0,
        ),
        KnowledgeEdge(
            edge_id="edge-see",
            source_id="term-alpha",
            target_id="term-see",
            relation="see_also",
            weight=0.9,
        ),
        KnowledgeEdge(
            edge_id="edge-duplicate",
            source_id="term-alpha",
            target_id="term-duplicate",
            relation="alias_of",
            weight=0.8,
        ),
        KnowledgeEdge(
            edge_id="edge-too-deep",
            source_id="term-alias",
            target_id="term-deep",
            relation="see_also",
            weight=1.0,
        ),
        KnowledgeEdge(
            edge_id="edge-parent",
            source_id="root",
            target_id="seed",
            relation="parent_of",
            weight=1.0,
        ),
        KnowledgeEdge(
            edge_id="edge-child",
            source_id="seed",
            target_id="child",
            relation="parent_of",
            weight=1.0,
        ),
        KnowledgeEdge(
            edge_id="edge-previous",
            source_id="previous",
            target_id="seed",
            relation="next_section",
            weight=1.0,
        ),
        KnowledgeEdge(
            edge_id="edge-next",
            source_id="seed",
            target_id="next",
            relation="next_section",
            weight=1.0,
        ),
        KnowledgeEdge(
            edge_id="edge-child-previous",
            source_id="alias",
            target_id="child",
            relation="next_section",
            weight=0.7,
        ),
        *[
            KnowledgeEdge(
                edge_id=f"edge-co-{index}",
                source_id="term-alpha",
                target_id=f"term-co-{index}",
                relation="co_occurs",
                weight=float(10 - index),
            )
            for index in range(6)
        ],
    ]
    _install_book(
        store,
        workspace_id,
        logical_id="graph-book",
        nodes=nodes,
        terms=terms,
        target_specs=targets,
        edges=edges,
    )

    direct = store.route_book_knowledge(workspace_id, "Alpha", limit=20)
    assert {route["term_id"] for route in direct} == {"term-alpha"}

    expanded = store.route_book_knowledge(
        workspace_id,
        "Alpha",
        limit=20,
        expand_sections=True,
        global_query=True,
    )
    paths = [route["retrieval_path"] for route in expanded]
    chunk_ids = [route["chunk_id"] for route in expanded]

    assert "book-graph-alias_of" in paths
    assert "book-graph-see_also" in paths
    assert paths.count("book-graph-co_occurs") == 4
    assert "book-graph-parent" in paths
    assert "book-graph-child" in paths
    assert "term-deep" not in {route["term_id"] for route in expanded}
    assert len(chunk_ids) == len(set(chunk_ids))
    assert len(expanded) <= 20

    adjacency = store.route_book_knowledge(
        workspace_id,
        "Alpha",
        limit=20,
        include_adjacency=True,
    )
    adjacency_paths = [route["retrieval_path"] for route in adjacency]
    assert adjacency_paths.count("book-graph-previous") == 1
    assert adjacency_paths.count("book-graph-next") == 1
    store.close()
