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
  // Read through the service, which owns the one copy of shell.json.
  function setting(name, fallback) {
    return root.service ? root.service.setting(name, fallback) : fallback
  }


  // The scoped shell facade lets a plugin write its own inline settings back to
  // shell.json — the same entry `setting()` reads from.
  function writeOwnSetting(key, value) {
    if (root.service && root.service.writeSetting(key, value)) root.flash(key + " saved")
    else root.flash("Could not save the setting", true)
  }

  // Loaded when the section is first opened rather than on startup: nobody
  // needs 159 settings fetched to ask a question.
  onTabChanged: {
    if (root.tab === "setup") {
      if (!toolchain.version.length) toolchain.readVersion()
      if (root.backend.phase === "ready" && !root.backend.settings.length) root.backend.loadSettings()
      // Reads what is already on disk; no network, so it comes with the tab
      // rather than waiting for a button.
      if (root.backend.phase === "ready" && !root.backend.installedModels.length) root.backend.loadModels()
    }
    Qt.callLater(function() { root.placeFocus() })
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
  // Compact opens a small card under the bar icon, big fills the middle of the
  // screen. Remembered, because the choice is a habit rather than a per-use
  // decision.
  onCompactChanged: if (root.compact) root.tab = "chat"
  readonly property bool compact: root.setting("compactMode", false) === true
                               || String(root.setting("compactMode", false)) === "true"
  readonly property real anchorX: root.service ? root.service.anchorX : 0
  readonly property real anchorY: root.service ? root.service.anchorY : 0
  readonly property bool anchorAtTop: root.service ? root.service.anchorAtTop : true

  // 1.0 is opaque. Clamped well short of invisible: a window you cannot read
  // is not a setting, it is a fault.
  readonly property real cardOpacity: Math.max(0.45,
      Math.min(1.0, Number(root.setting("windowOpacity", 1.0)) || 1.0))

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
                               : (root.showingAnswer ? root.backend.answerSources.length : root.hits.length)

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
      var src = root.backend.answerSources[root.selected]
      if (src) root.backend.openDocument(src.url, src.pages)
    } else {
      chatTab.expandSelected()
    }
    return true
  }

  // The wordmark breathes while work is happening — and only then. Movement
  // that is always on stops meaning anything.
  readonly property bool working: root.backend.phase === "starting" || root.backend.phase === "searching"
                               || root.backend.phase === "answering" || root.backend.phase === "indexing"
                               || root.backend.phase === "busy"

  readonly property bool blocked: root.backend.phase === "busy"
  readonly property bool indexing: root.backend.phase === "indexing"
  readonly property bool answering: root.backend.phase === "answering"
  readonly property bool waiting: root.backend.phase === "searching" || root.backend.phase === "starting"

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

  readonly property bool showingAnswer: root.answering || root.backend.answerText.length > 0

  readonly property string panelMode: {
    if (root.backend.phase === "error") return "error"
    if (root.indexing) return "indexing"
    if (root.blocked) return "blocked"
    if (root.showingAnswer && !root.indexing) return "none"
    if (root.waiting && !root.hits.length) return "waiting"
    if (root.backend.phase === "ready" && root.query.length && !root.hits.length) return "empty"
    return "none"
  }

  readonly property string phaseGlyph: {
    switch (root.backend.phase) {
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
  readonly property color phaseColor: root.backend.phase === "error" ? root.urgent
                                    : root.backend.phase === "ready" ? root.accent
                                    : root.muted
  readonly property string phaseLabel: {
    switch (root.backend.phase) {
      case "ready": return "Ready"
      case "starting": return root.backend.message || "Starting"
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
    if (root.backend) root.backend.rawAnswer = ""
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

    if (root.service) { root.service.viewOpen = true; root.service.markSeen() }
    root.connectIfWanted()
    // Only Chat wants the caret. Setup and Library are read-and-click screens,
    // and handing the hidden input the focus there swallows Page/Home/End.
    Qt.callLater(function() { root.placeFocus() })
    if (wanted && root.backend.phase === "ready") root.runPending()

    if (toAdd.length) {
      root.tab = "library"
      root.pendingAdd = toAdd
      if (root.backend.phase === "ready") root.runPendingAdd()
    }
  }

  function close() {
    root.opened = false
    if (root.service) root.service.viewOpen = false
    root.backend.releaseEngine()
  }

  function dismiss() {
    root.opened = false
    if (root.service) root.service.viewOpen = false
    root.backend.releaseEngine()
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
    root.backend.addPaths(paths)
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
    if (!root.history) return
    var e = root.history.entries[index]
    if (!e) return
    root.historyIndex = index
    root.tab = "chat"
    root.query = e.question
    root.hits = []
    root.selected = -1
    chatTab.setInputText(e.question)
    root.backend.showStored(e.answer, e.sources)
  }

  function runQuery() {
    var q = chatTab.inputText().trim()
    if (!q) return
    if (root.blocked) { root.flash(root.backend.message); return }
    root.query = q
    root.hits = []
    root.selected = -1
    root.historyIndex = -1
    root.backend.ask(q)
  }

  function runSearchOnly() {
    var q = chatTab.inputText().trim()
    if (!q) return
    if (root.blocked) { root.flash(root.backend.message); return }
    root.query = q
    root.hits = []
    root.selected = -1
    if (root.backend) root.backend.rawAnswer = ""
    root.backend.search(q, 5)
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
        if (paths.length) root.backend.addPaths(paths)
      }
    }
    onRunningChanged: if (!running) Qt.callLater(function() { chatTab.focusInput() })
  }

  // The machinery lives in the service so a running answer survives closing.
  // These aliases keep the rest of the view reading the way it always did.
  property var service: null
  readonly property var backend: root.service ? root.service.backend : null

  // The loader assigns `service` after it assigns the rest, and the payload
  // arrives later still — so opening cannot be the only thing that connects.
  onServiceChanged: root.connectIfWanted()
  function connectIfWanted() {
    if (!root.opened || !root.backend) return
    if (root.backend.phase === "idle" || root.backend.phase === "error") root.backend.connect()
  }
  readonly property var history: root.service ? root.service.history : null
  readonly property var toolchain: root.service ? root.service.toolchain : null

  Connections {
    target: root.service
    function onSoundNoticed(text, problem) { root.flash(text, problem) }
  }

  Connections {
    target: root.backend
    function onSearchFinished(rows) { root.hits = rows }
    function onRefused(reason) { root.flash(reason) }
    function onOpenFailed(what) {
      root.flash("Could not open " + (what.length ? what : "the document")
                 + " — has it been moved?", true)
    }
    function onIndexingFinished(ok, rejected) { root.reportIndexing(ok, rejected) }
    function onEnginePutAway() { root.flash("Retrieval models released") }
    function onAnswerFinished(ok) {
      if (ok && root.history)
        root.history.add(root.query, root.backend.answerText, root.backend.answerSources)
    }
    function onPhaseChanged() {
      if (root.backend.phase !== "ready") return
      root.runPending()
      root.runPendingAdd()
      // Setup may have been opened before the backend was up; nothing retried
      // the load, so the sections stayed empty.
      if (root.tab === "setup" && !root.backend.settings.length) root.backend.loadSettings()
      if (root.tab === "setup" && !root.backend.installedModels.length) root.backend.loadModels()
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

    // Two sizes, one window. The shell gives a plugin exactly one window loader
    // and prefers `panel` over `overlay` (shell.qml computePanelEntries), so a
    // separate popup for the compact mode is not available. The card simply
    // shrinks and moves under the bar icon instead, which also makes switching
    // a matter of two numbers rather than two windows.
    BorderSurface {
      id: card
      // Math.max because the panel reports a placeholder size while the layer
      // surface is being configured, and a negative width leaves an empty card.
      width: Math.max(Style.space(280), root.compact
        ? Math.min(Style.space(460), panel.width - Style.gapsOut * 2)
        : Math.min(Style.space(900), panel.width - Style.gapsOut * 4))
      height: Math.max(Style.space(200), root.compact
        ? Math.min(Style.space(520), panel.height - Style.gapsOut * 4)
        : Math.min(Style.space(620), panel.height - Style.gapsOut * 4))
      radius: Style.cornerRadius

      // Centred when big; under the icon when compact, clamped so it never
      // hangs off an edge. The bar may sit at the top or the bottom, so the
      // card goes below or above the anchor accordingly.
      anchors.centerIn: root.compact ? undefined : parent
      x: root.compact ? Math.max(Style.gapsOut,
                        Math.min(panel.width - width - Style.gapsOut,
                                 root.anchorX - width / 2)) : 0
      y: root.compact
        ? (root.anchorAtTop ? Math.min(panel.height - height - Style.gapsOut,
                                       root.anchorY + Style.gapsOut)
                            : Math.max(Style.gapsOut,
                                       root.anchorY - height - Style.gapsOut))
        : 0

      // Only the card's ground takes the alpha. Fading the text with it would
      // trade legibility for looks, and this is a window for reading numbers
      // out of a textbook.
      color: Qt.rgba(Color.menu.background.r, Color.menu.background.g,
                     Color.menu.background.b, root.cardOpacity)
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
            if (root.answering) root.backend.cancelAsk()
            else if (root.detailsOpen) root.detailsOpen = false
            else if (root.selected >= 0) root.selected = -1
            else if (chatTab.inputText().length || root.backend.answerText.length) {
              chatTab.setInputText(""); root.hits = []; root.query = ""; root.backend.rawAnswer = ""
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

          // Two sizes, one control. A Nerd Font glyph rather than a word, so it
          // stays out of the way of the wordmark in the small window.
          Text {
            id: sizeToggle
            anchors { right: status.left; verticalCenter: parent.verticalCenter }
            anchors.rightMargin: Style.spacing.lg
            text: root.compact ? "" : ""
            color: sizeHover.hovered ? root.accent : root.muted
            font.family: OmaFont.face
            font.pixelSize: OmaFont.subtitle

            HoverHandler { id: sizeHover }
            TapHandler {
              onTapped: {
                root.writeOwnSetting("compactMode", !root.compact)
                root.flash(root.compact ? "Large window" : "Compact window")
              }
            }
          }

          TabStrip {
            id: tabStrip
            // Compact is the chat alone, so there is nothing to switch between.
            visible: !root.compact
            anchors {
              left: wordmark.right; right: sizeToggle.left
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
            backend: root.backend
            lilbeeVersion: toolchain.version
            updateNote: toolchain.note
            checkingUpdate: toolchain.checking
            backendWhenClosed: root.setting("backendWhenClosed", "Stop with OMA")
            answerModel: root.setting("answerModel", "")
            fontScale: root.setting("omaFontScale", 1.0)
            fontFamily: root.setting("omaFontFamily", "")
            compactMode: root.compact
            windowOpacity: root.cardOpacity
            soundFile: root.service ? root.service.soundFile : ""
            herdrSoundFile: root.service ? root.service.herdrSoundFile : ""
            desktopSoundFile: root.service ? root.service.desktopSoundFile : ""
            foreground: root.foreground
            muted: root.muted
            accent: root.accent
            urgent: root.urgent
            onCheckUpdate: toolchain.checkLatest()
            onReleaseEngine: { root.backend.stopEngineNow(); root.flash("Models released") }
            onOpenLog: root.backend.openDocument("file://" + Quickshell.env("HOME")
                                            + "/.local/share/lilbee/logs/server.log", "")
            onOmaSettingChanged: function(key, value) { root.writeOwnSetting(key, value) }
            // Picking herdr means lifting its sound out of the installed binary
            // first; the service writes the setting once it has a file.
            onSoundChosen: function(which) {
              if (!root.service) return
              if (which === "herdr") { root.flash("Reading herdr's sound"); root.service.adoptHerdrSound() }
              else root.writeOwnSetting("notifySound",
                     which === "desktop" ? root.service.desktopSoundFile : "")
            }
            onSoundTested: {
              if (!root.service) return
              if (!root.service.soundFile.length) root.flash("Sound is off")
              else root.service.chime()
            }
          }

          ChatTab {
            id: chatTab
            anchors.fill: parent
            visible: root.tab === "chat"
            backend: root.backend
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
            onOpenSource: function(url, pages) { root.backend.openDocument(url, pages) }
            onRetry: root.backend.retry()
            onDetailsToggled: root.detailsOpen = !root.detailsOpen
            onPickFiles: root.pickFiles()
            onPickFolder: root.pickFolder()
            onSelectedChanged: root.selected = Math.max(-1, Math.min(selected, root.rowCount - 1))
            onActivateRow: root.activateSelected()
            history: root.history ? root.history.entries : []
            historyIndex: root.historyIndex
            onHistoryPicked: function(i) { root.recall(i) }
            onHistoryRemoved: function(i) { if (root.history) root.history.removeAt(i) }
            onCopied: function(n) { root.flash(n + " characters copied") }
          }

          LibraryTab {
            anchors.fill: parent
            visible: root.tab === "library"
            backend: root.backend
            waited: root.waited
            foreground: root.foreground
            muted: root.muted
            accent: root.accent
            urgent: root.urgent
            detailsOpen: root.detailsOpen
            onPickFiles: root.pickFiles()
            onPickFolder: root.pickFolder()
            onRetry: root.backend.retry()
            onDetailsToggled: root.detailsOpen = !root.detailsOpen
          }
        }
      }
    }
  }
}
