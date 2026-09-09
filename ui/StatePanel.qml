import QtQuick
import qs.Commons
import qs.Ui
import "../common"

// Everything the middle of the window says when there are no results to show:
// waiting, occupied, empty, broken. Kept together because the rules that decide
// between them belong side by side — busy in particular must never look like a
// failure, and waiting must never claim progress it cannot know.
Item {
  id: root

  // waiting · indexing · blocked · empty · error · none
  property string mode: "none"
  property string what: ""
  property string headline: ""
  property string detail: ""
  property int waited: 0
  property bool hasLibrary: false
  property bool detailsOpen: false

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color urgent: Color.urgent

  signal retryRequested()
  signal detailsToggled()

  // ------------------------------------------------------------- waiting
  Column {
    anchors.centerIn: parent
    width: parent.width - Style.spacing.panelPadding * 2
    spacing: Style.spacing.md
    visible: root.mode === "waiting"

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: "◐  " + root.headline + (root.waited > 2 ? "   " + root.waited + "s" : "")
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.subtitle
    }
    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      visible: root.waited > 20
      text: "The embedding model is loading. First use after a rest takes a while."
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.bodySmall
    }
  }

  // ------------------------------------------------------------- indexing
  // No percentage: lilbee reports no progress for `add`, so the only honest
  // numbers are what is being indexed and how long it has been going.
  Column {
    anchors.centerIn: parent
    width: parent.width - Style.spacing.panelPadding * 2
    spacing: Style.spacing.md
    visible: root.mode === "indexing"

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: "◐  Indexing" + (root.waited > 2 ? "   " + root.waited + "s" : "")
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.subtitle
    }
    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      text: root.what
      color: root.foreground
      font.family: OmaFont.face
      font.pixelSize: OmaFont.bodySmall
    }
    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      text: "Extracting, chunking and embedding. A book takes minutes.\n"
          + "Search and answering wait until this finishes."
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.caption
    }
  }

  // ------------------------------------------------------------- blocked
  // The single-embedder constraint, said out loud. Muted, not red: nothing is
  // broken, the backend is simply occupied.
  Column {
    anchors.centerIn: parent
    width: parent.width - Style.spacing.panelPadding * 2
    spacing: Style.spacing.md
    visible: root.mode === "blocked"

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: "◐  Indexing in progress"
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.subtitle
    }
    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      text: "lilbee has a single embedder, and an index run is holding it.\n"
          + "Search comes back on its own when the run finishes."
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.bodySmall
    }
  }

  // ------------------------------------------------------------- empty
  Column {
    anchors.centerIn: parent
    width: parent.width - Style.spacing.panelPadding * 2
    spacing: Style.spacing.md
    visible: root.mode === "empty"

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      text: "Nothing matched"
      color: root.foreground
      font.family: OmaFont.face
      font.pixelSize: OmaFont.subtitle
    }
    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      text: root.hasLibrary
          ? "Try a more specific noun phrase — retrieval works better with the words the document itself would use."
          : "Nothing is indexed yet, so there is nothing to match."
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.bodySmall
    }
  }

  // ------------------------------------------------------------- error
  // No stack traces in the ordinary view. A sentence, one action, and the
  // technical text folded away for whoever needs it.
  Column {
    anchors.centerIn: parent
    width: parent.width - Style.spacing.panelPadding * 2
    spacing: Style.spacing.lg
    visible: root.mode === "error"

    Text {
      width: parent.width
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WordWrap
      text: root.headline
      color: root.urgent
      font.family: OmaFont.face
      font.pixelSize: OmaFont.subtitle
    }

    Button {
      anchors.horizontalCenter: parent.horizontalCenter
      text: "Retry"
      bordered: true
      focusable: true
      onClicked: {
        root.detailsOpen = false
        root.retryRequested()
      }
    }

    Text {
      anchors.horizontalCenter: parent.horizontalCenter
      text: (root.detailsOpen ? "▾" : "▸") + "  Technical details"
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.caption
      MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: root.detailsToggled()
      }
    }

    Text {
      width: parent.width
      visible: root.detailsOpen && root.detail.length > 0
      horizontalAlignment: Text.AlignHCenter
      wrapMode: Text.WrapAnywhere
      text: root.detail
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.caption
    }
  }
}
