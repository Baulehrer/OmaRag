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
| `Super+A` | Open OMA, and close it again |
| `Enter` | Ask — the model answers from the retrieved passages |
| `Ctrl+Enter` | Retrieve only, no model |
| `↑` `↓` | Walk the results; up from the first row returns to the input |
| `Enter` on a row | Open the cited page, or expand the passage |
| `Ctrl+O` | Add documents — files, one or many |
| `Ctrl+Shift+O` | Add a folder |
| `Esc` | Cancel the answer → fold details → drop the selection → clear the query → close |
| `Tab` / `1`–`3` | Move between Setup, Chat and Library |
| `Ctrl+M` | Switch between the large and the small window |
| `PgUp` `PgDn` `Home` `End` | Scroll the Setup sheet |

Answers are selectable, and a selection lands on the clipboard on its own.
Subscripts and superscripts are set properly — `f_ck` and `N/mm^2` come out as
they should — and every question is kept in
`~/.local/state/omarchy/omarag-history.json` so it can be opened again.

`Enter` does the obvious thing for wherever you are: in the input it asks, on a
result row it acts on that row. `Super+A` is a line in `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + A", "OMA local knowledge", "omarchy-shell shell toggle kaufmann.omarag '{}'")
```

Clicking a cited source opens the document at that page — lilbee reports PDF page
numbers, and zathura, okular and evince all take exactly that. Clicking a search hit
expands it to the full retrieved passage.

While an answer runs, the chat shows which of three things is happening —
loading the model, finding passages, writing — with the clock beside it. From
the second answer with the same model there is a bar as well, because by then
there is a measured duration to put it against; cold and warm starts are counted
separately, since loading the weights costs most of a minute on this machine.
Overrun the estimate and the bar stops at the end and says so. The durations are
kept in `~/.local/state/omarchy/omarag-timing.json` so a shell restart does not
take the bar away again.

The model is bound to what retrieval found. Ask it something the library does not
cover and it says so instead of filling the gap from general knowledge — lilbee's
system prompt enforces that, and it holds in practice.

### Two sizes

Large fills the screen except the bar, which stays visible and usable — OMA's
own icon included. Small opens under that icon, on the side of the screen the
icon is on and above or below the bar depending on where the bar sits, and
shows the chat alone: at that width a history column would take a third of the
room from the thing it was opened for. The large window dims what is behind it,
the small one does not — it is meant to sit beside the work.

The header glyph switches between them, so does `Ctrl+M`, and so do the buttons
in Setup. There is one window either way: the shell gives a plugin exactly one
window loader and prefers `panel` over `overlay` (`computePanelEntries`), so the
card changes size and place rather than there being two windows.

The bar button toggles the same overlay. A plugin that is both `overlay` and
`bar-widget` stays owned by the overlay loader, so the button triggers OMA
rather than replacing it — the same arrangement `omarchy.menu` uses.

### Setup

Everything in one sheet: which lilbee is installed and where its server is
listening, buttons to check for an update, release the loaded models, and open
the backend log. Below that OMA's own settings — whether the backend stops when
you close OMA, which model answers, the window size and its opacity, and OMA's
text size and font, which apply to OMA alone and never touch the shell's.

`sound when done` picks what plays when an answer lands while OMA is closed:
the desktop's own completion sound, silence, or herdr's. herdr ships its
notification sounds compiled into its binary, so there is no file to point at —
choosing `herdr` runs `tools/herdr-sound.py`, which reads the herdr already
installed on this machine and writes the sound as a WAV under
`~/.local/share/omarchy/omarag/sounds/`. Nothing is downloaded, and the sound
exists only where herdr does. `Play` plays whatever is set.

Then lilbee's own settings, generated from what lilbee reports rather than
hard-coded, so the help text beside each field is lilbee's: the four model
roles, the answering knobs, the warm-up time, and the retrieval knobs. A field
that differs from its default offers `reset`.

