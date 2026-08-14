# Implementation status: 1.2.0

Updated: 14 August 2026. The plugin system is deliberately out of scope.

## Ready in 1.2

- **Book Index V3:** preserves evidence at page-range boundaries and marks prose, tables, formulas,
  figures, navigation, OCR, and legacy content separately. A bounded local fallback may improve
  structure, but only unchanged raw book content can support an answer.
- **Progressive retrieval:** batches related searches and expands them only when required evidence
  is missing. Fast, Normal, and Quality keep bounded, adaptive budgets.
- **Source-bound answers:** RAG and analysis use the same path. Normal and Explore cannot add
  unsupported model knowledge. Exact support spans and honest calibration/verification status are
  stored when available.
- **Fail-closed verification:** missing calibration never becomes a confidence score. Risky claims
  are rejected when no suitable verifier is available, unless verification is explicitly disabled
  in an expert workspace.
- **Privacy:** opaque source references and one egress policy protect paths and content. Device-only
  mode blocks content-bearing external requests; LAN and cloud require separate consent.
- **Retention and deletion:** new workspaces use limited retention. Existing workspaces keep their
  old unlimited history until the user opts in. Cleanup needs current preflight and confirmation;
  pinned provenance is preserved. Permanent deletion removes the managed original and all related
  index, graph, media, preview, and cache data.
- **Consistent backups:** new backups include the workspace's SQLite generation catalog. Legacy
  filesystem-only backups fail closed instead of restoring mixed state.
- **Lower resource use:** compressed bounded caches, lazy media processing, writer-lease release for
  paused jobs, and adaptive 30-second-to-five-minute model residency reduce storage and memory use.
- **Pinned providers:** Run, Search, Explain, and exact-cache acceptance verify the generator/VL,
  embedder, and reranker identities actually used. A model change during work invalidates the
  result.

## Preserved foundation

- Ten hardware tiers and the release-bound model catalog `2026.08.1`; model downloads always require
  explicit `DOWNLOAD_MODELS` consent.
- Fast, Normal, and Quality profiles with question-aware budgets and expert overrides.
- Stable figure/table crops with page, bounding-box, hash, caption, and graph provenance. The
  inspector shows cited pages first and then up to four relevant crops.
- The verified Book V2 runtime remains Haiku RAG Slim `0.74.0` with Docling `2.119.0`, used only
  through public APIs. Existing V2 indexes remain readable.
- Immutable managed originals, segmented long-book indexing, resumable jobs, exact-page previews,
  stable evidence IDs, and generation-aware exact-answer caching.
- English Ratatui UI, scriptable CLI, API daemon, read-only MCP process, non-root Docker image,
  AppImage client, installer, and update metadata.

## Safety and compatibility

- The packaged target remains Linux x86-64. The AppImage is the console client; Docker or the
  installer supplies the Python/Haiku daemon.
- No scan, launch, benchmark, query, or catalog refresh silently downloads models.
- A populated library cannot switch embedders with plain `APPLY`. Full rebuild requires persisted
  preflight, explicit `REINDEX` or `APPLY_AND_REINDEX` confirmation, verified source fingerprints,
  enough disk space, exact runtime/model digests, and a resumable maintenance boundary.
- A generation becomes `ready` only after complete page coverage and all evidence, structure,
  snapshot, and graph references validate. Queries stay blocked during failed maintenance.
- A missing resident model causes only cold-start delay. A model digest mismatch or an embedder that
  differs from the ready generation is a hard error.
- Search inspection may show at most three clearly marked uncalibrated hits after reranker failure.
  Answer generation instead reports insufficient evidence.
- Media endpoints enforce workspace ownership and bounded paths; they never expose arbitrary local
  files.

## Honest limits

- The private 300-case gold set required for release-grade calibration is not distributed.
- No production NLI verifier model is bundled. Automatic risky-claim handling therefore fails
  closed.
- Visual Dense is experimental and unavailable until its media-quality gate passes. Caption, page,
  and graph retrieval remain the default.
- Late Chunking is research work, not a production feature.
- V1.2 finds formulas, variables, and table values but does not perform domain calculations.
- User-attached images are not accepted as new evidence sources in V1.2.

## Release checks

- Rust formatting, Clippy with warnings denied, and all Rust tests.
- Ruff formatting/linting and all non-environmental Python tests.
- Generated OpenAPI, event, and workspace contract drift checks.
- Non-root Docker build and health smoke test.
- AppImage execution and SHA-256 verification.

Future work belongs in GitHub issues, not disabled UI.
