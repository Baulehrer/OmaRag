import QtQuick
import qs.Commons
import qs.Ui

// One titled group of lilbee settings.
//
// A component rather than a Repeater inside a Repeater: nesting them left the
// fields without usable heights and only the last one landed on screen. One
// level of repetition, laid out by a Column that can measure its children.
Column {
  id: root

  property string title: ""
  property var keys: []
  property var backend: null

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent
  property color urgent: Color.urgent

  spacing: Style.spacing.md

  Text {
    text: root.title.toUpperCase()
    color: root.muted
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    font.letterSpacing: 1.5
  }

  Repeater {
    model: root.keys

    SettingField {
      width: root.width
      // Read through the backend's settings array so the binding re-evaluates
      // when the list arrives — Setup is usually open before the backend is.
      meta: {
        if (!root.backend) return null
        var all = root.backend.settings
        for (var i = 0; i < all.length; i++) if (all[i].key === modelData) return all[i]
        return null
      }
      visible: meta !== null
      height: meta !== null ? implicitHeight : 0
      foreground: root.foreground
      muted: root.muted
      accent: root.accent
      urgent: root.urgent
      onChanged: function(k, v) { if (root.backend) root.backend.writeSetting(k, v) }
      onResetRequested: function(k) { if (root.backend) root.backend.resetSetting(k) }
    }
  }
}
