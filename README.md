# wszaq.cursor-agent

Omarchy **service** plugin that collects Cursor IDE/CLI activity and writes a
stock-contract usage record for the built-in `omarchy.agents` bar panel.

Cursor appears as a third subscription chip next to Claude and Codex. There is
no separate bar widget.

## What it shows

| Field | Source |
|-------|--------|
| Sessions / prompts / active days | Local transcripts + `ai-code-tracking.db`, enriched by dashboard usage events |
| Tokens by day / by model | Cursor dashboard API (`GetFilteredUsageEvents`, `GetAggregatedUsageEvents`) via the IDE access token in `state.vscdb` |
| Included-usage limits | `GetCurrentPeriodUsage` (percent of included pool + billing-cycle reset) |
| Plan tier | `GetPlanInfo` / `cursor-agent about` |

Open Cursor at least once while signed in so `~/.config/Cursor/User/globalStorage/state.vscdb` has an access token. Without that token the panel still shows local prompt/session counts, but tokens and limits stay empty.

## Install

```bash
omarchy plugin add file:///home/wszaq/Work/_WAJBKODING/omarchy-cursor-agent --enable --yes
omarchy-shell shell rescanPlugins
```

Or from another machine after pushing the repo:

```bash
omarchy plugin add <git-url> --enable --yes
```

Smoke test:

```bash
~/.config/omarchy/plugins/wszaq.cursor-agent/bin/collect-cursor --print
ls ~/.local/state/omarchy/agents/usage/cursor.json
```

### Cursor mark in the Agents panel

The stock panel only loads marks from its own `assets/` directory. Overlay the
mark after cloning the agents plugin:

```bash
omarchy plugin clone omarchy.agents
cp ~/.config/omarchy/plugins/wszaq.cursor-agent/assets/cursor.svg \
   ~/.config/omarchy/plugins/wszaq.agents/assets/cursor.svg
cp ~/.config/omarchy/plugins/wszaq.cursor-agent/assets/cursor-light.svg \
   ~/.config/omarchy/plugins/wszaq.agents/assets/cursor-light.svg
```

Without the overlay, Cursor still appears; the panel uses the generic bar glyph.

## Disable / uninstall

```bash
# Hide Cursor in the Agents panel only
omarchy bar set omarchy.agents providers '{
  "claude": { "enabled": true },
  "codex": { "enabled": true },
  "fireworks": { "enabled": true },
  "cursor": { "enabled": false }
}' --json

# Remove the service plugin
omarchy plugin remove wszaq.cursor-agent --yes
rm -f ~/.local/state/omarchy/agents/usage/cursor.json
```

## Development

```bash
python3 -m unittest discover -s tests -v
./bin/collect-cursor --print --no-write --cursor-home /path/to/fixture
```

## License

MIT. Cursor mark from [Simple Icons](https://simpleicons.org/icons/cursor.svg)
(same source Omarchy uses for font glyph `U+E90D`).
