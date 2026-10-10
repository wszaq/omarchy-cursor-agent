# Collector reference

## Arguments

| Argument | Behavior |
| --- | --- |
| `--force` | Accepted for compatibility; collection always rescans |
| `--print` | Prints the JSON record |
| `--no-write` | Leaves the usage file unchanged; also prints any record |
| `--usage-dir PATH` | Overrides the usage output directory |
| `--cursor-home PATH` | Overrides the local Cursor data directory |

See [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L859-L879).
`--no-write` does not disable dashboard API calls. It controls the usage file write.
See [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L818-L822) and [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L852-L855).

## Paths

| Setting | Default |
| --- | --- |
| `CURSOR_HOME` or `CURSOR_DIR` | `~/.cursor` |
| `CURSOR_CONFIG_DIR` | Otherwise `Cursor` below `XDG_CONFIG_HOME`, or `~/.config/Cursor` |
| IDE database | `User/globalStorage/state.vscdb` below the Cursor config directory |
| `XDG_STATE_HOME` | `~/.local/state` |
| Usage output | `omarchy/agents/usage` below the state directory |

See [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L64-L94).

## Bounds

Dashboard calls have a 20-second timeout and an 8 MiB response cap.
Events use pages of 100, with at most 30 pages. Aggregations have a 500-record cap.
See [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L35-L45).
These caps limit collection. They do not guarantee complete account history.

## Inspect and test

From a working checkout, run:

```sh
./bin/collect-cursor --print --no-write
python3 -m unittest discover -s tests -v
omarchy plugin validate .
```

The CLI options come from [bin/collect_cursor.py](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/bin/collect_cursor.py#L859-L879).
The test and manifest commands come from [README.md](https://github.com/wszaq/omarchy-cursor-agent/blob/0f132f6eeb372c58a9306a3abbbc84165dcf9d82/README.md#L74-L80).
This documentation review did not execute collection, account requests, or application tests.
An empty output can mean that no activity was found. See [Architecture](Architecture.md#failure-behavior).
