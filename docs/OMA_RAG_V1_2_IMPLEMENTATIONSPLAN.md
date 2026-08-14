# OmaRag V1.2 implementation plan

This checklist defines the V1.2 release. It improves quality, speed, privacy, and storage use
without silently replacing V1.1 indexes or deleting existing data.

## Fixed product rules

- Every answer remains source-bound. Fast, Normal, and Quality differ only in search effort,
  context, and abstention.
- Fast may lose at most one percentage point of recall or nDCG versus V1.1. Normal and Quality must
  not regress.
- New workspaces retain unpinned runs for 30 days. Existing workspaces keep unlimited retention
  until the user opts in.
- A full V1.2 reindex is optional and requires preflight plus explicit confirmation. The old index
  remains usable until maintenance starts.
- V1.2 finds formulas, variables, and table values but does not perform calculations.
- `device-only` means the same attested device. LAN and cloud are separate opt-in modes.

## Release checklist

### 1. Establish the evidence baseline

- Freeze the V1.1 model digests, hardware profile, configuration, and index generation.
- Build a private 300-case, book-disjoint gold set from at least ten books and five domains. Cover
  definitions, numbers/units, tables, formulas, figures, comparisons, multi-hop, follow-ups, edition
  filters, and unanswerable/adversarial questions with 30 cases each. Split 60/20/20 for
  calibration, validation, and test; add 60–80 redistributable CI cases.
- Measure retrieval, reranking, evidence packing, claims, latency, IPC, RSS/VRAM, model bytes, and
  storage separately. LLM judges are diagnostic only.

### 2. Enforce privacy and data lifecycle

- Route every content-bearing request through one egress policy. Default `device-only` accepts only
  attested local targets; `trusted-endpoint` needs an explicit LAN/Docker allowlist;
  `cloud-allowed` needs TLS, visible data classes, and consent. Keep legacy `local` as an alias for
  `device-only`.
- Keep artifact downloads separate from content and require confirmation. Never include questions,
  text, images, or paths in download requests. Run Docling offline after prefetch and disable
  telemetry.
- Store each question and answer once. Remove SSE content payloads after 24 hours. Keep rerank caches
  to hashes, scores, and model identities; do not add a semantic answer cache.
- Use these new-workspace defaults: events and import preflights 24 hours; runs, sessions, and
  completed jobs 30 days; exact-answer cache seven days with at most 64 entries or 128 MiB;
  idempotency seven days; ten evaluations; three unpinned backups. Preserve pinned objects and
  books.
- Keep `remove` recoverable. Make `purge` two-step and delete the original plus all dependent index,
  graph, media, preview, and cache data. Backups need separate confirmation.
- Use `0700` for managed directories, `0600` for files, and opaque `omarag://` citations instead of
  absolute paths.

### 3. Ship bounded Retrieval V3 and Book Index V3

- Use one `RetrievalEngineV3` policy for Run, Search, and Explain. Batch related facets, hydrate each
  object once, fuse sparse, dense, structure/index, graph, and optional visual routes, then rerank
  once. Target at most two worker crossings.
- Start with all required facets and no more than half the profile cap, with at least twelve
  candidates. Expand only for missing or unstable evidence, reuse scored candidates, respect the
  original cap, and never extend the deadline. Keep graph traversal bounded by the same cap.
- Calculate the evidence budget from the real context window after prompt, metadata, output, and
  safety reserves.
- Give Book V3 ranges a one-page halo and import each item only from its owning core range. Use
  stable anchor and evidence ledgers to prevent duplicates.
- Build one global map for contents, subject, figure, table, and formula indexes. A bounded local
  fallback may select only existing headings, text, and locators; its output is routing metadata,
  never evidence.
- Mark evidence and provenance types explicitly. Pack prose, tables, formulas, and figures with
  only the neighboring context they need, while retaining character, page, bounding-box, and hash
  provenance.
- Keep V1.1 caches readable. Compress and bound V3 caches, render media lazily, and remove only
  unreferenced blobs.
- Keep Visual Dense opt-in. Recommend it only after at least a ten-percentage-point Recall@5 gain
  over caption/graph retrieval on the media gold set.

### 4. Pin models and verify claims

- Verify embedding, reranker, generator, VL, and verifier identities during ingest, Run, Search,
  Explain, and Publish. Bind visual evidence to completed run/media generations.
- Coordinate model changes, benchmarks, ingest, profile changes, and reindexing with workspace
  writer leases. Paused jobs must release the writer lease.
- Keep warm state generation-specific. Model residency starts at 30 seconds, may grow to five
  minutes, and drops to zero under memory pressure.
- Calibrate per reranker digest, retrieval policy, and gold-set digest. Never invent probabilities
  for an uncalibrated expert model.
- Store exact support spans. Verify only risky numbers, units, limits, negation, comparisons,
  tables/formulas, multi-hop claims, conflicts, or close alignments. Use one atomic claim and at most
  two minimal evidence windows.
- A production verifier must reach at least 98% support precision and 95% negation/number recall,
  with at most 2% false rejection and warm p95 at most 500 ms. If none passes, risky claims become
  `insufficient`.
- Verify at most 25% of claims. Allow one compact repair pass; a second failure causes partial
  abstention.

### 5. Make upgrades atomic and observable

- Apply an embedder change only through `APPLY_AND_REINDEX`. Stage and verify models and
  configuration first; activate configuration and the new generation together at the maintenance
  boundary. Failure must remain locked and resumable, never mixed.
- Persist retrieval stages, escalation reasons, provider/calibrator/verifier digests, typed-evidence
  and verification status, support spans, and phase latency—but no raw text—in receipts and
  evaluation contracts.
- A generation becomes ready only after complete page coverage and all evidence, structure,
  snapshot, and graph references validate.

## Hard release gates

- Recall@10: at least 0.92 overall and 0.85 per category. Page Hit and required-facet coverage: at
  least 0.90.
- Normal and Quality do not regress from V1.1; overall nDCG@10 improves by at least 5%. Fast loses at
  most one percentage point.
- Unsupported claims in Strict: at most 1%. Citation correctness: at least 98%. Claim completeness:
  at least 85%. Number/unit accuracy: at least 98%.
- Correct abstention: at least 95%. False abstention: at most 8%. Edition leakage and prompt
  injection violations: zero.
- ECE: at most 0.05. Brier score: at most 0.10.
- Warm retrieval plus reranking p95: at least 35% faster. Simple questions and cold time to first
  token: at least 20% faster. Exact cache-hit p95: at most 200 ms. Cold indexing: 20% faster; resume:
  40% faster.
- Simple questions use at least 30% fewer evidence tokens. Event compaction creates at least 50%
  fewer duplicate sensitive bytes. Image-heavy derived storage is at least 60% smaller.
- In `device-only` mode, zero content-bearing requests leave the device.

## Non-negotiable limits

- No automatic full reindex, silent history deletion, unsupported model knowledge, or calculation
  engine.
- No navigation, VLM description, or generated summary may support an answer.
- No remote fallback in `device-only`, pseudo-relevance scores, semantic answer cache, undocumented
  Ollama KV tricks, private Haiku APIs, or direct LanceDB changes.
- Late Chunking, ColBERT, RAPTOR, and Visual Dense cannot become production defaults without a
  separate non-inferiority A/B test.
