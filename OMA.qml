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

  readonly property bool blocked: backend.phase === "busy"
  readonly property bool answering: backend.phase === "answering"
  readonly property bool waiting: backend.phase === "searching" || backend.phase === "starting"

  // Elapsed seconds while we wait. The one honest thing to show when the
  // remaining time is unknowable — a cold embedder takes past a minute, and a
  // progress bar would be a guess.
  property int waited: 0
  Timer {
    interval: 1000
    repeat: true
    running: (root.waiting || root.answering) && root.opened
    onTriggered: root.waited += 1
  }
  onWaitingChanged: root.waited = 0
  onAnsweringChanged: root.waited = 0

  readonly property bool showingAnswer: root.answering || backend.answerText.length > 0

  readonly property string panelMode: {
    if (backend.phase === "error") return "error"
    if (root.blocked) return "blocked"
    if (root.showingAnswer) return "none"
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

    var wanted = ""
    if (payloadJson) {
      try {
        var parsed = JSON.parse(String(payloadJson))
        if (parsed && typeof parsed.query === "string") wanted = parsed.query
      } catch (e) { /* an unreadable payload just opens OMA empty */ }
    }
    input.text = wanted
    root.pending = wanted

    if (backend.phase === "idle" || backend.phase === "error") backend.connect()
    Qt.callLater(function() { input.forceActiveFocus() })
    if (wanted && backend.phase === "ready") root.runPending()
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
  function runPending() {
    if (!root.pending.length) return
    var q = root.pending
    root.pending = ""
    input.text = q
    root.runQuery()
  }

  // Enter asks, Ctrl+Enter retrieves only. Both are slow here — retrieval alone
  // takes 15 seconds — so the difference is 15 versus 30-odd, not instant
  // versus slow. The short way is for when the passage is what you want.
  function runQuery() {
    var q = input.text.trim()
    if (!q) return
    if (root.blocked) { root.flash(backend.message); return }
    root.query = q
    root.hits = []
    backend.ask(q)
  }

  function runSearchOnly() {
    var q = input.text.trim()
    if (!q) return
    if (root.blocked) { root.flash(backend.message); return }
    root.query = q
    root.hits = []
    backend.rawAnswer = ""
    backend.search(q, 5)
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
      if (backend.phase === "ready") root.runPending()
    }
  }

  // ------------------------------------------------------------- window

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-omarag"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
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

        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) {
            // Work outwards: stop what is running, fold details, clear the
            // query, then close.
            if (root.answering) backend.cancelAsk()
            else if (root.detailsOpen) root.detailsOpen = false
            else if (input.text.length || backend.answerText.length) {
              input.text = ""; root.hits = []; root.query = ""; backend.rawAnswer = ""
            }
            else root.dismiss()
            event.accepted = true
          }
        }

        // ---------------------------------------------------------- header
        Item {
          id: header
          anchors { top: parent.top; left: parent.left; right: parent.right }
          height: Style.font.title + Style.spacing.md * 2

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "OMA"
            color: root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.title
            font.letterSpacing: 1.5
          }

          Row {
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

        // ---------------------------------------------------------- library
        LibraryPane {
          id: library
          anchors { top: header.bottom; bottom: parent.bottom; left: parent.left }
          anchors.topMargin: Style.spacing.panelGap
          width: Style.space(220)
          documents: backend.documents
          totalChunks: backend.totalChunks
          ready: backend.phase === "ready"
          foreground: root.foreground
          muted: root.muted
        }

        // ---------------------------------------------------------- ask
        Item {
          anchors {
            top: header.bottom; bottom: parent.bottom
            left: library.right; right: parent.right
          }
          anchors.topMargin: Style.spacing.panelGap
          anchors.leftMargin: Style.spacing.panelPadding

          Rectangle {
            id: inputBox
            anchors { top: parent.top; left: parent.left; right: parent.right }
            height: Style.spacing.controlHeight + Style.spacing.md
            color: Style.controlFill(input.activeFocus, false, Color.menu.text, root.accent)
            border.color: Style.controlBorder(input.activeFocus, false, Color.menu.text, root.accent)
            border.width: Style.controlBorderWidth(input.activeFocus, false)
            radius: Style.cornerRadius

            TextInput {
              id: input
              anchors.fill: parent
              anchors.leftMargin: Style.spacing.rowPaddingX
              anchors.rightMargin: Style.spacing.rowPaddingX
              verticalAlignment: TextInput.AlignVCenter
              color: root.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.subtitle
              selectByMouse: true
              onAccepted: root.runQuery()

              Keys.onPressed: function(event) {
                if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                    && (event.modifiers & Qt.ControlModifier)) {
                  root.runSearchOnly()
                  event.accepted = true
                }
              }

              Text {
                anchors.fill: parent
                verticalAlignment: Text.AlignVCenter
                visible: !input.text.length
                text: "Ask your knowledge…"
                color: root.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.subtitle
              }
            }
          }

          AnswerView {
            anchors {
              top: inputBox.bottom; bottom: parent.bottom
              left: parent.left; right: parent.right
            }
            anchors.topMargin: Style.spacing.panelGap
            visible: root.showingAnswer
            answer: backend.answerText
            sources: backend.answerSources
            answering: root.answering
            waited: root.waited
            foreground: root.foreground
            muted: root.muted
            accent: root.accent
            onCancelRequested: backend.cancelAsk()
          }

          SourceList {
            anchors {
              top: inputBox.bottom; bottom: parent.bottom
              left: parent.left; right: parent.right
            }
            anchors.topMargin: Style.spacing.panelGap
            visible: !root.showingAnswer && root.hits.length > 0
            hits: root.hits
            foreground: root.foreground
            muted: root.muted
          }

          StatePanel {
            anchors.fill: parent
            mode: root.panelMode
            headline: backend.phase === "error" ? backend.message : (backend.message || "Searching")
            detail: backend.detail
            waited: root.waited
            hasLibrary: backend.totalChunks > 0
            detailsOpen: root.detailsOpen
            foreground: root.foreground
            muted: root.muted
            urgent: root.urgent
            onRetryRequested: backend.retry()
            onDetailsToggled: root.detailsOpen = !root.detailsOpen
          }

          Text {
            anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
            visible: root.notice.length > 0
            text: root.notice
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }
}
