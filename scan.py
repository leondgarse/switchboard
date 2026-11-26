#!/usr/bin/env python3
"""Scan git repos under ~/workspace and emit an Obsidian-style vault + graph.json.

Edges:
  submodule  - parent declares child in .gitmodules
  nested     - repo lives inside another repo's folder (not a submodule)
  mentions   - repo A's tracked text files mention repo B by name / package name
  stack      - repos share notable dependencies (weighted by count)
Tags (Obsidian #tags, hub nodes in the HTML): org/<owner>, lang/<language>.
"""
import argparse, json, os, re, subprocess, sys
try:
    import tomllib   # 3.11+; only needed to read pyproject/Cargo files during a full scan
except ImportError:
    tomllib = None
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(OUT))
import layout as layout_mod  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("root", nargs="?", default="~/workspace")
ap.add_argument("--render-only", action="store_true", help="skip scanning; rebuild layout/vault/graph.js from graph.json")
ap.add_argument("--relayout", action="store_true", help="discard saved positions and lay everything out again")
ARGS = ap.parse_args()
ROOT = Path(os.path.expanduser(ARGS.root))
CONFIG_PATH, LAYOUT_PATH, LABELS_PATH = OUT / "config.json", OUT / "layout.json", OUT / "labels.json"
DEFAULT_CONFIG = {"center": "case-demos", "exclude": ["temp"]}
SKIP_PARTS = {"node_modules", ".claudeprism", ".prism", ".venv", "venv", "dist", "build"}
TEXT_EXT = {".md", ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".toml", ".yaml", ".yml",
            ".txt", ".rs", ".go", ".sh", ".swift", ".html", ".css", ".ipynb", ".cfg", ".ini", ".tex", ".mjs"}
LANG = {".py": "Python", ".ipynb": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript",
        ".jsx": "JavaScript", ".mjs": "JavaScript", ".rs": "Rust", ".go": "Go", ".swift": "Swift",
        ".m": "ObjC", ".java": "Java", ".kt": "Kotlin", ".c": "C", ".cpp": "C++", ".cc": "C++", ".h": "C",
        ".tex": "TeX", ".sh": "Shell", ".vue": "Vue", ".rb": "Ruby", ".cs": "C#", ".dart": "Dart"}
# names too generic to count as a cross-reference when found in text
GENERIC = {"temp", "portfolio", "test-cases", "admin-ui", "admin-service", "agent-service", "ai-core",
           "evaluation", "zef"}
# dependencies too ubiquitous to say anything about a relationship
COMMON_DEPS = {"typescript", "eslint", "prettier", "pytest", "requests", "numpy", "python-dotenv", "dotenv",
               "@types/node", "@types/react", "@types/react-dom", "vite", "@vitejs/plugin-react", "tqdm",
               "pyyaml", "setuptools", "wheel", "black", "ruff", "mypy", "pydantic", "python", "serde",
               "serde_json", "anyhow", "tokio", "thiserror", "jest", "vitest", "postcss", "autoprefixer",
               "tailwindcss", "pillow", "rich", "click", "typer", "httpx", "aiohttp"}
GENERIC_PKG = {"web", "website", "docs", "app", "core", "root", "client", "server", "frontend", "backend",
               "ui", "export-schema", "make-test-images", "pyo3-test", "northstar"}
MAX_FILES, MAX_BYTES = 4000, 300_000


def git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def find_repos():
    repos = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        p = Path(dirpath)
        if any(part in SKIP_PARTS for part in p.relative_to(ROOT).parts):
            dirnames[:] = []
            continue
        if ".git" in dirnames or ".git" in filenames:
            repos.append(p)
        dirnames[:] = [d for d in dirnames if d != ".git" and d not in SKIP_PARTS and not d.startswith(".")]
        if len(p.relative_to(ROOT).parts) >= 3:
            dirnames[:] = []
    return sorted(repos)


def norm_dep(name):
    return re.split(r"[<>=!~\[;@ ]", name.strip(), maxsplit=1)[0].lower().replace("_", "-") if name else ""


