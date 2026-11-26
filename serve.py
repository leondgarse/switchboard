#!/usr/bin/env python3
"""Serve the switchboard page locally so it can save edits and show live sessions.

  python3 serve.py            # http://127.0.0.1:8767

GET  /api/sessions                    -> cc-menubar's session snapshot (refreshed when stale)
GET  /api/room                        -> the room server's state (links, messages, unread), if configured
POST /api/layout  {id: [x, y], ...}   -> merged into layout.json
POST /api/config  {exclude: [...], center: "..."} -> merged into config.json
POST /api/labels  {"source>target": "short label", ...} -> merged into labels.json
POST /api/relayout                    -> recompute the layered layout from scratch
Each save re-renders graph.js and the vault (no rescan).
"""
import json, os, subprocess, sys, threading, time, urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8767
PUBLIC = {"/": "index.html", "/index.html": "index.html", "/graph.js": "graph.js", "/tasks/tasks.js": "tasks/tasks.js"}
TASK_CMDS = {"add", "set", "review", "collect", "send"}
SNAPSHOT = Path.home() / ".cache" / "cc-menubar" / "sessions.json"
STALE_AFTER = 15          # seconds; the menu bar app rewrites the snapshot every poll (~5 s)
_refreshing = threading.Lock()


def cc_menubar_script():
    cfg = json.loads((HERE / "config.json").read_text()) if (HERE / "config.json").exists() else {}
    p = Path(os.path.expanduser(cfg.get("ccMenubar", "~/workspace/cc-menubar/cc-menubar.py")))
    return p if p.exists() else None


def refresh_snapshot():
    """Collect once via cc-menubar.py when the app isn't keeping the snapshot fresh. Never blocks a request."""
    script = cc_menubar_script()
    if not script or not _refreshing.acquire(blocking=False):
        return
    def work():
        try:
            subprocess.run([sys.executable, str(script), "--snapshot"], timeout=60, check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            _refreshing.release()
    threading.Thread(target=work, daemon=True).start()


def sessions_payload():
    try:
        snap = json.loads(SNAPSHOT.read_text())
    except (OSError, ValueError):
        snap = {"generated": 0, "hosts": [], "sessions": []}
    age = time.time() - snap.get("generated", 0)
    if age > STALE_AFTER:
        refresh_snapshot()
    snap["age"] = round(age, 1)
    snap["collector"] = bool(cc_menubar_script())
    return snap


def room_state():
    """Links, messages and unread counts from the room server (~/.config/switchboard/room.json)."""
    try:
        c = json.loads((Path.home() / ".config" / "switchboard" / "room.json").read_text())
    except (OSError, ValueError):
        return {"configured": False}
    req = urllib.request.Request(c["url"].rstrip("/") + "/state", headers={"Authorization": "Bearer " + c.get("token", "")})
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            return {"configured": True, "ok": True, **json.loads(r.read())}
    except Exception as e:
        return {"configured": True, "ok": False, "error": str(e)}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/api/sessions", "/api/room"):
            body = json.dumps(sessions_payload() if path == "/api/sessions" else room_state()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path not in PUBLIC:            # only the page itself; never the repo (.git, json state, …)
            return self.send_error(404)
        self.path = "/" + PUBLIC[path]
        return super().do_GET()

    def do_POST(self):
        if self.path in ("/api/tasks", "/api/open"):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            except Exception:
                return self.send_error(400)
            if self.path == "/api/open":   # open a TODO file in its default app
                path = os.path.realpath(str(body.get("path", "")))
                if not path.startswith(str(Path.home()) + "/") or not os.path.isfile(path):
                    return self.send_error(403)
                subprocess.Popen(["open", path])
                self.send_response(204)
                return self.end_headers()
            args = [str(x) for x in body.get("args", [])]
            if not args or args[0] not in TASK_CMDS:
                return self.send_error(400, "unknown tasks command")
            r = subprocess.run([sys.executable, str(HERE / "tasks" / "tasks.py")] + args, capture_output=True, text=True, timeout=120)
            out = json.dumps({"ok": r.returncode == 0, "out": (r.stdout + r.stderr).strip()[-500:]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            return self.wfile.write(out)
        if self.path == "/api/relayout":
            r = subprocess.run([sys.executable, str(HERE / "scan.py"), "--render-only", "--relayout"], check=False)
            self.send_response(204 if r.returncode == 0 else 500)
            return self.end_headers()
        target = {"/api/layout": HERE / "layout.json", "/api/config": HERE / "config.json",
                  "/api/labels": HERE / "labels.json"}.get(self.path)
        if not target:
            return self.send_error(404)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            assert isinstance(body, dict)
        except Exception:
            return self.send_error(400, "expected a JSON object")
        current = json.loads(target.read_text()) if target.exists() else {}
        current.update(body)
        target.write_text(json.dumps(current, indent=1) + "\n")
        subprocess.run([sys.executable, str(HERE / "scan.py"), "--render-only"], check=False)
        self.send_response(204)
        self.end_headers()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        if "/api/" not in (str(args[0]) if args else ""):   # error logs pass the status code (an int) first
            super().log_message(fmt, *args)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler, directory=str(HERE)))
    print(f"Switchboard: http://127.0.0.1:{PORT}/  (Ctrl-C to stop)")
    srv.serve_forever()
