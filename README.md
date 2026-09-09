# OMA

Local knowledge for [Omarchy](https://omarchy.org). Ask your own documents and
see where every answer comes from.

OMA is a native Omarchy overlay plugin. It does not index or embed anything
itself — that is [lilbee](https://github.com/tobocop2/lilbee)'s job. OMA is the
front end: it finds the backend, keeps out of its way, and shows the passages
behind an answer.

## Requirements

* Omarchy with its Quickshell-based shell
* `lilbee` on `PATH`

Nothing else. No npm, no pip, no QML libraries — only what Omarchy and
Quickshell already ship.

## Install

```bash
./sync-plugin.sh                        # copy into ~/.config/omarchy/plugins/
omarchy plugin enable kaufmann.omarag
omarchy restart shell
```

Open it from the bar — the plugin ships a button — or by hand:

```bash
omarchy-shell shell summon kaufmann.omarag '{}'
```

To put the button on the bar, add it to `bar.layout` in
`~/.config/omarchy/shell.json`:

```json
{ "id": "kaufmann.omarag" }
```

`omarchy bar put` reports success but does nothing here: the plugin already
counts as enabled through the top-level `plugins[]` entry, so the placement is
skipped.

The payload may carry a query, which is handy for a keybind:

```bash
omarchy-shell shell summon kaufmann.omarag '{"query":"Betondeckung"}'
```

**Never symlink this repository into the plugins directory.** The shell follows
symlinks and walks whatever it finds; pointing it at a git tree froze a session
during development. `sync-plugin.sh` copies, and `omarchy-plugin-validate`
refuses symlinks for the same reason.

## Backend

OMA reads `~/.local/share/lilbee/data/server.port` and `server.json` to find a
running lilbee server. That means it **joins a server you already have running**
— from lilbee's TUI, from a terminal — rather than starting a competing one,
which lilbee would refuse anyway: it allows one server per data directory.

If none is running, OMA starts one on a free port and reads back which.

### Backend when OMA is closed

One setting, in the plugin's Omarchy configuration:

| | |
|---|---|
| **Stop with OMA** (default) | The server and the models it loaded are released when you close OMA. The next question waits for both. |
| **Keep running** | The server stays, roughly 430 MB, and loaded models stay until they time out. The next question starts straight away. |

The default matters more than it looks: lilbee leaves its model fleet *warm*
when a server exits, so without an explicit release the weights sit in VRAM for
half an hour with nobody using them — long enough to stop a chat model from
loading. OMA releases them, but only when it started the server itself.

## Keyboard

| Key | |
|---|---|
| `Enter` | Ask — the model answers from the retrieved passages |
| `Ctrl+Enter` | Retrieve only, no model |
| `Esc` | Cancel the answer → fold details → clear the query → close |

Clicking a cited source opens the document at that page — lilbee reports PDF page
numbers, and zathura, okular and evince all take exactly that. Clicking a search hit
expands it to the full retrieved passage.

The model is bound to what retrieval found. Ask it something the library does not
cover and it says so instead of filling the gap from general knowledge — lilbee's
system prompt enforces that, and it holds in practice.

The bar button toggles the same overlay. A plugin that is both `overlay` and
`bar-widget` stays owned by the overlay loader, so the button triggers OMA
rather than replacing it — the same arrangement `omarchy.menu` uses.

More to come as the tabs arrive.

## Development

```bash
./sync-plugin.sh        # copy + validate
omarchy restart shell   # required: overlay QML is cached, saving is not enough
journalctl --user -f | grep -i omarag
```

A plugin runs **inside** the shell process. A blocking call freezes the whole
desktop — bar, notifications, lock screen. Everything here is asynchronous, and
should stay that way.

## Documents

| | |
|---|---|
| [`REALITY.md`](REALITY.md) | What the host and lilbee actually do, measured rather than assumed |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The decisions and what was rejected |
| [`DESIGN.md`](DESIGN.md) | The interface, state by state |
| [`MILESTONE-REPORT.md`](MILESTONE-REPORT.md) | Where the project stands |
| [`PROMPT.md`](PROMPT.md) | The brief |

## License

MIT — see [`LICENSE`](LICENSE).
