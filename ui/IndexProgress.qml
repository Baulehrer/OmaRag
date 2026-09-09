import QtQuick
import qs.Commons
import qs.Ui
import "../common"

// Indexing, with the honest amount of detail.
//
// lilbee reports no progress over MCP, so what is shown here is read out of its
// log: the extraction line names the total chunk count, and each embedding call
// after it moves the count on. Extraction itself has no intermediate signal —
// there only the clock runs. Nothing is drawn that was not measured.
Item {
  id: root

  property string what: ""
  property string stage: ""      // extracting · embedding · finishing
  property int done: -1
  property int total: -1
  property int calls: 0
  property int waited: 0

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent

  readonly property bool hasCount: root.total > 0 && root.done >= 0
  readonly property real fraction: root.hasCount ? Math.min(1, root.done / root.total) : 0

  function elapsed(s) {
    var m = Math.floor(s / 60)
    var r = s % 60
    return m > 0 ? m + ":" + (r < 10 ? "0" : "") + r : s + "s"
  }

  Column {
    anchors.centerIn: parent
    width: Math.min(parent.width, Style.space(520))
    spacing: Style.spacing.xl

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: root.what
      color: root.foreground
      font.family: OmaFont.face
      font.pixelSize: OmaFont.subtitle
      elide: Text.ElideMiddle
    }

    // The stage chain. The active one pulses; the rest are plain.
    Row {
      anchors.horizontalCenter: parent.horizontalCenter
      spacing: Style.spacing.md

      Repeater {
        model: [
          { id: "extracting", label: "Extracting" },
          { id: "embedding",  label: "Embedding" },
          { id: "finishing",  label: "Indexed" }
        ]

        Row {
          id: stageRow
          spacing: Style.spacing.md
          property bool active: modelData.id === root.stage
          property bool passed: {
            var order = ["extracting", "embedding", "finishing"]
            return order.indexOf(modelData.id) < order.indexOf(root.stage)
          }

          Text {
            text: (stageRow.active ? "◐" : (stageRow.passed ? "●" : "○")) + "  " + modelData.label
            color: stageRow.active ? root.accent : (stageRow.passed ? root.foreground : root.muted)
            font.family: OmaFont.face
            font.pixelSize: OmaFont.bodySmall

            SequentialAnimation on opacity {
              running: stageRow.active
              loops: Animation.Infinite
              NumberAnimation { to: 0.5; duration: 800; easing.type: Easing.InOutSine }
              NumberAnimation { to: 1.0; duration: 800; easing.type: Easing.InOutSine }
            }
          }

          Text {
            visible: index < 2
            text: "—"
            color: root.muted
            font.family: OmaFont.face
            font.pixelSize: OmaFont.bodySmall
          }
        }
      }
    }

    // A bar only once there is something real behind it.
    Rectangle {
      visible: root.hasCount
      width: parent.width
      height: Math.max(2, Style.space(3))
      color: Style.normalFill
      radius: Style.cornerRadius

      Rectangle {
        width: parent.width * root.fraction
        height: parent.height
        color: root.accent
        radius: Style.cornerRadius
        Behavior on width { NumberAnimation { duration: 300; easing.type: Easing.OutQuad } }
      }
    }

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      // "approx." is not modesty: the count comes from embedding calls, and a
      // call carries about two chunks. The number is derived, not reported.
      // Three levels of knowledge, three honest sentences: a share of a known
      // total, a bare count when only PDFs announce their total, or just the
      // clock while extraction runs.
      text: root.hasCount
          ? "approx. " + root.done + " of " + root.total + " chunks  ·  " + root.elapsed(root.waited)
          : (root.calls > 0
             ? "approx. " + Math.round(root.calls * 2) + " chunks so far  ·  " + root.elapsed(root.waited)
             : root.elapsed(root.waited) + " elapsed")
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.bodySmall
    }

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      text: "Search and answering wait until this finishes."
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.caption
    }
  }
}
