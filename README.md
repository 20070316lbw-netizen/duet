# duet

> Multi-agent coding assistant with a **Planner** + **Supervisor** loop.
> Each agent has its own conversation thread with the user — Supervisor reviews
> Planner's plans without sharing context. Tools cover file read/write, shell,
> and live `watch` (compile/test/log tailing). Runs as a terminal TUI **or** a
> local Web UI.

## Install (dev)

```bash
cd duet
uv venv .venv
source .venv/bin/activate
uv pip install -e .              # CLI / TUI only
uv pip install -e '.[web]'       # + FastAPI Web UI
```

## Configure

```bash
mkdir -p ~/.duet
cp config.example.toml ~/.duet/config.toml
# edit ~/.duet/config.toml — or set <PROVIDER>_API_KEY env vars
```

API keys can also be entered at runtime — they live only in process memory and
are never written to disk.

## Run

### Terminal (TUI)

```bash
cd ~/your-project
duet                    # interactive provider/key picker → main TUI
duet --resume <id>      # restore a previous session
```

### Web UI (browser)

```bash
duet --web              # → http://127.0.0.1:7878
duet --web --web-port 9000
```

Two-column chat (Planner | Supervisor), unified input, slash autocomplete,
side panel for file diffs / shell output / approval prompts.

## Slash commands

| | |
|---|---|
| `/help`            | list commands |
| `/status`          | planner/supervisor models + state |
| `/cost`            | session token usage / cost |
| `/poor`            | reset TokenGuard |
| `/agent <n> on/off`| toggle planner / supervisor |
| `/watch list`      | running watchers |
| `/clear`           | clear in-memory history (transcript on disk kept) |
| `/memory [add ...]`| project / user memory |
| `/forget <id>`     | drop a memory |
| `/sessions`        | list past sessions |
| `/copy [planner\|supervisor]` | copy last agent output to clipboard |
| `/trust ...`       | auto-approve rules for write / shell |
| `/exit`            | quit |

Routing: prefix with `@planner`, `@supervisor`, or `@all`.

## Layout

```
duet/
├── cli.py              # entry — argparse, --resume, --web
├── config.py           # ~/.duet/config.toml + env override + key scanner
├── launcher.py         # interactive provider/key picker
├── memory.py           # project / user memory store (SQLite)
├── trust.py            # /trust rules — auto-approve writes / shells
├── clipboard.py        # /copy
├── cost.py             # token tracking + TokenGuard
├── errors.py           # human-readable error formatting
├── sessions.py         # list / replay sessions (deep replay incl. tool_use)
├── slash.py            # /-command registry
├── tui/                # Textual app: Lobby pane + Diff pane
├── web/                # FastAPI server + vanilla-JS frontend
│   ├── server.py
│   ├── app_adapter.py  # WebApp ⇄ Textual DuetApp duck-type adapter
│   └── static/
├── lobby/              # transcript (DAG, branch_id) + router (@-parser)
├── agents/             # base.run() loop + planner + supervisor
├── providers/          # base.stream_events() + anthropic + openai_compat
├── tools/              # read/list/grep/write/edit/shell + watch + runner
├── safety/             # path sandbox + snapshots + secret-scanner
└── prompts/            # planner.md / supervisor.md
```

See `../方案.md` for the full design notes.
