# Release scope: 0.5.0

Status: 26 July 2026. The plugin system remains deliberately out of scope.

## Included

- English, symmetric Ratatui workbench with four themes, keyboard and mouse control.
- Library creation, profile selection, multi-select PDF/folder browser and one-step ingest queue.
- Vanilla Haiku RAG 0.70 adapter using public APIs only; no local Haiku fork or patch.
- Docling hybrid chunking with headings, element refs, PDF pages and normalized bounding boxes.
- Clickable citations and highlighted page/figure previews in Chat.
- Segmented long-book processing without a product file/page limit.
- Cooperative pause between PDF segments and chat-priority resource coordination.
- Chat, VL, Embedding and Rerank model catalog with three hardware-matched stacks,
  quantization, context and residency controls.
- Persistent workspaces, jobs, events, configuration, verified backups and safe restore.
- TUI, scriptable CLI, API daemon and read-only MCP process.
- Private persistent bearer token, non-root Docker image and memory-limited systemd service.
- AppImage client, release installer, weekly auto-update timer and zsync update metadata.

## Deliberate 0.5 boundaries

- Linux x86_64 is the packaged target.
- AppImage contains the console client; Docker or the installer provides the Python/Haiku daemon.
- Local source opening is supported. Remote media/proxy preview is deferred.
- Page images use Docling provenance and PDF rendering; dedicated figure extraction is still
  conservative.
- Jobs resumed after a daemon restart reprocess the current document generation. In-process pause
  and resume continue at the next page segment.
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
