#!/usr/bin/env python3
"""switchboard tasks: your own task list (tasks.md) next to every repo's TODO.md, with Claude reviews.

  tasks.py import-todoist dashboard.csv     add a Todoist CSV export to tasks.md (deduplicated)
  tasks.py add "<project>" "<task text>"    add one task (then reviews it in the background)
  tasks.py set <id> open|doing|done|dropped change a task's state
  tasks.py edit <id> key=value …            change text=, project=, pri=high|"", review=skip|"" or due=YYYY-MM-DD|""
                                            (repo=<folder under ~/workspace>|"" relinks the task's whole project)
  (add: start the text with "! " for high priority, "- " to skip the review; both work: "! - text")
  tasks.py review [<id>…] [--all]           ask Claude whether new tasks are clear + for a short proposal
  tasks.py collect                          read tasks.md and the repos' TODO.md files into tasks.js

Reviews run `claude -p` locally in the task's repo, with read-only tools (Read, Grep, Glob) and the room hooks
off. Results are kept in reviews.json, not in tasks.md. Python 3.9+, stdlib only.
"""
import csv, hashlib, json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_DIR = HERE.parent
TASKS_MD, REVIEWS, OUT_JS = HERE / "tasks.md", HERE / "reviews.json", HERE / "tasks.js"
WORKSPACE = Path(os.path.expanduser("~/workspace"))
GPU_HOST = "gwwang@micl-loyccws3.dynip.ntu.edu.sg"   # where work that needs a GPU runs
STATES = {" ": "open", "~": "doing", "x": "done", "X": "done", "-": "dropped"}
MARK = {v: k for k, v in STATES.items() if k != "X"}
# Todoist project headings -> repo (and folder inside it)
PROJECT_REPOS = {
    "keystone": "keystone-ai-system", "pdf adapter": "keystone-ai-system/pdf_adapter", "ocr": "ocr-bench",
    "rstn": "rstn_local", "cocoa": "Cocoa_Shared", "case-demo": "case-demos", "case-demos": "case-demos",
    "switchboard": "switchboard",
}
# Todoist top-level tasks (no children) that belong to a project
TASK_PROJECTS = {"claude task board and chatting room": "switchboard",
                 "a todo list that agent can auto track, pick, update": "switchboard"}

HEAD_RE = re.compile(r"^##\s+(.+?)\s*$")
ITEM_RE = re.compile(r"^- \[([ ~xX-])\]\s+(.*?)\s*(?:<!--\s*t:(\w+)((?:\s+\w+:\S+)*)\s*-->)?\s*$")
META = ("pri", "review", "due", "added", "since", "done")   # review:skip = never auto-reviewed   # kept in the item's comment: <!-- t:id pri:high due:2026-12-01 … -->


def tid(project, text):
    return hashlib.sha1(f"{project}\n{text}".encode()).hexdigest()[:8]


# ---- tasks.md ----------------------------------------------------------------------------------------------
def load_tasks():
    """[{id, project, repo, state, text, line}] in file order. Project headings: `## Name` or `## Name · repo: id`."""
    out, project, repo = [], "General", ""
    if not TASKS_MD.exists():
        return out
    for i, line in enumerate(TASKS_MD.read_text().splitlines(), 1):
        m = HEAD_RE.match(line)
        if m:
            name = m.group(1)
            project, _, r = name.partition(" · repo: ")
            project, repo = project.strip(), r.strip()
            continue
        m = ITEM_RE.match(line)
        if m:
            text = m.group(2)
            meta = dict(kv.split(":", 1) for kv in (m.group(4) or "").split())
            out.append({"id": m.group(3) or tid(project, text), "project": project, "repo": repo,
                        "state": STATES[m.group(1)], "text": text, "line": i,
                        **{k: meta.get(k, "") for k in META}})
    return out


def write_tasks(tasks):
    order = []
    for t in tasks:
        if (t["project"], t["repo"]) not in order:
            order.append((t["project"], t["repo"]))
    lines = ["# Tasks", "",
             "Your own task list, by project. Edit here or in the Tasks tab; format: TODO_FORMAT.md.", ""]
    for project, repo in order:
        lines.append(f"## {project}" + (f" · repo: {repo}" if repo else ""))
        for t in tasks:
            if (t["project"], t["repo"]) == (project, repo):
                meta = "".join(f" {k}:{t[k]}" for k in META if t.get(k))
                lines.append(f"- [{MARK[t['state']]}] {t['text']} <!-- t:{t['id']}{meta} -->")
        lines.append("")
    TASKS_MD.write_text("\n".join(lines))


