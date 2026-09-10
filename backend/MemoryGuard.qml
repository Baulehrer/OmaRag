import QtQuick
import Quickshell
import Quickshell.Io

// Whether there is room to work, and when to give memory back.
//
// A published plugin cannot promise that nothing on the machine will ever run
// out of memory — it can promise not to be the cause, and to let go before the
// system has to start killing things. That is two jobs, and they need two
// different numbers: MemAvailable to decide whether a load fits, and the
// kernel's pressure signal to notice that something else needs the memory now.
// tools/memory-guard.py reads both; this only reacts to what it says.
//
// It runs while OMA holds models or has work in flight, and not otherwise: an
// OMA nobody has opened should cost nothing at all.
Item {
  id: root

  property bool active: false
  // Empty means the guard's own default: six percent of RAM, never under 2 GiB.
  property string reserveGib: ""

  // ok · tight · critical
  property string state: "ok"
  property real availableGib: 0
  property real totalGib: 0
  property real someTen: 0
  property real reserve: 0

  readonly property bool tight: root.state === "tight" || root.state === "critical"

  // Give up what is idle · stop what is running as well.
  signal easeOff()
  signal standDown()

  function _consume(line) {
    var next = ({})
    var fields = String(line).trim().split(/\s+/)
    for (var i = 0; i < fields.length; i++) {
      var pair = fields[i].split("=")
      if (pair.length === 2) next[pair[0]] = pair[1]
    }
    if (!next.state) return

    root.availableGib = parseFloat(next.available) || 0
    root.totalGib = parseFloat(next.total) || 0
    root.someTen = parseFloat(next.some10) || 0
    root.reserve = parseFloat(next.reserve) || 0

    var was = root.state
    root.state = String(next.state)
    if (root.state === was) return

    // Only on the way in. Coming back down is a relief, not an instruction.
    if (root.state === "critical") root.standDown()
    else if (root.state === "tight") root.easeOff()
  }

  Process {
    running: root.active
    command: {
      var cmd = ["python3",
        String(Qt.resolvedUrl("../tools/memory-guard.py")).replace(/^file:\/\//, "")]
      if (root.reserveGib.length) { cmd.push("--reserve-gib"); cmd.push(root.reserveGib) }
      return cmd
    }
    stdout: SplitParser { onRead: function(line) { root._consume(line) } }
  }

  // Back to a clean slate when it stops, so a stale "critical" cannot outlive
  // the situation that caused it and keep refusing work.
  onActiveChanged: if (!root.active) root.state = "ok"
}
