import QtQuick
import Quickshell
import Quickshell.Io

// Where the indexing progress comes from.
//
// lilbee reports nothing about an `add` or `sync` over MCP — but its server log
// says plenty. Two lines carry everything needed:
//
//   lilbee.ingest.trace: extract source='…' type=pdf pages=532 chunks=459
//     → extraction finished, and the total chunk count is now known
//
//   httpx … POST http://127.0.0.1:PORT/v1/embeddings "HTTP/1.1 200 OK"
//     → one embedding call, worth roughly two chunks
//
// The ratio is measured, not assumed: 231 calls for 459 chunks on a real run.
// It is still an estimate, so the view says "approx." and falls back to a bare
// count if the ratio turns out wrong.
Item {
  id: root

  property string logPath: Quickshell.env("HOME") + "/.local/share/lilbee/logs/server.log"
  property bool active: false

  // extracting · embedding · finishing
  property string stage: ""
  property string source: ""
  property int total: -1
  property int done: -1
  property int ocrPages: 0

  // Measured on a 459-chunk book: 231 embedding calls. Held as a property so a
  // future run can correct it rather than baking a guess into the arithmetic.
  readonly property real chunksPerCall: 2.0

  property int calls: 0

  // Seconds since the log last said anything, and whether that has gone on long
  // enough to be worth mentioning. The index call itself waits forty minutes
  // before giving up, so a backend that dies mid-run would otherwise leave a
  // progress display running for most of an hour with nothing behind it.
  property int quietFor: 0
  readonly property int quietLimit: 180
  readonly property bool stalled: root.active && root.quietFor >= root.quietLimit

  function begin() {
    root.stage = "extracting"
    root.source = ""
    root.total = -1
    root.done = -1
    root.ocrPages = 0
    root.calls = 0
    root.quietFor = 0
    root.active = true
  }

  function finish() {
    root.active = false
    root.stage = ""
  }

  function _consume(line) {
    if (!root.active) return
    root.quietFor = 0

    // extract source='Name.pdf' type=pdf elapsed_ms=85219 pages=532 chunks=459
    if (line.indexOf("ingest.trace") !== -1 && line.indexOf("extract ") !== -1) {
      var src = /source='([^']*)'/.exec(line)
      var chunks = /chunks=(\d+)/.exec(line)
      if (src) root.source = src[1]
      if (chunks) {
        root.total = parseInt(chunks[1], 10)
        root.done = 0
        root.calls = 0
        root.stage = "embedding"
      }
      return
    }

    if (line.indexOf("ocr_pages=") !== -1) {
      var ocr = /ocr_pages=(\d+)/.exec(line)
      if (ocr) root.ocrPages = parseInt(ocr[1], 10)
      return
    }

    if (line.indexOf("/v1/embeddings") !== -1) {
      root.calls += 1
      // The extract trace is written for PDFs only — a text or markdown file
      // never announces its chunk count. Embeddings arriving while we still
      // think we are extracting mean extraction is over; carry on without a
      // total and show a bare count rather than a percentage of nothing.
      if (root.stage === "extracting") root.stage = "embedding"
      if (root.total > 0) {
        // Never let the estimate run past the total: claiming 103% would be
        // worse than being a little behind.
        root.done = Math.min(root.total, Math.round(root.calls * root.chunksPerCall))
        if (root.done >= root.total) root.stage = "finishing"
      }
    }
  }

  Timer {
    running: root.active
    interval: 15000
    repeat: true
    onTriggered: root.quietFor += 15
  }

  // `tail -F` rather than FileView: the file grows while we watch, and -F also
  // survives the rotation lilbee does on restart.
  Process {
    running: root.active
    command: ["tail", "-n", "0", "-F", root.logPath]
    stdout: SplitParser { onRead: function(line) { root._consume(line) } }
  }
}
