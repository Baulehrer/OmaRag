import QtQuick
import qs.Commons
import qs.Ui

// One lilbee setting, drawn from what lilbee says about it.
//
// The backend hands over key, value, default, type, help, choices and whether a
// change invalidates the index — so nothing here is hardcoded per key, and the
// view stays right when lilbee gains or renames a setting.
Item {
  id: root

  property var meta: null        // one entry from settings_list
  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent
  property color urgent: Color.urgent

  signal changed(string key, var value)
  signal resetRequested(string key)

  readonly property string key: meta ? String(meta.key) : ""
  readonly property string kind: {
    if (!meta) return "str"
    var t = String(meta.type).replace("|null", "")
    if (meta.choices && meta.choices.length) return "enum"
    if (t === "bool") return "bool"
    if (t === "int" || t === "float") return "number"
    return "str"
  }
  readonly property bool modified: meta && JSON.stringify(meta.value) !== JSON.stringify(meta.default)

  implicitHeight: row.implicitHeight + help.implicitHeight + Style.spacing.sm

  Row {
    id: row
    anchors { top: parent.top; left: parent.left; right: parent.right }
    spacing: Style.spacing.md

    Item {
      width: Style.space(210)
      height: Math.max(control.implicitHeight, label.implicitHeight)

      Text {
        id: label
        anchors { left: parent.left; verticalCenter: parent.verticalCenter }
        text: root.key
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
    }

    // -------------------------------------------------------------- control
    Item {
      id: control
      width: Style.space(240)
      implicitHeight: Style.spacing.controlHeight

      // bool
      Toggle {
        anchors.verticalCenter: parent.verticalCenter
        visible: root.kind === "bool"
        checked: root.meta && root.meta.value === true
        onClicked: root.changed(root.key, !checked)
      }

      // enum
      Dropdown {
        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter }
        visible: root.kind === "enum"
        options: root.meta && root.meta.choices ? root.meta.choices : []
        value: root.meta ? String(root.meta.value) : ""
        onChanged: function(v) { root.changed(root.key, v) }
      }

      // number and free text share a field; the difference is what is sent.
      TextField {
        id: field
        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter }
        visible: root.kind === "number" || root.kind === "str"
        text: root.meta ? String(root.meta.value === null ? "" : root.meta.value) : ""

        onAccepted: {
          if (root.kind === "number") {
            var n = Number(text)
            // A field that will not parse is left alone rather than writing
            // something the backend has to reject.
            if (isNaN(n)) { text = String(root.meta.value); return }
            root.changed(root.key, String(root.meta.type).indexOf("float") === 0 ? n : Math.round(n))
          } else {
            root.changed(root.key, text)
          }
        }
      }
    }

    // Only offered where it means something — a value already at its default
    // has nothing to go back to.
    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: root.modified
      text: "reset"
      color: root.muted
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
      HoverHandler { cursorShape: Qt.PointingHandCursor }
      TapHandler { onTapped: root.resetRequested(root.key) }
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: root.meta && root.meta.reindex_required === true
      text: "needs reindex"
      color: root.urgent
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
  }

  Text {
    id: help
    anchors { top: row.bottom; left: parent.left; right: parent.right }
    anchors.leftMargin: Style.space(210) + Style.spacing.md
    anchors.topMargin: Style.spacing.xxs
    text: root.meta ? String(root.meta.help || "") : ""
    color: root.muted
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    wrapMode: Text.WordWrap
  }
}
