import QtQuick
import qs.Commons
import qs.Ui
import "../common"

// What is actually in the knowledge base. Becomes the Library tab once there
// are tabs; until then it is the left column.
Item {
  id: root

  property var documents: []
  property int totalChunks: -1
  property bool ready: false
  property color foreground: Color.menu.text
  property color muted: Color.muted

  Text {
    id: heading
    text: "LIBRARY"
    color: root.muted
    font.family: OmaFont.face
    font.pixelSize: OmaFont.caption
    font.letterSpacing: 1.5
  }

  ListView {
    id: list
    anchors { top: heading.bottom; left: parent.left; right: parent.right }
    anchors.topMargin: Style.spacing.md
    height: Math.min(contentHeight, parent.height - heading.height - Style.space(60))
    clip: true
    model: root.documents

    // list_documents names a document `filename`; search results call the same
    // thing `source`. Cover both rather than guessing one.
    delegate: Item {
      width: list.width - Style.spacing.md
      height: Style.spacing.popupRowHeight

      Text {
        anchors { left: parent.left; right: count.left; verticalCenter: parent.verticalCenter }
        anchors.rightMargin: Style.spacing.sm
        text: modelData.filename || modelData.source || modelData.title || ""
        color: root.foreground
        font.family: OmaFont.face
        font.pixelSize: OmaFont.bodySmall
        elide: Text.ElideMiddle
      }
      Text {
        id: count
        anchors { right: parent.right; verticalCenter: parent.verticalCenter }
        text: modelData.chunk_count ? modelData.chunk_count + "" : ""
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.caption
      }
    }
  }

  Text {
    anchors { top: heading.bottom; left: parent.left; right: parent.right }
    anchors.topMargin: Style.spacing.md
    visible: root.ready && !root.documents.length
    text: "Nothing indexed yet"
    color: root.muted
    font.family: OmaFont.face
    font.pixelSize: OmaFont.bodySmall
    wrapMode: Text.WordWrap
  }

  Text {
    anchors { bottom: parent.bottom; left: parent.left }
    text: root.totalChunks < 0 ? "" : root.totalChunks + " chunks"
    color: root.muted
    font.family: OmaFont.face
    font.pixelSize: OmaFont.caption
  }
}
