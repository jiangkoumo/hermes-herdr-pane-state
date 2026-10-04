# hermes-herdr-pane-state

A [Herdr](https://herdr.dev) integration owned by Hermes Agent: Hermes reports
its own lifecycle to the pane it runs in, instead of Herdr guessing the state
off the screen. Built on
[Add Herdr support to your agent](https://herdr.dev/docs/add-herdr-support/) —
no Herdr release or pull request needed.

Herdr ships its own Hermes integration (`herdr integration install hermes`), and
Hermes is listed as a "supported by Herdr" agent: that integration only tells
Herdr which session the agent is in, and Herdr reads `idle` / `working` /
`blocked` out of the drawn screen. This plugin makes Hermes an agent that
supports Herdr itself.

| Herdr gets | From |
| --- | --- |
| `working` | a turn starts |
| `idle` | a turn finishes, or is interrupted |
| `blocked` | Hermes needs a decision — a command approval or a `clarify` question, with the reason as Herdr's `--message` |
| resume command | `hermes --resume <session-id>` (or `hermes --tui --resume <session-id>` in the TUI), so a Herdr server restart reopens the same conversation in the same pane |
| release | on exit, so the pane drops back to a plain shell |

Outside a Herdr pane (`HERDR_ENV != 1`) every callback is a no-op. Reports come
from a background thread with a 1 s timeout, newest state wins, failures are
dropped: a slow or absent Herdr never touches a turn.

## Install

```bash
hermes plugins install jiangkoumo/hermes-herdr-pane-state
hermes plugins enable herdr-pane-state
# one reporting source per pane: retire the integration Herdr installs itself
hermes plugins disable herdr-agent-state
hermes plugins doctor herdr-pane-state     # expect: registration passed, 11 hooks
```

Hermes clones the plugin into `~/.hermes/plugins/herdr-pane-state` and records
the pinned revision; `hermes plugins update herdr-pane-state` pulls a newer one.

For hacking on it, clone it yourself and symlink that checkout to
`~/.hermes/plugins/herdr-pane-state`, so edits land immediately instead of
drifting from a copy.

## Launch Hermes so Herdr can see it

Herdr identifies an agent by the pane's **foreground process**, and Hermes'
launcher execs into `python3`, so a bare `hermes` in a pane is invisible to
Herdr's detection: `herdr agent prompt` then refuses with *"agent … is no longer
the pane foreground process"*, and notifications and `herdr agent wait` do not
work either. Declare the identity on the launch:

```bash
HERDR_AGENT=hermes hermes
```

`HERDR_AGENT` tells Herdr that this foreground process is the known agent
`hermes`. Herdr's docs warn against exporting it globally — that would claim
every process inheriting it, including other agents started in the same shell —
so wrap just the command. For zsh (only inside Herdr panes, removable as one
block):

```zsh
if [ -n "$HERDR_ENV" ]; then
  hermes() { HERDR_AGENT=hermes command hermes "$@" }
fi
```

## Verify

Inside a pane:

```bash
herdr agent get "$HERDR_PANE_ID"      # agent=hermes, status=done|working|blocked
herdr agent explain "$HERDR_PANE_ID"  # detection detail for that pane
herdr agent wait "$HERDR_PANE_ID" --until blocked --timeout 60000
```

After a Herdr server restart the pane reopens in its directory and runs the
reported resume command; `~/.config/herdr/session.json` holds it under the
pane's `agent_resume` (source `hermes:pane-state`).

## Development

Tests need neither Herdr nor the Hermes runtime: `HERDR_BIN_PATH` points at a
fake binary that records every report, so the assertions are on the exact argv
Hermes sends.

```bash
python3 -m unittest discover -s tests -v
```

Twelve tests cover the outside-Herdr no-op, the startup pane claim, the turn
transitions, TUI vs CLI resume commands, approval and `clarify` blocking, message
truncation, session-id validation, gateway/cron surfaces being ignored, release
ordering and the strictly rising `--seq`.

## Known limits

- **Do not run two integrations at once.** With `herdr-agent-state` (the plugin
  Herdr installs itself) enabled alongside this one, the pane stopped following
  the agent's reports — measured: `idle` for a whole turn, never `working`.
  Herdr treats its own integration as authoritative for a pane it ships support
  for, so the choice is accurate state (this plugin) or the display-only session
  id (Herdr's). Restoration does not depend on it: Herdr replays `agent_resume`.
- **The session id field stays empty.** Herdr records an agent's session
  identity (`agent_session`) only from its own `herdr:`-prefixed sources; a
  third-party `report-agent-session` is accepted (`{"type":"ok"}`) and ignored.
  The plugin sends it anyway, so nothing has to change if that widens.
- **Screen-based `blocked` detection is superseded, not extended**: while this
  plugin reports, Herdr prefers the reports over the agent's detection manifest.
- Panes running Hermes subcommands (`hermes sessions list`, `hermes cron …`)
  never claim the pane — only an interactive launch does, so short commands do
  not make an agent flicker in and out of the sidebar.
- The resume command needs Herdr 0.9.2+; older versions ignore it, state reports
  and release still work.

## License

MIT — see [LICENSE](LICENSE).
