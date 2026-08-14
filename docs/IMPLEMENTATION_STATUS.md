# Release scope: 1.0.0

Status: 13 August 2026. The plugin system remains deliberately out of scope.

## Included

- English, SOAP-inspired Ratatui shell with a persistent sidebar, task workspace and contextual
  inspector; fourteen themes plus keyboard and mouse control.
- Chat, Library, Models and Settings are the four core sections. Indexing absorbs activity, Help
  stays behind `?`, and Simple mode hides advanced views without deleting their configuration.
- All mutable controls live in the center workspace. The right inspector is read-only except for
  selecting and opening Chat sources and their exact PDF pages.
- Library creation, profile selection, multi-select PDF/folder browser and one-step ingest queue.
- Book-v2's verified runtime pair is exactly Haiku RAG Slim `0.74.0` and Docling `2.119.0`.
  The adapter uses public APIs only; there is no local Haiku fork, patch or direct LanceDB access.
- A compatibility probe gates the public convert/chunk/embed/import/search/chunk-lookup boundary,
  absolute Docling page ranges and heading hierarchy before the runtime is accepted.
- The PDF pipeline identifier is exactly `book-index-v2`. It converts immutable originals in
  bounded absolute ranges with one converter per book, then applies the globally reconciled book
  structure in a second pass before embedding and import.
- Printed contents, subject index, glossary, abbreviation and symbol lists contribute navigation,
  aliases and routing. PDF bookmarks and body headings provide independent structure signals;
  deterministic page windows cover books without a usable hierarchy.
- Raw Haiku chunks remain the only answer evidence. Stable evidence records, the book tree,
  register routes and the provenance-bound BookRAG-lite graph are derived sidecars and snapshots.
- Selective per-page OCR, header/footer suppression and extraction quality/provenance reporting.
- Bibliographic import preflight with confirmed title, author, ISBN, edition and validity metadata.
- Hash-addressed immutable originals and explicit current-edition/document filtering.
- Strict, Normal and Explore evidence modes with stable evidence IDs and technical-token checks.
- Deterministic Silver evaluation and FTS/vector/hybrid A/B retrieval metrics.
- Markdown-rendered answers plus keyboard/mouse-selectable citations and exact-page previews.
- Persistent conversation IDs, a bounded generation-aware exact-answer cache and a compact answer
  receipt showing fresh/reused answers, checked sources and known/new evidence.
- Query-v2 classifies simple, standard and complex questions deterministically, searches bounded
  facets in parallel, adds book-tree/register/KG routes, fuses them with weighted RRF, reranks once
  and selects a thresholded, deduplicated and diversified evidence set.
- Query receipts expose complexity, facets, effective budgets, candidate and selected counts,
  cutoff, facet coverage, fallbacks, phase latency, model digests and time to first accepted claim.
- Segmented long-book processing without a product file/page limit.
- Real phase telemetry, measured indexing ETA ranges and a compact Metis/Aletheia activity display.
- Book-scoped chat, a first-run readiness card and lazy evidence enrichment only when selected.
- Safe opportunistic model warm-up, adaptive worker residency and idle TUI redraw throttling.
- A scriptable `oracle-cli doctor` readiness check with plain-English corrective actions.
- Immutable managed originals using reflinks where possible and a single-pass copy/hash fallback.
- Cooperative pause between PDF segments and chat-priority resource coordination.
- Lazy API startup plus isolated import/query workers; native model memory is reclaimed when the
  worker exits instead of remaining swapped out in the idle daemon.
- Chat, VL, Embedding and Rerank catalog with atomic Fast/Balanced/Quality defaults, custom Ollama
  IDs, streamed GGUF import, quantization, context and residency controls.
- Persistent workspaces, jobs, events, configuration, verified backups and safe restore.
- TUI, scriptable CLI, API daemon and read-only MCP process.
- Private persistent bearer token, slim Haiku dependency set, non-root Docker image and delegated
  per-worker memory limits in the systemd service.
- AppImage client, release installer, weekly auto-update timer and zsync update metadata.

## Deliberate 1.0 boundaries

- Linux x86_64 is the packaged target.
- AppImage contains the console client; Docker or the installer provides the Python/Haiku daemon.
- Local source opening is supported. Authenticated remote evidence previews are rendered by the
  daemon; opening the original file still requires a reachable desktop path.
- Page images use Docling provenance and PDF rendering. VL is deliberately selective and never
  scans every page by default.
- Jobs resumed after a daemon restart continue after the last committed page segment. Completed
  Docling conversions are reused from a bounded content-addressed cache.
- Source-manager compatibility endpoints remain in API v1 but are not part of the primary TUI flow.
- Parser choice currently resolves `auto` and `docling` to Docling. The API shape leaves room for
  additional parsers without changing the import interaction.
- `llm_fallback=auto` is accepted as an indexing option, but low-confidence navigation regions are
  currently rejected by deterministic rules. The result reports `llm_fallback_used=false`; it does
  not silently claim model-assisted reconciliation. Requested VLM enrichment is reported in the
  same way and currently retains existing captions/provenance without running a VLM.
- BookRAG-lite is a lexical and structural routing graph, not a generated-fact knowledge base and
  not an independent evidence index. Every usable target resolves back to a raw evidence chunk.

## Operational gates and degradation

- A full rebuild has a persisted preflight and requires the explicit `REINDEX` confirmation. It
  verifies archived originals and SHA-256 fingerprints, disk reserve, cache writability, the exact
  Haiku/Docling pair, workspace-config hash and the installed embedding-model digest.
- Source fingerprints, catalogue epoch and the frozen runtime lock are checked again before any
  destructive step and at the exclusive rebuild boundary. Full rebuild is deliberately in-place:
  there is no live rollback, but checkpoints make an interrupted rebuild resumable.
- A generation becomes `ready` only after homogeneous pipeline/generation, complete absolute page
  coverage, chunk/evidence chains, structures, snapshots and graph references validate. Queries are
  blocked while the generation is in maintenance and after `maintenance_failed` until resumed.
- `/v1/readiness` reports control-plane/adapter availability. Workspace query readiness separately
  checks the ready index generation plus resident generator and embedding models. Missing residents
  are a cold-start/latency degradation; digest mismatch or an embedding digest different from the
  ready generation is a hard error.
- Search inspection can return at most three explicitly uncalibrated fused hits when the reranker
  fails. Query-v2 answer generation is stricter: it returns insufficient evidence instead of
  treating uncalibrated retrieval scores as relevance.

## Release gates

- Rust formatting, Clippy with warnings denied and all Rust tests.
- Ruff formatting/linting and all non-environmental Python tests.
- Generated OpenAPI/event/workspace contract drift check.
- Non-root Docker build and health smoke test.
- AppImage execution and SHA-256 verification.

Future work is tracked in GitHub issues rather than presented as disabled UI.
