import QtQuick
import Quickshell.Io
import Quickshell.Wayland

Item {
  id: root
  // The launcher allows graceful controller cleanup when this service unloads.
  property var shell: null
  property var manifest: null
  property var state: ({ ready: false, autoBrightness: false, autoColor: false, offset: 0 })
  property string processError: ""
  property bool shuttingDown: false
  readonly property string helper: decodeURIComponent(String(Qt.resolvedUrl("launcher.py")).replace(/^file:\/\//, ""))

  function command(values) {
    if (!backend.running) return
    backend.write(JSON.stringify(values) + "\n")
  }
  function setBrightness(value) { command({ autoBrightness: value }) }
  function setColor(value) { command({ autoColor: value }) }
  function setOffset(value) { command({ offset: Math.round(value) }) }

  IdleMonitor {
    id: idle
    // Pause before the configured screen dim/blank actions can run.
    timeout: 120
    respectInhibitors: true
    onIsIdleChanged: root.command({ idle: isIdle })
  }

  Process {
    id: backend
    command: ["python", "-u", root.helper]
    stdinEnabled: true
    running: true
    onStarted: root.command({ idle: idle.isIdle })
    stdout: SplitParser {
      onRead: function(line) {
        try {
          root.state = JSON.parse(line)
          root.processError = ""
        } catch (e) { root.processError = "Could not read controller status" }
      }
    }
    stderr: SplitParser { onRead: function(line) { root.processError = String(line) } }
    onExited: function(code) {
      if (!root.shuttingDown) {
        root.processError = "Display controller stopped (" + code + ")"
        restartTimer.restart()
      }
    }
  }
  Timer { id: restartTimer; interval: 3000; onTriggered: backend.running = true }

  IpcHandler {
    target: "andrewf.ambient-display.control"
    function status(): string { return JSON.stringify(root.state) }
    function brightness(enabled: bool): void { root.setBrightness(enabled) }
    function color(enabled: bool): void { root.setColor(enabled) }
    function offset(value: int): void { root.setOffset(value) }
  }

  Component.onDestruction: {
    root.shuttingDown = true
    // EOF and SIGTERM both make the controller restore its owned settings.
    if (backend.running) backend.write('{"quit":true}\n')
  }
}
