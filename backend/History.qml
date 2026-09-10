import QtQuick
import Quickshell
import Quickshell.Io

// Past questions, kept so they can be opened again.
//
// Each entry stands on its own — question, answer, sources, timestamp. There is
// deliberately no conversation thread: `lilbee ask` answers one question at a
// time, and pretending otherwise would mean OMA carrying a context that
// competes with the retrieved passages for room.
//
// Stored where the shell's own plugins keep their state — the clipboard manager
// writes ~/.local/state/omarchy/clipboard-history.json next door.
Item {
  id: root

  readonly property string path: (Quickshell.env("XDG_STATE_HOME")
                                  || Quickshell.env("HOME") + "/.local/state")
                                 + "/omarchy/omarag-history.json"

  property var entries: []
  readonly property int limit: 200

  signal loaded()

  function add(question, answer, sources) {
    if (!question || !answer) return
    var e = {
      question: String(question),
      answer: String(answer),
      sources: sources || [],
      at: Date.now()
    }
    var next = [e]
    for (var i = 0; i < root.entries.length && next.length < root.limit; i++) {
      // A repeat of the same question replaces the old entry rather than
      // stacking duplicates.
      if (root.entries[i].question !== e.question) next.push(root.entries[i])
    }
    root.entries = next
    root._save()
  }

  function removeAt(index) {
    if (index < 0 || index >= root.entries.length) return
    var next = root.entries.slice()
    next.splice(index, 1)
    root.entries = next
    root._save()
  }

  function clear() {
    root.entries = []
    root._save()
  }

  // `ask` wraps its output mid-URL, and until that was understood the space it
  // left landed inside a percent escape: ".../04%20Lehr-%20und%20L ernmaterial/".
  // Answers stored before then keep the damage, and a source that cannot be
  // opened is the one thing this file exists to prevent. Repaired on the way
  // in, and written back so it is repaired for good — measured on this
  // machine's history: 6 of 18 links.
  function _repair(entries) {
    var mended = 0
    for (var i = 0; i < entries.length; i++) {
      var sources = entries[i] && entries[i].sources
      if (!Array.isArray(sources)) continue
      for (var j = 0; j < sources.length; j++) {
        var url = String(sources[j] && sources[j].url || "")
        if (!/\s/.test(url)) continue
        sources[j].url = url.replace(/\s+/g, "")
        mended += 1
      }
    }
    if (mended > 0) Qt.callLater(function() { root._save() })
    return entries
  }

  function _save() {
    file.setText(JSON.stringify({ version: 1, entries: root.entries }, null, 1))
  }

  // Created before anything is written, with the mode it should have had all
  // along: every question and every answer is in here, and on a shared machine
  // 0644 hands that to whoever else has an account. FileView cannot set a mode,
  // so the file is made once, up front, and the umask does the rest.
  Process {
    id: secure
    command: ["sh", "-c",
      'umask 077; [ -e "$1" ] || : > "$1"; chmod 600 "$1" 2>/dev/null; exit 0',
      "oma", root.path]
    onExited: file.reload()
  }

  FileView {
    id: file
    path: root.path
    printErrors: false
    // Written whole on every change; atomic so a crash mid-write cannot leave
    // a truncated file behind.
    atomicWrites: true

    onLoaded: {
      try {
        var parsed = JSON.parse(String(text()))
        root.entries = root._repair((parsed && parsed.entries) || [])
      } catch (e) {
        root.entries = []
      }
      root.loaded()
    }
    // No file yet is the normal state on a first run, not a failure.
    onLoadFailed: { root.entries = []; root.loaded() }
  }

  // The mode first, the contents second: reloading is what `secure` does when it
  // is finished, so an existing file is never read before it is locked down.
  Component.onCompleted: secure.running = true
}
