#!/usr/bin/env python3
"""switchboard room server: a mailbox for Claude Code sessions on several hosts.

One small HTTP + SQLite service on the LAN. It stores sessions, links and messages and does nothing on its
own: sessions (through their hooks and the `room` CLI) post and fetch. No agent runs here.

  python3 server.py [--host 0.0.0.0] [--port 8766] [--db ~/.local/share/switchboard/room.db]

Every request needs `Authorization: Bearer <token>`; the token is read from $ROOM_TOKEN or
~/.config/switchboard/room.json ("token"). Python 3.9+, stdlib only.
"""
import argparse, fnmatch, json, os, re, sqlite3, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LIVE_FOR = 6 * 3600          # a session not seen for this long (and not ended) counts as gone quiet
AUTO_NOTICE_EVERY = 30 * 60  # one "edited X" notice per session/link/file in this window
SAME_FILE_WINDOW = 20 * 60   # "another session edited this file" warning window
MAX_BODY = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, host TEXT, name TEXT, cwd TEXT, repo TEXT,
  status TEXT DEFAULT '', started REAL, last_seen REAL, ended REAL);
CREATE TABLE IF NOT EXISTS links (name TEXT PRIMARY KEY, purpose TEXT, created REAL, created_by TEXT);
CREATE TABLE IF NOT EXISTS members (link TEXT, member TEXT, watch TEXT DEFAULT '[]', joined REAL,
  PRIMARY KEY (link, member));
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sender TEXT, to_spec TEXT,
  link TEXT, kind TEXT, body TEXT, reply_to INTEGER);
