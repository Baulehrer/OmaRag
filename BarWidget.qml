import QtQuick
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
  }

  // A third-party plugin gets a scoped shell facade, not the shell itself: it
  // may summon, hide, toggle and query only its own id. `openPanelIds` is not
  // on it — `isPluginOpen` is.
  readonly property bool opened: {
    var s = root.bar ? root.bar.shell : null
    return s && typeof s.isPluginOpen === "function" ? s.isPluginOpen(root.moduleName) === true : false
  }
}
