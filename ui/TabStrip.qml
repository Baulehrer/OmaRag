import QtQuick
import qs.Commons
import qs.Ui

// The three sections, spread across the width of the header.
//
// Not `Ui/ButtonGroup`: that lays out bordered chips in a Row sized to their
// text, which neither fills the header nor matches the quiet uppercase-and-
// underline the rest of OMA uses. The behaviour it does give — one of N,
// keyboard walkable — is small enough to carry here.
Item {
  id: root

  property var tabs: []          // [{ id, label }]
  property string current: ""
  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent

  signal selected(string id)

  function step(delta) {
    if (!root.tabs.length) return
    var i = root.indexOf(root.current)
    var n = (i + delta + root.tabs.length) % root.tabs.length
    root.selected(root.tabs[n].id)
  }

  function indexOf(id) {
    for (var i = 0; i < root.tabs.length; i++) if (root.tabs[i].id === id) return i
    return 0
  }

  Row {
    anchors.fill: parent

    Repeater {
      model: root.tabs

      Item {
        width: root.width / Math.max(1, root.tabs.length)
        height: root.height
        property bool active: modelData.id === root.current

        Text {
          id: label
          anchors.centerIn: parent
          text: modelData.label
          color: parent.active ? root.accent : (hover.hovered ? root.foreground : root.muted)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          font.letterSpacing: 1.5

          Behavior on color { ColorAnimation { duration: 90 } }
        }

        // The underline sits under the word, not the whole cell — a full-width
        // rule would read as a divider rather than a marker.
        Rectangle {
          anchors { top: label.bottom; horizontalCenter: label.horizontalCenter }
          anchors.topMargin: Style.spacing.xs
          width: label.width
          height: Math.max(1, Style.space(1))
          color: root.accent
          opacity: parent.active ? 1 : 0
          Behavior on opacity { NumberAnimation { duration: 90 } }
        }

        HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
        TapHandler { onTapped: root.selected(modelData.id) }
      }
    }
  }
}
