import QtQuick
import Quickshell
import Quickshell.Io

// Talks to lilbee over MCP on streamable HTTP. The daemon has no REST
// surface — /mcp is the whole API (see REALITY.md, "lilbee serve ist ein
// MCP-Server"). Everything here is asynchronous: this runs inside the
// Omarchy shell process, where one blocking call freezes the desktop.
Item {
  id: root

  property string bin: "lilbee"
  property int port: 8787
  readonly property string endpoint: "http://127.0.0.1:" + port + "/mcp"

  // idle · starting · ready · busy · error
  //
  // busy is not an error. lilbee holds a single embedder worker, so an index
  // run — ours or one started from the terminal or lilbee's own TUI — takes
  // the only slot there is, and a query would simply hang. Saying so is the
  // difference between an app that looks broken and one that explains itself.
  property string phase: "idle"
  property string message: ""
  property string detail: ""

  // Keep the daemon alive after OMA closes. Off by default: a closed overlay
  // then costs nothing at all. On, roughly 378 MB stays resident with no
  // models loaded, and the next question starts without a backend boot.
  property bool persistDaemon: false

  // Cheap calls should fail fast; a cold search legitimately runs past a
  // minute while the embedding model loads.
  readonly property int quickTimeout: 15000
  readonly property int searchTimeout: 180000

  property string token: ""
  property string sessionId: ""
  property bool ownsDaemon: false

  property int totalChunks: -1
  property var documents: []

  signal statusUpdated()
  signal documentsUpdated()
  signal searchFinished(var hits)
  signal failed(string message, string detail)
  signal refused(string reason)

  property int _nextId: 0

  // ---------------------------------------------------------------- helpers

  function _fail(msg, det) {
    root.phase = "error"
    root.message = msg
    root.detail = det || ""
    root.failed(msg, root.detail)
  }

  // Replies come back as text/event-stream — "event: message", then a
  // "data: {...}" line carrying the JSON-RPC envelope. A stream may hold more
  // than one frame, so take the first that actually carries a result or an
  // error rather than blindly taking the first data line.
  function _parseBody(raw) {
    var text = String(raw || "")
    var lines = text.split("\n")
    var fallback = null
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf("data: ") !== 0) continue
      var env = null
      try { env = JSON.parse(lines[i].substring(6)) } catch (e) { continue }
      if (env && (env.result !== undefined || env.error !== undefined)) return env
      if (!fallback) fallback = env
    }
    if (fallback) return fallback
    try { return JSON.parse(text) } catch (e2) { return null }
  }

  // Qt's QML XMLHttpRequest implements neither `timeout` nor `ontimeout`, so a
  // backend that accepts the connection and then goes quiet would hang the
  // request forever. The deadline has to be ours. One request is in flight at
  // a time — there is one embedder, so there is nothing to overlap.
  property var _active: null
  property var _activeCb: null

  Timer {
    id: requestGuard
    repeat: false
    onTriggered: {
      var cb = root._activeCb
      if (root._active) {
        root._active._handled = true
        try { root._active.abort() } catch (e) { /* already gone */ }
      }
      root._active = null
      root._activeCb = null
      if (cb) cb(null, "timeout")
    }
  }

  function _post(payload, expectReply, cb, timeoutMs) {
    var x = new XMLHttpRequest()
    x._handled = false
    root._active = x
    root._activeCb = cb
    requestGuard.interval = timeoutMs || root.quickTimeout
    requestGuard.restart()
    x.onreadystatechange = function() {
      if (x.readyState !== XMLHttpRequest.DONE) return
      if (x._handled) return          // aborted by the deadline above
      x._handled = true
      requestGuard.stop()
      root._active = null
      root._activeCb = null
      var sid = x.getResponseHeader("Mcp-Session-Id")
      if (sid) root.sessionId = sid
      if (x.status === 0) { cb(null, "unreachable"); return }
      if (x.status === 401) { cb(null, "unauthorized"); return }
      if (x.status !== 200 && x.status !== 202) { cb(null, "HTTP " + x.status + " " + x.responseText); return }
      if (!expectReply) { cb(null, null); return }
      var env = root._parseBody(x.responseText)
      if (!env) { cb(null, "unparsable response"); return }
      if (env.error) { cb(null, (env.error.message || "rpc error")); return }
      cb(env.result, null)
    }
    x.open("POST", root.endpoint)
    x.setRequestHeader("Content-Type", "application/json")
    x.setRequestHeader("Accept", "application/json, text/event-stream")
    if (root.token) x.setRequestHeader("Authorization", "Bearer " + root.token)
    if (root.sessionId) x.setRequestHeader("Mcp-Session-Id", root.sessionId)
    x.send(JSON.stringify(payload))
  }

  function _rpc(method, params, cb, timeoutMs) {
    root._nextId += 1
    root._post({ jsonrpc: "2.0", id: root._nextId, method: method, params: params }, true, cb, timeoutMs)
  }

  function _notify(method) {
    root._post({ jsonrpc: "2.0", method: method }, false, function() {})
  }

  // Tool replies differ in shape: `search` returns one JSON object per content
  // item, while `list_documents` returns a single wrapper {documents, total}.
  // Decoding stays generic; unwrapping is the caller's job.
  function _decodeTool(result) {
    var out = []
    var items = (result && result.content) || []
    for (var i = 0; i < items.length; i++) {
      try { out.push(JSON.parse(items[i].text)) } catch (e) { /* not JSON, skip */ }
    }
    return out
  }

  // Every call after `initialize` must carry the session id. Lose the session —
  // an aborted handshake, a restarted daemon — and the server answers HTTP 400
  // rather than starting a new one. So: shake hands again and retry once.
  function _isSessionLost(err) {
    return String(err || "").indexOf("Missing session ID") !== -1
  }

  function _handshakeThen(next) {
    root.sessionId = ""
    root._rpc("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "oma", version: "0.1.0" }
    }, function(result, err) {
      if (err) { next(err); return }
      root._notify("notifications/initialized")
      next(null)
    })
  }

  function _callTool(name, args, cb, timeoutMs, retried) {
    root._rpc("tools/call", { name: name, arguments: args || {} }, function(result, err) {
      if (err && root._isSessionLost(err) && !retried) {
        root._handshakeThen(function(hErr) {
          if (hErr) { cb(null, hErr); return }
          root._callTool(name, args, cb, timeoutMs, true)
        })
        return
      }
      if (err) { cb(null, err); return }
      cb(root._decodeTool(result), null)
    }, timeoutMs)
  }

  // A call that never came back means the embedder is taken. That is the one
  // failure worth naming precisely, because waiting actually fixes it.
  function _enterBusy() {
    root.phase = "busy"
    root.message = "Indexing in progress — search available again shortly"
    root.detail = ""
    busyPoll.restart()
  }

  // ---------------------------------------------------------------- connect

  function connect() {
    if (root.phase === "starting") return
    root.phase = "starting"
    root.message = "Connecting"
    root.sessionId = ""
    tokenProc.running = true
  }

  function _handshake() {
    root._handshakeThen(function(err) {
      if (err === "unreachable") { root._startDaemon(); return }
      // A backend that listens but never answers is occupied, not broken.
      if (err === "timeout") { root._enterBusy(); return }
      if (err) { root._fail("Backend refused the connection", err); return }
      root.phase = "ready"
      root.message = ""
      root.refresh()
    })
  }

  property bool _daemonLaunched: false

  function _startDaemon() {
    // With `setsid --fork` the launcher exits immediately, so `daemon.running`
    // is no longer a guard against launching twice — and a second daemon would
    // only fight the first for the port.
    if (daemon.running || root._daemonLaunched) { retryTimer.restart(); return }
    root.message = "Starting backend"
    root.ownsDaemon = true
    root._daemonLaunched = true
    daemon.running = true
    retryTimer.restart()
  }

  // ---------------------------------------------------------------- queries

  function refresh() {
    root._callTool("status", {}, function(rows, err) {
      if (err === "timeout") { root._enterBusy(); return }
      if (err) { root._fail("Could not read backend status", err); return }
      if (rows.length && rows[0].total_chunks !== undefined) root.totalChunks = rows[0].total_chunks
      if (root.phase === "busy") { busyPoll.stop(); root.phase = "ready"; root.message = "" }
      root.statusUpdated()
    })
    root._callTool("list_documents", {}, function(rows, err) {
      if (err) return
      var payload = rows.length ? rows[0] : null
      root.documents = (payload && payload.documents) || []
      root.documentsUpdated()
    })
  }

  function search(query, topK) {
    // One operation at a time — there is only one embedder to go around.
    if (root.phase === "busy") { root.refused(root.message); return }
    if (root.phase !== "ready") { root.refused("Backend is not ready yet"); return }

    root.phase = "searching"
    root.message = ""
    root._callTool("search", { query: query, top_k: topK || 5 }, function(rows, err) {
      if (err === "timeout") { root._enterBusy(); return }
      if (err) {
        root.phase = "ready"
        root._fail("Search did not come back", err)
        return
      }
      root.phase = "ready"
      root.searchFinished(rows)
    }, root.searchTimeout)
  }

  // ---------------------------------------------------------------- processes

  Process {
    id: tokenProc
    command: [root.bin, "token"]
    stdout: StdioCollector {
      onStreamFinished: function() {
        var t = String(text).trim()
        if (t && t.indexOf(" ") === -1) {
          root.token = t
          root._handshake()
        } else {
          // No running server to read a token from.
          root._startDaemon()
        }
      }
    }
    onExited: function(code) {
      if (code !== 0 && !root.token) root._startDaemon()
    }
  }

  // Started as a plain child, the daemon dies with the overlay: closing OMA
  // then leaves nothing behind. `setsid --fork` detaches it instead, so it
  // outlives the QML tree and the next open finds it already listening.
  Process {
    id: daemon
    // Detaching needs more than setsid: the forked daemon inherits Quickshell's
    // stdio pipes, and those close the moment the launcher exits — the daemon
    // then dies on the first write. So redirect its streams away as well.
    // Binary and port go in as positional arguments; nothing is interpolated
    // into the command string.
    command: root.persistDaemon
      ? ["sh", "-c",
         "exec setsid --fork \"$1\" serve --host 127.0.0.1 --port \"$2\" >/dev/null 2>&1 </dev/null",
         "oma", root.bin, String(root.port)]
      : [root.bin, "serve", "--host", "127.0.0.1", "--port", String(root.port)]
    onExited: function(code) {
      // With --fork the launcher exits at once and the daemon lives on, so a
      // zero exit here says nothing about the backend.
      if (root.persistDaemon && code === 0) return
      root.ownsDaemon = false
      if (root.phase !== "ready" && root.phase !== "busy")
        root._fail("Backend stopped", "exit code " + code)
    }
  }

  // While the slot is taken, ask the backend now and then whether it is free.
  // status needs no embedder, so a prompt reply means the run has finished.
  Timer {
    id: busyPoll
    interval: 8000
    repeat: true
    running: false
    onTriggered: {
      if (root.phase !== "busy" || tokenProc.running) return
      // No session means the handshake never completed — start there, not at a
      // tool call the server will reject.
      if (!root.sessionId) root._handshake()
      else root.refresh()
    }
  }

  // `lilbee token` talks to the daemon, so a wedged backend hangs the process
  // too — and a Process has no timeout of its own. Without this the UI sits on
  // "Connecting" forever instead of saying the backend is occupied.
  Timer {
    id: tokenGuard
    interval: 12000
    running: tokenProc.running
    onTriggered: {
      if (!tokenProc.running) return
      tokenProc.running = false
      if (root.phase !== "ready") root._enterBusy()
    }
  }

  // The daemon needs ~20s before it listens; poll for the token until it does.
  Timer {
    id: retryTimer
    interval: 3000
    repeat: true
    running: false
    property int tries: 0
    onTriggered: {
      tries += 1
      if (tries > 20) { stop(); tries = 0; root._fail("Backend did not start", "timed out after 60s"); return }
      if (root.phase === "ready") { stop(); tries = 0; return }
      // One `lilbee token` costs ~2.3s; never stack them.
      if (!tokenProc.running) tokenProc.running = true
    }
    onRunningChanged: if (running) tries = 0
  }
}