Each model role opens a list of what is already on this machine — no network,
so it comes with the tab. Searching lilbee's catalogue reaches Hugging Face and
therefore waits behind a button that says so, and nothing downloads until you
press `Get`. A download holds the single embedder, so questions and indexing
wait for it, and OMA says so while it runs.

### Memory

Models are big and the machine is shared, so OMA follows one rule: **load only
what fits, and give it back the moment something else needs it.**

Two numbers, because they answer different questions.

`MemAvailable` is predictive — it says whether a load would fit, and on a
shared-memory APU it is the only figure that sees model weights at all. They
live in the GPU translation table, where cgroup accounting cannot follow:
measured here, 9.19 GiB of weights while the cgroup reported 0.68 and a 3 GiB
limit never fired. `MemoryMax` is not a tool that works for this.

`/proc/pressure/memory` is reactive — it says whether anything is *stalling* on
memory right now. With swap configured that matters more than free bytes: the
machine is not killed, it crawls, and `MemAvailable` can look calm while
everything thrashes. `systemd-oomd` watches the same signal and starts killing
cgroups at 50 % sustained for 20 seconds. OMA acts far below that, so nothing
ever has to be chosen as a victim.

**Before loading** — a question, a search or an index run, and only when nothing
is loaded yet — `tools/admit.py` checks `MemAvailable >= reserve + margin +
what is about to load`, under a lock so the answer cannot be racing another
load. A no is a refusal that names what is in the way.

**While holding models** `tools/memory-guard.py` watches both numbers and says
when to let go: at `tight` OMA releases what is idle, at `critical` it stops
what is running as well and stays out of the way for a minute. Nothing is
watched while OMA holds nothing — an OMA nobody has opened runs no watcher at
all.

`Keep free (GiB)` in Setup sets the reserve. Empty means six percent of the
machine's RAM and never under 2 GiB, so the same plugin behaves on a laptop and
on a workstation.

Where `llama-manager` is installed, OMA uses *its* lock and *its* numbers
instead, so the two cannot admit a model each at the same moment. The chat model
is deliberately not counted: lilbee reaches it through `llama-manager`'s own
proxy, so that half is already gated, and holding the lock across an answer
would deadlock against the load the answer triggers.

What this cannot do: stop another program from allocating everything at once.
OMA can promise not to be the cause, and to have let go before the system's own
guard has to act.

### What OMA never does on its own

No root, no telemetry, no cloud. The only three things that leave this machine
are `Check for updates` (asks GitHub through mise), `Search catalogue` (asks
Hugging Face through lilbee) and `Get` (downloads a model) — each one a button
you press, none of them on a timer or on opening a view.

## Development

```bash
./sync-plugin.sh        # copy + validate
omarchy restart shell   # required: overlay QML is cached, saving is not enough
journalctl --user -f | grep -i omarag

node tools/tests/formula-escaping.js   # no model output can become markup
node tools/tests/answer-timing.js      # the estimate behind the progress bar
bash tools/tests/admission.sh          # the gate in front of every model load
bash tools/tests/memory-guard.sh       # when the models are given back
```

Both tests read the real source — `ui/Formula.js`, and the three timing
functions lifted out of `backend/Lilbee.qml` — so they fail if the code they
cover is renamed rather than passing against a stale copy.

A plugin runs **inside** the shell process. A blocking call freezes the whole
desktop — bar, notifications, lock screen. Everything here is asynchronous, and
should stay that way.

## Documents

| | |
|---|---|
| [`../notes/REALITY.md`](../notes/REALITY.md) | What the host and lilbee actually do, measured rather than assumed |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The decisions and what was rejected |
| [`../notes/DESIGN.md`](../notes/DESIGN.md) | The interface, state by state |
| [`MILESTONE-REPORT.md`](MILESTONE-REPORT.md) | Where the project stands |
| [`PROMPT.md`](PROMPT.md) | The brief |

## License

MIT — see [`LICENSE`](LICENSE).
