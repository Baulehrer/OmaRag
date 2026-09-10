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
  // The two places a plugin's settings can sit in shell.json. OMA is both a
  // plugin and a bar widget, and the shell treats those as separate homes:
  // `plugins` is where an entry starts, `bar.layout` is where the shell puts a
  // bar widget's settings and — crucially — where updateEntryInline writes
  // whenever the id appears in the layout (shell.qml:1078). Reading only
  // `plugins` meant every setting written from Setup vanished on the next open.
  property var configEntry: ({})
  property var barEntry: ({})

  function entrySettings() {
    var merged = ({})
    var from = [root.configEntry || ({}), root.barEntry || ({})]
    for (var i = 0; i < from.length; i++)
      for (var k in from[i]) if (k !== "id") merged[k] = from[i][k]
    return merged
  }

  function syncSettings() { root.settings = entrySettings() || ({}) }

  function setting(name, fallback) {
    var value = root.settings ? root.settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  // The scoped facade can write a plugin's own entry back to shell.json —
  // but updateEntryInline *replaces* the entry with { id, ...settings } rather
  // than merging into it, so sending one key would delete every other setting.
  // The whole set goes out on every write.
  function writeSetting(key, value) {
    if (!root.shell || typeof root.shell.updateEntryInline !== "function") return false
    var next = ({})
    var current = root.settings || ({})
    for (var k in current) if (k !== "id") next[k] = current[k]
    next[key] = value
    var id = String((root.manifest && root.manifest.id) || "kaufmann.omarag")
    if (!root.shell.updateEntryInline(id, next)) return false
    // Shown at once rather than waiting for the file to come back: the write is
    // asynchronous, and a switch that only moves on the next reload reads as
    // broken.
    root.settings = next
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
      var inBar = ({})
      try {
        var doc = JSON.parse(text())
        var list = Array.isArray(doc.plugins) ? doc.plugins : []
        for (var i = 0; i < list.length; i++)
          if (String((list[i] && list[i].id) || "") === id) { found = list[i]; break }

        // The bar layout holds the same id when OMA is placed in the bar, and
        // that is the copy the shell writes to.
        var layout = (doc.bar && doc.bar.layout) || ({})
        var sections = ["left", "center", "right"]
        for (var s = 0; s < sections.length; s++) {
          var arr = layout[sections[s]] || []
          for (var j = 0; j < arr.length; j++)
            if (String((arr[j] && arr[j].id) || "") === id) { inBar = arr[j]; break }
        }
      } catch (e) { /* a config we cannot read leaves every setting at default */ }
      root.configEntry = found
      root.barEntry = inBar
      root.syncSettings()
    }
    onLoadFailed: { root.configEntry = ({}); root.barEntry = ({}); root.syncSettings() }
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
    memoryReserve: String(root.setting("memoryReserveGib", ""))

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
  // Which edge the bar is on and how thick it is, so the large window can fill
  // the screen and still leave the bar visible. The overlay covers the whole
  // output (exclusionMode Ignore) — without these it would have no way to know
  // where the bar ends.
  property string barSide: "top"
  property real barThickness: 0

  property bool viewOpen: false
  property bool unseenAnswer: false

  function markSeen() { root.unseenAnswer = false }

  // Configurable because the good sound is whatever the user already
  // recognises. Empty turns it off; the default is the desktop's own
  // completion sound rather than something OMA invents.
  readonly property string desktopSoundFile:
      "/usr/share/sounds/freedesktop/stereo/complete.oga"
  readonly property string soundFile: String(root.setting("notifySound",
      root.desktopSoundFile))

  // herdr keeps its notification sounds compiled into its binary, so there is
  // no file to point `notifySound` at until tools/herdr-sound.py has lifted one
  // out of the copy already installed here. That keeps somebody else's asset
  // out of this repository and means nothing is downloaded — on a machine
  // without herdr the button just reports that there is nothing to take.
  readonly property string herdrSoundFile: (Quickshell.env("XDG_DATA_HOME")
      || Quickshell.env("HOME") + "/.local/share")
      + "/omarchy/omarag/sounds/herdr-done.wav"

  signal soundNoticed(string text, bool problem)

  function adoptHerdrSound() {
    herdrLift.command = ["python3",
      String(Qt.resolvedUrl("../tools/herdr-sound.py")).replace(/^file:\/\//, "")]
    herdrLift.running = true
  }

  Process {
    id: herdrLift
    stdout: StdioCollector { }
    stderr: StdioCollector { }

    onExited: function(code) {
      // The script prints the finished file last, so the path comes back
      // without OMA having to guess where it landed.
      var lines = String(herdrLift.stdout.text || "").trim().split("\n")
      var path = lines.length ? lines[lines.length - 1].trim() : ""
      if (code === 0 && /\.wav$/.test(path)) {
        root.writeSetting("notifySound", path)
        root.soundNoticed("herdr's sound is now OMA's", false)
      } else {
        var why = String(herdrLift.stderr.text || "").trim().split("\n").pop()
        root.soundNoticed(why.length ? why : "herdr's sound could not be read", true)
      }
    }
  }

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
