#!/usr/bin/env python3
"""Copy a Claude Code session's transcript to another host, so it can be resumed there with `claude -r`.

  python3 move_session.py <session-id> <from-host> <to-host> [--force]

Hosts are the names in cc-menubar's session snapshot (~/.cache/cc-menubar/sessions.json), e.g. Local, RTX, MS.
Copies ~/.claude/projects/<project>/<id>.jsonl and its <id>/ folder (subagent transcripts, tool results),
rewrites the source home directory (and its real path, if home is a symlink) to the destination's, and writes them
into the destination's project folder for the rewritten cwd. The source session is left untouched.
Refuses (unless --force) when the session is running on the destination, or when the destination's copy has turns
the source doesn't (it was continued there). A copy already there is moved to .switchboard-moved/<time>-<pid>/ first.
Prints one JSON object; a refusal that --force overrides carries "forceable": true.
"""
import io, json, re, shlex, subprocess, sys, tarfile
from pathlib import Path

SNAPSHOT = Path.home() / ".cache" / "cc-menubar" / "sessions.json"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
UUID_RE = re.compile(rb'"uuid":"([^"]+)"')


class Refused(Exception):
    """A safety check that --force overrides."""


def run(ssh, script, data=None, timeout=120):
    """Run a sh script locally or on an ssh host; returns stdout bytes, raises on failure."""
    cmd = ["/usr/bin/ssh"] + SSH_OPTS + [ssh, "sh -c " + shlex.quote(script)] if ssh else ["/bin/sh", "-c", script]
    r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode(errors="replace").strip()[-400:] or f"exit {r.returncode}")
    return r.stdout


def homes(ssh):
    out = run(ssh, 'echo "$HOME"; cd "$HOME" && pwd -P').decode().split("\n")
    return out[0].rstrip("/"), out[1].rstrip("/")


def project_dir(cwd):   # Claude Code's encoding of a cwd into ~/.claude/projects/<dir>
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def move(sid, src_name, dst_name, force=False):
    if not ID_RE.match(sid):
        raise ValueError("bad session id")
    if src_name == dst_name:
        raise ValueError("source and destination are the same host")
    snap = json.loads(SNAPSHOT.read_text())
    hosts = {h["name"]: h.get("ssh") for h in snap.get("hosts", [])}
    for n in (src_name, dst_name):
        if n not in hosts:
            raise ValueError(f"unknown host {n!r} (known: {', '.join(hosts)})")
    src, dst = hosts[src_name], hosts[dst_name]
    sess = next((s for s in snap.get("sessions", []) if s.get("sessionId") == sid and s.get("host") == src_name), None)
    if not force and any(s.get("sessionId") == sid and s.get("host") == dst_name for s in snap.get("sessions", [])):
        raise Refused(f"the session is running on {dst_name}: exit it there first, or it keeps writing into the new copy")

    # 1. find and pack the transcript on the source
    found = run(src, f'ls "$HOME"/.claude/projects/*/{sid}.jsonl 2>/dev/null | head -n 1').decode().strip()
    if not found:
        raise RuntimeError(f"no transcript for {sid} on {src_name}")
    pdir = found.rsplit("/", 1)[0]
    blob = run(src, f"cd {shlex.quote(pdir)} && if [ -d {sid} ]; then tar cf - {sid}.jsonl {sid}; else tar cf - {sid}.jsonl; fi")

    # 2. map the home directory
    s_home, s_real = homes(src)
    d_home, d_real = homes(dst)
    cwd = (sess or {}).get("cwd") or ""
    if not cwd:   # not live: take it from the transcript
        tf = tarfile.open(fileobj=io.BytesIO(blob))
        for line in tf.extractfile(f"{sid}.jsonl").read().decode(errors="replace").splitlines():
            m = re.search(r'"cwd":"([^"]+)"', line)
            if m:
                cwd = m.group(1); break
    prefixes = sorted({p for p in (s_home, s_real) if p and p != d_real}, key=len, reverse=True)
    pat = re.compile("|".join(re.escape(p) for p in prefixes) + r"(?=[/\"\\'\s]|$)") if prefixes else None
    remap = (lambda t: pat.sub(lambda _m: d_real, t)) if pat else (lambda t: t)
    new_cwd = remap(cwd)

    # 3. rewrite the text files; drop a half-written last line of a transcript still being written
    src_tar, out = tarfile.open(fileobj=io.BytesIO(blob)), io.BytesIO()
    dst_tar = tarfile.open(fileobj=out, mode="w")
    for m in src_tar.getmembers():
        if (m.name != sid + ".jsonl" and not m.name.startswith(sid + "/")) or ".." in m.name.split("/") or m.issym() or m.islnk():
            continue
        if not m.isfile():
            dst_tar.addfile(m); continue
        data = src_tar.extractfile(m).read()
        try:
            text = data.decode()
        except UnicodeDecodeError:
            text = None
        if text is not None:
            if m.name.endswith(".jsonl") and text and not text.endswith("\n"):
                text = text[: text.rfind("\n") + 1]
            data = remap(text).encode()
        m.size = len(data)
        dst_tar.addfile(m, io.BytesIO(data))
    dst_tar.close()

    # 4. a copy already on the destination must not have turns the source lacks (it was continued there)
    ddir = f'"$HOME"/.claude/projects/{project_dir(new_cwd)}'
    if not force:
        tail = run(dst, f"[ -f {ddir}/{sid}.jsonl ] && tail -n 300 {ddir}/{sid}.jsonl || true")
        theirs = set(UUID_RE.findall(tail))
        if theirs:
            ours = set(UUID_RE.findall(tarfile.open(fileobj=io.BytesIO(blob)).extractfile(f"{sid}.jsonl").read()))
            extra = len(theirs - ours)
            if extra:
                raise Refused(f"the copy on {dst_name} has diverged ({extra}+ entries not on {src_name}): "
                              f"it was continued there. Move it the other way, or force to replace it (it is backed up)")

    # 5. unpack on the destination, keeping any existing copy aside
    run(dst, f"""set -e; mkdir -p {ddir}; cd {ddir}
if [ -e {sid}.jsonl ] || [ -e {sid} ]; then b=.switchboard-moved/$(date +%Y%m%d-%H%M%S)-$$; mkdir -p "$b"
  [ -e {sid}.jsonl ] && mv {sid}.jsonl "$b"/; [ -e {sid} ] && mv {sid} "$b"/; fi
tar xf -""", data=out.getvalue())

    resume = f"cd {shlex.quote(new_cwd)} && claude -r {sid}"
    if dst:
        resume = f"ssh -t {dst} {shlex.quote(resume)}"
    return {"ok": True, "from": src_name, "to": dst_name, "cwd": new_cwd, "resume": resume,
            "note": f"copied to {dst_name}; the session on {src_name} is still open there"}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--force"]
    if len(args) != 3:
        sys.exit(__doc__)
    try:
        print(json.dumps(move(*args, force="--force" in sys.argv[1:])))
    except Refused as e:
        print(json.dumps({"ok": False, "error": str(e), "forceable": True}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)
