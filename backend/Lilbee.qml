import QtQuick
import Quickshell
import Quickshell.Io

// Talks to lilbee over MCP on streamable HTTP. The daemon has no REST surface —
// /mcp is the whole API (see REALITY.md). Everything here is asynchronous: this
// runs inside the Omarchy shell process, where one blocking call freezes the
// desktop.
Item {
  id: root

  property string bin: "lilbee"

  // lilbee allows exactly one server per data directory and records it there:
  // `server.port` holds the port, `server.json` the bearer token. Reading those
  // is how OMA finds a server the user already has running — from the TUI, from
  // a terminal — instead of starting a second one and being refused outright.
  // It also saves calling `lilbee token`, which costs 2.3s of process startup.
  property string dataDir: Quickshell.env("HOME") + "/.local/share/lilbee/data"
  property int port: 0
  property string token: ""
  readonly property string endpoint: "http://127.0.0.1:" + port + "/mcp"

  // idle · starting · ready · searching · busy · error
  //
  // busy is not an error. lilbee holds a single embedder worker, so an index
  // run — ours or one started from the terminal or lilbee's own TUI — takes the
  // only slot there is, and a query would simply hang. Saying so is the
  // difference between an app that looks broken and one that explains itself.
  property string phase: "idle"
  property string message: ""
  property string detail: ""

  // Keep the daemon alive after OMA closes. Off by default: a closed overlay
  // then costs nothing at all. On, roughly 430 MB stays resident with no models
  // loaded, and the next question starts without a backend boot.
  property bool persistDaemon: false

  // Cheap calls should fail fast; a cold search legitimately runs past a minute
  // while the embedding model loads.
  readonly property int quickTimeout: 15000
  readonly property int searchTimeout: 180000

  property string sessionId: ""
  property bool ownsDaemon: false
  property int totalChunks: -1
  property var documents: []

  // Answering runs through `lilbee ask` as a short-lived process: MCP has no
  // ask tool, and rebuilding lilbee's RAG prompt here would make OMA the very
  // engine it is meant not to be.
  property string answerModel: ""   // empty: whatever lilbee is configured with
  property string rawAnswer: ""
  property string answerDetail: ""

  // `ask` prints the answer, then a "Sources:" block whose numbering the [1]
  // markers in the answer refer to. Splitting its output keeps answer and
  // citations consistent — running our own search alongside would produce a
  // different list under the same numbers.
  readonly property string answerText: {
    var i = root.rawAnswer.search(/\n\s*Sources:/)
    return (i === -1 ? root.rawAnswer : root.rawAnswer.substring(0, i)).trim()
  }

  readonly property var answerSources: {
    var i = root.rawAnswer.search(/\n\s*Sources:/)
    if (i === -1) return []
    // `ask` wraps its output to a fixed width, pipe or not, so a single source
    // entry is spread over several lines and a line-by-line match finds
    // nothing. Fold the block into one string first.
    var block = root.rawAnswer.substring(i).replace(/\s*\n\s*/g, " ")
    var out = []
    var re = /(\d+)\.\s*\[([^\]]*)\]\(([^)]*)\)(?:\s*,\s*pages?\s*([0-9\u2013-]+))?/g
    var m
    while ((m = re.exec(block)) !== null)
      out.push({ index: parseInt(m[1], 10), title: m[2], url: m[3], pages: m[4] || "" })
    return out
  }

  signal answerStarted()
  signal answerFinished(bool ok)
  signal statusUpdated()
  signal documentsUpdated()
  signal searchFinished(var hits)
  signal failed(string message, string detail)
  signal refused(string reason)

  property int _nextId: 0
  property bool _daemonLaunched: false

  // ---------------------------------------------------------------- errors

  // The headline is for the person, the detail for whoever has to debug it.
  // Nothing technical ever reaches the headline.
  function _fail(msg, det) {
    root.phase = "error"
    root.message = msg
    root.detail = det || ""
    root.failed(msg, root.detail)
  }

  function _humanise(err) {
    var e = String(err || "")
    if (e === "unreachable") return "Backend is not reachable"
    if (e === "timeout") return "Backend did not answer in time"
    if (e === "unauthorized") return "Backend rejected the token"
    if (e.indexOf("HTTP 4") === 0) return "Backend refused the request"
    if (e.indexOf("HTTP 5") === 0) return "Backend ran into an error"
    return "Something went wrong talking to the backend"
  }

  // A call that never came back means the embedder is taken. That is the one
  // failure worth naming precisely, because waiting actually fixes it.
  function _enterBusy() {
    root.phase = "busy"
    root.message = "Indexing in progress — search available again shortly"
    root.detail = ""
    busyPoll.restart()
  }

  // Closing OMA kills the server, but not the model fleet it spawned: the
  // llama-swap processes reparent to systemd and keep the weights in VRAM until
  // their 30-minute TTL expires. Long enough to stop an external chat model from
  // loading — which is exactly how `lilbee ask` failed here once.
  //
  // Only ever released when OMA started the server itself. A server the user is
  // running from their TUI or a terminal owns its engine; `lilbee engine stop`
  // takes it down "whoever started it", so it must not be aimed at someone
  // else's work.
  function releaseEngine() {
    if (!root.ownsDaemon || root.persistDaemon) return
    engineStop.running = true
  }

  function retry() {
    root.phase = "idle"
    root.message = ""
    root.detail = ""
    root.sessionId = ""
    root._daemonLaunched = false
    busyPoll.stop()
    root.connect()
  }

  // ---------------------------------------------------------------- transport

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
  // request forever. The deadline has to be ours. One request is in flight at a
  // time — there is one embedder, so there is nothing to overlap.
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

  // Every tool reply carries its payload as JSON text, one content item per
  // object. `search` returns one per hit; `list_documents` a single wrapper.
  // Decoding stays generic — unwrapping is the caller's job.
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

  // ---------------------------------------------------------------- connect

  function connect() {
    if (root.phase === "starting") return
    root.phase = "starting"
    root.message = "Connecting"
    root.sessionId = ""
    root._readServerFiles()
    serverProbe.start()
  }

  // Reads only. Restarting the probe from here reset its own attempt counter
  // on every tick, so it polled for a server forever: it never got far enough
  // to start one, and never far enough to give up either.
  function _readServerFiles() {
    portFile.reload()
    tokenFile.reload()
  }

  function _tryConnect() {
    if (root.phase === "ready" || root.phase === "busy") return
    if (root.port > 0 && root.token.length > 0) {
      serverProbe.stop()
      root._handshake()
    }
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

  function _handshake() {
    root._handshakeThen(function(err) {
      // Recorded but not answering: the files are stale, so start our own.
      if (err === "unreachable") { root._startDaemon(); return }
      // Listening but silent means occupied, not broken.
      if (err === "timeout") { root._enterBusy(); return }
      if (err) { root._fail(root._humanise(err), "initialize: " + err); return }
      root.phase = "ready"
      root.message = ""
      root.refresh()
    })
  }

  function _startDaemon() {
    if (daemon.running || root._daemonLaunched) return
    root.message = "Starting backend"
    root.ownsDaemon = true
    root._daemonLaunched = true
    daemon.running = true
    if (!serverProbe.running) serverProbe.start()
  }

  // ---------------------------------------------------------------- queries

  function refresh() {
    root._callTool("status", {}, function(rows, err) {
      if (err === "timeout") { root._enterBusy(); return }
      if (err) { root._fail(root._humanise(err), "status: " + err); return }
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
        root._fail(root._humanise(err), "search: " + err)
        return
      }
      root.phase = "ready"
      // The backend returns roughly twice top_k — neighbouring chunks come
      // along as context — and not in score order (measured: 0.989, 0.944,
      // 0.953, …). Numbered rows imply a ranking, so establish one.
      rows.sort(function(a, b) { return (b.score || 0) - (a.score || 0) })
      root.searchFinished(rows)
    }, root.searchTimeout)
  }

  // ---------------------------------------------------------------- answering

  function ask(question) {
    if (root.phase === "busy") { root.refused(root.message); return }
    if (root.phase !== "ready") { root.refused("Backend is not ready yet"); return }

    root.rawAnswer = ""
    root.answerDetail = ""
    root.phase = "answering"

    var cmd = [root.bin, "ask", String(question), "--no-sync"]
    if (root.answerModel.length) { cmd.push("--model"); cmd.push(root.answerModel) }
    askProc.command = cmd
    askProc.running = true
    root.answerStarted()
  }

  // There is no server-side cancel, so stopping means ending the process.
  function cancelAsk() {
    if (askProc.running) askProc.running = false
  }

  Process {
    id: askProc
    // Line-buffered rather than a single collector: the answer arrives over
    // several seconds and should appear as it does.
    stdout: SplitParser {
      onRead: function(line) { root.rawAnswer += line + "\n" }
    }
    stderr: SplitParser {
      onRead: function(line) { root.answerDetail += line + "\n" }
    }
    onExited: function(code) {
      root.phase = "ready"
      if (code === 0 && root.answerText.length) { root.answerFinished(true); return }
      // A model that cannot be loaded is the common failure here, and it is
      // worth naming rather than dumping LiteLLM's output into the window.
      var det = root.answerDetail + root.rawAnswer
      if (det.indexOf("unavailable") !== -1 || det.indexOf("insufficient safe memory") !== -1)
        root._fail("The answering model could not be loaded",
                   "Retrieval models hold memory the chat model needs.\n" + det.trim())
      else if (code !== 0)
        root._fail("Answering failed", "lilbee ask exited with code " + code + "\n" + det.trim())
      root.answerFinished(false)
    }
  }

  // ---------------------------------------------------------------- files

  FileView {
    id: portFile
    path: root.dataDir + "/server.port"
    printErrors: false
    onLoaded: {
      var n = parseInt(String(text()).trim(), 10)
      if (!isNaN(n) && n > 0 && n !== root.port) root.port = n
      root._tryConnect()
    }
    onLoadFailed: root.port = 0
  }

  FileView {
    id: tokenFile
    path: root.dataDir + "/server.json"
    printErrors: false
    onLoaded: {
      try {
        var parsed = JSON.parse(String(text()))
        if (parsed && parsed.token) root.token = String(parsed.token)
      } catch (e) { root.token = "" }
      root._tryConnect()
    }
    onLoadFailed: root.token = ""
  }

  // ---------------------------------------------------------------- processes

  // Both branches redirect the daemon's streams. lilbee logs every embedding
  // call; left writing into a pipe nobody drains, it stalls once the buffer
  // fills. `exec` in the plain branch keeps the daemon as Quickshell's own
  // child, so it still goes when OMA closes. Port 0 lets lilbee pick a free one
  // and write it to server.port, which is where we read it back from.
  Process {
    id: daemon
    command: root.persistDaemon
      ? ["sh", "-c",
         "exec setsid --fork \"$1\" serve --host 127.0.0.1 --port 0 >/dev/null 2>&1 </dev/null",
         "oma", root.bin]
      : ["sh", "-c",
         "exec \"$1\" serve --host 127.0.0.1 --port 0 >/dev/null 2>&1 </dev/null",
         "oma", root.bin]
    onExited: function(code) {
      // With --fork the launcher exits at once and the daemon lives on, so a
      // zero exit here says nothing about the backend.
      if (root.persistDaemon && code === 0) return
      root.ownsDaemon = false
      if (root.phase !== "ready" && root.phase !== "busy")
        root._fail("Backend stopped", "lilbee serve exited with code " + code)
    }
  }

  // Detached, because it has to outlive the teardown that triggers it.
  Process {
    id: engineStop
    command: ["sh", "-c",
              "exec setsid --fork \"$1\" engine stop >/dev/null 2>&1 </dev/null",
              "oma", root.bin]
  }

  // Poll the recorded port and token until a server shows up — ours, or one the
  // user started themselves.
  Timer {
    id: serverProbe
    interval: 2000
    repeat: true
    running: false
    property int tries: 0
    onTriggered: {
      tries += 1
      if (root.phase === "ready") { stop(); tries = 0; return }
      if (tries > 45) {
        stop(); tries = 0
        root._fail("Backend did not start", "no server recorded in " + root.dataDir + " after 90s")
        return
      }
      // No server has registered itself, so there is none to join — start one.
      // The first tick is skipped because the file reads are asynchronous and
      // an existing server would still be landing.
      if (tries >= 2 && (root.port <= 0 || !root.token.length)) root._startDaemon()
      root._readServerFiles()
    }
    onRunningChanged: if (running) tries = 0
  }

  // While the slot is taken, ask now and then whether it is free again. status
  // needs no embedder, so a prompt reply means the run has finished.
  Timer {
    id: busyPoll
    interval: 8000
    repeat: true
    running: false
    onTriggered: {
      if (root.phase !== "busy") return
      // No session means the handshake never completed — start there, not at a
      // tool call the server will reject.
      if (!root.sessionId) root._handshake()
      else root.refresh()
    }
  }
}
