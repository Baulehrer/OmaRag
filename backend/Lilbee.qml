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
  // Indexing a 670-page book took 13 minutes. A deadline here is a last
  // resort, not a schedule.
  readonly property int indexTimeout: 2400000

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
    if (root.showingStored) return root.rawAnswer.trim()
    var i = root.rawAnswer.search(/\n\s*Sources:/)
    return (i === -1 ? root.rawAnswer : root.rawAnswer.substring(0, i)).trim()
  }

  readonly property var answerSources: {
    if (root.showingStored) return root.storedSources
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
      // The fold turned every line break into a space — right for the title,
      // wrong inside the URL, where `ask` breaks mid-path and the space lands
      // in the middle of an escape: ".../04%20Lehr-%20und%20L ernmaterial/...".
      // That is also what made decodeURIComponent throw "URI malformed"; a
      // lone `%` was never the cause. A real space in a markdown link is
      // percent-encoded, so stripping whitespace here cannot lose anything.
      out.push({ index: parseInt(m[1], 10), title: m[2],
                 url: m[3].replace(/\s+/g, ""), pages: m[4] || "" })
    return out
  }

  // `rejected` names the files lilbee could not read. They are not an error
  // of the call — the call succeeds — so without this they vanish silently.
  signal indexingFinished(bool ok, var rejected)
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

  // Terminal colour codes end up in the output because `ask` writes for a
  // terminal whether or not one is attached.
  function _stripAnsi(t) {
    return String(t || "").replace(/\u001b\[[0-9;]*[A-Za-z]/g, "")
                          .replace(/\[[0-9;]{1,8}m/g, "")
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

  // Explicit "release the models now" from Setup. Unlike releaseEngine this is
  // asked for directly, so it runs whoever started the server.
  function stopEngineNow() { engineStop.running = true }

  // An index run leaves the embedder, the reranker and — if any page needed OCR
  // — the vision model resident, measured at 8.2 GB and a further 5.0. lilbee
  // holds them for thirty minutes by default, which is long enough to stop a
  // chat model from being admitted: it happened three times in one afternoon,
  // each time needing `lilbee engine stop` by hand.
  //
  // Not released the instant indexing ends, because asking a question about
  // what was just added is the obvious next move and would pay for a reload.
  // Ninety seconds of quiet first, and any work at all cancels it.
  Timer {
    id: postIndexRelease
    interval: 90000
    onTriggered: {
      if (root.phase === "ready" && root.ownsDaemon) {
        root.releaseEngine()
        root.enginePutAway()
      }
    }
  }
  signal enginePutAway()

  function _keepEngine() { postIndexRelease.stop() }

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
    root._keepEngine()
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

  // Put a stored answer back on screen without asking anything. The parsed
  // fields are set directly, so a recalled answer looks exactly like the one
  // that was given — not a fresh reply that happens to match.
  property var storedSources: []
  property bool showingStored: false

  function showStored(answer, sources) {
    root.storedSources = sources || []
    root.showingStored = true
    root.rawAnswer = String(answer || "")
  }

  // ---------------------------------------------------------------- settings

  // lilbee describes its own configuration: every key comes with value,
  // default, type, help text, choices and whether changing it invalidates the
  // index. The Setup view is built from that rather than from a hardcoded list,
  // so it stays right across lilbee versions.
  property var settings: []
  property bool settingsLoading: false
  signal settingsLoaded()
  signal settingWritten(string key, bool ok, bool needsReindex)

  function loadSettings(force) {
    if (root.phase !== "ready") return
    // A reload right after a write must not be swallowed by a fetch that is
    // still in flight — it would leave the field showing the old value.
    if (root.settingsLoading && !force) return
    root.settingsLoading = true
    root._callTool("settings_list", {}, function(rows, err) {
      root.settingsLoading = false
      if (err) return
      var payload = rows.length ? rows[0] : null
      root.settings = (payload && payload.settings) || []
      root.settingsLoaded()
    })
  }

  function setting(key) {
    for (var i = 0; i < root.settings.length; i++)
      if (root.settings[i].key === key) return root.settings[i]
    return null
  }

  function writeSetting(key, value) {
    var updates = {}
    updates[key] = value
    root._callTool("settings_set", { updates: updates }, function(rows, err) {
      if (err) { root.settingWritten(key, false, false); return }
      // The answer says whether the change invalidates the index; that beats
      // the flag on our own copy of the key, which may be a load behind.
      var payload = rows.length ? rows[0] : null
      var reindex = payload ? payload.reindex_required === true : false
      root.loadSettings(true)
      root.settingWritten(key, true, reindex)
    })
  }

  function resetSetting(key) {
    root._callTool("settings_reset", { keys: [key] }, function(rows, err) {
      if (err) { root.settingWritten(key, false, false); return }
      var payload = rows.length ? rows[0] : null
      root.loadSettings(true)
      root.settingWritten(key, true, payload ? payload.reindex_required === true : false)
    })
  }

  // ---------------------------------------------------------------- models

  property var installedModels: []
  property var catalog: []
  property bool catalogLoading: false
  signal catalogLoaded()

  // Local only — reads what is already on disk, no network.
  function loadModels() {
    root._callTool("model_list", {}, function(rows, err) {
      if (err) return
      var payload = rows.length ? rows[0] : null
      root.installedModels = (payload && (payload.models || payload.installed)) || (Array.isArray(payload) ? payload : [])
    })
  }

  // Reaches Hugging Face. Only ever called from an explicit button, never on
  // opening a view — OMA does not telephone out unasked.
  function browseCatalog(task, search) {
    if (root.catalogLoading) return
    root.catalogLoading = true
    var args = { task: task || "", limit: 30 }
    if (search && search.length) args.search = search
    root._callTool("catalog_browse", args, function(rows, err) {
      root.catalogLoading = false
      if (err) { root._fail("Could not reach the model catalogue", "catalog_browse: " + err); return }
      var payload = rows.length ? rows[0] : null
      root.catalog = (payload && (payload.models || payload.results)) || (Array.isArray(payload) ? payload : [])
      root.catalogLoaded()
    }, 60000)
  }

  // Downloads. Never called on its own — only from a pressed button, and the
  // download itself pins the single embedder, so everything else has to wait
  // the way it does for indexing.
  property string pulling: ""
  signal pullFinished(string model, bool ok, string detail)

  function pullModel(model) {
    if (!model || !model.length) return
    if (root.pulling.length) { root.refused("A model is already downloading"); return }
    if (root.phase === "busy") { root.refused(root.message); return }
    if (root.phase !== "ready") { root.refused("Backend is not ready yet"); return }
    root.pulling = model
    root._callTool("model_pull", { model: model }, function(rows, err) {
      root.pulling = ""
      if (err) { root.pullFinished(model, false, err); return }
      // A fresh file changes what the picker may offer.
      root.loadModels()
      root.pullFinished(model, true, "")
    }, 30 * 60 * 1000)
  }

  // ---------------------------------------------------------------- indexing

  property string indexingWhat: ""
  // Filled by the log watcher — see IngestWatch. -1 means "not known yet",
  // which the progress view renders as elapsed time only.
  readonly property string indexStage: watch.stage
  readonly property int indexDone: watch.done
  readonly property int indexTotal: watch.total
  readonly property int indexCalls: watch.calls
  readonly property bool indexStalled: watch.stalled

  // The log names the document once extraction reports it, which is more
  // accurate than the path we guessed from — take it over when it arrives.
  IngestWatch {
    id: watch
    onSourceChanged: if (source.length && root.phase === "indexing") root.indexingWhat = source
  }

  // `add` copies or links the files and indexes them in one call. It pins the
  // single embedder for its whole run, which is why everything else is locked
  // out while it works rather than left to hang.
  function addPaths(paths) {
    if (!paths || !paths.length) return
    if (root.phase === "busy") { root.refused(root.message); return }
    if (root.phase !== "ready") { root.refused("Backend is not ready yet"); return }

    root.indexingWhat = paths.length === 1
      ? String(paths[0]).split("/").pop()
      : paths.length + " items"
    root.phase = "indexing"
    root.message = ""
    watch.begin()

    root._callTool("add", { paths: paths }, function(rows, err) {
      // A timeout does not mean it stopped — the daemon keeps working, and
      // there is no cancel for `add`. Saying "busy" is the truthful state.
      watch.finish()
      if (err === "timeout") { root._enterBusy(); root.indexingFinished(false, []); return }
      root.phase = "ready"
      root.indexingWhat = ""
      if (err) {
        root._fail(root._humanise(err), "add: " + err)
        root.indexingFinished(false, [])
        return
      }
      root.refresh()

      // lilbee reports unreadable files inside a successful answer: the call
      // returns 0 and `sync.failed` names them. Measured against a truncated
      // PDF, a text file with a .pdf name, an empty file, random bytes and an
      // encrypted document — four of the five come back this way.
      var payload = rows.length ? rows[0] : null
      var sync = (payload && payload.sync) || {}
      var rejected = []
      var lists = [sync.failed, sync.skipped, payload && payload.errors]
      for (var i = 0; i < lists.length; i++)
        if (Array.isArray(lists[i]))
          for (var j = 0; j < lists[i].length; j++) {
            var name = lists[i][j]
            if (typeof name !== "string") name = JSON.stringify(name)
            if (rejected.indexOf(name) === -1) rejected.push(name)
          }
      root.indexingFinished(rejected.length === 0, rejected)
      postIndexRelease.restart()
    }, root.indexTimeout)
  }

  // ---------------------------------------------------------------- answering

  function ask(question) {
    root._keepEngine()
    if (root.phase === "busy") { root.refused(root.message); return }
    if (root.phase !== "ready") { root.refused("Backend is not ready yet"); return }

    root.rawAnswer = ""
    root.answerDetail = ""
    root.showingStored = false
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
      var det = root.answerDetail + root.rawAnswer

      // Checked before success, not after: LiteLLM writes this to stdout, and
      // an exit code of 0 with the message in it would otherwise be shown as
      // though it were the answer.
      var providerDown = det.indexOf("unavailable") !== -1
                      || det.indexOf("insufficient safe memory") !== -1
      if (code === 0 && !providerDown && root.answerText.length) {
        root.answerFinished(true)
        return
      }

      // The window is for answers. A failure belongs in the state panel, with
      // the raw output behind "Technical details" — stripped of the colour
      // escapes, which the renderer would otherwise show as "[1;31m".
      var detail = root._stripAnsi(det).trim()
      root.rawAnswer = ""
      root.answerDetail = ""

      // Two ways the answering model can be wrong, and each has its own remedy.
      // Measured: a ref without a provider prefix fails validation before the
      // request is made; a well-formed ref for a model nobody serves is
      // rejected by the provider.
      var named = root.answerModel.length ? "\"" + root.answerModel + "\"" : "the configured model"
      var badRef = detail.indexOf("must be a HuggingFace ref") !== -1
                || detail.indexOf("known provider prefix") !== -1
      var unknownModel = detail.indexOf("provider rejected the request") !== -1

      if (providerDown)
        root._fail("The answering model could not be loaded",
                   "Retrieval models hold memory the chat model needs.\n" + detail)
      else if (badRef)
        root._fail(named + " is not a usable model name",
                   "Setup > answering model takes a Hugging Face reference such as "
                   + "org/repo/file.gguf, or a provider prefix such as lm_studio/name. "
                   + "Leave it empty to use whatever lilbee is set to.\n\n" + detail)
      else if (unknownModel)
        root._fail("Nothing is serving " + named,
                   "The name is well formed, but no provider offers it. Check Setup > "
                   + "answering model against what is actually loaded.\n\n" + detail)
      else
        root._fail("Answering failed",
                   "lilbee ask exited with code " + code + "\n" + detail)
      root.answerFinished(false)
    }
  }

  // Open a cited page in a document viewer. lilbee's page numbers are PDF page
  // numbers, not printed page labels — verified against the book: what it cites
  // as page 293 is PDF page 293. evince's --page-index takes exactly that
  // ("the exact page number, not a page label"), and zathura and okular count
  // the same way.
  //
  // Detached, because opening a source and then closing OMA is the normal move
  // and the viewer must not go with it.
  function openDocument(url, pages) {
    var path = String(url || "")
    if (path.indexOf("file://") === 0) {
      // A lone `%` in a filename makes decodeURIComponent throw. The raw path
      // is still worth trying — better than dropping the click.
      var encoded = path.substring(7)
      try { path = decodeURIComponent(encoded) } catch (e) { path = encoded }
    }
    if (!path.length) return

    // "334-335" or "334–335" — jump to where the passage starts.
    var first = String(pages || "").split(/[\u2013-]/)[0]
    var page = parseInt(first, 10)

    // Check the file before handing it to a viewer. A document that has been
    // moved or replaced is ordinary — the library is a folder the user tends —
    // and without this the viewer opens on nothing and OMA says nothing.
    // Exit 66 is the one code this script produces itself.
    root.openWanted = path.split("/").pop()
    openProc.command = ["sh", "-c",
      '[ -f "$1" ] || exit 66; ' +
      'p="$2"; ' +
      'if [ -n "$p" ]; then ' +
        'command -v zathura >/dev/null 2>&1 && exec setsid --fork zathura -P "$p" "$1"; ' +
        'command -v okular  >/dev/null 2>&1 && exec setsid --fork okular -p "$p" "$1"; ' +
        'command -v evince  >/dev/null 2>&1 && exec setsid --fork evince --page-index="$p" "$1"; ' +
      'fi; ' +
      'exec setsid --fork xdg-open "$1"',
      "oma", path, isNaN(page) ? "" : String(page)]
    openProc.running = true
  }

  // The name of the document a click asked for, so a failure can say which.
  property string openWanted: ""
  signal openFailed(string what)

  Process {
    id: openProc
    onExited: function(code) {
      if (code === 66) root.openFailed(root.openWanted)
      else if (code !== 0) root.openFailed(root.openWanted)
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
