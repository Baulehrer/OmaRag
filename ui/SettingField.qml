import QtQuick
import qs.Commons
import qs.Ui
import "../common"

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

  // Optional: a value we measured to work better than lilbee's default, with
  // the reason. Shown under lilbee's own help, never applied on its own.
  property var recommended: undefined
  property string why: ""
  readonly property bool hasHint: root.recommended !== undefined
  readonly property bool atRecommended: root.hasHint && root.meta
      && String(root.meta.value) === String(root.recommended)

  readonly property string key: meta ? String(meta.key) : ""
  // Anything lilbee describes in a shape this view does not model is shown
  // read-only rather than guessed at. A future lilbee that turns a number into
  // an object would otherwise get "[object Object]" typed back at it — the
  // field falls back to a text box, and a text box writes text.
  readonly property bool unsupported: {
    if (!meta) return false
    var v = meta.value
    if (v !== null && typeof v === "object") return true
    var t = String(meta.type).replace("|null", "")
    return ["bool", "int", "float", "str", "string"].indexOf(t) === -1
        && !(meta.choices && meta.choices.length)
  }

  readonly property string kind: {
    if (!meta) return "str"
    if (root.unsupported) return "unsupported"
    var t = String(meta.type).replace("|null", "")
    if (meta.choices && meta.choices.length) return "enum"
    if (t === "bool") return "bool"
    if (t === "int" || t === "float") return "number"
    return "str"
  }
  readonly property bool modified: meta && JSON.stringify(meta.value) !== JSON.stringify(meta.default)

  implicitHeight: row.implicitHeight + help.implicitHeight
                  + (hint.visible ? hint.implicitHeight + Style.spacing.xxs : 0)
                  + Style.spacing.sm

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
        font.family: OmaFont.face
        font.pixelSize: OmaFont.bodySmall
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

      Text {
        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter }
        visible: root.kind === "unsupported"
        text: root.meta ? JSON.stringify(root.meta.value) : ""
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.bodySmall
        elide: Text.ElideRight
      }
    }

    // Only offered where it means something — a value already at its default
    // has nothing to go back to.
    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: root.modified && root.kind !== "unsupported"
      text: "reset"
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.caption
      HoverHandler { cursorShape: Qt.PointingHandCursor }
      TapHandler { onTapped: root.resetRequested(root.key) }
    }

    Text {
      anchors.verticalCenter: parent.verticalCenter
      visible: root.meta && root.meta.reindex_required === true
      text: "needs reindex"
      color: root.urgent
      font.family: OmaFont.face
      font.pixelSize: OmaFont.caption
    }
  }

  Text {
    id: help
    anchors { top: row.bottom; left: parent.left; right: parent.right }
    anchors.leftMargin: Style.space(210) + Style.spacing.md
    anchors.topMargin: Style.spacing.xxs
    text: {
      if (!root.meta) return ""
      var h = String(root.meta.help || "")
      if (root.kind === "unsupported")
        return (h.length ? h + " — " : "")
             + "Diese Einstellung hat eine Form, die OMA nicht bearbeiten kann. "
             + "Über lilbees eigene Oberfläche änderbar."
      return h
    }
    color: root.muted
    font.family: OmaFont.face
    font.pixelSize: OmaFont.caption
    wrapMode: Text.WordWrap
  }

  Text {
    id: hint
    anchors { top: help.bottom; left: parent.left; right: parent.right }
    anchors.leftMargin: Style.space(210) + Style.spacing.md
    anchors.topMargin: Style.spacing.xxs
    visible: root.hasHint
    // Green-lit once the value matches; otherwise it reads as a suggestion.
    text: (root.atRecommended ? "✓ empfohlen: " : "empfohlen: ")
          + String(root.recommended) + (root.why.length ? " — " + root.why : "")
    color: root.atRecommended ? root.muted : root.accent
    font.family: OmaFont.face
    font.pixelSize: OmaFont.caption
    wrapMode: Text.WordWrap
  }
}
