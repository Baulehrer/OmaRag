# RAG for technical books

This document explains the few rules behind OmaRag's book search.

## The basic idea

A good answer starts with a good source passage. A larger language model cannot reliably repair a
misread table, a missing page, or the wrong edition.

OmaRag therefore follows this order:

```text
PDF or scan
  -> preserve layout, pages, headings, tables, formulas, and figures
  -> recover the book structure
  -> create small, source-linked evidence chunks
  -> search text, meaning, book structure, and index terms
  -> rerank and remove duplicates
  -> answer only from the selected source passages
```

## Preparing a book

OmaRag keeps the original PDF immutable and identifies it by SHA-256. It uses embedded text when
possible and OCR only where the text layer is unusable.

Book Index V3 uses the table of contents, PDF bookmarks, subject index, glossary, captions, and
body headings as independent structure signals. If these are missing, deterministic page windows
still cover the full book. A small local fallback may classify existing headings and page labels,
but it may not invent text or page references.

Each evidence chunk keeps:

- the unchanged source text;
- the physical page and element location;
- the book, edition, and section;
- its evidence type, such as prose, table, formula, or figure caption;
- stable previous and next links.

Tables, formulas, and figures are not mixed blindly into prose. Navigation pages help routing but
do not become answer evidence.

## Finding evidence

Retrieval V3 combines several bounded routes:

- exact text search for terms, symbols, standards, and numbers;
- semantic search for paraphrases;
- the recovered chapter tree and subject index;
- limited alias and related-term links;
- one dedicated cross-encoder reranker.

Simple questions use a small candidate set. Comparisons and multi-part questions get more facets,
sources, and context. Fast, Normal, and Quality change the available budget, but the adaptive
selection remains active in every profile.

The final evidence set is thresholded, deduplicated, diversified, and limited by the actual model
context. A failed or unpinned reranker is never presented as calibrated confidence.

## Writing the answer

All public answer modes are source-bound. Important claims carry exact support spans and stable
evidence identifiers. Numbers, units, comparisons, negations, and multi-source conclusions receive
extra checks. If required support is missing, OmaRag says so instead of filling the gap with model
knowledge.

The answer inspector shows cited pages first. It can then show up to four relevant, real crops from
figures or tables. Full-page previews are never mislabeled as extracted figures.

## Editions and filters

OmaRag stores title, authors, edition, year, ISBN, language, status, and validity information when
available. It searches the newest active edition by default. Explicit filters can select a work,
edition, year, ISBN, language, tag, or status.

## Measuring quality

Retrieval and answer quality must be measured separately. Useful release metrics include:

- Recall@10 and nDCG@10;
- correct-page rate;
- required-facet coverage;
- citation and support-span correctness;
- unsupported-claim and false-abstention rates;
- latency, memory use, and index size.

The repository contains deterministic evaluation plumbing and CI cases. The planned private
300-case, human-reviewed technical-book gold set is not distributed, so OmaRag does not claim a
universal quality win from that dataset yet.

## Privacy and speed

OmaRag defaults to device-only processing. Remote content endpoints require a visible policy and
consent. Questions, source paths, and book content are not telemetry. URL imports are copied into
private managed storage and their original secret-bearing URLs are replaced with opaque references.

Search facets share one worker call, model residency adapts to repeated use and memory pressure,
media crops are generated lazily, and derived caches are bounded. These optimizations must not
change the cited raw evidence.

## Current boundaries

- Haiku RAG Slim `0.74.0` and Docling `2.119.0` are the verified public-API pair.
- A production NLI verifier is not bundled; risky claims fail closed when no approved verifier is
  available.
- Visual Dense and Late Chunking remain measured experiments, not silent defaults.
- OmaRag retrieves formulas and table values but does not perform domain calculations in V1.2.
- User-attached image questions remain disabled until they have the same evidence and generation
  guarantees as indexed book crops.
