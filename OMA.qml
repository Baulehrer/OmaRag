import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "backend"
import "ui"

// OMA — local knowledge for Omarchy.
//
// The overlay itself: window, settings, keyboard, and the decision about which
// state the middle of the window is in. Everything that draws is in ui/.
Item {
  id: root

  property var shell: null
  property var manifest: null
  property var settings: ({})
  property bool opened: false

  // Settings live inline in the plugin's shell.json entry, the same place the
  // other overlays keep theirs.
  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function entrySettings() {
    var id = String((manifest && manifest.id) || "kaufmann.omarag")
    var config = shell ? shell.shellConfig : null
    var plugins = config ? config.plugins : []
    if (!Array.isArray(plugins)) return settings || ({})
    for (var i = 0; i < plugins.length; i++)
      if (String((plugins[i] && plugins[i].id) || "") === id) return plugins[i]
    return settings || ({})
  }

  function syncSettings() { root.settings = entrySettings() || ({}) }

  onManifestChanged: syncSettings()
  onShellChanged: syncSettings()
  Component.onCompleted: syncSettings()

  Connections {
    target: root.shell
    ignoreUnknownSignals: true
    function onShellConfigChanged() { root.syncSettings() }
  }

  // ------------------------------------------------------------- appearance

  // Shares the [menu] surface tokens, like the other overlays — themes that
  // style the menu style OMA too. No colour is defined here.
  readonly property color foreground: Color.menu.text
  readonly property color muted: Color.muted
  readonly property color accent: Color.accent
  readonly property color urgent: Color.urgent
  readonly property var borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))

  // ------------------------------------------------------------- state

  property var hits: []
  property string query: ""
  property string pending: ""
  property string notice: ""
  property bool detailsOpen: false

  // Which result row the keyboard is on. -1 means the input: typing continues,
  // and Enter asks. Once a row is picked, Enter acts on that row instead — the
  // one key does the obvious thing for wherever you are.
  property string tab: "chat"
  readonly property var tabs: [
    { id: "setup",   label: "SETUP" },
    { id: "chat",    label: "CHAT" },
    { id: "library", label: "LIBRARY" }
  ]

  property int selected: -1
  readonly property int rowCount: root.tab !== "chat" ? 0
                               : (root.showingAnswer ? backend.answerSources.length : root.hits.length)

  function moveSelection(delta) {
    if (!root.rowCount) return
    var n = root.selected + delta
    if (n < -1) n = -1
    if (n >= root.rowCount) n = root.rowCount - 1
    root.selected = n
  }

  function activateSelected() {
    if (root.selected < 0 || root.selected >= root.rowCount) return false
    if (root.showingAnswer) {
      var src = backend.answerSources[root.selected]
      if (src) backend.openDocument(src.url, src.pages)
    } else {
      chatTab.expandSelected()
    }
    return true
  }

  // The wordmark breathes while work is happening — and only then. Movement
  // that is always on stops meaning anything.
  readonly property bool working: backend.phase === "starting" || backend.phase === "searching"
                               || backend.phase === "answering" || backend.phase === "indexing"
                               || backend.phase === "busy"

  readonly property bool blocked: backend.phase === "busy"
  readonly property bool indexing: backend.phase === "indexing"
  readonly property bool answering: backend.phase === "answering"
  readonly property bool waiting: backend.phase === "searching" || backend.phase === "starting"

  // Elapsed seconds while we wait. The one honest thing to show when the
  // remaining time is unknowable — a cold embedder takes past a minute, and a
  // progress bar would be a guess.
  property int waited: 0
  Timer {
    interval: 1000
    repeat: true
    running: (root.waiting || root.answering || root.indexing) && root.opened
    onTriggered: root.waited += 1
  }
  onWaitingChanged: root.waited = 0
  onAnsweringChanged: root.waited = 0
  onIndexingChanged: root.waited = 0

  readonly property bool showingAnswer: root.answering || backend.answerText.length > 0

  readonly property string panelMode: {
    if (backend.phase === "error") return "error"
    if (root.indexing) return "indexing"
    if (root.blocked) return "blocked"
    if (root.showingAnswer && !root.indexing) return "none"
    if (root.waiting && !root.hits.length) return "waiting"
    if (backend.phase === "ready" && root.query.length && !root.hits.length) return "empty"
    return "none"
  }

  readonly property string phaseGlyph: {
    switch (backend.phase) {
      case "ready": return "●"
      case "error": return "!"
      case "starting":
      case "searching":
      case "answering":
      case "indexing":
      case "busy": return "◐"
      default: return "○"
    }
  }
  readonly property color phaseColor: backend.phase === "error" ? root.urgent
                                    : backend.phase === "ready" ? root.accent
                                    : root.muted
  readonly property string phaseLabel: {
    switch (backend.phase) {
      case "ready": return "Ready"
      case "starting": return backend.message || "Starting"
      case "searching": return "Searching"
      case "answering": return "Answering"
      case "indexing": return "Indexing"
      case "busy": return "Busy"
      case "error": return "Error"
      default: return "Sleeping"
    }
  }

  // Shown briefly when an action was turned away, so a dead keypress is never
  // silent.
  Timer { id: noticeTimer; interval: 4000; onTriggered: root.notice = "" }
  function flash(text) { root.notice = text; noticeTimer.restart() }

  // ------------------------------------------------------------- lifecycle

  // The payload may carry {"query": "..."} so a keybind — or a test — can open
  // OMA with the question already in the box, the way the image picker takes
  // its rows from the summon payload.
  function open(payloadJson) {
    root.opened = true
    root.hits = []
    root.query = ""
    backend.rawAnswer = ""
    root.detailsOpen = false
    root.notice = ""
    root.pending = ""
    root.pendingAdd = []
    root.selected = -1

    var wanted = ""
    var toAdd = []
    if (payloadJson) {
      try {
        var parsed = JSON.parse(String(payloadJson))
        if (parsed && typeof parsed.query === "string") wanted = parsed.query
        // {"tab": "library"} opens straight into a section.
        if (parsed && typeof parsed.tab === "string") root.tab = parsed.tab
        // {"add": "/path"} or {"add": ["/a","/b"]} — lets a keybind hand OMA a
        // document without going through the chooser.
        if (parsed && parsed.add) toAdd = [].concat(parsed.add)
      } catch (e) { /* an unreadable payload just opens OMA empty */ }
    }
    chatTab.setInputText(wanted)
    root.pending = wanted

    if (backend.phase === "idle" || backend.phase === "error") backend.connect()
    Qt.callLater(function() { chatTab.focusInput() })
    if (wanted && backend.phase === "ready") root.runPending()

    if (toAdd.length) {
      root.tab = "library"
      root.pendingAdd = toAdd
      if (backend.phase === "ready") root.runPendingAdd()
    }
  }

  function close() {
    root.opened = false
    backend.releaseEngine()
  }

  function dismiss() {
    root.opened = false
    backend.releaseEngine()
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide((root.manifest && root.manifest.id) || "kaufmann.omarag")
  }

  function toggle() { if (root.opened) root.dismiss(); else root.open("{}") }

  // A payload query waits for the backend rather than polling for it. A timer
  // here used to give up after a minute and leave the UI claiming nothing
  // matched — for a query that had never run.
  property var pendingAdd: []

  function runPendingAdd() {
    if (!root.pendingAdd.length) return
    var paths = root.pendingAdd
    root.pendingAdd = []
    backend.addPaths(paths)
  }

  function runPending() {
    if (!root.pending.length) return
    var q = root.pending
    root.pending = ""
    chatTab.setInputText(q)
    root.runQuery()
  }

  // Enter asks, Ctrl+Enter retrieves only. Both are slow here — retrieval alone
  // takes 15 seconds — so the difference is 15 versus 30-odd, not instant
  // versus slow. The short way is for when the passage is what you want.
  function runQuery() {
    var q = chatTab.inputText().trim()
    if (!q) return
    if (root.blocked) { root.flash(backend.message); return }
    root.query = q
    root.hits = []
    root.selected = -1
    backend.ask(q)
  }

  function runSearchOnly() {
    var q = chatTab.inputText().trim()
    if (!q) return
    if (root.blocked) { root.flash(backend.message); return }
    root.query = q
    root.hits = []
    root.selected = -1
    backend.rawAnswer = ""
    backend.search(q, 5)
  }

  // The overlay holds the keyboard exclusively, but a file chooser still gets
  // it — verified: typing reached zenity's search field with OMA open. So no
  // juggling of the layer-shell focus is needed.
  function pickFiles() {
    if (picker.running) return
    picker.command = ["zenity", "--file-selection", "--multiple", "--separator=\n",
                      "--title=Add documents to OMA"]
    picker.running = true
  }

  function pickFolder() {
    if (picker.running) return
    picker.command = ["zenity", "--file-selection", "--directory",
                      "--title=Add a folder to OMA"]
    picker.running = true
  }

  Process {
    id: picker
    stdout: StdioCollector {
      onStreamFinished: {
        var paths = String(text).split("\n").filter(function(p) { return p.trim().length > 0 })
        if (paths.length) backend.addPaths(paths)
      }
    }
    onRunningChanged: if (!running) Qt.callLater(function() { chatTab.focusInput() })
  }

  Lilbee {
    id: backend
    persistDaemon: root.setting("backendWhenClosed", "Stop with OMA") === "Keep running"
    answerModel: root.setting("answerModel", "")
    onSearchFinished: function(rows) { root.hits = rows }
    onRefused: function(reason) { root.flash(reason) }
  }

  Connections {
    target: backend
    function onPhaseChanged() {
      if (backend.phase === "ready") { root.runPending(); root.runPendingAdd() }
    }
  }

  // ------------------------------------------------------------- window

  PanelWindow {
    id: panel
    // Steps aside while the file chooser is up. An overlay layer sits above
    // ordinary windows, so the dialog would otherwise be drawn behind it —
    // running, focused, and invisible.
    visible: root.opened && !picker.running
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-omarag"
    WlrLayershell.layer: WlrLayer.Overlay
    // Released while the file chooser is up. Holding the keyboard exclusively
    // meant the dialog never got it and the typing landed in OMA's own input
    // instead — the path became a question.
    WlrLayershell.keyboardFocus: picker.running ? WlrKeyboardFocus.None
                                                : WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle { anchors.fill: parent; color: Color.menu.scrim }
    MouseArea { anchors.fill: parent; onClicked: root.dismiss() }

    // The [menu] section is empty in some themes, which leaves the surface
    // token without a usable fill and lets the desktop show straight through
    // the card. An opaque base keeps the text readable whatever the theme
    // defines, while the card itself still carries the menu colour.
    Rectangle {
      anchors.fill: card
      color: Color.background
      radius: Style.cornerRadius
    }

    BorderSurface {
      id: card
      width: Math.min(Style.space(900), panel.width - Style.gapsOut * 4)
      height: Math.min(Style.space(620), panel.height - Style.gapsOut * 4)
      radius: Style.cornerRadius
      anchors.centerIn: parent
      color: Color.menu.background
      borderSpec: root.borderSpec
      padding: Style.spacing.panelPadding

      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        anchors.fill: parent
        anchors.margins: Style.spacing.panelPadding
        focus: true

        // Also handled here, not only in the input: once an answer is on
        // screen its Flickable can hold the focus, and navigation must not
        // depend on which child happens to have it.
        Keys.onPressed: function(event) {
          // Tab walks the sections; 1-3 jump straight there. Both only when
          // the text field is not where the typing should go.
          if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
            tabStrip.step(event.key === Qt.Key_Backtab ? -1 : 1)
            event.accepted = true
            return
          }
          if (event.key >= Qt.Key_1 && event.key <= Qt.Key_3
              && !(event.modifiers & Qt.ControlModifier)
              && !chatTab.inputText().length) {
            root.tab = root.tabs[event.key - Qt.Key_1].id
            event.accepted = true
            return
          }
          if (event.key === Qt.Key_O && (event.modifiers & Qt.ControlModifier)) {
            if (event.modifiers & Qt.ShiftModifier) root.pickFolder()
            else root.pickFiles()
            event.accepted = true
            return
          }
          if (event.key === Qt.Key_Down) { root.moveSelection(1); event.accepted = true; return }
          if (event.key === Qt.Key_Up) { root.moveSelection(-1); event.accepted = true; return }
          if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && root.selected >= 0) {
            root.activateSelected()
            event.accepted = true
            return
          }
          if (event.key === Qt.Key_Escape) {
            // Work outwards: stop what is running, fold details, clear the
            // query, then close.
            if (root.answering) backend.cancelAsk()
            else if (root.detailsOpen) root.detailsOpen = false
            else if (root.selected >= 0) root.selected = -1
            else if (chatTab.inputText().length || backend.answerText.length) {
              chatTab.setInputText(""); root.hits = []; root.query = ""; backend.rawAnswer = ""
            }
            else root.dismiss()
            event.accepted = true
          }
        }

        // ---------------------------------------------------------- header
        Item {
          id: header
          anchors { top: parent.top; left: parent.left; right: parent.right }
          height: Style.font.title + Style.spacing.lg * 2

          Text {
            id: wordmark
            anchors { left: parent.left; verticalCenter: parent.verticalCenter }
            text: "OmaRag"
            color: root.working ? root.accent : root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.title
            font.letterSpacing: 1.5

            Behavior on color { ColorAnimation { duration: 240 } }

            SequentialAnimation on opacity {
              running: root.working && root.opened
              loops: Animation.Infinite
              alwaysRunToEnd: true
              NumberAnimation { to: 0.55; duration: 800; easing.type: Easing.InOutSine }
              NumberAnimation { to: 1.0;  duration: 800; easing.type: Easing.InOutSine }
            }
          }

          TabStrip {
            id: tabStrip
            anchors {
              left: wordmark.right; right: status.left
              top: parent.top; bottom: parent.bottom
            }
            anchors.leftMargin: Style.spacing.huge
            anchors.rightMargin: Style.spacing.huge
            tabs: root.tabs
            current: root.tab
            foreground: root.foreground
            muted: root.muted
            accent: root.accent
            onSelected: function(id) { root.tab = id }
          }

          Row {
            id: status
            anchors { right: parent.right; verticalCenter: parent.verticalCenter }
            spacing: Style.spacing.sm

            Text {
              text: root.phaseGlyph
              color: root.phaseColor
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              anchors.verticalCenter: parent.verticalCenter
            }
            Text {
              text: root.phaseLabel
              color: root.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              anchors.verticalCenter: parent.verticalCenter
            }
          }
        }

        // ---------------------------------------------------------- tabs
        Item {
          id: content
          anchors {
            top: header.bottom; bottom: parent.bottom
            left: parent.left; right: parent.right
          }
          anchors.topMargin: Style.spacing.panelGap

          SetupTab {
            anchors.fill: parent
            visible: root.tab === "setup"
            backend: backend
            foreground: root.foreground
            muted: root.muted
          }

          ChatTab {
            id: chatTab
            anchors.fill: parent
            visible: root.tab === "chat"
            backend: backend
            hits: root.hits
            query: root.query
            selected: root.selected
            waited: root.waited
            detailsOpen: root.detailsOpen
            notice: root.notice
            foreground: root.foreground
            muted: root.muted
            accent: root.accent
            urgent: root.urgent
            onAsk: root.runQuery()
            onSearchOnly: root.runSearchOnly()
            onOpenSource: function(url, pages) { backend.openDocument(url, pages) }
            onRetry: backend.retry()
            onDetailsToggled: root.detailsOpen = !root.detailsOpen
            onPickFiles: root.pickFiles()
            onPickFolder: root.pickFolder()
            onSelectedChanged: root.selected = Math.max(-1, Math.min(selected, root.rowCount - 1))
          }

          LibraryTab {
            anchors.fill: parent
            visible: root.tab === "library"
            backend: backend
            waited: root.waited
            foreground: root.foreground
            muted: root.muted
            accent: root.accent
            onPickFiles: root.pickFiles()
            onPickFolder: root.pickFolder()
          }
        }
      }
    }
  }
}
