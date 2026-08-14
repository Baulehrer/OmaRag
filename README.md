<p align="center">
  <img src="assets/omarag.svg" width="128" alt="OmaRag">
</p>

<h1 align="center">OmaRag</h1>

<p align="center">
  <strong>Your textbooks. Your questions. Answers with page references.</strong>
</p>

<p align="center">
  <a href="https://github.com/Baulehrer/OmaRag/releases/latest">Latest release</a> ·
  <a href="https://github.com/Baulehrer/OmaRag/issues">Get help</a>
</p>

![OmaRag](docs/screenshots/dashboard.png)

## What is OmaRag?

OmaRag turns PDF textbooks into a private knowledge library. Ask a question and it finds relevant
passages, answers in plain language, and shows the exact book and page. Your books and questions
stay on your computer by default.

## Get started

1. Start OmaRag.
2. Choose **Fast**, **Normal**, or **Quality**.
3. Add PDF books.
4. Ask a question and inspect the original sources.

On first launch, OmaRag checks your hardware and recommends suitable models. It never downloads
models without your confirmation. Advanced model settings remain available in Expert mode.

The source panel shows cited pages first, followed by up to four relevant figures or tables when
available.

Version 1.2 keeps simple searches small and expands them only when evidence is missing. Device-only
mode blocks content from being sent to external services. Existing libraries are never silently
deleted or reindexed.

## Install

You need a Linux x86-64 computer and [Ollama](https://ollama.com/). The smallest profile needs 8 GB
of RAM; 16 GB or more is recommended.

```bash
curl -fsSL https://github.com/Baulehrer/OmaRag/releases/latest/download/install.sh | sh
```

Start OmaRag with:

```bash
oracle
```

## Update

```bash
oracle-update
```

A major update may require reindexing. Your original books are preserved.

## Get help

```bash
oracle-cli doctor
```

If the problem continues, open a [GitHub issue](https://github.com/Baulehrer/OmaRag/issues). Do not
upload private books or credentials.

## License

OmaRag is released under the MIT License.
