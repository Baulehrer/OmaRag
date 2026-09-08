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
  property string phase: "idle"
  property string message: ""
  property string detail: ""

  property string token: ""
  property string sessionId: ""
  property bool ownsDaemon: false

  property int totalChunks: -1
  property var documents: []

  signal statusUpdated()
  signal documentsUpdated()
  signal searchFinished(var hits)
  signal failed(string message, string detail)

  property int _nextId: 0

  // ---------------------------------------------------------------- helpers

  function _fail(msg, det) {
    root.phase = "error"
    root.message = msg
    root.detail = det || ""
    root.failed(msg, root.detail)
  }

  // A streamable-HTTP response may arrive as a single JSON body or as one
  // SSE frame. Both carry the same JSON-RPC envelope.
  function _parseBody(raw) {
    var text = String(raw || "")
    var lines = text.split("\n")
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].indexOf("data: ") === 0) { text = lines[i].substring(6); break }
    }
    try { return JSON.parse(text) } catch (e) { return null }
  }

  function _post(payload, expectReply, cb) {
    var x = new XMLHttpRequest()
    x.onreadystatechange = function() {
      if (x.readyState !== XMLHttpRequest.DONE) return
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

  function _rpc(method, params, cb) {
    root._nextId += 1
    root._post({ jsonrpc: "2.0", id: root._nextId, method: method, params: params }, true, cb)
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

  function _callTool(name, args, cb) {
    root._rpc("tools/call", { name: name, arguments: args || {} }, function(result, err) {
      if (err) { cb(null, err); return }
      cb(root._decodeTool(result), null)
    })
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
    root._rpc("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "oma", version: "0.1.0" }
    }, function(result, err) {
      if (err === "unreachable") { root._startDaemon(); return }
      if (err) { root._fail("Backend refused the connection", err); return }
      root._notify("notifications/initialized")
      root.phase = "ready"
      root.message = ""
      root.refresh()
    })
  }

  function _startDaemon() {
    if (daemon.running) { retryTimer.restart(); return }
    root.message = "Starting backend"
    root.ownsDaemon = true
    daemon.running = true
    retryTimer.restart()
  }

  // ---------------------------------------------------------------- queries

  function refresh() {
    root._callTool("status", {}, function(rows, err) {
      if (err) { root._fail("Could not read backend status", err); return }
      if (rows.length && rows[0].total_chunks !== undefined) root.totalChunks = rows[0].total_chunks
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
    if (root.phase !== "ready") return
    root.phase = "busy"
    root.message = "Searching"
    root._callTool("search", { query: query, top_k: topK || 5 }, function(rows, err) {
      root.phase = "ready"
      root.message = ""
      if (err) {
        // A hanging search means indexing holds the single embedder slot.
        root._fail("Search did not come back", err)
        return
      }
      root.searchFinished(rows)
    })
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

  Process {
    id: daemon
    command: [root.bin, "serve", "--host", "127.0.0.1", "--port", String(root.port)]
    onExited: function(code) {
      root.ownsDaemon = false
      if (root.phase !== "ready") root._fail("Backend stopped", "exit code " + code)
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
