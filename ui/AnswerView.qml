import QtQuick
import qs.Commons
import qs.Ui

// The answer, and the passages it was allowed to use.
//
// lilbee's system prompt binds the model to the retrieved passages and tells it
// to say so when they do not cover the question — verified, and even a small
// model obeys. So a refusal is a legitimate answer here, not a failure, and it
// gets the same treatment as any other.
//
// Note that a refusal still comes with sources: those are the passages that
// were looked at, not evidence for a claim. The heading says so.
Item {
  id: root

  property string answer: ""
  property var sources: []
  property bool answering: false
  property int waited: 0

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent

  signal cancelRequested()

  Flickable {
    anchors.fill: parent
    contentWidth: width
    contentHeight: column.height
    clip: true
    boundsBehavior: Flickable.StopAtBounds

    Column {
      id: column
      width: parent.width
      spacing: Style.spacing.panelGap

      // Two phases, because that is what the backend actually does: a long
      // silence while it retrieves and loads, then text. Elapsed seconds are
      // the only honest number for the first part.
      Row {
        spacing: Style.spacing.sm
        visible: root.answering && !root.answer.length

        Text {
          text: "◐  Thinking" + (root.waited > 2 ? "   " + root.waited + "s" : "")
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.subtitle
        }
        Text {
          text: "· Esc to cancel"
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      Text {
        width: parent.width
        visible: root.answer.length > 0
        text: root.answer
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        wrapMode: Text.WordWrap
        textFormat: Text.PlainText
        lineHeight: 1.35
      }

      Text {
        visible: root.sources.length > 0
        text: "PASSAGES USED"
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        font.letterSpacing: 1.5
      }

      Repeater {
        model: root.sources

        Row {
          spacing: Style.spacing.sm

          Text {
            text: modelData.index + ""
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            text: modelData.title || ""
            color: root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            width: Math.min(implicitWidth, Style.space(420))
            elide: Text.ElideMiddle
          }
          Text {
            text: modelData.pages ? "p. " + modelData.pages : ""
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
        }
      }
    }
  }
}
