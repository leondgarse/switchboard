"""Stable positions for the repo architecture map.

Positions live in layout.json and are never recomputed for a repo that already has one;
links can change underneath without moving anything. `scan.py --relayout` (or "Tidy layout"
in the page) discards layout.json and computes a fresh layered layout.
"""
import json, math
from pathlib import Path

def load(path: Path):
    return json.loads(path.read_text()) if path.exists() else {}


def place_new(node_ids, edges, layout):
    """Keep every saved position; a repo seen for the first time goes to the end of the standalone shelf
    (a new submodule just becomes another row of its system box)."""
    out = dict(layout)
    out.setdefault("_routes", {}); out.setdefault("_kids", {})
    ids = set(node_ids)
    parent_of = {e["target"]: e["source"] for e in edges
                 if e["type"] in ("submodule", "nested") and e["source"] in ids and e["target"] in ids}
    for c, p in sorted(parent_of.items()):
        ks = out["_kids"].setdefault(p, [])
        if c not in ks:
            ks.append(c)
    shelf_y = out.get("_shelf_y", 0) + 40
    on_shelf = [v for k, v in out.items() if not k.startswith("_") and v[1] >= shelf_y]
    for n in sorted(ids):
        if n in parent_of or n in out:
            continue
        last = max(on_shelf, key=lambda v: (v[1], v[0]), default=[-(CARD_W + 40), shelf_y])
        x, y = last[0] + CARD_W + 40, last[1]
        if x > 4 * (CARD_W + 40):
            x, y = 0, y + CARD_H + 30
        out[n] = [x, y]; on_shelf.append(out[n])
    return out


# ---------------------------------------------------------------------------
# Architecture layout (layered / Sugiyama style)
#   * a repo with submodules or nested repos is one box ("system") with its children as rows
#   * references (mentions / depends) flow left -> right in columns; cycles are broken so the hub is first
#   * row order inside every column comes from barycenter sweeps that minimise crossings
#   * repos with no external references go on a "standalone" shelf underneath
# Output: {super_id: [x, y_top]} plus "_routes" (lane hints for edges spanning columns) and "_kids" (row order).
# ---------------------------------------------------------------------------
from collections import defaultdict

CARD_W, CARD_H, HEAD_H, ROW_H, COL_GAP, V_GAP = 232, 58, 58, 38, 180, 34


def box_height(n_kids):
    return CARD_H if not n_kids else HEAD_H + n_kids * ROW_H + 8


def _crossings(layers, nbr_down):
    total = 0
    for li in range(len(layers) - 1):
        idx = {n: i for i, n in enumerate(layers[li + 1])}
        segs = [(i, idx[m]) for i, n in enumerate(layers[li]) for m in nbr_down.get(n, []) if m in idx]
        for a in range(len(segs)):
            for b in range(a + 1, len(segs)):
                if (segs[a][0] - segs[b][0]) * (segs[a][1] - segs[b][1]) < 0:
                    total += 1
    return total


