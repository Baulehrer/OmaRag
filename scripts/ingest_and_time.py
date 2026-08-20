#!/usr/bin/env python3
"""Ingest one file and record what each pipeline phase cost."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"
DONE = {"succeeded", "completed", "failed", "cancelled", "blocked"}


def api(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    head = {"content-type": "application/json", **(headers or {})}
    request = urllib.request.Request(
        BASE + path, data=data, method=method, headers=head
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read() or "null")
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode()[:400]}", file=sys.stderr)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("--workspace", default="ws-bau-2dda66")
    parser.add_argument("--duplicate-policy", default="review")
    args = parser.parse_args()

    started = api(
        "POST",
        f"/v1/workspaces/{args.workspace}/documents/ingest",
        {
            "sources": [{"type": "file", "path": args.source}],
            "tags": [],
            "metadata": {},
            "parser_id": "auto",
            "processing_profile": "default",
            "duplicate_policy": args.duplicate_policy,
            "validity_policy": "prefer-current",
            "indexing": {
                "pipeline": "book-v3",
                "enrichment": "captions",
                "llm_fallback": "auto",
                "visual_dense": "off",
            },
        },
        {"Idempotency-Key": f"ingest-{int(time.time())}"},
    )
    job_id = started["id"]
    print(f"job {job_id}", flush=True)

    phases: list[tuple[str, float]] = []
    phase = None
    began = start = time.monotonic()
    while True:
        time.sleep(3.0)
        snapshot = api("GET", f"/v1/jobs/{job_id}")
        now = time.monotonic()
        current = snapshot.get("phase") or snapshot.get("status")
        if current != phase:
            if phase is not None:
                phases.append((phase, now - began))
            detail = snapshot.get("progress_detail") or {}
            print(
                f"  {now - start:7.1f}s  {current:<14} "
                f"{detail.get('page_end') or '-'}/{detail.get('total_pages') or '-'}",
                flush=True,
            )
            phase, began = current, now
        if snapshot.get("status") in DONE:
            phases.append((phase, now - began))
            break

    print(f"\nstatus {snapshot.get('status')}   total {time.monotonic() - start:.1f} s")
    for name, seconds in phases:
        print(f"  {name:<14} {seconds:8.1f} s")
    if snapshot.get("error"):
        print(f"error: {json.dumps(snapshot['error'], ensure_ascii=False)[:400]}")
    return 0 if snapshot.get("status") in {"succeeded", "completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
