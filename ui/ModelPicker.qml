import QtQuick
import qs.Commons
import qs.Ui
import "../common"

// One model role — what is set now, what is already on disk, and, only if you
// ask for it, what lilbee's catalogue and Hugging Face have.
//
// The two halves are deliberately unequal. The installed list costs nothing and
// reaches no network, so it opens with the row. The catalogue leaves the
// machine, so it waits behind a button and says so above it.
Column {
  id: root

  property var backend: null
  property string settingKey: ""      // chat_model, embedding_model, …
  property string task: ""            // chat, embedding, rerank, vision
  property string label: ""
  property bool open: false

  property color foreground: Color.menu.text
  property color muted: Color.muted
  property color accent: Color.accent
  property color urgent: Color.urgent

  signal choose(string key, string model)
  signal download(string model)

  readonly property var meta: root.backend ? root.backend.setting(root.settingKey) : null
  readonly property string current: root.meta && root.meta.value !== null ? String(root.meta.value) : ""

  // lilbee files the OCR model under task "chat", so a strict filter would show
  // an empty list for vision. Rather than hide everything, fall back to the
  // whole list and let the names speak.
  readonly property var installed: {
    var all = (root.backend && root.backend.installedModels) || []
    if (!root.task.length) return all
    var hits = []
    for (var i = 0; i < all.length; i++)
      if (String(all[i].task || "") === root.task) hits.push(all[i])
    return hits.length ? hits : all
  }

  // No `width` binding of its own: the caller sets it, and a component that
  // also binds its own width leaves every instance but the last at zero.
  spacing: Style.spacing.sm

  // ------------------------------------------------------------- the row
  Row {
    width: parent.width
    // Explicit: the children anchor to this row's vertical centre, so the row
    // cannot also take its height from them.
    height: Style.space(34)
    spacing: Style.spacing.md

    Text {
      width: Style.space(210)
      anchors.verticalCenter: parent.verticalCenter
      text: root.label.length ? root.label : root.settingKey
      color: root.foreground
      font.family: OmaFont.face
      font.pixelSize: OmaFont.bodySmall
    }

    Text {
      width: Style.space(300)
      anchors.verticalCenter: parent.verticalCenter
      text: root.current.length ? root.current : "not set"
      color: root.current.length ? root.foreground : root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.bodySmall
      elide: Text.ElideLeft
    }

    Button {
      anchors.verticalCenter: parent.verticalCenter
      text: root.open ? "close" : "change"
      onClicked: {
        root.open = !root.open
        // The list is local; fetching it on open costs a round trip and no
        // network, so there is no reason to make the user ask twice.
        if (root.open && root.backend && !root.backend.installedModels.length)
          root.backend.loadModels()
      }
    }
  }

  // --------------------------------------------------------- what is here
  Column {
    width: parent.width
    visible: root.open
    spacing: Style.spacing.sm
    leftPadding: Style.space(210)

    Text {
      text: "ON THIS MACHINE"
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.caption
      font.letterSpacing: 1.5
    }

    Repeater {
      model: root.installed

      Rectangle {
        width: root.width - Style.space(210)
        height: OmaFont.bodySmall + Style.spacing.md * 2
        color: hover.hovered ? Style.hoverFill : "transparent"
        radius: Style.cornerRadius

        HoverHandler { id: hover }
        TapHandler { onTapped: root.choose(root.settingKey, String(modelData.name || "")) }

        Text {
          anchors { left: parent.left; right: size.left; verticalCenter: parent.verticalCenter }
          anchors.leftMargin: Style.spacing.sm
          anchors.rightMargin: Style.spacing.md
          text: (String(modelData.name || "") === root.current ? "● " : "  ")
              + (String(modelData.display_name || "").length
                 ? modelData.display_name + "   " + modelData.name
                 : String(modelData.name || ""))
          color: String(modelData.name || "") === root.current ? root.accent : root.foreground
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
          elide: Text.ElideMiddle
        }

        Text {
          id: size
          anchors { right: parent.right; verticalCenter: parent.verticalCenter }
          anchors.rightMargin: Style.spacing.sm
          text: modelData.size_gb ? Number(modelData.size_gb).toFixed(1) + " GB" : String(modelData.source || "")
          color: root.muted
          font.family: OmaFont.face
          font.pixelSize: OmaFont.caption
        }
      }
    }

    // ------------------------------------------------------- the catalogue
    Text {
      width: parent.width - Style.space(210)
      text: "Searching asks lilbee, which asks Hugging Face. Nothing is downloaded "
          + "until you press Get."
      color: root.muted
      font.family: OmaFont.face
      font.pixelSize: OmaFont.caption
      wrapMode: Text.WordWrap
      topPadding: Style.spacing.md
    }

    Row {
      height: Style.space(34)
      spacing: Style.spacing.md

      TextField {
        id: search
        width: Style.space(240)
        anchors.verticalCenter: parent.verticalCenter
        onAccepted: if (root.backend) root.backend.browseCatalog(root.task, text)
      }
      Button {
        anchors.verticalCenter: parent.verticalCenter
        text: root.backend && root.backend.catalogLoading ? "searching…" : "Search catalogue"
        enabled: !(root.backend && root.backend.catalogLoading)
        onClicked: if (root.backend) root.backend.browseCatalog(root.task, search.text)
      }
    }

    Repeater {
      model: (root.backend && root.backend.catalog) || []

      Row {
        width: root.width - Style.space(210)
        height: Style.space(30)
        spacing: Style.spacing.md
        // Only what belongs to this role; catalog_browse is asked per task but
        // a broad search can still come back mixed.
        visible: !root.task.length || !modelData.task
                 || String(modelData.task) === root.task

        Text {
          width: parent.width - Style.space(190)
          anchors.verticalCenter: parent.verticalCenter
          text: String(modelData.name || modelData.model || modelData.id || "")
          color: root.foreground
          font.family: OmaFont.face
          font.pixelSize: OmaFont.bodySmall
          elide: Text.ElideMiddle
        }
        Text {
          width: Style.space(100)
          anchors.verticalCenter: parent.verticalCenter
          // The skill file is explicit: check compat before pulling.
          text: String(modelData.compat || "") === "supported" ? ""
              : (String(modelData.compat || "") === "unsupported" ? "unsupported" : "untested")
          color: String(modelData.compat || "") === "unsupported" ? root.urgent : root.muted
          font.family: OmaFont.face
          font.pixelSize: OmaFont.caption
        }
        Button {
          anchors.verticalCenter: parent.verticalCenter
          text: "Get"
          enabled: String(modelData.compat || "") !== "unsupported"
          onClicked: root.download(String(modelData.name || modelData.model || modelData.id || ""))
        }
      }
    }
  }
}
