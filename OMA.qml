import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "backend"
import "common"
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

  // The type scale is read once here and pushed into the singleton the rest of
  // OMA reads; nothing else touches it.
  Binding { target: OmaFont; property: "scale"; value: Number(root.setting("omaFontScale", 1.0)) || 1.0 }
  Binding { target: OmaFont; property: "family"; value: String(root.setting("omaFontFamily", "")) }

  // The shell injects `settings` into bar widgets (Bar.qml:611) but into
  // nothing else — an overlay's loader hands it omarchyPath, shell, manifest
  // and the two registries, and stops there (shell.qml:1341). Writing works
  // (`updateEntryInline`), reading does not, so OMA reads its own entry out of
  // shell.json, which the README names as the one place settings live:
  // "Settings are inline on the entry. No config: sub-object, no separate
  // per-plugin settings file, no merge layers."
  property var configEntry: ({})

  function entrySettings() {
    var id = String((manifest && manifest.id) || "kaufmann.omarag")
    // If a future shell does expose the config to overlays, prefer it.
    var config = shell ? shell.shellConfig : null
    var plugins = config ? config.plugins : null
    if (Array.isArray(plugins))
      for (var i = 0; i < plugins.length; i++)
        if (String((plugins[i] && plugins[i].id) || "") === id) return plugins[i]
    return root.configEntry || ({})
  }

  FileView {
    id: shellConfigFile
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

  function syncSettings() { root.settings = entrySettings() || ({}) }

  // The scoped shell facade lets a plugin write its own inline settings back to
  // shell.json — the same entry `setting()` reads from.
  function writeOwnSetting(key, value) {
    var s = root.shell
    if (!s || typeof s.updateEntryInline !== "function") { root.flash("Could not save the setting"); return }
    var patch = {}
    patch[key] = value
    s.updateEntryInline((root.manifest && root.manifest.id) || "kaufmann.omarag", patch)
    root.flash(key + " saved")
  }

  // Loaded when the section is first opened rather than on startup: nobody
  // needs 159 settings fetched to ask a question.
  onTabChanged: {
    if (root.tab === "setup") {
      if (!toolchain.version.length) toolchain.readVersion()
      if (backend.phase === "ready" && !backend.settings.length) backend.loadSettings()
      // Reads what is already on disk; no network, so it comes with the tab
      // rather than waiting for a button.
      if (backend.phase === "ready" && !backend.installedModels.length) backend.loadModels()
    }
    Qt.callLater(function() { root.placeFocus() })
  }

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
  // A failure earns a longer look and the urgent colour; a confirmation does not.
  property bool noticeIsProblem: false
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
  Timer { id: noticeTimer; onTriggered: root.notice = "" }
  function flash(text, problem) {
    root.notice = text
    root.noticeIsProblem = problem === true
    noticeTimer.interval = root.noticeIsProblem ? 9000 : 4000
    noticeTimer.restart()
  }

  // Adding a document that lilbee cannot read used to end in silence: the call
  // succeeded, the list refreshed, and nothing said the file was missing.
  function reportIndexing(ok, rejected) {
    if (ok) { root.flash("Indexing finished"); return }
    if (!rejected || !rejected.length) { root.flash("Indexing did not finish", true); return }
    var names = []
    for (var i = 0; i < rejected.length && i < 3; i++)
      names.push(String(rejected[i]).split("/").pop())
    var more = rejected.length > names.length ? " and " + (rejected.length - names.length) + " more" : ""
    root.flash("Could not read " + names.join(", ") + more, true)
  }

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
    // Only Chat wants the caret. Setup and Library are read-and-click screens,
    // and handing the hidden input the focus there swallows Page/Home/End.
    Qt.callLater(function() { root.placeFocus() })
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
  property int historyIndex: -1

  // Recall shows the stored answer as it was — no new call to the backend, and
  // no pretence that it was answered again just now.
  function recall(index) {
    var e = history.entries[index]
    if (!e) return
    root.historyIndex = index
    root.tab = "chat"
    root.query = e.question
    root.hits = []
    root.selected = -1
    chatTab.setInputText(e.question)
    backend.showStored(e.answer, e.sources)
  }

  function runQuery() {
    var q = chatTab.inputText().trim()
    if (!q) return
    if (root.blocked) { root.flash(backend.message); return }
    root.query = q
    root.hits = []
    root.selected = -1
    root.historyIndex = -1
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

  History { id: history }
  Toolchain { id: toolchain }

  Lilbee {
    id: backend
    persistDaemon: root.setting("backendWhenClosed", "Stop with OMA") === "Keep running"
    answerModel: root.setting("answerModel", "")
    onSearchFinished: function(rows) { root.hits = rows }
    onRefused: function(reason) { root.flash(reason) }
    onIndexingFinished: function(ok, rejected) { root.reportIndexing(ok, rejected) }
    onAnswerFinished: function(ok) {
      if (ok) history.add(root.query, backend.answerText, backend.answerSources)
    }
  }

  Connections {
    target: backend
    function onPhaseChanged() {
      if (backend.phase !== "ready") return
      root.runPending()
      root.runPendingAdd()
      // Setup may have been opened before the backend was up; nothing retried
      // the load, so the sections stayed empty.
      if (root.tab === "setup" && !backend.settings.length) backend.loadSettings()
      if (root.tab === "setup" && !backend.installedModels.length) backend.loadModels()
    }
  }

  // ------------------------------------------------------------- window

  // Where the keyboard should point for the tab now on screen.
  function placeFocus() {
    if (root.tab === "chat") chatTab.focusInput()
    else if (keyRoot) keyRoot.forceActiveFocus()
  }



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
        id: keyRoot
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
          // Page and Home/End scroll the sheet under the pointer-free hand.
          // Setup is the long one, but the others may grow.
          var sheet = root.tab === "setup" ? setupTab : null
          if (sheet && typeof sheet.scrollBy === "function") {
            var step = card.height - Style.space(80)
            if (event.key === Qt.Key_PageDown) { sheet.scrollBy(step); event.accepted = true; return }
            if (event.key === Qt.Key_PageUp) { sheet.scrollBy(-step); event.accepted = true; return }
            if (event.key === Qt.Key_Home) { sheet.scrollBy(-1e6); event.accepted = true; return }
            if (event.key === Qt.Key_End) { sheet.scrollBy(1e6); event.accepted = true; return }
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
          height: OmaFont.title + Style.spacing.lg * 2

          Text {
            id: wordmark
            anchors { left: parent.left; verticalCenter: parent.verticalCenter }
            text: "OmaRag"
            color: root.working ? root.accent : root.foreground
            font.family: OmaFont.face
            font.pixelSize: OmaFont.title
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
              font.family: OmaFont.face
              font.pixelSize: OmaFont.bodySmall
              anchors.verticalCenter: parent.verticalCenter
            }
            Text {
              text: root.phaseLabel
              color: root.muted
              font.family: OmaFont.face
              font.pixelSize: OmaFont.bodySmall
              anchors.verticalCenter: parent.verticalCenter
            }
          }
        }

        // ---------------------------------------------------------- tabs
        // One place for short-lived messages, at the card's foot rather than
        // inside a tab. It lived in ChatTab, which meant adding a document —
        // done from Library — reported failures into a line nobody could see.
        Text {
          id: noticeLine
          anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
          visible: root.notice.length > 0
          text: root.notice
          color: root.noticeIsProblem ? root.urgent : root.muted
          font.family: OmaFont.face
          font.pixelSize: OmaFont.caption
          wrapMode: Text.WordWrap
          horizontalAlignment: Text.AlignHCenter
        }

        Item {
          id: content
          anchors {
            top: header.bottom
            bottom: noticeLine.visible ? noticeLine.top : parent.bottom
            left: parent.left; right: parent.right
          }
          anchors.topMargin: Style.spacing.panelGap
          anchors.bottomMargin: noticeLine.visible ? Style.spacing.sm : 0

          SetupTab {
            id: setupTab
            anchors.fill: parent
            visible: root.tab === "setup"
            backend: backend
            lilbeeVersion: toolchain.version
            updateNote: toolchain.note
            checkingUpdate: toolchain.checking
            backendWhenClosed: root.setting("backendWhenClosed", "Stop with OMA")
            answerModel: root.setting("answerModel", "")
            fontScale: root.setting("omaFontScale", 1.0)
            fontFamily: root.setting("omaFontFamily", "")
            foreground: root.foreground
            muted: root.muted
            accent: root.accent
            urgent: root.urgent
            onCheckUpdate: toolchain.checkLatest()
            onReleaseEngine: { backend.stopEngineNow(); root.flash("Models released") }
            onOpenLog: backend.openDocument("file://" + Quickshell.env("HOME")
                                            + "/.local/share/lilbee/logs/server.log", "")
            onOmaSettingChanged: function(key, value) { root.writeOwnSetting(key, value) }
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
            onActivateRow: root.activateSelected()
            history: history.entries
            historyIndex: root.historyIndex
            onHistoryPicked: function(i) { root.recall(i) }
            onHistoryRemoved: function(i) { history.removeAt(i) }
            onCopied: function(n) { root.flash(n + " characters copied") }
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