def deps_of(repo, files):
    deps = set()
    for f in files:
        if len(Path(f).parts) > 3:
            continue
        path, base = repo / f, Path(f).name
        try:
            if base == "package.json":
                d = json.loads(path.read_text())
                for k in ("dependencies", "devDependencies", "peerDependencies"):
                    deps |= {n.lower() for n in d.get(k, {})}
            elif re.match(r"requirements.*\.txt$", base):
                for line in path.read_text().splitlines():
                    line = line.split("#")[0].strip()
                    if line and not line.startswith("-"):
                        deps.add(norm_dep(line))
            elif base == "pyproject.toml":
                d = tomllib.loads(path.read_text())
                proj = d.get("project", {})
                items = list(proj.get("dependencies", []))
                for v in proj.get("optional-dependencies", {}).values():
                    items += v
                items += list(d.get("tool", {}).get("poetry", {}).get("dependencies", {}).keys())
                deps |= {norm_dep(x) for x in items}
            elif base == "Cargo.toml":
                d = tomllib.loads(path.read_text())
                for k in ("dependencies", "dev-dependencies", "workspace"):
                    v = d.get(k, {})
                    if k == "workspace":
                        v = v.get("dependencies", {})
                    deps |= {n.lower() for n in v}
            elif base == "go.mod":
                for m in re.finditer(r"^\s*([\w./-]+\.[\w./-]+)\s+v", path.read_text(), re.M):
                    deps.add(m.group(1).lower())
        except Exception:
            pass
    deps.discard("")
    return deps


def package_names(repo, files):
    """Names this repo publishes under (so others can depend on it)."""
    names = set()
    for f in files:
        if len(Path(f).parts) > 2:
            continue
        base = Path(f).name
        try:
            if base == "package.json":
                n = json.loads((repo / f).read_text()).get("name")
                n and names.add(n.lower())
            elif base in ("pyproject.toml", "Cargo.toml"):
                d = tomllib.loads((repo / f).read_text())
                n = d.get("project", {}).get("name") or d.get("package", {}).get("name") \
                    or d.get("tool", {}).get("poetry", {}).get("name")
                n and names.add(norm_dep(n))
        except Exception:
            pass
    return names


def readme_summary(repo, files):
    top = {f.lower(): f for f in files if len(Path(f).parts) == 1}
    order = ["readme.md"] + sorted(k for k in top if k.startswith("readme") and k != "readme.md") \
        + ["claude.md", "agents.md"]
    for key in order:
        if key not in top:
            continue
        try:
            text = re.sub(r"<!--.*?-->", "", (repo / top[key]).read_text(errors="ignore"), flags=re.S)
        except Exception:
            continue
        in_code = False
        for line in text.splitlines():
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code or line.lstrip().startswith(("#", "<", "|", "!", ">", "-", "*")) and not line.lstrip().startswith("**"):
                continue
            s = re.sub(r"<[^>]+>|!\[[^\]]*\]\([^)]*\)", "", line)
            s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s).replace("**", "").replace("`", "").strip()
            cjk = len(re.findall(r"[\u4e00-\u9fff]", s))
            if ("|" in s or "→" in s) or (len(s.split()) < 6 and cjk < 10):
                continue
            return s[:220]
    return ""


def main():
    if ARGS.render_only:
        render(json.loads((OUT / "graph.json").read_text()))
    else:
        scan()


