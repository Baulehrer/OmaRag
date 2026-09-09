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
  property color accent: Color.accent

  // Only one open at a time: the point is to read a passage, not to build a
  // wall of them.
  property int expanded: -1

  // Page numbers exist only for PDFs; anything else reports 0 and gets no
  // label rather than a claimed "page 0".
  function pageLabel(hit) {
    if (!hit || !hit.page_start) return ""
    return hit.page_start === hit.page_end ? "p. " + hit.page_start
                                           : "p. " + hit.page_start + "–" + hit.page_end
  }

  onHitsChanged: root.expanded = -1

  Text {
    id: heading
    text: root.hits.length ? "SOURCES — click to read the passage" : ""
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

    // Two lines of a passage are enough to recognise a hit and never enough to
    // check one. Expanding shows exactly the text that was retrieved — not a
    // tidied version of it — because that is the whole point of provenance.
    delegate: Column {
      id: hitRow
      width: ListView.view.width
      spacing: Style.spacing.xs
      property bool open: index === root.expanded

      Row {
        spacing: Style.spacing.sm

        Text {
          text: hitRow.open ? "▾" : "▸"
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        Text {
          text: (index + 1) + ""
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        Text {
          text: modelData.title || modelData.source || ""
          color: hover.hovered ? root.accent : root.foreground
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

        HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
        TapHandler { onTapped: root.expanded = hitRow.open ? -1 : index }
      }

      Text {
        width: parent.width
        visible: !hitRow.open
        text: String(modelData.chunk || "").replace(/\s+/g, " ").substring(0, 220)
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
        maximumLineCount: 2
        elide: Text.ElideRight
      }

      Column {
        width: parent.width
        visible: hitRow.open
        spacing: Style.spacing.sm

        Text {
          width: parent.width
          text: String(modelData.chunk || "")
          color: root.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WordWrap
          lineHeight: 1.3
        }

        // The numbers, for whoever wants them. Never as a percentage: `score`
        // is normalised so the best hit is always 1.0, and a percentage would
        // claim a confidence that is only a rank.
        Text {
          width: parent.width
          text: "vector " + Number(modelData.distance || 0).toFixed(3)
              + "  ·  lexical " + Number(modelData.bm25_score || 0).toFixed(1)
              + "  ·  rank " + (index + 1) + " of " + root.hits.length
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
