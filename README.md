# switchboard

A map of the git repos under `~/workspace`, plus the Claude Code sessions running in them across hosts.

- **Architecture tab:** each repo is a card, and submodules or nested repos sit inside their parent's box. References between repos read left to right: a repo naming another one, or depending on its package. Each link has a short editable label (`labels.json`), and `case-demos` is the hub. Live sessions show as small squares on each card: red is waiting for input, orange is working, green is idle.
- **Sessions tab:** one row per repo that has a live session and one column per host. Arcs on the left show which running sessions *should* talk, based on how the repo map connects their repos. Click a session to copy its `claude --resume` command; for a remote host it's wrapped in `ssh -t`.

## Run

    python3 scan.py        # scan ~/workspace (about a minute); existing card positions never move
    python3 serve.py       # http://127.0.0.1:8767  (saves edits, serves live sessions)

**Sessions board in cc-menubar (macOS):** choose **Sessions board…** (⌘B) in the cc-menubar dropdown, or open
CCMenubar.app again. The page opens in a native window. cc-menubar pushes sessions and room state on every poll, so
no `serve.py` is needed. Clicking a session jumps to its tmux pane, and edits are saved through the app.

You can also open `index.html` directly. Without the server, edits are kept in the browser only and there are no live sessions.

| Command | What it does |
|---|---|
| `scan.py --render-only` | Rebuild `graph.js` and `vault/` from the last scan |
| `scan.py --relayout` | Recompute the layered layout (same as **Tidy layout** in the page) |

## Files

| File | Tracked | Contents |
|---|---|---|
| `config.json` | yes | `center` (hub repo), `exclude` (removed repos), optional `ccMenubar` path |
| `labels.json` | yes | Short label for each `"source>target"` link |
| `layout.json` | yes | Card positions; written by layout runs and by dragging |
| `graph.json`, `graph.js`, `vault/` | no | Scan output; `vault/` is an Obsidian-style note per repo |

## Live sessions

