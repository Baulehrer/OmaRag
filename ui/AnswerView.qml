import QtQuick
import Quickshell
import qs.Commons
import qs.Ui
import "Formula.js" as Formula

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
  property int current: -1

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent

  signal cancelRequested()
  signal openRequested(string url, string pages)
  signal copied(int characters)

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

      // A TextEdit rather than a Text, because an answer you cannot select is
      // an answer you cannot quote. Read-only, so it behaves like text that
      // happens to be selectable.
      TextEdit {
        id: body
        width: parent.width
        visible: root.answer.length > 0
        // StyledText, not MarkdownText: subscripts matter more here than
        // Markdown's own feature set, and Formula.js carries the light Markdown
        // the model writes across into the same markup.
        textFormat: TextEdit.RichText
        text: Formula.toStyled(root.answer)
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        wrapMode: TextEdit.WordWrap
        readOnly: true
        selectByMouse: true
        selectionColor: Style.selectionFill

        // Copy on release rather than on every selection change: dragging a
        // selection would otherwise write to the clipboard on every pixel.
        onSelectedTextChanged: copyTimer.restart()
        Timer {
          id: copyTimer
          interval: 400
          onTriggered: {
            var sel = body.selectedText
            if (!sel || !sel.length) return
            Quickshell.execDetached(["bash", "-c",
                                     "printf %s " + Util.shellQuote(sel) + " | wl-copy"])
            root.copied(sel.length)
          }
        }
      }

      Text {
        visible: root.sources.length > 0
        text: "PASSAGES USED — click to open the page"
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        font.letterSpacing: 1.5
      }

      // The model reads the passages correctly and can still interpret them
      // wrongly, so getting to the actual page in one click is the safety net,
      // not a convenience. For a textbook the page beats the chunk anyway:
      // figures, tables and formulae survive there, in OCR text they do not.
      Repeater {
        model: root.sources

        Row {
          id: sourceRow
          spacing: Style.spacing.sm
          property bool hot: hover.hovered || index === root.current

          Text {
            text: modelData.index + ""
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
          Text {
            text: modelData.title || ""
            color: sourceRow.hot ? root.accent : root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            width: Math.min(implicitWidth, Style.space(420))
            elide: Text.ElideMiddle
          }
          Text {
            text: modelData.pages ? "p. " + modelData.pages : ""
            color: sourceRow.hot ? root.accent : root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }

          HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
          TapHandler {
            onTapped: root.openRequested(modelData.url || "", modelData.pages || "")
          }
        }
      }
    }
  }
}
