import QtQuick
import qs.Commons
import qs.Ui
import "../common"

// Everything OMA and lilbee can be told, in one place.
//
// lilbee exposes 159 settings across eleven groups. Showing them all would be a
// database browser, not a setup screen, so the sections below name the handful
// that matter for daily use and offer the rest of a group behind a disclosure.
// The fields themselves are still generated from lilbee's own description.
Item {
  id: root

  property var backend: null
  property string lilbeeVersion: ""
  // The version OMA's generated fields were built and measured against. A newer
  // lilbee is not an error — the fields come from lilbee's own description — but
  // a changed shape is worth a word before it produces nonsense.
  readonly property string testedAgainst: "0.6.90b432"
  readonly property bool versionKnown: root.lilbeeVersion.length > 0
  readonly property bool versionMatches: root.lilbeeVersion === root.testedAgainst
  property string updateNote: ""
  property bool checkingUpdate: false

  property string fontFamily: ""
  property real fontScale: 1.0
  property string backendWhenClosed: "Stop with OMA"
  property string answerModel: ""

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent
  property color urgent: Color.urgent

  signal checkUpdate()
  signal installUpdate()
  signal releaseEngine()
  signal omaSettingChanged(string key, var value)
  signal openLog()

  property var expandedGroups: ({})
  property string flash: ""

  // The keys worth surfacing, in the order they are worth reading.
  readonly property var curated: [
    { section: "Models",     keys: ["chat_model", "embedding_model", "reranker_model", "vision_model"] },
    { section: "Answering",  keys: ["chat_n_ctx_target", "max_tokens", "temperature"] },
    { section: "Warm-up",    keys: ["model_keep_alive"] },
    { section: "Retrieval",  keys: ["top_k", "rerank_candidates", "diversity_max_per_source", "max_distance"] }
  ]

  function metaFor(key) { return root.backend ? root.backend.setting(key) : null }

  // Setup is taller than the card by design; the keyboard has to reach the
  // bottom of it too, not just the wheel.
  function scrollBy(px) {
    var max = Math.max(0, sheet.contentHeight - sheet.height)
    sheet.contentY = Math.max(0, Math.min(max, sheet.contentY + px))
  }

  Flickable {
    id: sheet
    anchors.fill: parent
    contentWidth: width
    contentHeight: column.height
    clip: true
    boundsBehavior: Flickable.StopAtBounds

    Column {
      id: column
      width: parent.width
      spacing: Style.spacing.xl

      // ----------------------------------------------------------- backend
      Text {
        text: "BACKEND"
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.caption
        font.letterSpacing: 1.5
      }

      Grid {
        columns: 2
        columnSpacing: Style.spacing.xxl
        rowSpacing: Style.spacing.sm

        Text {
          text: "lilbee"
          color: root.muted
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
        }
        Text {
          text: {
            if (!root.versionKnown) return "…"
            if (root.versionMatches) return root.lilbeeVersion + "   ✓ geprüft"
            return root.lilbeeVersion + "   ⚠ nicht gegen diese Fassung geprüft ("
                 + root.testedAgainst + ")"
          }
          color: root.versionKnown && !root.versionMatches ? root.accent : root.foreground
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
        }

        Text {
          text: "Server"
          color: root.muted
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
        }
        Text {
          text: root.backend && root.backend.port > 0
              ? "127.0.0.1:" + root.backend.port
                + (root.backend.ownsDaemon ? "  ·  started by OMA" : "  ·  already running")
              : "not connected"
          color: root.foreground
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
        }

        Text {
          text: "Library"
          color: root.muted
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
        }
        Text {
          text: root.backend && root.backend.totalChunks >= 0
              ? root.backend.documents.length + " documents  ·  " + root.backend.totalChunks + " chunks"
              : "—"
          color: root.foreground
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
        }
      }

      // Every one of these reaches outside the machine or ends a running
      // process, so each says what it does before it does it.
      Row {
        spacing: Style.spacing.controlGap

        Button {
          text: root.checkingUpdate ? "Checking…" : "Check for updates"
          bordered: true
          focusable: true
          onClicked: root.checkUpdate()
        }
        Button {
          text: "Release models now"
          bordered: true
          focusable: true
          onClicked: root.releaseEngine()
        }
        Button {
          text: "Open backend log"
          bordered: true
          focusable: true
          onClicked: root.openLog()
        }
      }

      Text {
        width: parent.width
        visible: root.updateNote.length > 0
        text: root.updateNote
        color: root.foreground
        font.family: OmaFont.face
        font.pixelSize: OmaFont.bodySmall
        wrapMode: Text.WordWrap
      }

      Text {
        width: parent.width
        text: "“Check for updates” asks GitHub through mise. Nothing here contacts the "
            + "network on its own."
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.caption
        wrapMode: Text.WordWrap
      }

      // ------------------------------------------------------------- OMA
      Text {
        text: "OMA"
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.caption
        font.letterSpacing: 1.5
      }

      Row {
        spacing: Style.spacing.md

        Text {
          width: Style.space(210)
          anchors.verticalCenter: parent.verticalCenter
          text: "backend when closed"
          color: root.foreground
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
        }
        Dropdown {
          width: Style.space(240)
          options: ["Stop with OMA", "Keep running"]
          value: root.backendWhenClosed
          onChanged: function(v) { root.omaSettingChanged("backendWhenClosed", v) }
        }
      }

      Row {
        spacing: Style.spacing.md

        Text {
          width: Style.space(210)
          anchors.verticalCenter: parent.verticalCenter
          text: "answering model"
          color: root.foreground
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
        }
        TextField {
          width: Style.space(240)
          text: root.answerModel
          onAccepted: root.omaSettingChanged("answerModel", text)
        }
        Text {
          anchors.verticalCenter: parent.verticalCenter
          text: "empty: whatever lilbee is set to"
          color: root.muted
          font.family: OmaFont.face
          font.pixelSize: OmaFont.caption
        }
      }

      Row {
        spacing: Style.spacing.md

        Text {
          width: Style.space(210)
          anchors.verticalCenter: parent.verticalCenter
          text: "text size"
          color: root.foreground
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
        }
        ButtonGroup {
          options: [
            { value: "0.9",  label: "Small" },
            { value: "1.0",  label: "Normal" },
            { value: "1.15", label: "Large" },
            { value: "1.3",  label: "Larger" }
          ]
          // Matched as a number: String(1.0) is "1", which would never equal
          // the "1.0" in the options and leave every button unlit.
          value: {
            var opts = ["0.9", "1.0", "1.15", "1.3"]
            for (var i = 0; i < opts.length; i++)
              if (Math.abs(parseFloat(opts[i]) - root.fontScale) < 0.001) return opts[i]
            return ""
          }
          onChanged: function(v) { root.omaSettingChanged("omaFontScale", parseFloat(v)) }
        }
      }

      Row {
        width: parent.width
        spacing: Style.spacing.md

        Text {
          width: Style.space(210)
          anchors.verticalCenter: parent.verticalCenter
          text: "font"
          color: root.foreground
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
        }
        TextField {
          id: familyField
          width: Style.space(270)
          anchors.verticalCenter: parent.verticalCenter
          text: root.fontFamily
          onAccepted: root.omaSettingChanged("omaFontFamily", text)
        }
        Text {
          anchors.verticalCenter: parent.verticalCenter
          text: "empty: the shell's font"
          color: root.muted
          font.family: OmaFont.face
          font.pixelSize: OmaFont.caption
        }
      }

      Text {
        width: parent.width
        text: "Text size and font apply to OMA's own text. Buttons and dropdowns keep the shell's kit styling, and the shell's font is never changed."
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.caption
        wrapMode: Text.WordWrap
      }

      // -------------------------------------------------------- lilbee keys
      Text {
        text: "MODELS"
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.caption
        font.letterSpacing: 1.5
      }

      Repeater {
        model: [
          { key: "chat_model",      task: "chat",      label: "chat" },
          { key: "embedding_model", task: "embedding", label: "embedding" },
          { key: "reranker_model",  task: "rerank",    label: "reranker" },
          { key: "vision_model",    task: "vision",    label: "vision (OCR)" }
        ]

        ModelPicker {
          // parent, not the outer `column` id: inside a Repeater delegate the
          // outer id resolves too late and every instance but the last stays
          // zero-wide.
          width: parent.width
          backend: root.backend
          settingKey: modelData.key
          task: modelData.task
          label: modelData.label
          foreground: root.foreground; muted: root.muted; accent: root.accent; urgent: root.urgent
          onChoose: function(key, model) {
            if (root.backend) root.backend.writeSetting(key, model)
            open = false
          }
          onDownload: function(model) { if (root.backend) root.backend.pullModel(model) }
        }
      }

      Text {
        width: parent.width
        visible: root.backend && root.backend.pulling.length > 0
        text: root.backend ? "Downloading " + root.backend.pulling
                           + " — indexing and questions wait until it finishes." : ""
        color: root.accent
        font.family: OmaFont.face
        font.pixelSize: OmaFont.caption
        wrapMode: Text.WordWrap
      }

      SettingGroup {
        width: column.width
        title: "Answering"
        keys: ["chat_n_ctx_target", "max_tokens", "temperature"]
        backend: root.backend
        foreground: root.foreground; muted: root.muted; accent: root.accent; urgent: root.urgent
      }

      SettingGroup {
        width: column.width
        title: "Warm-up"
        keys: ["model_keep_alive"]
        backend: root.backend
        foreground: root.foreground; muted: root.muted; accent: root.accent; urgent: root.urgent
      }

      Text {
        width: parent.width
        text: "Warm-up is what llama-swap is given as its unload timer — measured: the "
            + "value here and the engine's ttl are the same number."
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.caption
        wrapMode: Text.WordWrap
      }

      SettingGroup {
        width: column.width
        title: "Retrieval"
        keys: ["top_k", "rerank_candidates", "diversity_max_per_source", "max_distance"]
        backend: root.backend
        // Measured against this library, both books, three chat models.
        recommendations: ({
          "top_k": { value: 6,
                     why: "lilbee liefert das Doppelte, also 12 Passagen. Mehr Kontext "
                        + "brachte in den Messungen keine bessere Antwort, nur eine längere." },
          "rerank_candidates": { value: 48,
                     why: "muss über der zurückgelieferten Zahl liegen. Bei 24 bekommt der "
                        + "Reranker genauso viele Kandidaten, wie am Ende zurückgehen — "
                        + "er sortiert dann nur um, statt auszuwählen. Bei 48 tauscht er "
                        + "5 von 24 Passagen gegen besser bewertete." }
        })
        foreground: root.foreground; muted: root.muted; accent: root.accent; urgent: root.urgent
      }

      Text {
        width: parent.width
        text: "Retrieval bestimmt, was das Modell überhaupt zu sehen bekommt — meist "
            + "wirksamer als jede Anweisung im Prompt, weil das Modell ihr nicht folgen muss."
        color: root.muted
        font.family: OmaFont.face
        font.pixelSize: OmaFont.caption
        wrapMode: Text.WordWrap
      }

      Text {
        width: parent.width
        visible: root.flash.length > 0
        text: root.flash
        color: root.foreground
        font.family: OmaFont.face
        font.pixelSize: OmaFont.bodySmall
        wrapMode: Text.WordWrap
      }

      Item { width: 1; height: Style.spacing.xl }
    }
  }
}