Session data comes from [cc-menubar](../cc-menubar), which already polls every host over SSH. On each poll it writes `~/.cache/cc-menubar/sessions.json`. If that file is more than 15 s old (the menu bar app isn't running), `serve.py` refreshes it with `cc-menubar.py --snapshot`. A session belongs to the repo whose name appears as a folder in its working directory, so paths on remote hosts match too.

## Tasks tab

`tasks/` puts your own task list next to every repo's `TODO.md`, one card per project.

- **Two views.** **Yours** is one list of every task you wrote, across projects: a "next up" top 3, counts
  (open, doing, ★ high, late, due this week, done this week), project filters and search, then In progress and Next, with
  Closed in a column on the right (the **closed** box hides it), grouped by week: this week and last week open,
  older weeks collapsed. Click a checkbox there to reopen a task. Ordering is doing first, then ★, then due date, then oldest. **Repo boards** is one card per project, with your
  tasks next to that repo's TODO files and live sessions.
- **Your tasks** live in `tasks/tasks.md` (first imported from Todolist's CSV export: `tasks.py import-todoist <csv>`).
  In the tab: add (start with `! ` for high priority, `- ` to skip Claude's review, or both: `! - …`), tick, ☆ star, and click a task for Start/Pause, a due date, moving
  it to another project, editing the text, or dropping it. Project headings link to repos (`## OCR · repo: ocr-bench`).
  **Skip review** (or the `- ` prefix) marks a task `review:skip`: it is never reviewed automatically, but **Review now**
  still works. In Yours, each section is grouped by project.
  Dates, priority and the skip mark are kept in the item's comment: `<!-- t:id pri:high review:skip due:2026-12-01 added:… -->`.
- **Claude reviews each task you add:** is it clear (with up to 2 questions), a one-line restatement, a 3–5 line
  proposal, the files involved, size S/M/L, and whether it needs a GPU (then it names the RTX host). This runs
  `claude -p` locally in the repo with read-only tools and the room hooks off, about 15 s and $0.10–0.15 a task.
  Results go to `tasks/reviews.json`. **Send to sessions** posts a task and its proposal to that repo's live
  sessions through the room.
- **Repo TODO files** (any `*TODO.md` that git tracks or would track, so a new one counts before its first commit; also in
  repos the last scan hasn't seen yet) are re-read each time the tab opens. They are only read, never written: their open and
  in-progress items, counts, and a flag on files that aren't in the shared format. Click an item to open the file.
- **The shared format** is `tasks/TODO_FORMAT.md`. To have sessions keep a repo's board in it, add one line to that repo's
  `CLAUDE.md`: `Keep TODO.md in the format of ~/workspace/switchboard/tasks/TODO_FORMAT.md.`

    python3 tasks/tasks.py add|set|edit|review|send|collect …   (the tab drives these too)

## Room: sessions messaging each other across hosts

`room/` has a small mailbox for Claude Code sessions. One server (`room/room_server.py`, HTTP + SQLite, stdlib only) runs on the
Mac Studio. Each host gets the `room` CLI (`~/.local/bin/room`) and five hooks in `~/.claude/settings.json`. Nothing
controls a session; sessions only post and receive.

Claude Code's own messaging (SendMessage) is direct and wakes the recipient, so the session-start hint tells sessions
to use it first, and the room when the target is unreachable that way (another host, not in the session's list). The
room is also for links, `repo:`/`host:`/`all`, and threads others should be able to read.

- **Zero tokens when quiet.** Hooks print nothing unless there's a message. Each session gets one hint line at start,
  plus one line per link it belongs to.
- **Delivery:** at the next prompt (`UserPromptSubmit`), mid-turn after a tool call (`PostToolUse`, at most every
  15 s), and when a turn ends (`Stop`). An `ask` or a `reply` keeps the turn going so it gets handled. An idle session
  only reads messages when you next use it, so cc-menubar shows ✉N and sends a notification, unless it runs
  `room listen` in the background: that ends when an ask or reply arrives and wakes the session (no tokens until then).
- **Links** group sessions or whole repos on one topic. Joining as a repo covers every future session there. A repo
  member's `--watch` globs (repo-relative) post an automatic "[auto] X edited <file>" notice to the link, at most
  once per file per 30 min.
- **Same-file heads-up:** if another live session edited the same file in the last 20 minutes, the editor is told.
- Sessions find their own id by walking up the process tree to Claude Code's `~/.claude/sessions/<pid>.json`.

### Common commands

`room help` lists everything, grouped by who uses it; `room <command> -h` shows one command in detail.

| Command | What it does |
|---|---|
| `room who` | Live sessions on every host: `name@host`, id, repo, client version (`?` = older than 0.1.4), state, unread `✉N`, `(listen)` or `(wait #N)` while one runs, links, status. Exited sessions show for 24 h; `--all` shows every session |
| `room send <to> "msg"` | `<to>` is `name@host` as `who` prints it, an id prefix, `link:NAME`, `repo:REPO`, `host:NAME` or `all`. A single session on your own host is refused (use Claude Code's own messaging); `--local` overrides when that can't reach it |
| `room send <to> "msg" --ask` | A question: the recipient's turn keeps going until it answers |
| `room send <to> --re <id> "msg"` | Answer message `#id` |
| `room wait <id>` | Block until `#id` is answered. Run in the background, it wakes the session when the answer arrives. Both sides run it after each reply while a topic goes on, so the exchange carries on until it is done |
| `room listen` | Keep an idle session reachable: run in the background, it ends (and wakes the session) when an ask or reply arrives; restart it after each. No tokens while waiting. `--all` wakes on every message |
| `room inbox [--peek]` | Your unread messages. `--session name@host` peeks at another session's, leaving them unread |
| `room thread [LINK]` | Recent messages, all or one link's |
| `room status "now: …"` | One line on what this session is doing, shown in `who` |
| `room link list \| create \| join \| leave` | Topic groups; `join NAME --repo --watch 'glob'` covers a whole repo and notifies the link when matching files change |
| `room update` | Upgrade a PyPI install (pipx, uv tool or pip). Hosts in cc-menubar's config get `room` from `deploy.sh` instead |
| `room join <url> <token> --host NAME` | Set up a new machine: config, server check, hooks |
| `room serve` | Run a room server (the first run makes a token); `--service` prints a launchd/systemd unit |

| Task | How |
|---|---|
| Update all hosts | `room/deploy.sh [--restart]` (hosts from cc-menubar's config; server host `MS`) |
| Someone else joins (any host on the LAN) | `pipx install switchboard-room && room join http://<MS-IP>:8766 <token> --host <name>` (see `room/README.md`). The token is `"token"` in `~/.config/switchboard/room.json` |
| Release to PyPI | Only for changes worth a release (small fixes ride along with the next one). Bump `__version__` in `room/room.py`, commit, `room/deploy.sh`, then `git push origin main && git tag v<version> && git push origin v<version>`; `.github/workflows/publish.yml` publishes with trusted publishing. Colleagues' first session after that is told to run `room update`; to push it now, `room send all "room <version> is out: run room update"` |
| Server on MS | `room/room_server.py`; cron runs `ensure-server.sh` (from `room/`, deployed to `~/.local/share/switchboard/`) every 5 min and at boot. Data in `~/.local/share/switchboard/room.db` |
| Config on each host | `~/.config/switchboard/room.json` `{url, token, host}` (mode 600). The server address is MS's LAN IP |
| Remove hooks | Restore `~/.claude/settings.json.bak-switchboard`, or delete the `room hook` entries |