def _layer_component(comp, E, hub, height):
    """E: directed edges (left -> right) inside comp. Returns pos, routes, crossings."""
    succ, pred = defaultdict(list), defaultdict(list)
    for s, t in E:
        succ[s].append(t); pred[t].append(s)
    # longest-path layering (Kahn)
    indeg = {n: len(pred[n]) for n in comp}
    queue = sorted([n for n in comp if indeg[n] == 0], key=lambda n: (n != hub, n))
    layer, topo = {}, []
    while queue:
        n = queue.pop(0); topo.append(n)
        layer[n] = max([layer[p] + 1 for p in pred[n]], default=0)
        for t in succ[n]:
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    # sources other than the hub sit right next to what they reference
    for n in reversed(topo):
        if n != hub and not pred[n] and succ[n]:
            layer[n] = max(0, min(layer[t] for t in succ[n]) - 1)
    if hub in layer and any(layer[n] == 0 for n in comp if n != hub) and succ[hub]:
        pass
    # dummy nodes for long edges
    nbr_down, nbr_up, chains = defaultdict(list), defaultdict(list), {}
    nodes_in_layer = defaultdict(list)
    for n in comp:
        nodes_in_layer[layer[n]].append(n)
    for s, t in E:
        prev, chain = s, []
        for l in range(layer[s] + 1, layer[t]):
            d = f"~{s}>{t}#{l}"
            layer[d] = l; nodes_in_layer[l].append(d); chain.append(d)
            nbr_down[prev].append(d); nbr_up[d].append(prev); prev = d
        nbr_down[prev].append(t); nbr_up[t].append(prev)
        chains[(s, t)] = chain
    nl = max(nodes_in_layer) + 1
    # initial order: BFS from the hub
    seen, order = {}, [hub] if hub in comp else []
    for n in order + sorted(comp, key=lambda n: -len(succ[n]) - len(pred[n])):
        if n not in seen:
            seen[n] = len(seen)
            for m in nbr_down[n] + nbr_up[n]:  # lists built from sorted E: deterministic
                seen.setdefault(m, len(seen))
    layers = [sorted(nodes_in_layer[l], key=lambda n: seen.get(n, 1e9)) for l in range(nl)]
    best, best_c = [list(l) for l in layers], _crossings(layers, nbr_down)
    for it in range(40):
        rng = range(1, nl) if it % 2 == 0 else range(nl - 2, -1, -1)
        for l in rng:
            ref = layers[l - 1] if it % 2 == 0 else layers[l + 1]
            nb = nbr_up if it % 2 == 0 else nbr_down
            pos_ref = {n: i for i, n in enumerate(ref)}
            cur = {n: i for i, n in enumerate(layers[l])}
            def bary(n):
                ps = [pos_ref[m] for m in nb.get(n, []) if m in pos_ref]
                return sum(ps) / len(ps) if ps else cur[n]
            layers[l].sort(key=lambda n: (bary(n), cur[n]))
        # transpose: adjacent swaps that help
        for l in range(nl):
            for i in range(len(layers[l]) - 1):
                c0 = _crossings(layers, nbr_down)
                layers[l][i], layers[l][i + 1] = layers[l][i + 1], layers[l][i]
                if _crossings(layers, nbr_down) >= c0:
                    layers[l][i], layers[l][i + 1] = layers[l][i + 1], layers[l][i]
        c = _crossings(layers, nbr_down)
        if c < best_c:
            best, best_c = [list(l) for l in layers], c
        if best_c == 0:
            break
    layers = best
    # vertical placement: stack, then pull each node toward its neighbours' centres
    h = lambda n: 0 if n.startswith("~") else height(n)
    y = {}
    for l in layers:
        cur = 0
        for n in l:
            y[n] = cur; cur += h(n) + (16 if n.startswith("~") else V_GAP)
    for it in range(16):
        rng = range(1, nl) if it % 2 == 0 else range(nl - 2, -1, -1)
        for l in rng:
            nb = nbr_up if it % 2 == 0 else nbr_down
            want = []
            for n in layers[l]:
                cs = [y[m] + h(m) / 2 for m in nb.get(n, [])]
                want.append((sum(cs) / len(cs) - h(n) / 2) if cs else y[n])
            # keep order + gaps, minimising movement: forward pass, then backward pass
            ys = []
            for i, n in enumerate(layers[l]):
                gap = 16 if n.startswith("~") or (i and layers[l][i - 1].startswith("~")) else V_GAP
                lo = ys[-1] + h(layers[l][i - 1]) + gap if i else -1e9
                ys.append(max(want[i], lo))
            for i in range(len(ys) - 2, -1, -1):
                n, m = layers[l][i], layers[l][i + 1]
                gap = 16 if n.startswith("~") or m.startswith("~") else V_GAP
                ys[i] = min(ys[i], ys[i + 1] - h(n) - gap) if ys[i] > want[i] else ys[i]
            for n, v in zip(layers[l], ys):
                y[n] = v
    top = min(y[n] for n in y)
    pos = {n: [layer[n] * (CARD_W + COL_GAP), round(y[n] - top, 1)] for n in comp}
    routes = {f"{s}>{t}": [round(y[d] - top, 1) for d in ch] for (s, t), ch in chains.items() if ch}
    return pos, routes, best_c, nl


