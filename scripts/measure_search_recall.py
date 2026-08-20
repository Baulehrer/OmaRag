#!/usr/bin/env python3
"""Measure retrieval on the path the book index actually feeds.

The bundled evaluation harness calls ``adapter.search`` directly, so it never
touches ``route_book_knowledge`` -- the printed table of contents and subject
index cannot influence its numbers at all.  This drives ``/search/explain``
instead, which runs the adaptive retrieval the answer path uses, and reports
two figures against the same stored evaluation cases:

  candidates  did retrieval plus the book router surface the expected chunk?
  ranked      did it also survive the selection gate?

The gate is deliberately conservative, so the second figure is expected to be
much lower; reporting them apart keeps a strict gate from being mistaken for
poor retrieval.

Usage:
    python scripts/measure_search_recall.py [evaluation-id] [--workspace ID]
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

WORKSPACES = Path.home() / ".local/share/omarag/workspaces"
BASE = "http://127.0.0.1:8765"


def post(path: str, body: dict, timeout: float) -> dict | list:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or "null")


def latest_evaluation(workspace: str, evaluation_id: str | None) -> dict:
    history = WORKSPACES / f"{workspace}.omarag" / "evaluations" / "history"
    if evaluation_id:
        return json.loads((history / f"{evaluation_id}.json").read_text())
    newest = max(history.glob("eval-*.json"), key=lambda item: item.stat().st_mtime)
    return json.loads(newest.read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evaluation", nargs="?", default=None)
    parser.add_argument("--workspace", default="ws-bau-2dda66")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    report = latest_evaluation(args.workspace, args.evaluation)
    cases = report.get("cases") or []
    print(f"{report.get('id')}: {len(cases)} Faelle\n")

    in_candidates = in_ranked = page_hits = failed = 0
    # Degradation accumulates over a session rather than depending on the
    # query, so record when it starts and what it said.
    degraded: list[tuple[int, str]] = []
    started = time.monotonic()
    for index, case in enumerate(cases, 1):
        try:
            explanation = post(
                f"/v1/workspaces/{args.workspace}/search/explain",
                {"query": case["question"], "limit": args.limit},
                args.timeout,
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            failed += 1
            print(f"  {index:>3}/{len(cases)}  Fehler: {exc}", flush=True)
            continue
        expected = case.get("expected_chunk_id")
        pages = set(case.get("expected_pages") or [])
        candidates = explanation.get("candidates") or []
        ranked = explanation.get("ranked") or []
        found = any(item.get("chunk_id") == expected for item in candidates)
        in_candidates += found
        in_ranked += any(item.get("chunk_id") == expected for item in ranked)
        page_hits += any(pages & set(item.get("pages") or []) for item in candidates)
        notes = [
            note
            for note in (explanation.get("provider_notes") or [])
            if "degraded" in note
        ]
        degraded.extend((index, note) for note in notes)
        print(
            f"  {index:>3}/{len(cases)}  {'TREFFER' if found else 'daneben':<8} "
            f"{len(candidates):>3} Kandidaten  {case['question'][:44]}"
            + (f"  [{len(notes)} degradiert]" if notes else ""),
            flush=True,
        )

    measured = len(cases) - failed
    if not measured:
        print("Keine Faelle gemessen.")
        return 1
    print(
        f"\nGemessen: {measured} Faelle in {time.monotonic() - started:.0f} s"
        + (f" ({failed} Fehler)" if failed else "")
    )
    print(
        f"  erwarteter Chunk unter den Kandidaten: {in_candidates}/{measured} "
        f"({100 * in_candidates / measured:.0f} %)"
    )
    print(
        f"  erwartete Seite unter den Kandidaten:  {page_hits}/{measured} "
        f"({100 * page_hits / measured:.0f} %)"
    )
    print(
        f"  nach der Auswahlschwelle uebrig:       {in_ranked}/{measured} "
        f"({100 * in_ranked / measured:.0f} %)"
    )
    if degraded:
        print(f"\nDegradierung ab Fall {degraded[0][0]} ({len(degraded)} Meldungen):")
        seen: set[str] = set()
        for _index, note in degraded:
            key = note.split(";")[0]
            if key not in seen:
                seen.add(key)
                print(f"  {note[:150]}")
    else:
        print("\nKeine Degradierung.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
