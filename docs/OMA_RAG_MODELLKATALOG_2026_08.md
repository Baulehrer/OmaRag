# OmaRag model catalog 08/2026

Updated: 14 August 2026 · Catalog: `oma-rag-model-catalog-2026.08` · Release: `2026.08.1`

This release-bound catalog is the reviewed model selection used by OmaRag 1.2. It targets common,
affordable consumer hardware. Tier 1 starts at 8 GB of RAM; tier 10 ends at 64 GB of RAM and 24 GB
of VRAM. The highest tier does not require 128 GB of VRAM.

## How selection works

OmaRag scans total and available RAM, VRAM, accelerators, CPU, and free disk space on first launch
or when you request a new scan. It reports two separate results:

- **Device tier:** what the hardware can support in principle.
- **Ready tier:** what can run with the memory currently available.

An optional local benchmark can lower the recommendation, but never raise it above the safe memory
tier. This keeps profiles reproducible while adapting them to the current computer.

## Hardware tiers

Every automatic tier uses 1,024-dimensional text embeddings and the multilingual
`mmarco-mMiniLMv2-L12-H384-v1` reranker.

| Tier | Typical hardware | Chat and vision | Text embedding | Separate visual index |
|---:|---|---|---|---|
| 1 | 8 GB RAM, CPU or iGPU | Qwen3.5 2B Q4_K_M | Qwen3 Embedding 0.6B | Off |
| 2 | 12 GB RAM or 8 GB + 4 GB VRAM | Ministral 3 3B Q4_K_M | Qwen3 Embedding 0.6B | Off |
| 3 | 16 GB RAM, CPU or iGPU | Qwen3.5 4B Q4_K_M | Qwen3 Embedding 0.6B | Off |
| 4 | 16 GB RAM + 4–6 GB VRAM | Qwen3.5 4B Q4_K_M | Qwen3 Embedding 0.6B | Off |
| 5 | 16 GB RAM + 8 GB VRAM | Ministral 3 8B Q4_K_M | Qwen3 Embedding 0.6B | SigLIP 2 Base |
| 6 | 24 GB + 8–12 GB VRAM, or 32 GB without dGPU | Ministral 3 8B Q4_K_M | Qwen3 Embedding 0.6B | SigLIP 2 Base |
| 7 | 32 GB RAM + 8–12 GB VRAM | Gemma 4 12B QAT | Qwen3 Embedding 0.6B | SigLIP 2 Base |
| 8 | 32 GB + 16 GB VRAM, or 48 GB + 12 GB VRAM | Ministral 3 14B Q4_K_M | Qwen3 Embedding 0.6B | Qwen3-VL Embedding 2B |
| 9 | 32–64 GB RAM + 16–24 GB VRAM | Gemma 4 26B-A4B QAT | Qwen3 Embedding 0.6B | Qwen3-VL Embedding 2B |
| 10 | 64 GB RAM + 24 GB VRAM | Qwen3.6 27B Q4_K_M | Qwen3 Embedding 0.6B | Qwen3-VL Embedding 2B |

These are target ranges, not buying advice. Shared graphics memory is not counted twice as both RAM
and VRAM. If a model, backend, or digest does not match, OmaRag reports the problem and uses only a
safe fallback defined by the catalog.

The text embedding model is intentionally identical across automatic tiers. Switching between
Fast, Normal, and Quality, or upgrading hardware, therefore does not silently change the vector
space or force a full reindex. Expert mode may use another embedder, but an existing library then
requires an explicitly planned rebuild.

## Fast, Normal, and Quality

The hardware tier chooses models that fit. The profile controls how much work is spent on each
question. Search remains adaptive inside every profile.

| Profile | Candidates: simple / standard / complex | Sources: simple / standard / complex | Context cap |
|---|---:|---:|---:|
| Fast | 16 / 28 / 48 | 1–4 / 2–6 / 3–10 | 8,192 tokens |
| Normal | 24 / 40 / 72 | 1–5 / 2–8 / 4–14 | 16,384 tokens |
| Quality | 32 / 56 / 96 | 2–6 / 3–10 / 5–18 | 49,152 tokens |

The real context limit is the lower of the hardware and profile limits. OmaRag also removes
irrelevant or redundant sources. More sources are not automatically better. At most four
individual figures or tables are shown with an answer.

## Updates and downloads

The catalog ships with OmaRag and is never replaced from the internet at runtime. OmaRag reports
the catalog as potentially stale after 120 days. A refreshed stack arrives only in a later,
evaluated release.

OmaRag checks the embedded catalog's SHA-256 checksum and verifies pinned model revisions or Ollama
digests. The checksum detects modification; it is not an independent public-key signature. Release
provenance and artifact checksums remain part of the trust boundary.

A hardware scan, recommendation, benchmark, or launch never downloads a model. Preflight shows the
download size, free space, model changes, and any required reindex. Downloading starts only after
explicit `DOWNLOAD_MODELS` confirmation. An embedding change in a populated library requires a
separately confirmed full rebuild and is never applied halfway.

## Expert mode

Expert mode keeps full control over roles, providers, model IDs, quantization, context, and
residency. OmaRag labels this as an expert profile and does not silently replace it with an
automatic recommendation.

## Visual evidence

OmaRag stores individual figures and tables separately from full pages, with stable document, page,
bounding-box, and hash provenance. The inspector shows cited pages first and then up to four
relevant crops.

The default selector uses direct evidence links, cited pages, captions, full-text search, and graph
links. Visual evidence is built lazily after an answer, so it does not delay the first text output.

A separate visual vector index and VL embedding role exist, but are not built automatically for
every library. In 1.2, Visual Dense remains experimental and fails closed until its media-quality
gate passes. Page, caption, and graph search remain available without it. VL-generated OKF
suggestions are routing metadata, never a substitute for original page evidence.

## Catalog evaluation

New model stacks are evaluated on more than public leaderboards. Checks include German technical
questions with page evidence, embedding and reranker quality, multi-part questions, tables and
figures, hallucination and citation accuracy, latency, peak RAM and VRAM, reindex requirements, and
backward compatibility.

The final decision requires a versioned OmaRag evaluation set with real textbooks, fixed questions,
and reproducible quality and performance limits.
