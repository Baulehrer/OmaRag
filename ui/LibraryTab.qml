import QtQuick
import qs.Commons
import qs.Ui
import "../common"

// What is in the knowledge base, and what is currently going into it.
// Library and Indexing are one place because they are one subject: an index
// run is the library changing.
Item {
  id: root

  property var backend: null
  property int waited: 0

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent

  signal pickFiles()
  signal pickFolder()

  readonly property bool indexing: backend && backend.phase === "indexing"

  Text {
    id: heading
    anchors { top: parent.top; left: parent.left }
    text: root.indexing ? "INDEXING" : "LIBRARY"
    color: root.muted
    font.family: OmaFont.face
    font.pixelSize: OmaFont.caption
    font.letterSpacing: 1.5
  }

  Text {
    anchors { top: parent.top; right: parent.right }
    visible: !root.indexing
    text: root.backend && root.backend.totalChunks >= 0
        ? root.backend.documents.length + " documents · " + root.backend.totalChunks + " chunks"
        : ""
    color: root.muted
    font.family: OmaFont.face
    font.pixelSize: OmaFont.caption
  }

  // ------------------------------------------------------------ documents
  ListView {
    id: list
    anchors { top: heading.bottom; left: parent.left; right: parent.right }
    anchors.topMargin: Style.spacing.panelGap
    height: Math.min(contentHeight, parent.height - heading.height - Style.space(90))
    visible: !root.indexing
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    model: root.backend ? root.backend.documents : []

    // list_documents names a document `filename`; search results call the same
    // thing `source`. Cover both rather than guessing one.
    delegate: Item {
      width: list.width
      height: Style.spacing.popupRowHeight

      Text {
        anchors { left: parent.left; right: count.left; verticalCenter: parent.verticalCenter }
        anchors.rightMargin: Style.spacing.md
        text: modelData.filename || modelData.source || modelData.title || ""
        color: root.foreground
        font.family: OmaFont.face
        font.pixelSize: OmaFont.bodySmall
        elide: Text.ElideMiddle
      }
      Text {
        id: count
        anchors { right: parent.right; verticalCenter: parent.verticalCenter }
        text: modelData.chunk_count ? modelData.chunk_count + " chunks" : ""
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.caption
      }
    }
  }

  Text {
    anchors { top: heading.bottom; left: parent.left; right: parent.right }
    anchors.topMargin: Style.spacing.panelGap
    visible: !root.indexing && root.backend && root.backend.phase === "ready"
             && !root.backend.documents.length
    text: "Nothing indexed yet"
    color: root.muted
    font.family: OmaFont.face
    font.pixelSize: OmaFont.bodySmall
  }

  // ------------------------------------------------------------ add
  Column {
    anchors { bottom: parent.bottom; left: parent.left; right: parent.right }
    visible: !root.indexing
    spacing: Style.spacing.sm

    Rectangle {
      width: parent.width
      height: Style.spacing.controlHeight + Style.spacing.lg
      color: "transparent"
      border.color: Style.normalBorderColor
      border.width: Math.max(1, Style.space(1))
      radius: Style.cornerRadius

      Text {
        anchors.centerIn: parent
        text: "Ctrl+O  add files          Ctrl+Shift+O  add a folder"
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.bodySmall
      }

      HoverHandler { cursorShape: Qt.PointingHandCursor }
      TapHandler { onTapped: root.pickFiles() }
    }
  }

  // ------------------------------------------------------------ indexing
  IndexProgress {
    anchors.fill: parent
    anchors.topMargin: heading.height + Style.spacing.panelGap
    visible: root.indexing
    what: root.backend ? root.backend.indexingWhat : ""
    stage: root.backend ? root.backend.indexStage : ""
    done: root.backend ? root.backend.indexDone : -1
    total: root.backend ? root.backend.indexTotal : -1
    calls: root.backend ? root.backend.indexCalls : 0
    waited: root.waited
    foreground: root.foreground
    muted: root.muted
    accent: root.accent
  }
}