def today():
    return time.strftime("%Y-%m-%d")


def repo_for(project, tasks):
    repo = next((t["repo"] for t in tasks if t["project"] == project), None)
    if repo is None:
        repo = PROJECT_REPOS.get(project.lower(), "")
    if not repo and (WORKSPACE / project / ".git").exists():   # a project named after a repo links to it
        repo = project
    return repo


def add_task(project, text, repo=None):
    tasks = load_tasks()
    text = " ".join(text.split())
    pri = skip = ""
    while text[:2] in ("! ", "- "):   # "! " = high priority, "- " = no Claude review
        pri, skip = (("high", skip) if text[0] == "!" else (pri, "skip"))
        text = text[2:].strip()
    if repo is None:
        repo = repo_for(project, tasks)
    t = {"id": tid(project, text), "project": project, "repo": repo, "state": "open", "text": text,
         "pri": pri, "review": skip, "due": "", "added": today(), "since": "", "done": ""}
    if any(x["id"] == t["id"] for x in tasks):
        return None
    # keep a project's tasks together
    idx = max((i for i, x in enumerate(tasks) if x["project"] == project), default=len(tasks) - 1) + 1
    tasks.insert(idx, t)
    write_tasks(tasks)
    return t


def set_state(t, state):
    if state not in MARK:
        raise SystemExit(f"unknown state {state!r}")
    if state != t["state"]:
        t["done"] = today() if state in ("done", "dropped") else ""
        t["since"] = today() if state == "doing" else ""
    t["state"] = state


