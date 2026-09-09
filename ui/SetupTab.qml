import QtQuick
import qs.Commons
import qs.Ui

// Placeholder for the scaffolding step: shows what OMA knows about the backend
// right now. The real sections — models, warm-up, retrieval, appearance,
// toolchain — land here next.
Item {
  id: root

  property var backend: null
  property color foreground: Color.menu.text
  property color muted: Color.muted

  Column {
    anchors { top: parent.top; left: parent.left; right: parent.right }
    spacing: Style.spacing.panelGap

    Text {
      text: "BACKEND"
      color: root.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      font.letterSpacing: 1.5
    }

    Grid {
      columns: 2
      columnSpacing: Style.spacing.xxl
      rowSpacing: Style.spacing.sm

      Text {
        text: "Server"
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
      Text {
        text: root.backend && root.backend.port > 0
            ? "127.0.0.1:" + root.backend.port + (root.backend.ownsDaemon ? "  · started by OMA" : "  · already running")
            : "not connected"
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        text: "Data directory"
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
      Text {
        text: root.backend ? root.backend.dataDir : ""
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        text: "Indexed"
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
      Text {
        text: root.backend && root.backend.totalChunks >= 0
            ? root.backend.documents.length + " documents, " + root.backend.totalChunks + " chunks"
            : "—"
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
    }
  }

  Text {
    anchors.centerIn: parent
    width: parent.width * 0.7
    horizontalAlignment: Text.AlignHCenter
    wrapMode: Text.WordWrap
    text: "Models, warm-up, retrieval and appearance move here next."
    color: root.muted
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
  }
}
