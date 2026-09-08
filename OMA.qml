import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "backend"

// OMA — local knowledge for Omarchy.
//
// Workpack 2 spike: prove the whole chain end to end. Open the overlay,
// reach lilbee, list what is indexed, run a query, show a hit with its
// source. Polish belongs to the next workpack.
Item {
  id: root

  property var shell: null
  property var manifest: null
  property bool opened: false

  // Shares the [menu] surface tokens, like the other overlays — themes that
  // style the menu style OMA too. No colour is defined here.
  readonly property color background: Color.menu.background
  readonly property color foreground: Color.menu.text
  readonly property color muted: Color.muted
  readonly property color accent: Color.accent
  readonly property color urgent: Color.urgent
  readonly property var borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))

  property var hits: []
  property string query: ""

  readonly property string phaseGlyph: {
    switch (backend.phase) {
      case "ready": return "●"     // ●
      case "starting": return "◐"  // ◐
      case "busy": return "◐"
      case "error": return "!"
      default: return "○"          // ○
    }
  }
  readonly property color phaseColor: backend.phase === "error" ? root.urgent
                                    : backend.phase === "ready" ? root.accent
                                    : root.muted
  readonly property string phaseLabel: {
    switch (backend.phase) {
      case "ready": return "Ready"
      case "starting": return backend.message || "Starting"
      case "busy": return backend.message || "Busy"
      case "error": return "Error"
      default: return "Sleeping"
    }
  }

  function open(payloadJson) {
    root.opened = true
    root.hits = []
    root.query = ""
    if (backend.phase === "idle" || backend.phase === "error") backend.connect()
    Qt.callLater(function() { input.forceActiveFocus() })
  }

  function close() { root.opened = false }

  function dismiss() {
    root.opened = false
    if (root.shell && typeof root.shell.hide === "function")
      root.shell.hide((root.manifest && root.manifest.id) || "kaufmann.omarag")
  }

  function toggle() { if (root.opened) root.dismiss(); else root.open("{}") }

  function runQuery() {
    var q = input.text.trim()
    if (!q) return
    root.query = q
    root.hits = []
    backend.search(q, 5)
  }

  function pageLabel(hit) {
    if (!hit || !hit.page_start) return ""
    return hit.page_start === hit.page_end ? "p. " + hit.page_start
                                           : "p. " + hit.page_start + "–" + hit.page_end
  }

  Lilbee {
    id: backend
    onSearchFinished: function(rows) { root.hits = rows }
  }

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-omarag"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle { anchors.fill: parent; color: Color.menu.scrim }
    MouseArea { anchors.fill: parent; onClicked: root.dismiss() }

    BorderSurface {
      id: card
      width: Math.min(Style.space(900), panel.width - Style.gapsOut * 4)
      height: Math.min(Style.space(620), panel.height - Style.gapsOut * 4)
      radius: Style.cornerRadius
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: Style.spacing.panelPadding

      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        anchors.fill: parent
        anchors.margins: Style.spacing.panelPadding
        focus: true

        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Escape) {
            if (input.text.length) { input.text = ""; root.hits = [] }
            else root.dismiss()
            event.accepted = true
          }
        }

        // ---------------------------------------------------------- header
        Item {
          id: header
          anchors { top: parent.top; left: parent.left; right: parent.right }
          height: Style.font.title + Style.spacing.md * 2

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "OMA"
            color: root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.title
            font.letterSpacing: 1.5
          }

          Row {
            anchors { right: parent.right; verticalCenter: parent.verticalCenter }
            spacing: Style.spacing.sm

            Text {
              text: root.phaseGlyph
              color: root.phaseColor
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              anchors.verticalCenter: parent.verticalCenter
            }
            Text {
              text: root.phaseLabel
              color: root.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              anchors.verticalCenter: parent.verticalCenter
            }
          }
        }

        // ---------------------------------------------------------- library
        Item {
          id: library
          anchors { top: header.bottom; bottom: parent.bottom; left: parent.left }
          anchors.topMargin: Style.spacing.panelGap
          width: Style.space(220)

          Text {
            id: libraryHeading
            text: "LIBRARY"
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.letterSpacing: 1.5
          }

          ListView {
            id: docList
            anchors { top: libraryHeading.bottom; left: parent.left; right: parent.right }
            anchors.topMargin: Style.spacing.md
            height: Math.min(contentHeight, parent.height - libraryHeading.height - Style.space(60))
            clip: true
            model: backend.documents
            delegate: Text {
              width: docList.width - Style.spacing.md
              text: (modelData.source || modelData.title || "?")
              color: root.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              elide: Text.ElideMiddle
              height: Style.spacing.popupRowHeight
              verticalAlignment: Text.AlignVCenter
            }
          }

          Text {
            anchors { top: libraryHeading.bottom; left: parent.left; right: parent.right }
            anchors.topMargin: Style.spacing.md
            visible: backend.phase === "ready" && !backend.documents.length
            text: "Nothing indexed yet"
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          Text {
            anchors { bottom: parent.bottom; left: parent.left }
            text: backend.totalChunks < 0 ? "" : backend.totalChunks + " chunks"
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
        }

        // ---------------------------------------------------------- ask
        Item {
          anchors {
            top: header.bottom; bottom: parent.bottom
            left: library.right; right: parent.right
          }
          anchors.topMargin: Style.spacing.panelGap
          anchors.leftMargin: Style.spacing.panelPadding

          Rectangle {
            id: inputBox
            anchors { top: parent.top; left: parent.left; right: parent.right }
            height: Style.spacing.controlHeight + Style.spacing.md
            color: Style.controlFill(input.activeFocus, false, Color.menu.text, root.accent)
            border.color: Style.controlBorder(input.activeFocus, false, Color.menu.text, root.accent)
            border.width: Style.controlBorderWidth(input.activeFocus, false)
            radius: Style.cornerRadius

            TextInput {
              id: input
              anchors.fill: parent
              anchors.leftMargin: Style.spacing.rowPaddingX
              anchors.rightMargin: Style.spacing.rowPaddingX
              verticalAlignment: TextInput.AlignVCenter
              color: root.foreground
              font.family: Style.font.family
              font.pixelSize: Style.font.subtitle
              selectByMouse: true
              onAccepted: root.runQuery()

              Text {
                anchors.fill: parent
                verticalAlignment: Text.AlignVCenter
                visible: !input.text.length
                text: "Search your knowledge…"
                color: root.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.subtitle
              }
            }
          }

          Text {
            id: resultHeading
            anchors { top: inputBox.bottom; left: parent.left }
            anchors.topMargin: Style.spacing.panelGap
            text: backend.phase === "busy" ? "SEARCHING…"
                : root.hits.length ? "SOURCES"
                : root.query ? "NO MATCHES"
                : ""
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.letterSpacing: 1.5
          }

          ListView {
            anchors {
              top: resultHeading.bottom; bottom: parent.bottom
              left: parent.left; right: parent.right
            }
            anchors.topMargin: Style.spacing.md
            clip: true
            spacing: Style.spacing.rowGap
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
                  text: modelData.title || modelData.source || "?"
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

          // ------------------------------------------------------- states
          Text {
            anchors.centerIn: parent
            visible: backend.phase === "error"
            width: parent.width - Style.spacing.panelPadding * 2
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            text: backend.message + "\n\n" + backend.detail
            color: root.urgent
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
        }
      }
    }
  }
}
