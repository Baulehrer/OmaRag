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

  function _save() {
    file.setText(JSON.stringify({ version: 1, entries: root.entries }, null, 1))
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
        root.entries = (parsed && parsed.entries) || []
      } catch (e) {
        root.entries = []
      }
      root.loaded()
    }
    // No file yet is the normal state on a first run, not a failure.
    onLoadFailed: { root.entries = []; root.loaded() }
  }

  Component.onCompleted: file.reload()
}