def scan():
    repo_paths = find_repos()
    repos = {}
    for p in repo_paths:
        rel = str(p.relative_to(ROOT))
        files = [f for f in git(p, "ls-files").splitlines()
                 if not any(part in SKIP_PARTS for part in Path(f).parts)]
        remote = git(p, "remote", "get-url", "origin")
        m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", remote)
        owner, rname = (m.group(1), m.group(2)) if m else ("local", p.name)
        exts = Counter(LANG[Path(f).suffix.lower()] for f in files if Path(f).suffix.lower() in LANG)
        repos[rel] = dict(
            id=rel, name=p.name, path=str(p), remote=remote, owner=owner, remote_name=rname,
            last_commit=git(p, "log", "-1", "--format=%cs"), first_commit=git(p, "log", "--reverse", "--format=%cs", "--max-parents=0").split("\n")[0],
            commits=int(git(p, "rev-list", "--count", "HEAD") or 0), branch=git(p, "branch", "--show-current"),
            files=len(files), langs=[l for l, _ in exts.most_common(4)], lang_counts=dict(exts.most_common(6)),
            deps=sorted(deps_of(p, files)), packages=sorted(package_names(p, files)),
            summary=readme_summary(p, files), _files=files,
        )
        print(f"scanned {rel:40s} {len(files):6d} files", file=sys.stderr)

    edges = []
    ids = list(repos)

    # submodules + nesting
    for rid in ids:
        gm = Path(repos[rid]["path"]) / ".gitmodules"
        subs = set()
        if gm.exists():
            for m in re.finditer(r"path\s*=\s*(.+)", gm.read_text()):
                subs.add(str((Path(rid) / m.group(1).strip())))
        for cid in ids:
            if cid != rid and cid.startswith(rid + "/") and "/" not in cid[len(rid) + 1:]:
                edges.append(dict(source=rid, target=cid, type="submodule" if cid in subs else "nested",
                                  label="submodule" if cid in subs else "lives inside"))

    # mentions: search each repo's text for other repos' names / remote names / package names
    aliases = {}
    for rid, r in repos.items():
        cand = {r["name"], r["remote_name"], *r["packages"]}
        cand = {c for c in cand if len(c) >= 4 and c.lower() not in GENERIC and c.lower() not in GENERIC_PKG
                and not c.startswith("@")}
        if cand:
            aliases[rid] = cand
    pats = {rid: re.compile(r"(?<![\w-])(" + "|".join(re.escape(a) for a in sorted(al, key=len, reverse=True))
                            + r")(?![\w-])", re.I) for rid, al in aliases.items()}
    for rid, r in repos.items():
        hits = defaultdict(list)
        own = {r["name"].lower(), r["remote_name"].lower(), *r["packages"]}
        n = 0
        for f in r["_files"]:
            if Path(f).suffix.lower() not in TEXT_EXT and not Path(f).name.lower().startswith("readme"):
                continue
            fp = Path(r["path"]) / f
            try:
                if fp.stat().st_size > MAX_BYTES:
                    continue
                text = fp.read_text(errors="ignore")
            except Exception:
                continue
            n += 1
            for tid, pat in pats.items():
                if tid == rid or tid.startswith(rid + "/") or rid.startswith(tid + "/"):
                    continue
                if {a.lower() for a in aliases[tid]} <= own:
                    continue
                if pat.search(text):
                    hits[tid].append(f)
            if n >= MAX_FILES:
                break
        for tid, fl in hits.items():
            edges.append(dict(source=rid, target=tid, type="mentions", weight=len(fl),
                              label=f"mentioned in {len(fl)} file(s)", files=fl[:8]))

    # dependency on another workspace repo's published package
    pkg_owner = {pkg: rid for rid, r in repos.items() for pkg in r["packages"]}
    for rid, r in repos.items():
        for d in r["deps"]:
            tid = pkg_owner.get(d)
            if tid and tid != rid:
                edges.append(dict(source=rid, target=tid, type="depends", label=f"depends on {d}"))

    # shared notable deps
    notable = {rid: set(r["deps"]) - COMMON_DEPS for rid, r in repos.items()}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            shared = sorted(notable[a] & notable[b])
            if len(shared) >= 3:
                edges.append(dict(source=a, target=b, type="stack", weight=len(shared),
                                  label=f"{len(shared)} shared deps", shared=shared[:15]))

    for r in repos.values():
        r.pop("_files")
    graph = dict(root=str(ROOT), generated=subprocess.run(["date", "+%Y-%m-%d %H:%M"], capture_output=True,
                 text=True).stdout.strip(), nodes=list(repos.values()), edges=edges)
    (OUT / "graph.json").write_text(json.dumps(graph, indent=1))
    render(graph)