CREATE TABLE IF NOT EXISTS deliveries (msg INTEGER, session TEXT, read REAL, PRIMARY KEY (msg, session));
CREATE TABLE IF NOT EXISTS touches (session TEXT, repo TEXT, path TEXT, ts REAL, PRIMARY KEY (session, repo, path));
CREATE TABLE IF NOT EXISTS notices (session TEXT, link TEXT, path TEXT, ts REAL, PRIMARY KEY (session, link, path));
CREATE INDEX IF NOT EXISTS deliveries_by_session ON deliveries (session, read);
"""


class Room:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)   # wakes /wait long-polls when a message arrives

    def q(self, sql, *args):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # ---- sessions -------------------------------------------------------------------------------------
    def seen(self, sid, host=None, name=None, cwd=None, repo=None, ended=False):
        now = time.time()
        row = self.q("SELECT * FROM sessions WHERE id=?", sid)
        if not row:
            self.db.execute("INSERT INTO sessions (id, host, name, cwd, repo, started, last_seen) VALUES (?,?,?,?,?,?,?)",
                            (sid, host or "", name or "", cwd or "", repo or "", now, now))
        else:
            r = row[0]
            self.db.execute("UPDATE sessions SET host=?, name=?, cwd=?, repo=?, last_seen=?, ended=? WHERE id=?",
                            (host or r["host"], name or r["name"], cwd or r["cwd"], repo or r["repo"], now,
                             now if ended else None, sid))

    def sessions(self):
        now = time.time()
        out = self.q("SELECT * FROM sessions ORDER BY last_seen DESC")
        for s in out:
            s["state"] = "exited" if s["ended"] else ("live" if now - s["last_seen"] < LIVE_FOR else "quiet")
            s["links"] = [m["link"] for m in self.q(
                "SELECT link FROM members WHERE member=? OR member=?", "s:" + s["id"], "r:" + (s["repo"] or "-"))]
        return out

    def resolve_session(self, who):
        """Session id, unique id prefix, session name, or name@host as `room who` prints it (most recently seen wins)."""
        name, _, host = who.rpartition("@")
        r = self.q("SELECT id FROM sessions WHERE id=?", who) or \
            (self.q("SELECT id FROM sessions WHERE name=? AND lower(host)=lower(?) ORDER BY last_seen DESC LIMIT 1",
                    name, host) if name else []) or \
            self.q("SELECT id FROM sessions WHERE name=? ORDER BY last_seen DESC LIMIT 1", who) or \
            (self.q("SELECT id FROM sessions WHERE id LIKE ?", who + "%") if len(who) >= 6 else [])
        return r[0]["id"] if len(r) == 1 else None

    # ---- links ----------------------------------------------------------------------------------------
    def link_members(self, link):
        """Session ids in a link: joined sessions (even if exited) + live sessions of joined repos."""
        ids = set()
        for m in self.q("SELECT member FROM members WHERE link=?", link):
            kind, val = m["member"].split(":", 1)
            if kind == "s":
                ids.add(val)
            else:
                ids |= {s["id"] for s in self.q(
                    "SELECT id FROM sessions WHERE repo=? AND ended IS NULL AND last_seen>?", val, time.time() - LIVE_FOR)}
        return ids

    def links(self):
        out = self.q("SELECT * FROM links ORDER BY name")
        for l in out:
            l["members"] = [dict(m, watch=json.loads(m["watch"])) for m in self.q(
                "SELECT member, watch, joined FROM members WHERE link=? ORDER BY member", l["name"])]
            l["sessions"] = sorted(self.link_members(l["name"]))
        return out

    # ---- messages -------------------------------------------------------------------------------------
    def send(self, sender, to, body, kind="notify", reply_to=None):
        body = (body or "").strip()[:MAX_BODY]
        if not body:
            raise ValueError("empty message")
        link = None
        if to.startswith("link:"):
            link = to[5:]
            if not self.q("SELECT 1 FROM links WHERE name=?", link):
                raise ValueError(f"no link named {link!r} (room link list)")
            rcpt = self.link_members(link)
        elif to.startswith("repo:"):
            rcpt = {s["id"] for s in self.q("SELECT id FROM sessions WHERE repo=? AND ended IS NULL AND last_seen>?",
                                            to[5:], time.time() - LIVE_FOR)}
        elif to == "all" or to.startswith("host:"):   # every live session, or every live one on one host
            rows = self.q("SELECT id, host FROM sessions WHERE ended IS NULL AND last_seen>? AND id NOT LIKE 'human:%'",
                          time.time() - LIVE_FOR)
            rcpt = {r["id"] for r in rows if to == "all" or r["host"].lower() == to[5:].lower()}
        else:
            sid = self.resolve_session(to)
            if not sid:
                raise ValueError(f"no single session matches {to!r} (room who)")
            rcpt = {sid}
        rcpt.discard(sender)
        if not rcpt:
            raise ValueError(f"nobody to deliver to for {to!r}")
        if reply_to and not link:   # a reply to a link message stays in that link's thread
            parent = self.q("SELECT link FROM messages WHERE id=?", reply_to)
            link = parent[0]["link"] if parent else None
        cur = self.db.execute("INSERT INTO messages (ts, sender, to_spec, link, kind, body, reply_to) VALUES (?,?,?,?,?,?,?)",
                              (time.time(), sender, to, link, kind, body, reply_to))
        mid = cur.lastrowid
        self.db.executemany("INSERT INTO deliveries (msg, session) VALUES (?,?)", [(mid, r) for r in sorted(rcpt)])
        self.cond.notify_all()
        return {"id": mid, "recipients": sorted(rcpt)}

    def inbox(self, sid, mark=True, kinds=None):
        rows = self.q("""SELECT m.*, s.name AS sender_name, s.host AS sender_host FROM deliveries d
                         JOIN messages m ON m.id=d.msg LEFT JOIN sessions s ON s.id=m.sender
                         WHERE d.session=? AND d.read IS NULL ORDER BY m.id""", sid)
        if kinds:
            rows = [r for r in rows if r["kind"] in kinds]
        if mark and rows:
            self.db.executemany("UPDATE deliveries SET read=? WHERE msg=? AND session=?",
                                [(time.time(), r["id"], sid) for r in rows])
        return rows

    def unread(self):
        return {r["session"]: r["n"] for r in self.q(
            "SELECT session, COUNT(*) AS n FROM deliveries WHERE read IS NULL GROUP BY session")}

    def thread(self, link=None, limit=100):
        where, args = ("WHERE m.link=?", (link,)) if link else ("", ())
        return self.q(f"""SELECT m.*, s.name AS sender_name, s.host AS sender_host FROM messages m
                          LEFT JOIN sessions s ON s.id=m.sender {where} ORDER BY m.id DESC LIMIT {int(limit)}""", *args)

    # ---- file touches ---------------------------------------------------------------------------------
    def touch(self, sid, repo, path):
        """Record an edit. Returns warnings for the editor; posts watched-file notices to links."""
        now = time.time()
        self.db.execute("INSERT OR REPLACE INTO touches VALUES (?,?,?,?)", (sid, repo, path, now))
        warn = [dict(r) for r in self.q(
            """SELECT t.session, t.ts, COALESCE(NULLIF(s.name, ''), substr(t.session, 1, 8)) AS name, s.host FROM touches t JOIN sessions s ON s.id=t.session
               WHERE t.repo=? AND t.path=? AND t.session<>? AND t.ts>? AND s.ended IS NULL""",
            repo, path, sid, now - SAME_FILE_WINDOW)]
        me = self.q("SELECT name, host FROM sessions WHERE id=?", sid)
        who = f"{me[0]['name'] or sid[:8]}@{me[0]['host']}" if me else sid[:8]
        posted = []
        for m in self.q("SELECT link, member, watch FROM members WHERE member=? OR member=?", "s:" + sid, "r:" + repo):
            globs = json.loads(m["watch"])
            if not globs or not any(fnmatch.fnmatch(path, g) for g in globs):
                continue
            last = self.q("SELECT ts FROM notices WHERE session=? AND link=? AND path=?", sid, m["link"], path)
            if last and now - last[0]["ts"] < AUTO_NOTICE_EVERY:
                continue
            self.db.execute("INSERT OR REPLACE INTO notices VALUES (?,?,?,?)", (sid, m["link"], path, now))
            try:
                self.send(sid, "link:" + m["link"], f"[auto] {who} edited {repo}/{path}", kind="auto")
                posted.append(m["link"])
            except ValueError:
                pass   # nobody else in the link right now
        return {"others": warn, "noticed": posted}


class Handler(BaseHTTPRequestHandler):
    room = None
    token = None

    def reply(self, code, obj=None):
        body = json.dumps(obj if obj is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        if self.headers.get("Authorization", "") == "Bearer " + self.token:
            return True
        self.reply(401, {"error": "bad token"})
        return False

    def body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        if not self.authed():
            return
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        R = self.room
        try:
            with R.lock:
                if u.path == "/sessions":
                    return self.reply(200, R.sessions())
                if u.path == "/links":
                    return self.reply(200, R.links())
                if u.path == "/unread":
                    return self.reply(200, R.unread())
                if u.path == "/thread":
                    return self.reply(200, R.thread(qs.get("link"), int(qs.get("limit", 100))))
                if u.path == "/state":   # everything the page needs, in one call
                    return self.reply(200, {"sessions": R.sessions(), "links": R.links(), "unread": R.unread(),
                                            "messages": R.thread(None, 200)})
                if u.path == "/inbox":
                    kinds = qs["kinds"].split(",") if qs.get("kinds") else None
                    if qs.get("session"):
                        R.seen(qs["session"])
                    return self.reply(200, R.inbox(qs["session"], qs.get("mark", "1") == "1", kinds))
                if u.path == "/wait":   # long-poll for replies to a message
                    mid, sid = int(qs["id"]), qs["session"]
                    deadline = time.time() + min(float(qs.get("timeout", 600)), 3600)
                    while True:
                        rows = R.q("""SELECT m.*, s.name AS sender_name, s.host AS sender_host FROM messages m
                                      JOIN deliveries d ON d.msg=m.id AND d.session=? AND d.read IS NULL
                                      LEFT JOIN sessions s ON s.id=m.sender WHERE m.reply_to=? ORDER BY m.id""", sid, mid)
                        if rows:
                            R.db.executemany("UPDATE deliveries SET read=? WHERE msg=? AND session=?",
                                             [(time.time(), r["id"], sid) for r in rows])
                            return self.reply(200, rows)
                        left = deadline - time.time()
                        if left <= 0:
                            return self.reply(200, [])
                        R.cond.wait(min(left, 30))
            self.reply(404, {"error": "not found"})
        except (KeyError, ValueError) as e:
            self.reply(400, {"error": str(e)})

    def do_POST(self):
        if not self.authed():
            return
        R = self.room
        try:
            b = self.body()
            with R.lock:
                p = self.path
                if p == "/seen":
                    R.seen(b["session"], b.get("host"), b.get("name"), b.get("cwd"), b.get("repo"), bool(b.get("ended")))
                    if "status" in b:
                        R.db.execute("UPDATE sessions SET status=? WHERE id=?", (str(b["status"])[:200], b["session"]))
                    return self.reply(200, {"unread": R.unread().get(b["session"], 0)})
                if p == "/send":
                    return self.reply(200, R.send(b["session"], b["to"], b["body"], b.get("kind", "notify"), b.get("reply_to")))
                if p == "/touch":
                    return self.reply(200, R.touch(b["session"], b["repo"], b["path"]))
                if p == "/link":
                    name = b["name"]
                    if not re.fullmatch(r"[A-Za-z0-9][\w.-]{0,40}", name):
                        raise ValueError("link names: letters, digits, - _ . (max 40)")
                    op = b.get("op", "join")
                    if op == "create":
                        R.db.execute("INSERT OR IGNORE INTO links VALUES (?,?,?,?)",
                                     (name, b.get("purpose", ""), time.time(), b.get("session", "")))
                        if b.get("purpose"):
                            R.db.execute("UPDATE links SET purpose=? WHERE name=?", (b["purpose"], name))
                        return self.reply(200, {"ok": True})
                    if not R.q("SELECT 1 FROM links WHERE name=?", name):
                        raise ValueError(f"no link named {name!r}")
                    member = b["member"]
                    if not re.fullmatch(r"[sr]:.+", member):
                        raise ValueError("member must be s:<session id> or r:<repo>")
                    if op == "join":
                        R.db.execute("INSERT OR REPLACE INTO members VALUES (?,?,?,?)",
                                     (name, member, json.dumps(b.get("watch") or []), time.time()))
                    elif op == "leave":
                        R.db.execute("DELETE FROM members WHERE link=? AND member=?", (name, member))
                    elif op == "delete":
                        R.db.execute("DELETE FROM members WHERE link=?", (name,))
                        R.db.execute("DELETE FROM links WHERE name=?", (name,))
                    return self.reply(200, {"ok": True})
            self.reply(404, {"error": "not found"})
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            self.reply(400, {"error": str(e)})

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--db", default="~/.local/share/switchboard/room.db")
    a = ap.parse_args()
    token = os.environ.get("ROOM_TOKEN")
    if not token:
        cfg = Path.home() / ".config" / "switchboard" / "room.json"
        token = json.loads(cfg.read_text()).get("token") if cfg.exists() else None
    if not token:
        raise SystemExit("no token: set ROOM_TOKEN or ~/.config/switchboard/room.json {\"token\": ...}")
    Handler.room, Handler.token = Room(os.path.expanduser(a.db)), token
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"room server on {a.host}:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
