#!/usr/bin/env python3
"""room: let Claude Code sessions on any host message each other through the switchboard room server.

Run `room help` for the commands. Hooks call `room hook <Event>`. They print nothing (no tokens) unless
there is something to deliver, and never fail the session.
Config: ~/.config/switchboard/room.json {"url": "http://<server>:8766", "token": "...", "host": "<label>"}
(`room join <url> <token> [--host NAME]` writes it and installs the hooks). Python 3.9+, stdlib only.
"""
__version__ = "0.1.0"

import glob, json, os, re, socket, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path

CFG_PATH = Path(os.environ.get("ROOM_CONFIG") or Path.home() / ".config" / "switchboard" / "room.json")
CACHE = Path.home() / ".cache" / "switchboard"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
INBOX_EVERY = 15          # seconds between inbox checks from PostToolUse


def _exe():
    """This command's own path (pipx/uv tool, the deploy.sh symlink, or room.py itself), so hooks and hints call it."""
    p = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if not os.path.basename(p).startswith("room"):
        import shutil
        p = shutil.which("room") or str(Path.home() / ".local" / "bin" / "room")
    return p


EXE = _exe()
ROOM = "~" + EXE[len(str(Path.home())):] if EXE.startswith(str(Path.home()) + os.sep) else EXE

HELP = f"""room: message other Claude Code sessions (this machine or others). Use it only when another
session needs to know or answer something.
  {ROOM} who                         sessions (live/exited), what they do, their links
  {ROOM} send <to> "msg" [--ask]     to = name@host (as in `who`) | id prefix | link:NAME | repo:REPO
  {ROOM} send <to> --re <id> "msg"   answer message #id
  {ROOM} wait <id> [--timeout 1800]  block until #id is answered. Run it as a background command whenever you
                                     need an answer: it wakes this session when the answer arrives, even if idle
  {ROOM} inbox [--peek]              unread messages (they also arrive on their own as "[room]" context)
  {ROOM} status "now: …"             one line on what this session is doing, shown to others
  {ROOM} link list | create NAME "purpose" | join NAME [--repo [REPO]] [--watch GLOB]… | leave NAME [--repo [REPO]]
                                     links group sessions or whole repos on one topic; files matching
                                     --watch globs (repo-relative) auto-notify the link when edited
  {ROOM} thread [NAME]               recent messages (all, or one link)
  {ROOM} join <url> <token> [--host NAME]   set up this machine: write the config and install the hooks
Keep messages short and concrete. Don't poll. Don't end a turn saying you are waiting for an answer
unless `room wait` is running in the background; an idle session gets messages only at its next prompt."""


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
            text = [f"[room] Other Claude sessions (all hosts) can be messaged with `{ROOM}`; run `{ROOM} help` only when you need to."]
            for l in links:
                text.append(f"[room] This session is in link `{l['name']}`: {l['purpose']}. Tell it (`{ROOM} send link:{l['name']} \"…\"`) "
                            "when you change something its members rely on.")
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
def opt(args, name, default=None, flag=False):
    if name not in args:
        return default
    i = args.index(name)
    if flag:
        args.pop(i)
        return True
    if i + 1 < len(args) and not args[i + 1].startswith("--"):
        v = args[i + 1]
        del args[i:i + 2]
        return v
    args.pop(i)
    return ""   # flag given without value


def cmd_who(args):
    show_all = opt(args, "--all", flag=True)
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


def cmd_send(args):
    ask = opt(args, "--ask", flag=True)
    re_ = opt(args, "--re")
    if len(args) < 2:
        raise RuntimeError('usage: room send <to> "message" [--ask] [--re ID]')
    sid, name, cwd = me()
    register(sid, name, cwd)
    r = api("POST", "/send", {"session": sid, "to": args[0], "body": " ".join(args[1:]),
                              "kind": "reply" if re_ else ("ask" if ask else "notify"), "reply_to": int(re_) if re_ else None})
    print(f"sent #{r['id']} to {len(r['recipients'])} session(s)")
    if not re_:
        print(f"if you need the answer: run `{ROOM} wait {r['id']}` as a background command now. It ends when the answer "
              "arrives and wakes this session even if it is idle; without it, you only see the answer at your next prompt.")


def cmd_inbox(args):
    peek = opt(args, "--peek", flag=True)
    sid, _, _ = me()
    print(fmt_messages(api("GET", f"/inbox?session={sid}&mark={'0' if peek else '1'}"), sid) or "no unread messages")


def cmd_wait(args):
    timeout = float(opt(args, "--timeout", "1800"))
    sid, _, _ = me()
    end = time.time() + timeout
    while time.time() < end:
        rows = api("GET", f"/wait?id={int(args[0])}&session={sid}&timeout={min(600, end - time.time())}", timeout=620)
        if rows:
            print(fmt_messages(rows, sid))
            return
    print(f"no answer to #{args[0]} after {int(timeout)}s")
    sys.exit(1)


def cmd_status(args):
    sid, name, cwd = me()
    register(sid, name, cwd, status=" ".join(args))
    print("status set")


def cmd_link(args):
    op = args.pop(0) if args else "list"
    if op == "list":
        for l in api("GET", "/links"):
            print(f"{l['name']}: {l['purpose']}")
            for m in l["members"]:
                print(f"    {m['member']}{'  watch ' + ' '.join(m['watch']) if m['watch'] else ''}")
            print(f"    live sessions now: {len(l['sessions'])}")
        return
    if not args:
        raise RuntimeError("usage: room link create|join|leave|delete NAME …")
    name = args.pop(0)
    sid, sname, cwd = me()
    register(sid, sname, cwd)
    if op == "create":
        api("POST", "/link", {"op": "create", "name": name, "purpose": " ".join(args), "session": sid})
        print(f"link {name} created; join it with: {ROOM} link join {name} [--repo] [--watch 'glob']")
        return
    repo = opt(args, "--repo")
    watch = []
    while "--watch" in args:
        watch.append(opt(args, "--watch"))
    if repo is not None:
        member = "r:" + (repo or repo_id(git_top(cwd or os.getcwd())))
    else:
        member = "s:" + sid
    if op in ("join", "leave", "delete"):
        api("POST", "/link", {"op": op, "name": name, "member": member, "watch": [w for w in watch if w]})
        print(f"{op} {name}: {member}{'  watch ' + ' '.join(watch) if watch else ''}")
    else:
        raise RuntimeError(f"unknown link command {op!r}")


def cmd_thread(args):
    q = f"?link={args[0]}" if args else ""
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


def cmd_join(args):
    """Write the config (mode 600), check the server answers, then install the hooks."""
    host = opt(args, "--host")
    if len(args) != 2:
        raise RuntimeError("usage: room join <url> <token> [--host NAME]   e.g. room join http://10.0.0.5:8766 TOKEN --host my-laptop")
    url, token = args
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


def main():
    args = sys.argv[1:]
    cmd = args.pop(0) if args else "help"
    try:
        if cmd == "hook":
            return hook(args[0] if args else "")
        if cmd in ("help", "-h", "--help"):
            return print(HELP)
        if cmd in ("version", "--version", "-V"):
            return print(f"room {__version__}")
        fn = {"who": cmd_who, "send": cmd_send, "inbox": cmd_inbox, "wait": cmd_wait, "status": cmd_status,
              "link": cmd_link, "thread": cmd_thread, "join": cmd_join}.get(cmd)
        if cmd == "install-hooks":
            return install_hooks()
        if not fn:
            print(HELP)
            sys.exit(2)
        fn(args)
    except (RuntimeError, OSError, urllib.error.URLError) as e:
        print(f"room: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
