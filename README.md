# switchboard

A map of the git repos under `~/workspace`, plus the Claude Code sessions running in them across hosts.

- **Architecture tab:** each repo is a card, and submodules or nested repos sit inside their parent's box. References between repos read left to right: a repo naming another one, or depending on its package. Each link has a short editable label (`labels.json`), and `case-demos` is the hub. Live sessions show as small squares on each card: red is waiting for input, orange is working, green is idle.
- **Sessions tab:** one row per repo that has a live session and one column per host. Arcs on the left show which running sessions *should* talk, based on how the repo map connects their repos. Click a session to copy its `claude --resume` command; for a remote host it's wrapped in `ssh -t`.

## Run

    python3 scan.py        # scan ~/workspace (about a minute); existing card positions never move
    python3 serve.py       # http://127.0.0.1:8765  (saves edits, serves live sessions)

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

## Room: sessions messaging each other across hosts

`room/` has a small mailbox for Claude Code sessions. One server (`room/server.py`, HTTP + SQLite, stdlib only) runs on the
Mac Studio. Each host gets the `room` CLI (`~/.local/bin/room`) and five hooks in `~/.claude/settings.json`. Nothing
controls a session; sessions only post and receive.

- **Zero tokens when quiet.** Hooks print nothing unless there's a message. Each session gets one hint line at start,
  plus one line per link it belongs to.
- **Delivery:** at the next prompt (`UserPromptSubmit`), mid-turn after a tool call (`PostToolUse`, at most every
  15 s), and when a turn ends (`Stop`). An `ask` or a `reply` keeps the turn going so it gets handled. An idle session
  only reads messages when you next use it, so cc-menubar shows ✉N and sends a notification.
- **Links** group sessions or whole repos on one topic. Joining as a repo covers every future session there. A repo
  member's `--watch` globs (repo-relative) post an automatic "[auto] X edited <file>" notice to the link, at most
  once per file per 30 min.
- **Same-file heads-up:** if another live session edited the same file in the last 20 minutes, the editor is told.
- Sessions find their own id by walking up the process tree to Claude Code's `~/.claude/sessions/<pid>.json`.

    room who | send <to> "msg" [--ask] | send <to> --re ID "msg" | wait ID | inbox | status "…" | link … | thread [NAME]

Links now: `ocr-accuracy` (ocr-bench ⇄ keystone-ai-system/ai-core, watching `*ocr*`) and `meeting-asr`
(qwen_asr_web `server.py`/`web_server.py` ⇄ case-demos `toolbox/meeting/server/*`).

| Task | How |
|---|---|
| Update all hosts | `room/deploy.sh [--restart]` (hosts from cc-menubar's config; server host `MS`) |
| Server on MS | cron runs `~/.local/share/switchboard/ensure-server.sh` every 5 min and at boot. Data in `~/.local/share/switchboard/room.db` |
| Config on each host | `~/.config/switchboard/room.json` `{url, token, host}` (mode 600). The server address is MS's LAN IP |
| Remove hooks | Restore `~/.claude/settings.json.bak-switchboard`, or delete the `room hook` entries |
