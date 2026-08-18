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

OmaRag turns PDF textbooks into a private knowledge library. Ask a question and it finds the
relevant passages, answers in plain language, and shows the exact book and page. Your books and
your questions stay on your computer.

## Get started

1. Start OmaRag.
2. Name your first library.
3. Add PDF books with `I`.
4. Ask a question, then open a cited page to check it.

On first launch OmaRag looks at your hardware and suggests models. It never downloads anything
without asking.

## Install

You need Linux x86-64 and [Ollama](https://ollama.com/). 8 GB of RAM works; 16 GB is better.

```bash
curl -fsSL https://github.com/Baulehrer/OmaRag/releases/latest/download/install.sh | sh
```

Start it with `omarag`, update it with `omarag-update`.

## Keys

`?` shows everything. The ones worth remembering:

| Key | Does |
| --- | --- |
| `Tab` | Move between the three panes |
| `I` | Add PDFs |
| `Enter` | Ask, or open what is selected |
| `Ctrl+T` | Next colour theme |
| `:` | Command palette |

## Appearance

36 colour themes are included. On an [Omarchy](https://omarchy.org) desktop, the *Omarchy System*
theme follows whatever your desktop is using.

Drop your own theme into `~/.config/omarag/themes/` as a `.toml` file and it appears in the list —
copy one from `assets/themes/` to start. Icons can be set to full, restrained, or off under
**Settings › Appearance**, with a plain-text fallback if your terminal has no Nerd Font.

## Get help

```bash
omarag-cli doctor
```

If that does not explain it, open an [issue](https://github.com/Baulehrer/OmaRag/issues). Please do
not upload private books or credentials.

## License

MIT. Bundled colour themes come from [superfile](https://github.com/yorukot/superfile) and
[Omarchy](https://github.com/basecamp/omarchy), both MIT — see
[assets/themes/ATTRIBUTION.md](assets/themes/ATTRIBUTION.md).
