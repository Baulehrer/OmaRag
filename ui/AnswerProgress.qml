import QtQuick
import qs.Commons
import qs.Ui
import "../common"

// What the answer is doing, and — once there is something to base it on — how
// far along it is.
//
// The stage comes from what `lilbee ask` prints on its way: the engine
// announces its own loading, then goes quiet while retrieval runs, then the
// first words arrive. Three states, each observed rather than guessed.
//
// The bar only appears from the second answer onwards, because before that
// there is no measured duration and a moving bar would be theatre. When the
// estimate is overrun it stops at the end and says so instead of pretending.
Item {
  id: root

  // loading · retrieving · writing
  property string stage: ""
  property int elapsed: 0
  property int expected: 0
  property string model: ""

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent

  readonly property bool known: root.expected > 0
  readonly property real fraction: root.known
    ? Math.min(1.0, root.elapsed / root.expected) : 0
  readonly property bool overrun: root.known && root.elapsed > root.expected

  readonly property string label: {
    if (root.stage === "loading")    return "Loading the model"
    if (root.stage === "retrieving") return "Finding passages"
    if (root.stage === "writing")    return "Writing the answer"
    return "Working"
  }

  function clock(s) {
    if (s < 60) return s + "s"
    return Math.floor(s / 60) + ":" + (s % 60 < 10 ? "0" : "") + (s % 60)
  }

  implicitHeight: column.implicitHeight

  Column {
    id: column
    anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter }
    spacing: Style.spacing.sm

    // Three dots, one per stage, filled as each is reached. The same shape the
    // indexing view uses, so the two read alike.
    Row {
      anchors.horizontalCenter: parent.horizontalCenter
      spacing: Style.spacing.md

      Repeater {
        model: [
          { key: "loading",    text: "Model" },
          { key: "retrieving", text: "Passages" },
          { key: "writing",    text: "Answer" }
        ]

        Row {
          spacing: Style.spacing.xs
          readonly property int at: ["loading", "retrieving", "writing"].indexOf(root.stage)
          readonly property int mine: ["loading", "retrieving", "writing"].indexOf(modelData.key)

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: parent.mine < parent.at ? "●" : (parent.mine === parent.at ? "◐" : "○")
            color: parent.mine <= parent.at ? root.accent : root.muted
            font.family: OmaFont.face
            font.pixelSize: OmaFont.caption
          }
          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: modelData.text
            color: parent.mine === parent.at ? root.foreground : root.muted
            font.family: OmaFont.face
            font.pixelSize: OmaFont.caption
          }
        }
      }
    }

    // The bar, only where a measurement backs it.
    Item {
      anchors.horizontalCenter: parent.horizontalCenter
      visible: root.known
      width: Math.min(Style.space(320), root.width * 0.7)
      height: Style.space(3)

      Rectangle {
        anchors.fill: parent
        radius: height / 2
        color: root.muted
        opacity: 0.25
      }
      Rectangle {
        anchors { left: parent.left; top: parent.top; bottom: parent.bottom }
        width: parent.width * root.fraction
        radius: height / 2
        color: root.accent
        Behavior on width { NumberAnimation { duration: 400; easing.type: Easing.OutCubic } }
      }
    }

    Text {
      anchors.horizontalCenter: parent.horizontalCenter
      text: {
        if (!root.known) return root.clock(root.elapsed) + " elapsed"
        if (root.overrun) return root.clock(root.elapsed) + " — longer than the usual "
                               + root.clock(root.expected)
        return root.clock(root.elapsed) + " of about " + root.clock(root.expected)
      }
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.caption
    }

    Text {
      anchors.horizontalCenter: parent.horizontalCenter
      visible: !root.known
      text: "The first answer sets the pace; from the next one there is a bar."
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.caption
      opacity: 0.7
    }
  }
}
