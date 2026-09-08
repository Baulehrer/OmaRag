import QtQuick
import qs.Commons
import qs.Ui

// The list of hits. Sources are the product here — the answer is only as good
// as the passage behind it — so this is the part meant to grow: expanding a
// row, the match detail, keyboard navigation.
Item {
  id: root

  property var hits: []
  property color foreground: Color.menu.text
  property color muted: Color.muted

  // Page numbers exist only for PDFs; anything else reports 0 and gets no
  // label rather than a claimed "page 0".
  function pageLabel(hit) {
    if (!hit || !hit.page_start) return ""
    return hit.page_start === hit.page_end ? "p. " + hit.page_start
                                           : "p. " + hit.page_start + "–" + hit.page_end
  }

  Text {
    id: heading
    text: root.hits.length ? "SOURCES" : ""
    color: root.muted
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    font.letterSpacing: 1.5
  }

  ListView {
    anchors {
      top: heading.bottom; bottom: parent.bottom
      left: parent.left; right: parent.right
    }
    anchors.topMargin: Style.spacing.panelGap
    clip: true
    spacing: Style.spacing.rowGap
    // Without this the list can rest on an overscrolled position and clip its
    // own first row.
    boundsBehavior: Flickable.StopAtBounds
    model: root.hits

    delegate: Column {
      width: ListView.view.width
      spacing: Style.spacing.xs

      Row {
        spacing: Style.spacing.sm

        Text {
          text: (index + 1) + ""
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        Text {
          text: modelData.title || modelData.source || ""
          color: root.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.body
          width: Math.min(implicitWidth, Style.space(420))
          elide: Text.ElideRight
        }
        Text {
          text: root.pageLabel(modelData)
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
      }

      Text {
        width: parent.width
        text: String(modelData.chunk || "").replace(/\s+/g, " ").substring(0, 220)
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
        maximumLineCount: 2
        elide: Text.ElideRight
      }
    }
  }
}