def render(graph):
    """Apply config (center/exclude) + stable layout, then write graph.js and the vault."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    config = {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
    excluded = set(config["exclude"])
    ids = [n["id"] for n in graph["nodes"]]
    saved = {} if ARGS.relayout else layout_mod.load(LAYOUT_PATH)
    kept = [i for i in ids if i not in excluded]
    if not saved or not any(k in saved for k in kept):
        saved, crossings = layout_mod.architecture(kept, graph["edges"], config.get("center"))
        print(f"architecture layout: {crossings} crossing(s) between columns", file=sys.stderr)
    pos = layout_mod.place_new(ids, graph["edges"], saved)
    LAYOUT_PATH.write_text(json.dumps(pos, indent=1) + "\n")
    labels = json.loads(LABELS_PATH.read_text()) if LABELS_PATH.exists() else {}
    (OUT / "graph.js").write_text("window.GRAPH = " + json.dumps(graph) + ";\nwindow.CONFIG = " + json.dumps(config)
                                  + ";\nwindow.LAYOUT = " + json.dumps(pos) + ";\nwindow.LABELS = " + json.dumps(labels) + ";\n")
    repos = {n["id"]: n for n in graph["nodes"] if n["id"] not in excluded}
    edges = [e for e in graph["edges"] if e["source"] in repos and e["target"] in repos]
    write_vault(repos, edges, config.get("center"), labels)
    print(f"{len(repos)} repos ({len(excluded & set(ids))} removed), {len(edges)} edges -> {OUT}", file=sys.stderr)


def slug(rid):
    return rid.replace("/", "__")


def write_vault(repos, edges, center=None, labels=None):
    labels = labels or {}
    lab = lambda a, b: (f" **{labels[f'{a}>{b}']}** ·" if labels.get(f"{a}>{b}") else "")
    vault = OUT / "vault"
    vault.mkdir(exist_ok=True)
    for old in vault.glob("*.md"):
        old.unlink()
    out_links, in_links = defaultdict(list), defaultdict(list)
    for e in edges:
        out_links[e["source"]].append(e)
        in_links[e["target"]].append(e)
    for rid, r in repos.items():
        tags = [f"#org/{r['owner']}"] + [f"#lang/{l.replace('+', 'p').replace('#', 'sharp')}" for l in r["langs"][:2]]
        if rid == center:
            tags.append("#hub")
        lines = ["---", f"path: {r['path']}", f"remote: {r['remote'] or 'none'}", f"owner: {r['owner']}",
                 f"commits: {r['commits']}", f"last_commit: {r['last_commit']}", f"branch: {r['branch']}", "---",
                 f"# {r['name']}", "", " ".join(tags), ""]
        if r["summary"]:
            lines += [f"> {r['summary']}", ""]
        lines += [f"- **Languages:** {', '.join(f'{k} ({v})' for k, v in r['lang_counts'].items()) or '—'}",
                  f"- **Files tracked:** {r['files']}  ·  **Commits:** {r['commits']}  ·  "
                  f"**Active:** {r['first_commit']} → {r['last_commit']}", ""]
        def section(title, items):
            if items:
                lines.extend([f"## {title}", *items, ""])
        section("Contains", [f"- [[{slug(e['target'])}|{repos[e['target']]['name']}]] ({e['label']})"
                             for e in out_links[rid] if e["type"] in ("submodule", "nested")])
        section("Part of", [f"- [[{slug(e['source'])}|{repos[e['source']]['name']}]] ({e['label']})"
                            for e in in_links[rid] if e["type"] in ("submodule", "nested")])
        section("Depends on", [f"- [[{slug(e['target'])}]] —{lab(rid, e['target'])} {e['label']}" for e in out_links[rid] if e["type"] == "depends"])
        section("Mentions", [f"- [[{slug(e['target'])}]] —{lab(rid, e['target'])} {e['label']}: " + ", ".join(f"`{f}`" for f in e["files"][:3])
                             for e in out_links[rid] if e["type"] == "mentions"])
        section("Mentioned by", [f"- [[{slug(e['source'])}]] —{lab(e['source'], rid)} {e['label']}" for e in in_links[rid] if e["type"] == "mentions"])
        stack = [e for e in out_links[rid] + in_links[rid] if e["type"] == "stack"]
        section("Shares stack with", [f"- [[{slug(e['target'] if e['source'] == rid else e['source'])}]] — "
                                      + ", ".join(e["shared"][:8]) for e in sorted(stack, key=lambda e: -e["weight"])])
        if r["deps"]:
            lines += ["## Dependencies", ", ".join(f"`{d}`" for d in r["deps"][:60])
                      + (f" … (+{len(r['deps']) - 60})" if len(r["deps"]) > 60 else ""), ""]
        (vault / f"{slug(rid)}.md").write_text("\n".join(lines))
    # index / MOC
    by_owner = defaultdict(list)
    for rid, r in repos.items():
        by_owner[r["owner"]].append(rid)
    idx = ["# Workspace repo map", "", f"{len(repos)} git repositories under `{ROOT}`, {len(edges)} connections.", ""]
    if center in repos:
        idx += [f"**Hub:** [[{slug(center)}|{repos[center]['name']}]]", ""]
    for owner, rids in sorted(by_owner.items(), key=lambda kv: -len(kv[1])):
        idx += [f"## {owner}", *[f"- [[{slug(x)}|{repos[x]['name']}]] — {repos[x]['summary'][:90]}" for x in sorted(rids)], ""]
    (vault / "_Index.md").write_text("\n".join(idx))


if __name__ == "__main__":
    main()
