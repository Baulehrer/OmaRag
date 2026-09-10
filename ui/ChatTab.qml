import QtQuick
import qs.Commons
import qs.Ui
import "../common"

// Ask, read the answer, get to the passages behind it.
//
// The backend object is handed down whole rather than unpacked into a dozen
// properties. This is one plugin, not a library, and the alternative is more
// wiring than substance.
Item {
  id: root

  property var backend: null
  property var hits: []
  property string query: ""
  property int selected: -1
  property int waited: 0
  property bool detailsOpen: false

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent
  property color urgent: Color.urgent

  signal activateRow()
  signal ask()
  signal searchOnly()
  signal openSource(string url, string pages)
  signal retry()
  signal detailsToggled()
  signal pickFiles()
  signal pickFolder()
  signal copied(int characters)

  readonly property bool answering: backend && backend.phase === "answering"
  readonly property bool indexing: backend && backend.phase === "indexing"
  readonly property bool blocked: backend && backend.phase === "busy"
  readonly property bool waiting: backend && (backend.phase === "searching" || backend.phase === "starting")
  readonly property bool showingAnswer: root.answering || (backend && backend.answerText.length > 0)

  readonly property string panelMode: {
    if (!backend) return "none"
    if (backend.phase === "error") return "error"
    if (root.indexing) return "indexing"
    if (root.blocked) return "blocked"
    if (root.answering) return "none"
    if (root.showingAnswer) return "none"
    if (root.waiting && !root.hits.length) return "waiting"
    if (backend.phase === "ready" && root.query.length && !root.hits.length) return "empty"
    return "none"
  }

  function focusInput() { input.forceActiveFocus() }
  function inputText() { return input.text }
  function setInputText(t) { input.text = t }

  property var history: []
  property int historyIndex: -1
  signal historyPicked(int index)
  signal historyRemoved(int index)

  HistoryPane {
    id: historyPane
    anchors { top: parent.top; bottom: parent.bottom; left: parent.left }
    width: Style.space(200)
    entries: root.history
    current: root.historyIndex
    foreground: root.foreground
    muted: root.muted
    accent: root.accent
    onPicked: function(i) { root.historyPicked(i) }
    onRemoveRequested: function(i) { root.historyRemoved(i) }
  }

  Rectangle {
    id: inputBox
    anchors { top: parent.top; left: historyPane.right; right: parent.right }
    anchors.leftMargin: Style.spacing.panelPadding
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
      font.family: OmaFont.face
      font.pixelSize: OmaFont.subtitle
      selectByMouse: true
      onAccepted: root.ask()

      // Up and Down do nothing in a single-line field, so they are free to
      // walk the results without stealing anything from typing.
      Keys.onPressed: function(event) {
        if (event.key === Qt.Key_O && (event.modifiers & Qt.ControlModifier)) {
          if (event.modifiers & Qt.ShiftModifier) root.pickFolder()
          else root.pickFiles()
          event.accepted = true
          return
        }
        if (event.key === Qt.Key_Down) { root.selected = root.selected + 1; event.accepted = true; return }
        if (event.key === Qt.Key_Up) { root.selected = root.selected - 1; event.accepted = true; return }
        if (event.key !== Qt.Key_Return && event.key !== Qt.Key_Enter) return
        if (event.modifiers & Qt.ControlModifier) { root.searchOnly(); event.accepted = true; return }
        // The card also handles Return, but it never gets the chance: a
        // TextInput emits onAccepted for any Return this handler does not
        // accept, and onAccepted asks. So a selected row has to be acted on
        // here, and the event accepted, or Enter silently re-asks the question
        // instead of opening the cited page.
        if (root.selected >= 0) { root.activateRow(); event.accepted = true }
      }

      Text {
        anchors.fill: parent
        verticalAlignment: Text.AlignVCenter
        visible: !input.text.length
        text: "Ask your knowledge…"
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.subtitle
      }
    }
  }

  AnswerView {
    anchors { top: inputBox.bottom; bottom: parent.bottom; left: inputBox.left; right: parent.right }
    anchors.topMargin: Style.spacing.panelGap
    visible: root.showingAnswer && !root.indexing
    answer: root.backend ? root.backend.answerText : ""
    sources: root.backend ? root.backend.answerSources : []
    answering: root.answering
    waited: root.waited
    current: root.selected
    foreground: root.foreground
    muted: root.muted
    accent: root.accent
    onOpenRequested: function(url, pages) { root.openSource(url, pages) }
    onCopied: function(n) { root.copied(n) }
  }

  SourceList {
    id: sourceList
    anchors { top: inputBox.bottom; bottom: parent.bottom; left: inputBox.left; right: parent.right }
    anchors.topMargin: Style.spacing.panelGap
    visible: !root.showingAnswer && !root.indexing && root.hits.length > 0
    hits: root.hits
    current: root.selected
    foreground: root.foreground
    muted: root.muted
    accent: root.accent
  }

  function expandSelected() {
    sourceList.expanded = (sourceList.expanded === root.selected) ? -1 : root.selected
  }

  AnswerProgress {
    anchors { top: inputBox.bottom; bottom: parent.bottom; left: inputBox.left; right: parent.right }
    anchors.topMargin: Style.spacing.panelGap
    visible: root.answering
    stage: root.backend ? root.backend.answerStage : ""
    elapsed: root.backend ? root.backend.answerElapsed : 0
    expected: root.backend ? root.backend.answerExpected : 0
    foreground: root.foreground
    muted: root.muted
    accent: root.accent
  }

  StatePanel {
    anchors { top: inputBox.bottom; bottom: parent.bottom; left: inputBox.left; right: parent.right }
    anchors.topMargin: Style.spacing.panelGap
    mode: root.panelMode
    what: root.backend ? root.backend.indexingWhat : ""
    headline: !root.backend ? ""
            : (root.backend.phase === "error" ? root.backend.message
                                              : (root.backend.message || "Searching"))
    detail: root.backend ? root.backend.detail : ""
    waited: root.waited
    hasLibrary: root.backend && root.backend.totalChunks > 0
    detailsOpen: root.detailsOpen
    foreground: root.foreground
    muted: root.muted
    urgent: root.urgent
    onRetryRequested: root.retry()
    onDetailsToggled: root.detailsToggled()
  }

}