def architecture(node_ids, edges, center=None):
    ids = set(node_ids)
    parent_of = {}
    for e in edges:
        if e["type"] in ("submodule", "nested") and e["source"] in ids and e["target"] in ids:
            parent_of[e["target"]] = e["source"]
    def sup(n):
        while n in parent_of:
            n = parent_of[n]
        return n
    kids = defaultdict(list)
    for c, p in parent_of.items():
        kids[p].append(c)
    supers = sorted({sup(n) for n in node_ids})
    height = lambda s: box_height(len(kids.get(s, [])))
    w = defaultdict(float)
    for e in edges:
        if e["type"] not in ("depends", "mentions") or e["source"] not in ids or e["target"] not in ids:
            continue
        s, t = sup(e["source"]), sup(e["target"])
        if s != t:
            w[(s, t)] += 50 if e["type"] == "depends" else e.get("weight", 1)
    hub = sup(center) if center in ids else None
    # one direction per pair (the stronger one), then break remaining cycles with a DFS from the hub
    strongest = {}
    for (s, t), v in w.items():
        k = tuple(sorted((s, t)))
        if k not in strongest or (v, (s, t)) > strongest[k][0]:
            strongest[k] = ((v, (s, t)), (s, t))
    dag = {e for _, e in strongest.values()}
    dag = sorted({(hub, s) if t == hub else (s, t) for s, t in dag})
    adj = defaultdict(list)
    for s, t in dag:
        adj[s].append(t)
    color, back = {}, set()
    roots = ([hub] if hub else []) + sorted({x for e in dag for x in e}, key=lambda n: -len(adj[n]))
    for r in roots:
        if r in color:
            continue
        stack = [(r, iter(sorted(adj[r], key=lambda t: -w.get((r, t), 0))))]
        color[r] = 1
        while stack:
            n, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[n] = 2; stack.pop(); continue
            if color.get(nxt) == 1:
                back.add((n, nxt))
            elif nxt not in color:
                color[nxt] = 1
                stack.append((nxt, iter(sorted(adj[nxt], key=lambda t: -w.get((nxt, t), 0)))))
    E = sorted({(t, s) if (s, t) in back else (s, t) for s, t in dag})
    # components
    und = defaultdict(set)
    for s, t in E:
        und[s].add(t); und[t].add(s)
    comps, seen = [], set()
    for s in sorted(und, key=lambda n: (n != hub, -len(und[n]), n)):
        if s in seen:
            continue
        comp, st = [], [s]; seen.add(s)
        while st:
            x = st.pop(); comp.append(x)
            for y in sorted(und[x]):
                if y not in seen:
                    seen.add(y); st.append(y)
        comps.append(comp)
    pos, routes, crossings, oy = {}, {}, 0, 0.0
    max_cols = 1
    for comp in comps:
        cset = set(comp)
        h0 = hub if hub in cset else max(comp, key=lambda n: (len(und[n]), n))
        p, r, c, nl = _layer_component(comp, [e for e in E if e[0] in cset], h0, height)
        crossings += c; max_cols = max(max_cols, nl)
        for n, (x, y) in p.items():
            pos[n] = [x, y + oy]
        for k, ys in r.items():
            routes[k] = [v + oy for v in ys]
        oy = max(pos[n][1] + height(n) for n in comp) + 110
    # standalone shelf: systems first, then single repos
    shelf = sorted([s for s in supers if s not in pos], key=lambda s: (-len(kids.get(s, [])), s.lower()))
    cols = max(4, max_cols + 1)
    x_i, row_y, row_h = 0, oy + 40, 0
    for s in shelf:
        if x_i >= cols:
            x_i, row_y, row_h = 0, row_y + row_h + 30, 0
        pos[s] = [x_i * (CARD_W + 40), row_y]
        row_h = max(row_h, height(s)); x_i += 1
    # row order inside each system: by where their outside neighbours sit
    nb_y = defaultdict(list)
    for e in edges:
        if e["type"] in ("depends", "mentions") and e["source"] in ids and e["target"] in ids:
            for a, b in ((e["source"], e["target"]), (e["target"], e["source"])):
                if sup(b) in pos and sup(a) != sup(b):
                    nb_y[a].append(pos[sup(b)][1])
    korder = {p: sorted(ks, key=lambda k: (sum(nb_y[k]) / len(nb_y[k]) if nb_y[k] else 1e9, k)) for p, ks in kids.items()}
    out = {k: [round(v[0], 1), round(v[1], 1)] for k, v in pos.items()}
    out["_routes"] = routes
    out["_kids"] = korder
    out["_shelf_y"] = oy
    return out, crossings
