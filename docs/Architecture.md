# Service architecture

At this review, [GitHub](https://github.com/wszaq/omarchy-cursor-agent) did not mark this repository as a fork.

The [README](../README.md) covers installation and the optional Cursor icon overlay.
This plugin supplies usage data to the existing Agents panel. It does not add a separate bar widget.
See [manifest.json](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/manifest.json#L8-L16).

## Collection flow

`Main.qml` starts `bin/collect-cursor` 15 seconds after startup.
It repeats collection every 900 seconds. It skips a new run while the previous collector is active.
See [Main.qml](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/Main.qml#L11-L19) and [Main.qml](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/Main.qml#L53-L75).
The shell launcher forwards its arguments to `collect_cursor.py`. See [bin/collect-cursor](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect-cursor#L1-L3).

The Python collector reads local transcripts and the tracking database.
It reads an IDE access token from the local Cursor database when available.
With that token, it requests usage from the Cursor dashboard API.
It merges the results into a shared usage record. See [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L807-L856).

## Local and remote data differ

Local records supply prompt and session counts. Dashboard responses supply token totals, model details, and included-usage limits.
The database opens in read-only mode. The collector queries the access-token field without changing the IDE database.
See [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L1-L10) and [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L252-L277).
Do not share the database or its token when you report a fault.

## Failure behavior

No detected Cursor installation, a disabled provider, or no activity can produce no record.
This is a successful empty result, not necessarily a collector crash.
See [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L799-L805), [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L841-L856), and [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L874-L879).
QML reports nonzero collector exit codes. Its stderr buffer has a 1 MiB cap.
See [Main.qml](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/Main.qml#L19-L49).

Read [Reference](Reference.md) for arguments, paths, and limits.
