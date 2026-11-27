#!/usr/bin/env python3
"""room: let Claude Code sessions on any host message each other through the switchboard room server.

Run `room help` for the commands. Hooks call `room hook <Event>`. They print nothing (no tokens) unless
there is something to deliver, and never fail the session.
Config: ~/.config/switchboard/room.json {"url": "http://<server>:8766", "token": "...", "host": "<label>"}
(`room join <url> <token> [--host NAME]` writes it and installs the hooks). Python 3.9+, stdlib only.
"""
__version__ = "0.1.3"

import glob, json, os, re, socket, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

CFG_PATH = Path(os.environ.get("ROOM_CONFIG") or Path.home() / ".config" / "switchboard" / "room.json")
CACHE = Path.home() / ".cache" / "switchboard"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
INBOX_EVERY = 15          # seconds between inbox checks from PostToolUse
PKG = "switchboard-room"
CHECK_EVERY = 12 * 3600   # seconds between PyPI checks for a newer room


def _exe():
    """This command's own path (pipx/uv tool, the deploy.sh symlink, or room.py itself), so hooks and hints call it."""
    p = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if not os.path.basename(p).startswith("room"):
        import shutil
        p = shutil.which("room") or str(Path.home() / ".local" / "bin" / "room")
    return p


EXE = _exe()
ROOM = "~" + EXE[len(str(Path.home())):] if EXE.startswith(str(Path.home()) + os.sep) else EXE

