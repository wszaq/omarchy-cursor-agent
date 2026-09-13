import QtQuick
import Quickshell
import Quickshell.Io

// Service side of wszaq.cursor-agent. Runs bin/collect-cursor once shortly
// after shell start and then on an interval. The stock omarchy.agents panel
// picks up the cursor.json record from the shared usage directory.
Item {
  id: root

  readonly property string pluginDir: Quickshell.env("HOME") + "/.config/omarchy/plugins/wszaq.cursor-agent"
  readonly property string engine: pluginDir + "/bin/collect-cursor"
  readonly property int refreshIntervalSec: 900

  Process {
    id: collectorProcess
    running: false

    readonly property int maxErrBytes: 1048576
    property int errBytes: 0

    stderr: SplitParser {
      id: errParser
      splitMarker: ""
      property string buf: ""

      onRead: function(data) {
        if (collectorProcess.stderr === null) return
        collectorProcess.errBytes += data.length
        errParser.buf += data
        if (collectorProcess.errBytes > collectorProcess.maxErrBytes ||
            errParser.buf.length > collectorProcess.maxErrBytes) {
          collectorProcess.stderr = null
          console.warn("wszaq.cursor-agent", "stderr cap exceeded; detached")
          return
        }
        while (true) {
          const idx = errParser.buf.indexOf("\n")
          if (idx < 0) break
          const line = errParser.buf.slice(0, idx)
          errParser.buf = errParser.buf.slice(idx + 1)
          if (line.trim() !== "") console.warn("wszaq.cursor-agent", line.trim())
        }
      }
    }

    onExited: function(exitCode) {
      if (exitCode !== 0)
        console.warn("wszaq.cursor-agent", "collector exited with code", exitCode)
    }
  }

  Timer {
    interval: root.refreshIntervalSec * 1000
    running: true
    repeat: true
    triggeredOnStart: false
    onTriggered: root.run()
  }

  Timer {
    interval: 15000
    running: true
    repeat: false
    onTriggered: root.run()
  }

  function run() {
    if (collectorProcess.running) return
    collectorProcess.errBytes = 0
    errParser.buf = ""
    if (collectorProcess.stderr === null) collectorProcess.stderr = errParser
    collectorProcess.command = [root.engine]
    collectorProcess.running = true
  }
}