def edit_task(t, tasks, changes):
    for kv in changes:
        k, _, v = kv.partition("=")
        v = " ".join(v.split())
        if k == "text" and v:
            t["text"] = v
        elif k == "project" and v and v != t["project"]:
            t["project"], t["repo"] = v, repo_for(v, [x for x in tasks if x is not t])
            tasks.remove(t)   # move it to the end of its new project
            idx = max((i for i, x in enumerate(tasks) if x["project"] == v), default=len(tasks) - 1) + 1
            tasks.insert(idx, t)
        elif k == "repo" and (v == "" or (WORKSPACE / v).is_dir()):
            for x in tasks:   # the repo belongs to the project heading, so every task of the project moves with it
                if x["project"] == t["project"]:
                    x["repo"] = v
        elif k == "pri" and v in ("", "high"):
            t["pri"] = v
        elif k == "review" and v in ("", "skip"):
            t["review"] = v
        elif k == "due" and (v == "" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", v)):
            t["due"] = v
        else:
            raise SystemExit(f"can't set {kv!r}")


def import_todoist(path):
    rows = [r for r in csv.DictReader(open(path, encoding="utf-8-sig")) if r.get("TYPE") == "task"]
    added, project = [], None
    for i, r in enumerate(rows):
        text, indent = r["CONTENT"].strip(), int(r["INDENT"] or 1)
        has_children = i + 1 < len(rows) and int(rows[i + 1]["INDENT"] or 1) > indent
        if indent == 1 and (has_children or text.lower() in PROJECT_REPOS):
            project = text
            continue
        if indent == 1:   # a task of its own at top level
            proj = TASK_PROJECTS.get(text.lower())
            t = add_task(proj or "General", text, PROJECT_REPOS.get(proj or "", "") if proj else "")
        else:
            t = add_task(project or "General", text)
        if t:
            added.append(t)
    return added


# ---- repo TODO.md files --------------------------------------------------------------------------------------
def section_kind(title):
    t = title.lower()
    if re.search(r"\b(done|finished|shipped|closed|completed)\b", t): return "done"
    if re.search(r"\b(reject|dropped|won't|abandon)", t): return "dropped"
    if re.search(r"\b(in progress|doing|current|active|now)\b", t): return "doing"
    return "open"


def parse_todo(path):
    """Tolerant reader: checkbox items (any section), or table rows; reports how structured the file is."""
    items, section, kind, in_code = [], "", "open", False
    header = None
    lines = path.read_text(errors="ignore").splitlines()
    for i, line in enumerate(lines, 1):
        if line.strip().startswith("```"):
            in_code = not in_code
        if in_code:
            continue
        m = re.match(r"^#{2,4}\s+(.+)", line)
        if m:
            section, kind, header = m.group(1).strip(), section_kind(m.group(1)), None
            continue
        m = re.match(r"^\s{0,3}[-*] \[([ ~xX-])\]\s+(.*)", line)
        if m:
            st = STATES[m.group(1)]
            items.append({"state": st, "text": clean(m.group(2)), "section": section, "line": i, "kind": "box"})
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if header is None:
                header = cells
                continue
            if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                continue
            if cells and cells[0]:
                items.append({"state": kind, "text": clean(cells[0]), "section": section, "line": i, "kind": "row"})
    boxes = sum(1 for x in items if x["kind"] == "box")
    if boxes >= 3:   # a checkbox board: its tables are notes, not tasks
        items = [x for x in items if x["kind"] == "box"]
    fmt = "board" if boxes >= 3 else ("table" if items else "prose")
    return items, fmt


def clean(s):
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    return s.replace("`", "").replace("**", "").replace("~~", "").strip()[:300]


def repo_list():
    """Repos from the last scan, plus top-level repos under ~/workspace that it hasn't seen yet."""
    try:
        nodes = [{"id": n["id"], "path": n["path"]} for n in json.loads((ROOT_DIR / "graph.json").read_text())["nodes"]]
    except (OSError, ValueError, KeyError):
        nodes = []
    known = {os.path.realpath(n["path"]) for n in nodes}
    try:
        excluded = set(json.loads((ROOT_DIR / "config.json").read_text()).get("exclude", []))
    except (OSError, ValueError):
        excluded = set()
    for g in sorted(WORKSPACE.glob("*/.git")):
        if os.path.realpath(g.parent) not in known and g.parent.name not in excluded:
            nodes.append({"id": g.parent.name, "path": str(g.parent)})
    return nodes


def repo_todos():
    """Every TODO.md in each repo that git tracks or would track (new files count before their first commit)."""
    out = []
    for n in repo_list():
        try:
            files = subprocess.run(["git", "-C", n["path"], "ls-files", "--cached", "--others", "--exclude-standard",
                                    "*TODO.md", "TODO.md"],
                                   capture_output=True, text=True, timeout=10).stdout.split()
        except (OSError, subprocess.SubprocessError):
            continue
        for f in sorted(set(files)):
            p = Path(n["path"]) / f
            if not p.exists() or any(part.startswith(".") for part in Path(f).parts):
                continue
            items, fmt = parse_todo(p)
            out.append({"repo": n["id"], "file": f, "path": str(p), "format": fmt, "items": items,
                        "mtime": p.stat().st_mtime})
    return out


# ---- reviews -------------------------------------------------------------------------------------------------
PROMPT = """You review one short personal task note before it is worked on.

Project: {project}
Repo: {repo} (current folder){where}
Task note: "{text}"

Look at the code only as much as you need to (a few searches or file reads at most); do not change anything.
Reply with ONLY a JSON object, no prose around it:
{{"clear": true or false (could a developer start without asking anything?),
 "questions": [at most 2 short questions, only if not clear],
 "title": "the task restated as one clear imperative line",
 "proposal": "3 to 5 short lines: where in the code, the approach, the main risk",
 "files": [up to 4 repo-relative paths that matter],
 "size": "S" (hours) or "M" (a day or two) or "L" (more),
 "needs_gpu": true or false (does doing or testing it need a GPU server?)}}"""


def load_reviews():
    try:
        return json.loads(REVIEWS.read_text())
    except (OSError, ValueError):
        return {}


def review_one(t):
    repo = t["repo"] or ""
    sub = ""
    if "/" in repo and not (WORKSPACE / repo / ".git").exists():   # a folder inside a repo, e.g. keystone/pdf_adapter
        sub = repo
    cwd = WORKSPACE / repo if repo and (WORKSPACE / repo).exists() else WORKSPACE
    prompt = PROMPT.format(project=t["project"], repo=repo or "none (whole workspace)", text=t["text"],
                           where=f"\nFocus on the folder {sub}." if sub else "")
    env = dict(os.environ, ROOM_CONFIG="/nonexistent")   # keep review runs out of the room
    started = time.time()
    try:
        r = subprocess.run(["claude", "-p", prompt, "--model", "sonnet", "--max-turns", "8", "--output-format", "json",
                            "--allowedTools", "Read,Grep,Glob"], cwd=str(cwd), env=env,
                           capture_output=True, text=True, timeout=300)
        res = json.loads(r.stdout)
        text = res.get("result", "")
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {"error": "no JSON in reply", "raw": text[:500]}
        data["cost_usd"] = res.get("total_cost_usd")
    except Exception as e:
        data = {"error": str(e)[:300]}
    data.update(ts=time.time(), secs=round(time.time() - started), text=t["text"], cwd=str(cwd))
    if data.get("needs_gpu"):
        data["gpu_host"] = GPU_HOST
    return t["id"], data


def review(ids=None, all_=False):
    tasks = [t for t in load_tasks() if t["state"] in ("open", "doing")]
    reviews = load_reviews()
    # an explicit id is always reviewed; otherwise tasks marked review:skip are left alone
    todo = [t for t in tasks if (ids and t["id"] in ids) or (not ids and t["review"] != "skip" and
                                                             (all_ or t["id"] not in reviews or "error" in reviews[t["id"]]))]
    if not todo:
        print("nothing to review")
        return
    print(f"reviewing {len(todo)} task(s)…", flush=True)
    with ThreadPoolExecutor(3) as pool:
        for tid_, data in pool.map(review_one, todo):
            reviews = load_reviews()
            reviews[tid_] = data
            REVIEWS.write_text(json.dumps(reviews, indent=1, ensure_ascii=False) + "\n")
            print(f"  {tid_}: {'error: ' + data['error'] if 'error' in data else ('clear' if data.get('clear') else 'unclear') + ' ' + str(data.get('size'))}", flush=True)
            collect()


def send(task_id):
    """Post a task (and its review's proposal) to the live sessions of its repo through the room."""
    t = next((x for x in load_tasks() if x["id"] == task_id), None)
    if not t or not t["repo"]:
        raise SystemExit("task not found, or it has no repo")
    rv = load_reviews().get(task_id, {})
    body = f"Task from the board ({t['project']}): {rv.get('title') or t['text']}"
    if rv.get("proposal"):
        body += "\nProposal: " + rv["proposal"]
    repo = t["repo"].split("/")[0] if not (WORKSPACE / t["repo"] / ".git").exists() else t["repo"]
    room = os.path.expanduser("~/.local/bin/room")
    r = subprocess.run([room, "send", "repo:" + repo, body], capture_output=True, text=True, timeout=20,
                       env={k: v for k, v in os.environ.items() if k != "ROOM_SESSION"})
    print((r.stdout or r.stderr).strip())


# ---- output for the page ---------------------------------------------------------------------------------
def collect():
    data = {"generated": time.time(), "tasks": load_tasks(), "reviews": load_reviews(), "todos": repo_todos()}
    OUT_JS.write_text("window.TASKS = " + json.dumps(data, ensure_ascii=False) + ";\n")
    return data


def main():
    a = sys.argv[1:]
    cmd = a.pop(0) if a else "collect"
    if cmd == "import-todoist":
        added = import_todoist(a[0])
        print(f"imported {len(added)} task(s) into {TASKS_MD}")
        collect()
    elif cmd == "add":
        t = add_task(a[0], a[1])
        collect()
        if not t:
            return print("already there")
        print(f"added {t['id']}")
        if "--no-review" not in a and t["review"] != "skip":   # review in the background, so adding returns at once
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "review", t["id"]], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif cmd in ("set", "edit"):
        tasks = load_tasks()
        t = next((x for x in tasks if x["id"] == a[0]), None)
        if not t:
            raise SystemExit("no task " + a[0])
        set_state(t, a[1]) if cmd == "set" else edit_task(t, tasks, a[1:])
        write_tasks(tasks)
        collect()
    elif cmd == "send":
        send(a[0])
    elif cmd == "review":
        review([x for x in a if not x.startswith("--")] or None, "--all" in a)
    elif cmd == "collect":
        d = collect()
        print(f"{len(d['tasks'])} tasks, {sum(len(f['items']) for f in d['todos'])} items in {len(d['todos'])} TODO files")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
