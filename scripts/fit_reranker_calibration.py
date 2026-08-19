#!/usr/bin/env python3
"""Fit the reranker's Platt calibration on real book pairs.

The shipped profile carries ``dataset_digest="bootstrap-silver-v1-not-release-gold"``
-- it was an estimate, never fitted.  Measured against a construction textbook,
it rejected 92 % of publisher-verified correct matches: the selection gate saw
raw cross-encoder scores around -5.7 while the strict threshold needs +0.77, so
``adaptive_select`` discarded nearly every candidate and answers had no evidence.

Labels come from the book's own printed subject index, which is expert-curated.
A locator names a *page*, and the graph builder turns it into one target per
chunk on that page -- 6.8 on average.  Only 17 % of those chunks actually
mention the term, so taking every target as a positive pair labels mostly noise
and makes any reranker look broken.  A positive therefore requires the chunk to
contain the term.  That leans towards lexical overlap and will slightly favour
lexical matchers, which is the lesser error.

Negatives are other chunks of the same book -- one from the same section,
because a threshold that only separates a term from unrelated pages is not
worth having.

This is silver, not gold: nobody reviewed the pairs by hand, and the fit covers
the books present in the workspace.  The emitted ``dataset_digest`` says so.

Usage:
    python scripts/fit_reranker_calibration.py [workspace_id] [--positives N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import statistics
import sys
import unicodedata
from pathlib import Path

STATE = Path.home() / ".local/share/omarag/omarag.sqlite3"
WORKSPACES = Path.home() / ".local/share/omarag/workspaces"
SEED = 20260819


def _folded(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold().replace("ß", "ss")
    return "".join(char for char in value if not unicodedata.combining(char))


def _mentions(term: str, text: str) -> bool:
    """Whether the chunk actually names the term the locator pointed at."""

    head = _folded(term).split()
    return bool(head) and head[0] in _folded(text)


def load_pairs(
    workspace_id: str, positives: int
) -> tuple[list[tuple[str, str]], list[int], dict]:
    connection = sqlite3.connect(f"file:{STATE}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """SELECT t.canonical, m.chunk_id, m.section_node_id
           FROM book_terms t
           JOIN book_term_targets g USING(workspace_id, term_id)
           JOIN chunk_manifest m
             ON m.workspace_id = g.workspace_id AND m.evidence_id = g.evidence_id
           WHERE t.workspace_id = ? AND t.kind = 'index' AND g.evidence_id IS NOT NULL""",
        (workspace_id,),
    ).fetchall()
    sections: dict[str, list[str]] = {}
    for row in connection.execute(
        "SELECT chunk_id, section_node_id FROM chunk_manifest WHERE workspace_id = ?",
        (workspace_id,),
    ):
        sections.setdefault(str(row["section_node_id"]), []).append(
            str(row["chunk_id"])
        )
    connection.close()
    if not rows:
        raise SystemExit(
            "No index-derived pairs: index a book whose subject index was parsed."
        )

    import lancedb

    database = WORKSPACES / f"{workspace_id}.omarag" / "database" / "knowledge.lancedb"
    table = lancedb.connect(str(database)).open_table("chunks").to_arrow().to_pylist()
    body = {row["id"]: (row.get("content") or "") for row in table}
    everything = [key for key, text in body.items() if text]

    rng = random.Random(SEED)
    chosen = rng.sample(rows, min(positives * 8, len(rows)))
    pairs: list[tuple[str, str]] = []
    labels: list[int] = []
    for row in chosen:
        correct = body.get(row["chunk_id"])
        if not correct:
            continue
        # The German head of a bilingual entry is what a reader would type.
        term = str(row["canonical"]).split(",")[0].strip()
        if not _mentions(term, correct):
            continue
        pairs.append((term, correct[:1200]))
        labels.append(1)
        neighbours = [
            item
            for item in sections.get(str(row["section_node_id"]), [])
            if item != row["chunk_id"] and body.get(item)
        ]
        if sum(labels) > positives:
            break
        near = rng.sample(neighbours, min(1, len(neighbours)))
        far = rng.sample(everything, 2)
        for other in [*near, *far]:
            if other == row["chunk_id"]:
                continue
            pairs.append((term, body[other][:1200]))
            labels.append(0)
    stats = {"terms": len(chosen), "pairs": len(pairs), "positives": sum(labels)}
    return pairs, labels, stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", nargs="?", default="ws-bau-2dda66")
    parser.add_argument("--positives", type=int, default=300)
    parser.add_argument(
        "--apply", action="store_true", help="print the code block to paste"
    )
    parser.add_argument(
        "--model",
        default=None,
        help="cross-encoder to measure instead of the configured one; "
        "separation is reported either way so candidates can be compared",
    )
    parser.add_argument("--revision", default=None)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
    from omarag_bridge.services.query_v2 import (
        DEFAULT_RERANKER_CALIBRATOR,
        RETRIEVAL_POLICY_DIGEST,
        PlattCalibrator,
        SelectionPolicy,
    )
    from omarag_bridge.services.reranker_service import (
        DEFAULT_RERANKER,
        DEFAULT_RERANKER_REVISION,
    )

    pairs, labels, stats = load_pairs(args.workspace, args.positives)
    print(
        f"{stats['terms']} Begriffe -> {stats['pairs']} Paare, davon {stats['positives']} positiv"
    )

    from sentence_transformers import CrossEncoder

    name = args.model or DEFAULT_RERANKER
    revision = args.revision or (None if args.model else DEFAULT_RERANKER_REVISION)
    print(f"Modell: {name}" + (f" @ {revision}" if revision else ""))
    model = CrossEncoder(name, revision=revision) if revision else CrossEncoder(name)
    scores = [float(value) for value in model.predict(pairs)]

    positive = [s for s, y in zip(scores, labels, strict=True) if y == 1]
    negative = [s for s, y in zip(scores, labels, strict=True) if y == 0]
    print(
        f"Rohwerte  positiv median {statistics.median(positive):+.2f}  "
        f"negativ median {statistics.median(negative):+.2f}"
    )
    # Separation is what decides whether a threshold can work at all: a Platt
    # fit only moves the cut, it cannot pull apart overlapping distributions.
    better = sum(1 for p in positive for n in negative if p > n)
    ties = sum(1 for p in positive for n in negative if p == n)
    auc = (better + 0.5 * ties) / (len(positive) * len(negative))
    print(f"Trennschaerfe AUC {auc:.3f}  (0,5 = Zufall)")

    fitted = PlattCalibrator.fit(
        model_digest=DEFAULT_RERANKER_CALIBRATOR.model_digest or "",
        dataset_digest="workspace-index-silver-"
        + hashlib.sha256(json.dumps(stats, sort_keys=True).encode()).hexdigest()[:12],
        policy_digest=RETRIEVAL_POLICY_DIGEST,
        scores=scores,
        labels=labels,
    )
    policy = SelectionPolicy()
    print(
        f"\nvorher   scale {DEFAULT_RERANKER_CALIBRATOR.scale:+.4f}  "
        f"bias {DEFAULT_RERANKER_CALIBRATOR.bias:+.4f}"
    )
    print(f"nachher  scale {fitted.scale:+.4f}  bias {fitted.bias:+.4f}")

    print("\nDurchlassquote je Schwelle (positiv = gewollt, negativ = Fehlannahme):")
    for name in ("explore", "normal", "strict"):
        threshold = max(policy.candidate_floor, policy.threshold(name))
        for label, calibrator in (
            ("vorher", DEFAULT_RERANKER_CALIBRATOR),
            ("nachher", fitted),
        ):
            hits = sum(calibrator.probability(s) >= threshold for s in positive)
            false = sum(calibrator.probability(s) >= threshold for s in negative)
            print(
                f"  {name:<8} {threshold:.2f}  {label:<8} "
                f"positiv {hits:>4}/{len(positive)} ({100 * hits / len(positive):5.1f} %)   "
                f"negativ {false:>4}/{len(negative)} ({100 * false / len(negative):5.1f} %)"
            )
    if args.apply:
        print(f"""
DEFAULT_RERANKER_CALIBRATOR = PlattCalibrator(
    scale={fitted.scale!r},
    bias={fitted.bias!r},
    digest={fitted.digest!r},
    model_digest={fitted.model_digest!r},
    dataset_digest={fitted.dataset_digest!r},
    policy_digest=RETRIEVAL_POLICY_DIGEST,
)""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
