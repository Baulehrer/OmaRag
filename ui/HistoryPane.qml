import QtQuick
import qs.Commons
import qs.Ui
import "../common"

// The questions asked before, newest first.
Item {
  id: root

  // Never undefined: the service that owns the history may arrive a moment
  // after the view does.
  property var entries: []
  property int current: -1

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent

  signal picked(int index)
  signal removeRequested(int index)

  Text {
    id: heading
    text: "HISTORY"
    color: root.muted
    font.family: OmaFont.face
    font.pixelSize: OmaFont.caption
    font.letterSpacing: 1.5
  }

  Text {
    anchors { top: heading.bottom; left: parent.left; right: parent.right }
    anchors.topMargin: Style.spacing.md
    visible: !(root.entries && root.entries.length)
    text: "Questions you ask show up here."
    color: root.muted
    font.family: OmaFont.face
    font.pixelSize: OmaFont.caption
    wrapMode: Text.WordWrap
  }

  ListView {
    id: list
    anchors {
      top: heading.bottom; bottom: parent.bottom
      left: parent.left; right: parent.right
    }
    anchors.topMargin: Style.spacing.md
    anchors.rightMargin: Style.spacing.md
    clip: true
    spacing: Style.spacing.xs
    boundsBehavior: Flickable.StopAtBounds
    model: root.entries

    delegate: Item {
      width: list.width
      height: label.implicitHeight + Style.spacing.sm * 2
      property bool active: index === root.current

      Rectangle {
        anchors.fill: parent
        color: parent.active ? Style.selectedFill : (hover.hovered ? Style.hoverFill : "transparent")
        radius: Style.cornerRadius
      }

      Text {
        id: label
        anchors {
          left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter
          leftMargin: Style.spacing.sm; rightMargin: Style.spacing.sm
        }
        text: modelData.question
        color: (parent.active || hover.hovered) ? root.foreground : root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.bodySmall
        wrapMode: Text.WordWrap
        maximumLineCount: 2
        elide: Text.ElideRight
      }

      HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
      TapHandler { onTapped: root.picked(index) }
    }
  }
}
