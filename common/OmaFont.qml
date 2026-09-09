pragma Singleton

import QtQuick
import Quickshell
import qs.Commons

// OMA's own type scale.
//
// `Style.font.*` is shell-wide: changing it would resize the bar, the
// notifications and the lock screen. OMA is read at arm's length from a
// textbook, so it gets its own factor on top of the shell's tokens and leaves
// the shell alone. Set from OMA.qml out of the plugin settings.
//
// A singleton rather than a property passed down: seventy-five call sites in
// ten files, and every one of them would otherwise need plumbing.
Singleton {
  id: root

  // 1.0 = exactly the shell's sizes.
  property real scale: 1.0
  // Empty = the shell's font.
  property string family: ""

  readonly property string face: root.family.length ? root.family : Style.font.family

  function px(base) { return Math.max(7, Math.round(base * root.scale)) }

  readonly property int caption:   root.px(Style.font.caption)
  readonly property int bodySmall: root.px(Style.font.bodySmall)
  readonly property int body:      root.px(Style.font.body)
  readonly property int subtitle:  root.px(Style.font.subtitle)
  readonly property int title:     root.px(Style.font.title)
  readonly property int heading:   root.px(Style.font.heading)
}