def cfg():
    try:
        return json.loads(CFG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def api(method, path, body=None, timeout=4):
    c = cfg()
    if not c.get("url"):
        raise RuntimeError(f"not configured: {CFG_PATH}")
    req = urllib.request.Request(c["url"].rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + c.get("token", ""), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", str(e))
        except ValueError:
            msg = str(e)
        raise RuntimeError(msg)


def host_label():
    return cfg().get("host") or socket.gethostname().split(".")[0]


# ---- this session -------------------------------------------------------------------------------------
def registry():
    out = {}
    for f in glob.glob(str(Path.home() / ".claude" / "sessions" / "*.json")):
        try:
            d = json.loads(Path(f).read_text())
            out[int(d.get("pid") or Path(f).stem)] = d
        except (OSError, ValueError):
            pass
    return out


def current_session():
    """The Claude Code session this process runs under: walk up parent pids to one in ~/.claude/sessions."""
    if os.environ.get("ROOM_SESSION"):
        return {"sessionId": os.environ["ROOM_SESSION"], "cwd": None, "name": ""}   # keep its registered folder
    reg, pid = registry(), os.getppid()
    for _ in range(12):
        if pid in reg:
            return reg[pid]
        try:
            pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip() or 0)
        except ValueError:
            pid = 0
        if pid <= 1:
            break
    return None


def session_name(sid):
    for d in registry().values():
        if d.get("sessionId") == sid:
            return d.get("name") or ""
    return ""


def git_top(path):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    try:
        r = subprocess.run(["git", "-C", d or "/", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=2)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def repo_id(top):
    """Same ids as the repo map: the path below a `workspace` folder, else the folder name."""
    if not top:
        return ""
    parts = Path(top).parts
    if "workspace" in parts:
        i = len(parts) - 1 - parts[::-1].index("workspace")
        rest = parts[i + 1:]
        if rest:
            return "/".join(rest)
    return Path(top).name


def me(required=True):
    s = current_session()
    if s:
        return s["sessionId"], s.get("name") or "", s.get("cwd")
    if required:
        # outside Claude Code (your own shell): you are "you@<host>"
        return "human:" + host_label(), "you", os.getcwd()
    return None, "", os.getcwd()


def register(sid, name, cwd, **extra):
    body = {"session": sid, "host": host_label(), "name": name}
    if cwd:
        body.update(cwd=cwd, repo=repo_id(git_top(cwd)))
    body.update(extra)
    return api("POST", "/seen", body)


# ---- formatting -------------------------------------------------------------------------------------------
def ago(ts):
    s = int(time.time() - (ts or 0))
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 86400 else f"{s // 86400}d"


def who_str(m):
    name = m.get("sender_name") or (m.get("sender") or "")[:8]
    return f"{name}@{m['sender_host']}" if m.get("sender_host") else name


def fmt_messages(msgs, sid, header=True):
    if not msgs:
        return ""
    lines = [f"[room] {len(msgs)} new message(s) from other sessions:"] if header else []
    for m in msgs:
        via = f" via link {m['link']}" if m.get("link") else ""
        re_ = f" (re #{m['reply_to']})" if m.get("reply_to") else ""
        lines.append(f"#{m['id']} {m['kind']} from {who_str(m)}{via}{re_}, {ago(m['ts'])} ago:")
        lines += ["  " + l for l in m["body"].splitlines()]
    asks = [m for m in msgs if m["kind"] == "ask"]
    if asks:
        a = asks[0]
        to = a["sender"] if a["sender"].startswith("human:") else a["sender"][:8]
        lines.append(f'Answer with: {ROOM} send {to} --re {a["id"]} "<short answer>"'
                     " (if you can't answer now, say when you will).")
    return "\n".join(lines)


# ---- versions and updates ---------------------------------------------------------------------------------
def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0")[:3])


def deployed():
    """Installed by switchboard's deploy.sh (a copy under ~/.local/share/switchboard), not from PyPI."""
    return os.path.realpath(EXE).startswith(str(Path.home() / ".local" / "share" / "switchboard") + os.sep)


def latest_cached():
    try:
        return json.loads((CACHE / "latest.json").read_text())
    except (OSError, ValueError):
        return {}


def check_latest():
    """Ask PyPI for the newest version; cached in ~/.cache/switchboard/latest.json. Runs detached from hooks."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{PKG}/json", timeout=10) as r:
            v = json.loads(r.read())["info"]["version"]
        CACHE.mkdir(parents=True, exist_ok=True)
        (CACHE / "latest.json").write_text(json.dumps({"version": v, "ts": time.time()}))
    except Exception:
        CACHE.mkdir(parents=True, exist_ok=True)   # retry after CHECK_EVERY anyway; never in a session's way
        (CACHE / "latest.json").write_text(json.dumps(dict(latest_cached(), ts=time.time())))


def update_notice():
    """One line for SessionStart when PyPI has a newer room: once per machine per version, so only one session acts."""
    if deployed():
        return ""
    c = latest_cached()
    if time.time() - c.get("ts", 0) > CHECK_EVERY:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "_check-latest"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    v = c.get("version")
    if not v or vtuple(v) <= vtuple(__version__):
        return ""
    mark = CACHE / f"notified-{v}"
    if mark.exists():
        return ""
    mark.touch()
    return (f"[room] room {v} is out (this machine has {__version__}). Run `{ROOM} update` once now, then carry on; "
            "other sessions on this machine are not told.")


def update_cmd():
    """The command that upgrades this install, as a list, or None for a deploy.sh copy."""
    import shutil
    pre = Path(sys.prefix)
    if (pre / "pipx_metadata.json").exists() and shutil.which("pipx"):
        return ["pipx", "upgrade", PKG]
    if (pre / "uv-receipt.toml").exists() and shutil.which("uv"):
        return ["uv", "tool", "upgrade", PKG]
    if subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode == 0:
        return [sys.executable, "-m", "pip", "install", "-U", PKG]
    if shutil.which("uv"):
        return ["uv", "pip", "install", "-U", "--python", sys.executable, PKG]
    raise RuntimeError(f"can't tell how room was installed ({sys.prefix}); upgrade {PKG} with the tool you used")


def cmd_update(a):
    if deployed():
        return print("room here is a copy from switchboard's deploy.sh: it is updated by running room/deploy.sh on the "
                     "machine with the switchboard repo, not from PyPI.")
    import fcntl
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(CACHE / "update.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # several sessions told at once: one upgrades, the rest find it done
        cmd = update_cmd()
        print("running: " + " ".join(cmd))
        r = subprocess.run(cmd)
        if r.returncode:
            raise RuntimeError(f"upgrade failed (exit {r.returncode})")
    new = subprocess.run([EXE, "--version"], capture_output=True, text=True).stdout.strip()
    print(f"{new or 'room'} installed (was {__version__}). Hooks pick it up on their next call; nothing to restart.")


# ---- hooks ----------------------------------------------------------------------------------------------
def emit(event, text):
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def hook(event):
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    sid = data.get("session_id")
    if not sid or not cfg().get("url"):
        return
    cwd = data.get("cwd") or os.getcwd()
    try:
        # sessions that were already running when the hooks were installed register on their first event
        mark = CACHE / f"reg-{sid}"
        if event != "SessionStart" and not mark.exists():
            register(sid, session_name(sid), cwd)
            CACHE.mkdir(parents=True, exist_ok=True)
            mark.touch()
        if event == "SessionStart":
            register(sid, session_name(sid), cwd)
            repo = repo_id(git_top(cwd))
            links = [l for l in api("GET", "/links") if any(m["member"] in ("s:" + sid, "r:" + repo) for m in l["members"])]
            text = [f"[room] Other Claude sessions (all hosts) can be messaged with `{ROOM}`; run `{ROOM} help` only when you need to. "
                    f"If the user wants this session reachable while idle, keep `{ROOM} listen` running in the background."]
            for l in links:
                text.append(f"[room] This session is in link `{l['name']}`: {l['purpose']}. Tell it (`{ROOM} send link:{l['name']} \"…\"`) "
                            "when you change something its members rely on.")
            try:
                n = update_notice()
                if n:
                    text.append(n)
            except Exception:
                pass
            msgs = api("GET", f"/inbox?session={sid}")
            if msgs:
                text.append(fmt_messages(msgs, sid))
            emit(event, "\n".join(text))
        elif event == "UserPromptSubmit":
            emit(event, fmt_messages(api("GET", f"/inbox?session={sid}"), sid))
        elif event == "PostToolUse":
            out = []
            if data.get("tool_name") in EDIT_TOOLS:
                path = (data.get("tool_input") or {}).get("file_path") or (data.get("tool_input") or {}).get("notebook_path")
                top = git_top(path) if path else None
                if top:
                    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(top))
                    r = api("POST", "/touch", {"session": sid, "repo": repo_id(top), "path": rel})
                    for o in r.get("others", []):
                        out.append(f"[room] Heads-up: {o['name']}@{o['host']} also edited {repo_id(top)}/{rel} {ago(o['ts'])} ago. "
                                   f"If your changes could clash, tell it: {ROOM} send {o['session'][:8]} \"…\"")
            stamp = CACHE / f"inbox-{sid}"
            if not stamp.exists() or time.time() - stamp.stat().st_mtime > INBOX_EVERY:
                CACHE.mkdir(parents=True, exist_ok=True)
                stamp.touch()
                m = fmt_messages(api("GET", f"/inbox?session={sid}"), sid)
                if m:
                    out.append(m)
            emit(event, "\n".join(out))
        elif event == "Stop":
            # questions and answers shouldn't wait for the next prompt: keep the turn going once to handle them
            msgs = api("GET", f"/inbox?session={sid}&kinds=ask,reply")
            if msgs:
                print(json.dumps({"decision": "block", "reason": fmt_messages(msgs, sid) +
                                  "\nHandle these, then finish your turn as you were going to."}))
        elif event == "SessionEnd":
            register(sid, session_name(sid), cwd, ended=True)
    except Exception:
        pass   # the room being down must never get in a session's way


# ---- commands -------------------------------------------------------------------------------------------
def cmd_who(a):
    show_all = a.all
    unread = api("GET", "/unread")
    rows = [s for s in api("GET", "/sessions") if show_all or s["state"] != "exited" or s["last_seen"] > time.time() - 86400]
    for s in rows:
        if s["id"].startswith("human:"):
            continue
        u = unread.get(s["id"], 0)
        print(f"{(s['name'] or s['id'][:8]) + '@' + s['host']:<34} {s['id'][:8]}  {s['repo'] or '-':<28} "
              f"{s['state']:<6} {ago(s['last_seen']):>4}{'  ✉' + str(u) if u else ''}"
              f"{'  [' + ', '.join(s['links']) + ']' if s['links'] else ''}{'  — ' + s['status'] if s['status'] else ''}")
    if not rows:
        print("(no sessions yet)")


def cmd_send(a):
    re_ = a.re
    sid, name, cwd = me()
    register(sid, name, cwd)
    r = api("POST", "/send", {"session": sid, "to": a.to, "body": " ".join(a.msg),
                              "kind": "reply" if re_ else ("ask" if a.ask else "notify"), "reply_to": re_})
    print(f"sent #{r['id']} to {len(r['recipients'])} session(s)")
    if not re_:
        print(f"if you need the answer: run `{ROOM} wait {r['id']}` as a background command now. It ends when the answer "
              "arrives and wakes this session even if it is idle; without it, you only see the answer at your next prompt.")


def cmd_inbox(a):
    peek = a.peek
    sid, _, _ = me()
    print(fmt_messages(api("GET", f"/inbox?session={sid}&mark={'0' if peek else '1'}"), sid) or "no unread messages")


def cmd_wait(a):
    timeout = a.timeout
    sid, _, _ = me()
    end = time.time() + timeout
    while time.time() < end:
        rows = api("GET", f"/wait?id={a.id}&session={sid}&timeout={min(600, end - time.time())}", timeout=620)
        if rows:
            print(fmt_messages(rows, sid))
            return
    print(f"no answer to #{a.id} after {int(timeout)}s")
    sys.exit(1)


def cmd_listen(a):
    """Block until a message arrives for this session, print it, exit. Run in the background, the exit wakes an idle
    session. Never exits without a message (any exit would cost the session a turn): long-polls and retries forever."""
    sid, name, cwd = me()
    CACHE.mkdir(parents=True, exist_ok=True)
    pidf = CACHE / f"listen-{sid.replace(':', '_')}.pid"
    try:
        old = int(pidf.read_text())
        os.kill(old, 0)
        if old != os.getpid():
            return print(f"already listening for this session (pid {old}); nothing to do.")
    except (OSError, ValueError):
        pass
    pidf.write_text(str(os.getpid()))
    kinds = "" if a.all else "&kinds=ask,reply"
    try:
        while True:
            try:
                rows = api("GET", f"/listen?session={sid}{kinds}&timeout=600", timeout=630)
            except Exception:
                time.sleep(15)   # server down or restarting: wait quietly, never exit
                continue
            if rows:
                print(fmt_messages(rows, sid))
                print(f"Handle it, then run `{ROOM} listen{' --all' if a.all else ''}` as a background command again "
                      "to stay reachable.")
                return
    finally:
        try:
            if int(pidf.read_text()) == os.getpid():
                pidf.unlink()
        except (OSError, ValueError):
            pass


def cmd_status(a):
    sid, name, cwd = me()
    register(sid, name, cwd, status=" ".join(a.text))
    print("status set")


def cmd_link_list(a):
    for l in api("GET", "/links"):
        print(f"{l['name']}: {l['purpose']}")
        for m in l["members"]:
            print(f"    {m['member']}{'  watch ' + ' '.join(m['watch']) if m['watch'] else ''}")
        print(f"    live sessions now: {len(l['sessions'])}")


def cmd_link_create(a):
    sid, sname, cwd = me()
    register(sid, sname, cwd)
    api("POST", "/link", {"op": "create", "name": a.name, "purpose": " ".join(a.purpose), "session": sid})
    print(f"link {a.name} created; join it with: {ROOM} link join {a.name} [--repo] [--watch 'glob']")


def cmd_link_member(a):
    """join / leave / delete: as this session, or with --repo [REPO] for a whole repo."""
    sid, sname, cwd = me()
    register(sid, sname, cwd)
    if a.repo is not None:
        member = "r:" + (a.repo or repo_id(git_top(cwd or os.getcwd())))
    else:
        member = "s:" + sid
    watch = [w for w in (getattr(a, "watch", None) or []) if w]
    api("POST", "/link", {"op": a.op, "name": a.name, "member": member, "watch": watch})
    print(f"{a.op} {a.name}: {member}{'  watch ' + ' '.join(watch) if watch else ''}")


def cmd_thread(a):
    q = f"?link={a.name}" if a.name else ""
    for m in reversed(api("GET", "/thread" + q)):
        print(f"#{m['id']} {ago(m['ts'])} {m['kind']} {who_str(m)} → {m['to_spec']}{' re #' + str(m['reply_to']) if m['reply_to'] else ''}: {m['body']}")


def install_hooks():
    """Add the room hooks to ~/.claude/settings.json (idempotent, keeps a backup)."""
    path = Path.home() / ".claude" / "settings.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    if path.exists():
        bak = path.with_name("settings.json.bak-switchboard")
        if not bak.exists():
            bak.write_text(path.read_text())
    hooks = settings.setdefault("hooks", {})
    exe = EXE
    for event, matcher in [("SessionStart", None), ("UserPromptSubmit", None), ("PostToolUse", "*"), ("Stop", None), ("SessionEnd", None)]:
        groups = hooks.setdefault(event, [])
        groups[:] = [g for g in groups if not any("room hook" in h.get("command", "") for h in g.get("hooks", []))]
        g = {"hooks": [{"type": "command", "command": f"{exe} hook {event}", "timeout": 5}]}
        if matcher:
            g["matcher"] = matcher
        groups.append(g)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"room hooks installed in {path}")


def cmd_join(a):
    """Write the config (mode 600), check the server answers, then install the hooks."""
    host, url, token = a.host, a.url, a.token
    if not re.match(r"https?://", url):
        url = "http://" + url
    c = cfg()
    c.update(url=url.rstrip("/"), token=token, host=host or c.get("host") or socket.gethostname().split(".")[0])
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CFG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(c, indent=2) + "\n")
    os.chmod(CFG_PATH, 0o600)
    print(f"config written: {CFG_PATH} (host label: {c['host']})")
    n = len(api("GET", "/sessions"))
    print(f"server OK: {c['url']} ({n} sessions known)")
    install_hooks()
    print("done: new Claude Code sessions join the room. Try `room who`.")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))   # no packet is sent; it picks the interface a LAN peer would reach
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


SERVICE_MAC = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.switchboard.room</string>
  <key>ProgramArguments</key><array><string>{exe}</string><string>serve</string><string>--port</string><string>{port}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
<!-- save as ~/Library/LaunchAgents/com.switchboard.room.plist, then:
     launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.switchboard.room.plist -->"""

SERVICE_LINUX = """[Unit]
Description=switchboard room server
After=network-online.target

[Service]
ExecStart={exe} serve --port {port}
Restart=always

[Install]
WantedBy=default.target
# save as ~/.config/systemd/user/switchboard-room.service, then:
#   systemctl --user enable --now switchboard-room && loginctl enable-linger $USER"""


def cmd_serve(a):
    port = a.port
    if a.service:
        tpl = SERVICE_MAC if sys.platform == "darwin" else SERVICE_LINUX
        return print(tpl.format(exe=EXE, port=port, log=Path.home() / ".local" / "share" / "switchboard" / "server.log"))
    import room_server, secrets
    c = cfg()
    if not os.environ.get("ROOM_TOKEN") and not c.get("token"):
        # first run: this machine becomes the server and also a member of its own room
        c.update(token=secrets.token_urlsafe(24), url=f"http://127.0.0.1:{port}",
                 host=c.get("host") or socket.gethostname().split(".")[0])
        CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(CFG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(c, indent=2) + "\n")
        print(f"new room: token saved in {CFG_PATH}. Run `{ROOM} install-hooks` so sessions here join too.")
    token = os.environ.get("ROOM_TOKEN") or c.get("token")
    shown = token if sys.stdout.isatty() else f"<token from {CFG_PATH}>"   # never into a service's log file
    print(f"others join with:\n  pipx install {PKG}\n  room join http://{lan_ip()}:{port} {shown} --host <their-name>\n"
          "Anyone with the token can post into the Claude sessions in this room: share it only with people you trust, "
          "on a network you trust (plain HTTP). Ctrl-C stops the server.", flush=True)
    os.environ["ROOM_TOKEN"] = token
    room_server.main(["--port", str(port), "--host", a.bind] + (["--db", a.db] if a.db else []))


# ---- command line -----------------------------------------------------------------------------------------
# One table: each row is a command's usage, its help, which section of `room help` it is in, and its arguments.
# It builds both the argparse parser (so `room <command> -h` and argument errors work) and the grouped overview,
# which is written by hand because it is also read by Claude sessions as instructions.
SECTIONS = [
    ("msg", "Messaging", "for sessions: use it only when another session needs to know or answer something",
     "Keep messages short and concrete. Don't poll. Don't end a turn saying you are waiting for an answer unless "
     "`room wait` is running in the background; an idle session gets messages only at its next prompt."),
    ("update", "Update", "any session may run it when told a new room is out", ""),
    ("setup", "Set up this machine", "once, by a person", ""),
    ("server", "Run a room server", "on one machine, by a person", ""),
]


def _who(p):
    p.add_argument("--all", action="store_true", help="also sessions that exited more than a day ago")


def _send(p):
    p.add_argument("to", help="name@host (as in `who`), id prefix, link:NAME, repo:REPO, host:NAME or all")
    p.add_argument("msg", nargs="+", help="the message (several words are joined)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ask", action="store_true", help="a question: the recipient's turn keeps going until it answers")
    g.add_argument("--re", type=int, metavar="ID", help="answer message #ID")


def _wait(p):
    p.add_argument("id", type=int, help="the message id that `send` printed")
    p.add_argument("--timeout", type=float, default=1800, metavar="SECONDS", help="give up after this long (default 1800)")


def _link_member(p, watch):
    p.add_argument("name", help="link name")
    p.add_argument("--repo", nargs="?", const="", metavar="REPO",
                   help="for every session of a repo (this one if REPO is left out), not just this session")
    if watch:
        p.add_argument("--watch", action="append", metavar="GLOB",
                       help="repo-relative glob; edits to matching files notify the link (repeatable)")


def _serve(p):
    p.add_argument("--port", type=int, default=8766, help="default 8766")
    p.add_argument("--bind", default="0.0.0.0", metavar="ADDR", help="address to listen on (default 0.0.0.0: the LAN)")
    p.add_argument("--db", metavar="PATH", help="SQLite file (default ~/.local/share/switchboard/room.db)")
    p.add_argument("--service", action="store_true", help="print a launchd/systemd unit that keeps it running, and exit")


COMMANDS = [   # (section, path, usage, help, add_arguments, func); usage shows in `room help`, help in both
    ("msg", ("who",), "who [--all]", "sessions (live/exited), what they do, their links", _who, cmd_who),
    ("msg", ("send",), 'send <to> "msg" [--ask | --re <id>]', "to = name@host (as in `who`) | id prefix | link:NAME | repo:REPO | "
     "host:NAME | all", _send, cmd_send),
    ("msg", None, 'send <to> --re <id> "msg"', "answer message #id", None, None),
    ("msg", ("wait",), "wait <id> [--timeout 1800]", "block until #id is answered. Run it as a background command whenever "
     "you need an answer: it wakes this session when the answer arrives, even if idle", _wait, cmd_wait),
    ("msg", ("listen",), "listen [--all]", "stay reachable while idle (only when the user asks): run it as a background "
     "command; it ends when an ask or reply arrives (--all: any message), which wakes this session, and costs nothing "
     "until then. Restart it after handling the message",
     lambda p: p.add_argument("--all", action="store_true", help="wake for every message, not only asks and replies"),
     cmd_listen),
    ("msg", ("inbox",), "inbox [--peek]", 'unread messages (they also arrive on their own as "[room]" context)',
     lambda p: p.add_argument("--peek", action="store_true", help="show without marking read"), cmd_inbox),
    ("msg", ("status",), 'status "now: …"', "one line on what this session is doing, shown to others",
     lambda p: p.add_argument("text", nargs="*", help="the status line"), cmd_status),
    ("msg", ("link", "list"), "link [list]", "links: what they are for, members, watched files", lambda p: None, cmd_link_list),
    ("msg", ("link", "create"), 'link create NAME "purpose"', "start a link that groups sessions or whole repos on one topic",
     lambda p: (p.add_argument("name"), p.add_argument("purpose", nargs="*")), cmd_link_create),
    ("msg", ("link", "join"), "link join NAME [--repo [REPO]] [--watch GLOB]…",
     "join as this session, or a whole repo; files matching --watch globs auto-notify the link when edited",
     lambda p: _link_member(p, True), cmd_link_member),
    ("msg", ("link", "leave"), "link leave NAME [--repo [REPO]]", "leave a link (as this session, or for the whole repo)",
     lambda p: _link_member(p, False), cmd_link_member),
    ("msg", ("link", "delete"), "link delete NAME", "delete a link", lambda p: _link_member(p, False), cmd_link_member),
    ("msg", ("thread",), "thread [NAME]", "recent messages (all, or one link)",
     lambda p: p.add_argument("name", nargs="?", help="a link name"), cmd_thread),
    ("update", ("update",), "update", "upgrade room on this machine (pipx, uv tool or pip, whichever installed it)",
     lambda p: None, cmd_update),
    ("update", None, "--version", "the installed version", None, None),
    ("setup", ("join",), "join <url> <token> [--host NAME]", "write the config, check the server, install the hooks",
     lambda p: (p.add_argument("url", help="the room server, e.g. http://10.0.0.5:8766"), p.add_argument("token"),
                p.add_argument("--host", metavar="NAME", help="this machine's label in `room who` (default: hostname)")),
     cmd_join),
    ("setup", ("install-hooks",), "install-hooks", "(re)install only the hooks in ~/.claude/settings.json",
     lambda p: None, lambda a: install_hooks()),
    ("server", ("serve",), "serve [--port 8766]", "serve a room here; the first run makes a token and prints the join line",
     _serve, cmd_serve),
    ("server", None, "serve --service", "print a launchd/systemd unit that keeps it running", None, None),
]


def help_text(color=None):
    """Grouped overview. Colors only for a person at a terminal (sessions read it too), and never with NO_COLOR."""
    import shutil, textwrap
    if color is None:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
    c = (lambda code, t: f"\033[{code}m{t}\033[0m") if color else (lambda code, t: t)
    width = min(shutil.get_terminal_size((100, 24)).columns, 120)
    col = max(len(u) for _, _, u, *_ in COMMANDS if len(u) <= 34) + 4
    out = [f"{c('1', 'room')}: message other Claude Code sessions (this machine or others). Command: {ROOM}",
           f"{c('2', 'room <command> -h shows one command in detail.')}"]
    for key, title, who, note in SECTIONS:
        out += ["", f"{c('1;34', title.upper())}  {c('2', '(' + who + ')')}"]
        for _, _, usage, desc, *_ in (r for r in COMMANDS if r[0] == key):
            lines = textwrap.wrap(desc, max(30, width - col - 2)) or [""]
            if len(usage) + 2 > col:   # long usage: description goes on the next line
                out.append("  " + c("32", usage))
                out += [" " * (col + 2) + l for l in lines]
            else:
                out.append("  " + c("32", usage) + " " * (col - len(usage)) + lines[0])
                out += [" " * (col + 2) + l for l in lines[1:]]
        if note:
            out += ["  " + c("2", l) for l in textwrap.wrap(note, width - 4)]
    return "\n".join(l.rstrip() for l in out)


def build_parser():
    import argparse

    class Parser(argparse.ArgumentParser):
        def error(self, message):   # short and pointing at the help, instead of argparse's usage dump
            more = f"room {self.prog[5:]} -h for details, " if len(self.prog) > 4 else ""
            self.exit(2, f"room: {message}\n{self.format_usage().strip()}\n({more}room help for all commands)\n")

    top = Parser(prog="room", add_help=False, usage="room <command> [options]")
    sub = top.add_subparsers(dest="cmd", metavar="<command>", parser_class=Parser)
    link_sub = None
    for _, path, usage, desc, add_args, func in COMMANDS:
        if not path:
            continue
        if path[0] == "link":
            if link_sub is None:
                lp = sub.add_parser("link", usage="room link {list,create,join,leave,delete} …",
                                    description="Links group sessions or whole repos on one topic.")
                lp.set_defaults(func=cmd_link_list, op="list")   # bare `room link` lists
                link_sub = lp.add_subparsers(dest="op", metavar="{list,create,join,leave,delete}", parser_class=Parser)
            p = link_sub.add_parser(path[1], usage="room " + usage, description=desc)
        else:
            p = sub.add_parser(path[0], usage="room " + usage, description=desc)
        add_args(p)
        p.set_defaults(func=func)
    v = sub.add_parser("version")
    v.set_defaults(func=lambda a: print(f"room {__version__}"))
    return top


def main():
    args = sys.argv[1:]
    try:
        # hooks run on every tool call: no argparse on that path
        if args[:1] == ["hook"]:
            return hook(args[1] if len(args) > 1 else "")
        if args[:1] == ["_check-latest"]:
            return check_latest()
        if not args or args[0] in ("help", "-h", "--help"):
            return print(help_text())
        if args[0] in ("--version", "-V"):
            return print(f"room {__version__}")
        a = build_parser().parse_args(args)
        a.func(a)
    except (RuntimeError, OSError, urllib.error.URLError) as e:
        print(f"room: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
