import QtQuick
import Quickshell
import Quickshell.Io

// lilbee's version, and the one step that reaches the network.
//
// lilbee is installed through mise and pinned in ~/.config/mise/config.toml.
// Checking for a newer build means asking GitHub, so it happens on a button
// press and never on opening a view — OMA is local-first, and an update check
// is the one place that would quietly stop being true.
Item {
  id: root

  property string bin: "lilbee"
  property string toolId: "github:tobocop2/lilbee"

  property string version: ""
  property string latest: ""
  property bool checking: false
  property string note: ""

  signal checked()

  function readVersion() { versionProc.running = true }

  function checkLatest() {
    if (root.checking) return
    root.checking = true
    root.note = "Asking GitHub through mise…"
    remoteProc.running = true
  }

  function compare(a, b) {
    if (!a.length || !b.length) return 0
    return a === b ? 0 : 1
  }

  Process {
    id: versionProc
    command: [root.bin, "version"]
    stdout: StdioCollector {
      onStreamFinished: {
        // `lilbee version` prints a line; take the first token that looks like
        // a version rather than assuming a fixed shape.
        var m = /([0-9]+\.[0-9]+\.[0-9A-Za-z]+)/.exec(String(text))
        root.version = m ? m[1] : String(text).trim().split("\n")[0]
      }
    }
  }

  Process {
    id: remoteProc
    command: ["mise", "ls-remote", root.toolId]
    stdout: StdioCollector {
      onStreamFinished: {
        var lines = String(text).trim().split("\n").filter(function(l) { return l.trim().length })
        root.latest = lines.length ? lines[lines.length - 1].trim() : ""
        root.checking = false
        if (!root.latest.length) {
          root.note = "mise returned no versions — is the network reachable?"
        } else if (root.version.length && root.latest.indexOf(root.version) !== -1) {
          root.note = "lilbee " + root.version + " is the newest build."
        } else {
          root.note = "Newer build available: " + root.latest + "  ·  update with: "
                    + "mise use -g \"" + root.toolId + "@" + root.latest + "\""
        }
        root.checked()
      }
    }
    onExited: function(code) {
      if (code !== 0) {
        root.checking = false
        root.note = "Could not ask mise (exit " + code + ")."
      }
    }
  }
}
