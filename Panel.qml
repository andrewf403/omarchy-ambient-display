import QtQuick
import Quickshell
import qs.Ui
import qs.Commons

Panel {
  id: root
  moduleName: "andrewf.ambient-display"
  ipcTarget: moduleName
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight
  readonly property var service: bar && bar.shell ? bar.shell.serviceFor(moduleName) : null
  readonly property var state: service ? service.state : ({})
  readonly property bool ready: service !== null && state.ready === true
  readonly property string errorText: service ? (service.processError || state.error || "") : "Starting controller…"
  property int selected: 0
  readonly property string sensorText: state.lux === null || state.lux === undefined
    ? "Waiting for light sensor…"
    : Number(state.lux).toFixed(1) + " lux · " + Math.round(state.ambientKelvin) + " K ambient"

  function activate() {
    if (!ready) return
    if (selected === 0) service.setBrightness(!state.autoBrightness)
    else if (selected === 2) service.setColor(!state.autoColor)
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰃟"
    active: root.state.autoBrightness === true || root.state.autoColor === true
    tooltipText: "Ambient Display\n" + root.sensorText + (root.errorText ? "\n" + root.errorText : "")
    onPressed: function(mouseButton) { if (mouseButton === Qt.LeftButton) root.toggle() }
  }

  KeyboardPanel {
    id: popup
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keys
    contentWidth: popup.fittedContentWidth(Style.space(330))
    contentHeight: popup.fittedContentHeight(content.implicitHeight)

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: root.activate()
      onMoveRequested: function(dx, dy) {
        if (dy) root.selected = Math.max(0, Math.min(2, root.selected + dy))
        else if (dx && root.selected === 1 && root.ready)
          root.service.setOffset(Math.max(-40, Math.min(40, Number(root.state.offset || 0) + dx * 2)))
      }

      Column {
        id: content
        width: parent.width
        spacing: Style.space(12)

        Text {
          text: "Ambient Display"
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.title
          font.bold: true
          color: root.bar.foreground
        }
        Text {
          width: parent.width
          text: root.sensorText
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          color: Qt.darker(root.bar.foreground, 1.4)
        }
        Toggle {
          width: parent.width
          label: "Auto brightness"
          description: root.state.autoBrightness
            ? (root.state.idle ? "Paused while idle" : "Screen " + Math.round(root.state.brightness || 0) + "% · follows room light")
            : "Off · manual brightness"
          checked: root.state.autoBrightness === true
          enabled: root.ready && root.state.brightnessAvailable === true
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          hasCursor: root.selected === 0
          onHovered: function(h) { if (h) root.selected = 0 }
          onClicked: root.service.setBrightness(!root.state.autoBrightness)
        }
        Column {
          width: parent.width
          spacing: Style.space(6)
          Text {
            text: "Brightness adjustment: " + (adjust.liveValue > 0 ? "+" : "") + Math.round(adjust.liveValue) + " points"
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            color: root.bar.foreground
          }
          PanelSlider {
            id: adjust
            width: parent.width
            bar: root.bar
            minimum: -40
            maximum: 40
            step: 2
            integer: true
            value: Number(root.state.offset || 0)
            enabled: root.ready
            onReleased: function(value) { root.service.setOffset(value) }
            HoverHandler { onHoveredChanged: if (hovered) root.selected = 1 }
          }
          Text {
            text: "Darker                           Brighter"
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
            color: Qt.darker(root.bar.foreground, 1.5)
          }
        }
        PanelSeparator { foreground: root.bar.foreground }
        Toggle {
          width: parent.width
          label: "Auto color temperature"
          description: root.state.colorPaused ? "Paused by a manual color change"
            : root.state.autoColor ? (root.state.idle ? "Paused while idle" : Math.round(root.state.targetKelvin || 6500) + " K · follows room light")
            : "Off · natural panel color"
          checked: root.state.autoColor === true
          enabled: root.ready && root.state.colorAvailable === true
          foreground: root.bar.foreground
          fontFamily: root.bar.fontFamily
          hasCursor: root.selected === 2
          onHovered: function(h) { if (h) root.selected = 2 }
          onClicked: root.service.setColor(!root.state.autoColor)
        }
        Text {
          width: parent.width
          visible: root.errorText !== ""
          text: root.errorText
          wrapMode: Text.WordWrap
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.caption
          color: root.bar.urgent
        }
      }
    }
  }
}
