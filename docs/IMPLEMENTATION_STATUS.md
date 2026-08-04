# Release scope: 0.8.0

Status: 4 August 2026. The plugin system remains deliberately out of scope.

## Included

- English, SOAP-inspired Ratatui shell with a persistent sidebar, task workspace and contextual
  inspector; fourteen themes plus keyboard and mouse control.
- Chat, Library, Models and Settings are the four core sections. Indexing absorbs activity, Help
  stays behind `?`, and Simple mode hides advanced views without deleting their configuration.
- All mutable controls live in the center workspace. The right inspector is read-only except for
  selecting and opening Chat sources and their exact PDF pages.
- Library creation, profile selection, multi-select PDF/folder browser and one-step ingest queue.
- Latest-gated vanilla Haiku RAG adapter using public APIs only; no local Haiku fork or patch.
- Candidate-runtime compatibility probe and last-known-good activation boundary for Haiku updates.
- Docling hybrid chunking with headings, element refs, PDF pages and normalized bounding boxes.
- Selective per-page OCR, header/footer suppression and extraction quality/provenance reporting.
- Bibliographic import preflight with confirmed title, author, ISBN, edition and validity metadata.
- Hash-addressed immutable originals and explicit current-edition/document filtering.
- Strict, Normal and Explore evidence modes with stable evidence IDs and technical-token checks.
- Deterministic Silver evaluation and FTS/vector/hybrid A/B retrieval metrics.
- Markdown-rendered answers plus keyboard/mouse-selectable citations and exact-page previews.
- Persistent conversation IDs, a bounded generation-aware exact-answer cache and a compact answer
  receipt showing fresh/reused answers, checked sources and known/new evidence.
- Segmented long-book processing without a product file/page limit.
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

## Deliberate 0.8 boundaries

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

## Release gates

- Rust formatting, Clippy with warnings denied and all Rust tests.
- Ruff formatting/linting and all non-environmental Python tests.
- Generated OpenAPI/event/workspace contract drift check.
- Non-root Docker build and health smoke test.
- AppImage execution and SHA-256 verification.

Future work is tracked in GitHub issues rather than presented as disabled UI.
