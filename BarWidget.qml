import QtQuick
import Quickshell
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

  // Where the icon sits, so the compact window can open under it. Reported in
  // screen coordinates: the bar is a layer surface, so its window position plus
  // the button's offset inside it is where the icon really is.
  function reportAnchor() {
    if (!root.service || !root.bar) return
    var w = button.QsWindow ? button.QsWindow.window : null
    if (!w || !w.screen) return
    var p = button.mapToItem(null, button.width / 2, 0)
    var pos = String(root.bar.position || "top")

    // A layer surface reports no x/y, so the bar's own `position` says where it
    // sits — the same thing Ui/PopupCard reads. Only top and bottom bars are
    // treated as horizontal; a side bar anchors on its own edge.
    root.service.anchorX = (pos === "left") ? w.width
                         : (pos === "right") ? (w.screen.width - w.width)
                         : p.x
    root.service.anchorY = (pos === "bottom") ? (w.screen.height - w.height)
                         : (pos === "top") ? w.height
                         : p.y
    root.service.anchorAtTop = (pos !== "bottom")
  }

  onXChanged: reportAnchor()
  onWidthChanged: reportAnchor()

  // The bar's window, the button's place in it and the service all arrive at
  // their own pace, so this keeps trying briefly rather than reporting once
  // into a half-built bar.
  Timer {
    id: anchorSettle
    interval: 400
    repeat: true
    running: true
    property int tries: 0
    onTriggered: {
      root.reportAnchor()
      tries += 1
      if ((root.service && root.service.anchorX > 0) || tries > 20) running = false
    }
  }

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
