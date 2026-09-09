import QtQuick
import qs.Commons
import qs.Ui

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
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        font.letterSpacing: 1.5
      }

      Grid {
        columns: 2
        columnSpacing: Style.spacing.xxl
        rowSpacing: Style.spacing.sm

        Text {
          text: "lilbee"
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        Text {
          text: root.lilbeeVersion || "…"
          color: root.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          text: "Server"
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        Text {
          text: root.backend && root.backend.port > 0
              ? "127.0.0.1:" + root.backend.port
                + (root.backend.ownsDaemon ? "  ·  started by OMA" : "  ·  already running")
              : "not connected"
          color: root.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          text: "Library"
          color: root.muted
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        Text {
          text: root.backend && root.backend.totalChunks >= 0
              ? root.backend.documents.length + " documents  ·  " + root.backend.totalChunks + " chunks"
              : "—"
          color: root.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
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
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      Text {
        width: parent.width
        text: "“Check for updates” asks GitHub through mise. Nothing here contacts the "
            + "network on its own."
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      // ------------------------------------------------------------- OMA
      Text {
        text: "OMA"
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        font.letterSpacing: 1.5
      }

      Row {
        spacing: Style.spacing.md

        Text {
          width: Style.space(210)
          anchors.verticalCenter: parent.verticalCenter
          text: "backend when closed"
          color: root.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
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
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
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
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }

      Row {
        spacing: Style.spacing.md

        Text {
          width: Style.space(210)
          anchors.verticalCenter: parent.verticalCenter
          text: "text size"
          color: root.foreground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        ButtonGroup {
          options: [
            { value: "0.9",  label: "Small" },
            { value: "1.0",  label: "Normal" },
            { value: "1.15", label: "Large" },
            { value: "1.3",  label: "Larger" }
          ]
          value: String(root.fontScale)
          onChanged: function(v) { root.omaSettingChanged("omaFontScale", parseFloat(v)) }
        }
      }

      Text {
        width: parent.width
        text: "Text size applies to OMA alone. The shell's own font is left as it is."
        color: root.muted
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      // -------------------------------------------------------- lilbee keys
      SettingGroup {
        width: column.width
        title: "Models"
        keys: ["chat_model", "embedding_model", "reranker_model", "vision_model"]
        backend: root.backend
        foreground: root.foreground; muted: root.muted; accent: root.accent; urgent: root.urgent
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
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      SettingGroup {
        width: column.width
        title: "Retrieval"
        keys: ["top_k", "rerank_candidates", "diversity_max_per_source", "max_distance"]
        backend: root.backend
        foreground: root.foreground; muted: root.muted; accent: root.accent; urgent: root.urgent
      }

      Text {
        width: parent.width
        visible: root.flash.length > 0
        text: root.flash
        color: root.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }

      Item { width: 1; height: Style.spacing.xl }
    }
  }
}
