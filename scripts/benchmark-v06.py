#!/usr/bin/env python3
"""Small reproducible 0.6 ingest/retrieval benchmark against a running daemon."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--token")
    parser.add_argument("--query", default="Summarize the central technical requirements")
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    client = httpx.Client(base_url=args.url, headers=headers, timeout=120)
    started = time.perf_counter()
    accepted = client.post(
        f"/v1/workspaces/{args.workspace}/documents/ingest",
        headers={"Idempotency-Key": f"benchmark-{time.time_ns()}"},
        json={
            "sources": [{"type": "file", "path": str(args.pdf.resolve())}],
            "duplicate_policy": "skip",
        },
    )
    accepted.raise_for_status()
    job_id = accepted.json()["id"]
    while True:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.25)
    ingest_seconds = time.perf_counter() - started
    timings = []
    for _ in range(max(1, args.runs)):
        response = client.post(
            f"/v1/workspaces/{args.workspace}/search/explain",
            json={"query": args.query, "limit": 10},
        )
        response.raise_for_status()
        timings.append(response.json()["timing"]["total_ms"])
    print(
        json.dumps(
            {
                "job_status": job["status"],
                "ingest_seconds": round(ingest_seconds, 3),
                "pipeline": (job.get("result") or {})
                .get("documents", [{}])[0]
                .get("pipeline_stats", {}),
                "retrieval_ms": {
                    "min": min(timings),
                    "mean": sum(timings) / len(timings),
                    "max": max(timings),
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
