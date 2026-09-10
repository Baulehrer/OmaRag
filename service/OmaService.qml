import QtQuick
import Quickshell
import Quickshell.Io
import "../backend"
import "../common"

// Everything OMA keeps between openings.
//
// The overlay is lazy-loaded: closing it sets its loader inactive and the whole
// view is destroyed — including a running `lilbee ask`. That is right for a
// window and wrong for an answer, so the machinery lives here instead. The
// shell creates one service per plugin at startup (shell.qml:990) and hands it
// to every other kind through `shell.serviceFor(id)`.
//
// Nothing here starts on its own. The backend is contacted when a view asks for
// something, so an OMA that is never opened costs a handful of idle objects and
// no processes.
Item {
  id: root

  // Injected by the shell.
  property var shell: null
  property var manifest: null
  property string omarchyPath: ""

  readonly property alias backend: lilbee
  readonly property alias history: hist
  readonly property alias toolchain: tools

  // ---------------------------------------------------------------- settings
  //
  // The shell injects `settings` into bar widgets (Bar.qml:611) but into
  // nothing else — a loader hands a plugin omarchyPath, shell, manifest and the
  // two registries and stops there (shell.qml:1341). Writing works through
  // `updateEntryInline`, reading does not, so OMA reads its own entry out of
  // shell.json, which the shell's README names as the one place settings live.
  property var settings: ({})
  property var configEntry: ({})

  function entrySettings() {
    var id = String((manifest && manifest.id) || "kaufmann.omarag")
    // If a future shell does expose the config to plugins, prefer it.
    var config = shell ? shell.shellConfig : null
    var plugins = config ? config.plugins : null
    if (Array.isArray(plugins))
      for (var i = 0; i < plugins.length; i++)
        if (String((plugins[i] && plugins[i].id) || "") === id) return plugins[i]
    return root.configEntry || ({})
  }

  function syncSettings() { root.settings = entrySettings() || ({}) }

  function setting(name, fallback) {
    var value = root.settings ? root.settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  // The scoped facade can write a plugin's own entry back to shell.json.
  function writeSetting(key, value) {
    if (!root.shell || typeof root.shell.updateEntryInline !== "function") return false
    var patch = ({})
    patch[key] = value
    root.shell.updateEntryInline((root.manifest && root.manifest.id) || "kaufmann.omarag", patch)
    return true
  }

  onManifestChanged: syncSettings()
  onShellChanged: syncSettings()

  FileView {
    path: Quickshell.env("HOME") + "/.config/omarchy/shell.json"
    watchChanges: true
    onFileChanged: reload()
    onLoaded: {
      var id = String((root.manifest && root.manifest.id) || "kaufmann.omarag")
      var found = ({})
      try {
        var doc = JSON.parse(text())
        var list = Array.isArray(doc.plugins) ? doc.plugins : []
        for (var i = 0; i < list.length; i++)
          if (String((list[i] && list[i].id) || "") === id) { found = list[i]; break }
      } catch (e) { /* a config we cannot read leaves every setting at default */ }
      root.configEntry = found
      root.syncSettings()
    }
    onLoadFailed: { root.configEntry = ({}); root.syncSettings() }
  }

  // OMA's own type scale, read once and pushed into the singleton every view
  // reads. Here rather than in a view, so both windows agree without either
  // owning it.
  Binding { target: OmaFont; property: "scale"; value: Number(root.setting("omaFontScale", 1.0)) || 1.0 }
  Binding { target: OmaFont; property: "family"; value: String(root.setting("omaFontFamily", "")) }

  // --------------------------------------------------------------- machinery

  History { id: hist }
  Toolchain { id: tools }

  Lilbee {
    id: lilbee
    persistDaemon: root.setting("backendWhenClosed", "Stop with OMA") === "Keep running"
    answerModel: root.setting("answerModel", "")

    // An answer that finishes while the window is shut has nobody to tell, so
    // the service says it: a short sound, and a flag the bar icon can show
    // until the answer has been looked at.
    onAnswerFinished: function(ok) {
      if (!ok) return
      if (root.viewOpen) return
      root.unseenAnswer = true
      root.chime()
    }
  }

  // -------------------------------------------------------------- attention

  // Set by whichever window is on screen. Without it the service cannot tell a
  // finished answer somebody is watching from one that arrived into an empty
  // desk.
  // Where the bar icon is, filled in by the bar widget. The compact window
  // opens under it, the way the shell's own popups do.
  property real anchorX: 0
  property real anchorY: 0
  property bool anchorAtTop: true

  property bool viewOpen: false
  property bool unseenAnswer: false

  function markSeen() { root.unseenAnswer = false }

  // Configurable because the good sound is whatever the user already
  // recognises. Empty turns it off; the default is the desktop's own
  // completion sound rather than something OMA invents.
  readonly property string soundFile: String(root.setting("notifySound",
      "/usr/share/sounds/freedesktop/stereo/complete.oga"))

  function chime() {
    if (!root.soundFile.length) return
    chimeProc.command = ["sh", "-c",
      '[ -f "$1" ] || exit 0; ' +
      'command -v canberra-gtk-play >/dev/null 2>&1 && exec canberra-gtk-play -f "$1"; ' +
      'command -v pw-play >/dev/null 2>&1 && exec pw-play "$1"; ' +
      'command -v paplay >/dev/null 2>&1 && exec paplay "$1"; ' +
      'exit 0',
      "oma", root.soundFile]
    chimeProc.running = true
  }

  Process { id: chimeProc }
}
