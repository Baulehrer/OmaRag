import QtQuick
import qs.Commons
import qs.Ui

// A book in the bar: click it and OMA opens.
//
// A plugin that is both bar-widget and overlay stays owned by the overlay
// loader (shell.qml isBarWidgetPanelPlugin explicitly steps aside for panel,
// overlay and menu kinds), so this button only triggers the overlay — it never
// takes over the summon routing. omarchy.menu works the same way.
BarWidget {
  id: root
  moduleName: "kaufmann.omarag"

  // The service holds the backend, so the icon can show what is going on even
  // when the window is closed — which is the whole point of it surviving.
  readonly property var service: {
    var s = root.bar ? root.bar.shell : null
    return s && typeof s.serviceFor === "function" ? s.serviceFor(root.moduleName) : null
  }
  readonly property var backend: root.service ? root.service.backend : null

  readonly property bool working: root.backend
    && (root.backend.phase === "answering" || root.backend.phase === "indexing"
     || root.backend.phase === "searching" || root.backend.phase === "starting")
  readonly property bool warm: root.backend && root.backend.engineWarm
  readonly property bool unseen: root.service && root.service.unseenAnswer

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // Nerd Font book. The bar's font resolves to a Nerd Font, so this renders
    // wherever the rest of the bar's icons do.
    text: ""
    tooltipText: "Local knowledge"
    // Deliberately not `active`: that paints the button in the urgent colour,
    // which means "something needs you", not "this is open". An overlay that
    // covers the screen does not need the bar to say so as well.

    onPressed: function(b) {
      if (!root.bar || !root.bar.shell) return
      // Straight to the shell rather than out through a process: the overlay
      // is a sibling in the same plugin, and the facade allows a plugin to
      // toggle itself.
      root.bar.shell.toggle(root.moduleName, "{}")
    }
    // Breathing while there is something to breathe about, and still otherwise.
    // Opacity rather than colour: the bar's own palette stays untouched.
    SequentialAnimation on opacity {
      running: root.working
      loops: Animation.Infinite
      alwaysRunToEnd: true
      NumberAnimation { to: 0.55; duration: 800; easing.type: Easing.InOutSine }
      NumberAnimation { to: 1.0;  duration: 800; easing.type: Easing.InOutSine }
    }
  }

  // The animation leaves opacity wherever it stopped; put it back.
  Connections {
    target: root
    function onWorkingChanged() { if (!root.working) button.opacity = 1.0 }
  }

  // A dot in the corner, in the theme's own colours: the accent when an answer
  // is waiting to be read, the muted foreground when models are merely loaded,
  // nothing at all when OMA holds neither. No third colour is invented.
  Rectangle {
    visible: root.unseen || root.warm
    width: Style.space(5)
    height: width
    radius: width / 2
    color: root.unseen ? Color.accent : Color.muted
    anchors { right: parent.right; top: parent.top; margins: Style.space(2) }

    SequentialAnimation on opacity {
      running: root.unseen
      loops: Animation.Infinite
      alwaysRunToEnd: true
      NumberAnimation { to: 0.35; duration: 900; easing.type: Easing.InOutSine }
      NumberAnimation { to: 1.0;  duration: 900; easing.type: Easing.InOutSine }
    }
    onVisibleChanged: if (!visible) opacity = 1.0
  }

  // A third-party plugin gets a scoped shell facade, not the shell itself: it
  // may summon, hide, toggle and query only its own id. `openPanelIds` is not
  // on it — `isPluginOpen` is.
  readonly property bool opened: {
    var s = root.bar ? root.bar.shell : null
    return s && typeof s.isPluginOpen === "function" ? s.isPluginOpen(root.moduleName) === true : false
  }
}
