"""steps 3-5: locate roots, sample random-walk trajectories, deduplicate by cluster granularity.

roots are business functions nothing calls (they may still be referenced, which is how
handlers, callbacks and main entry points look) and that reach at least `min_reach`
business functions; the procedures a dispatcher alone calls (a switch over message types)
are roots too, so each gets its own walks. when capped, every subsystem gets a share of the
root budget proportional to its candidates, filled greedily by the new functions each root
brings near it (so roots buried in another root's neighbourhood come last).

a walk follows call/ref edges in a fixed order (independent of the hash seed), skips
utility nodes, never revisits a node, prefers callees with small fan-in that lead somewhere
(specific steps over shared helpers and dead-end leaves), does not cross a ref/table edge
into another root (a registration is not the handler's business), and at a leaf backtracks
to the nearest caller with an untried callee, so sibling steps of a procedure follow each
other in the walk.

deduplication maps each walk to its cluster sequence at granularity K (consecutive repeats
collapsed). a sequence followed by fewer than min_share * walks / K walks is folded into the
kept sequence sharing its longest prefix (recorded as a variant), so fewer clusters keep
fewer, broader trajectories. a kept sequence that is a prefix of exactly one longer kept
sequence is merged into it (prefix_subsume; never a single-cluster sequence). support counts
every walk merged into a trajectory, own_support the walks that follow its whole sequence,
and step_walks how many walks reach each step; the representative is the best documented of
its own walks.
"""

from __future__ import annotations

import heapq
import math
import random
from collections import Counter
from dataclasses import asdict, dataclass, field

from codocs.bizlogic.graph import FuncGraph

DISPATCH_MIN = 15          # a function calling this many callees nobody else calls dispatches
NEAR_DEPTH = 3             # neighbourhood a root is credited with when roots are selected
STEER_CAP = 200            # reach cap of the "leads somewhere" term of the walk weights


@dataclass
class Trajectory:
    id: str
    clusters: list[int]                  # collapsed cluster sequence (the dedup signature)
    path: list[str]                      # representative walk (node ids), signature == clusters
    support: int                         # walks merged into this trajectory (all kinds)
    roots: list[str] = field(default_factory=list)
    subsumed: int = 0                    # prefix sequences merged into this one
    own_support: int = 0                 # walks that follow the whole sequence
    step_walks: list[int] = field(default_factory=list)   # walks reaching each step
    variants: list[tuple[list[int], int]] = field(default_factory=list)  # folded sequences
    # evidence aggregated over every walk merged into this trajectory: the most frequent
    # entry roots and, per step, the most frequent functions, both spread over the root
    # subsystems; a coarse K merges many walks, so this evidence is broad.
    root_counts: list[tuple[str, int]] = field(default_factory=list)
    root_groups: list[tuple[str, int, int]] = field(default_factory=list)  # (group, roots, walks)
    step_functions: list[list[tuple[str, int]]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _call_callers(fg: FuncGraph, n: str) -> list[str]:
    return [p for p, _, d in fg.graph.in_edges(n, data=True)
            if d.get("kind") == "call" and p not in fg.utilities]


def root_candidates(fg: FuncGraph, min_reach: int = 3, reach_cap: int = 5000,
                    dispatch_min: int = DISPATCH_MIN) -> list[tuple[str, int]]:
    """every root (uncalled, or dispatched) with its reach, largest reach first."""
    g, util = fg.graph, fg.utilities
    out: dict[str, int] = {}
    callers = {n: _call_callers(fg, n) for n in fg.business()}
    for n in fg.business():
        if callers[n] or not any(s not in util for s in g.successors(n)):
            continue
        reach = _reach(fg, n, reach_cap)
        if reach >= min_reach:
            out[n] = reach
    if dispatch_min > 0:
        for d in fg.business():
            own = [s for s in sorted(g.successors(d))
                   if s not in util and callers.get(s) == [d]]
            if len(own) < dispatch_min:
                continue
            for s in own:
                if s not in out and any(x not in util for x in g.successors(s)):
                    reach = _reach(fg, s, reach_cap)
                    if reach >= min_reach:
                        out[s] = reach
    # a root a walk cannot leave (its only business callees are other roots it merely
    # registers) would give one-function walks
    roots = set(out)
    out = {n: r for n, r in out.items()
           if any(s not in util and (g[n][s].get("kind") == "call" or s not in roots)
                  for s in g.successors(n))}
    return sorted(out.items(), key=lambda x: (-x[1], x[0]))


def find_roots(fg: FuncGraph, min_reach: int = 3, max_roots: int | None = 300,
               reach_cap: int = 5000, group_depth: int = 1) -> list[tuple[str, int]]:
    """(root, reachable business functions), largest reach first; when capped, roots are
    shared among subsystems (the first `group_depth` path segments) in proportion."""
    out = root_candidates(fg, min_reach, reach_cap)
    if not max_roots or len(out) <= max_roots:
        return out
    return select_roots(fg, out, max_roots, group_depth)


def _group(node: str, depth: int) -> str:
    pkg = node.split("::", 1)[0]
    return "/".join(pkg.split("/")[:depth]) or "(root)"


def _quotas(sizes: dict[str, int], limit: int) -> dict[str, int]:
    """a small floor per group, the rest of `limit` in proportion to `sizes` (largest
    remainder); what a group cannot use goes back to the others."""
    order = sorted(sizes, key=lambda k: (-sizes[k], k))
    floor = max(2, limit // (4 * max(1, len(sizes))))
    quota = {k: min(sizes[k], floor) for k in order}
    if sum(quota.values()) > limit:                  # more groups than budget: one each
        quota = {k: int(i < limit) for i, k in enumerate(order)}
    left = limit - sum(quota.values())
    while left > 0:
        open_ = [k for k in order if quota[k] < sizes[k]]
        if not open_:
            break
        total = sum(sizes[k] for k in open_)
        raw = {k: left * sizes[k] / total for k in open_}
        give = {k: min(sizes[k] - quota[k], int(raw[k])) for k in open_}
        for k in sorted(open_, key=lambda k: (-(raw[k] - int(raw[k])), k)):
            if sum(give.values()) >= left:
                break
            if quota[k] + give[k] < sizes[k]:
                give[k] += 1
        if not any(give.values()):
            break
        for k, v in give.items():
            quota[k] += v
        left = limit - sum(quota.values())
    return quota


def _near(fg: FuncGraph, start: str, depth: int) -> set[str]:
    seen, frontier = {start}, [start]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for s in fg.graph.successors(n):
                if s not in seen and s not in fg.utilities:
                    seen.add(s)
                    nxt.append(s)
        frontier = nxt
    return seen


def select_roots(fg: FuncGraph, ranked: list[tuple[str, int]], limit: int,
                 depth: int = 1) -> list[tuple[str, int]]:
    """per-subsystem quotas proportional to the candidate roots, each filled greedily by the
    business functions a root newly brings within NEAR_DEPTH calls (ties: larger reach)."""
    groups: dict[str, list[tuple[str, int]]] = {}
    for item in ranked:
        groups.setdefault(_group(item[0], depth), []).append(item)
    quota = _quotas({k: len(v) for k, v in groups.items()}, limit)
    picked: list[tuple[str, int]] = []
    for name, items in sorted(groups.items()):
        near = {r: _near(fg, r, NEAR_DEPTH) for r, _ in items}
        reach = dict(items)
        covered: set[str] = set()
        heap = [(-len(near[r]), -reach[r], r) for r, _ in items]
        heapq.heapify(heap)
        taken = 0
        while heap and taken < quota[name]:
            neg, nr, r = heapq.heappop(heap)
            gain = len(near[r] - covered)
            if heap and gain < -heap[0][0]:          # stale: re-queue with the true gain
                heapq.heappush(heap, (-gain, nr, r))
                continue
            picked.append((r, reach[r]))
            covered |= near[r]
            taken += 1
    picked.sort(key=lambda x: (-x[1], x[0]))
    return picked


def _reach(fg: FuncGraph, start: str, cap: int) -> int:
    seen, stack = {start}, [start]
    while stack and len(seen) <= cap:
        for s in sorted(fg.graph.successors(stack.pop())):
            if s not in seen and s not in fg.utilities:
                seen.add(s)
                stack.append(s)
    return len(seen) - 1


def walks_for(reach: int, walks_per_root: int) -> int:
    """walks given to a root: at least walks_per_root, more for a root that reaches far."""
    return max(walks_per_root, math.ceil(math.sqrt(max(0, reach))))


def sample_walks(fg: FuncGraph, roots: list[str], walks_per_root: int = 20,
                 max_len: int = 12, seed: int = 0, reach: dict[str, int] | None = None,
                 stop_at: set[str] | None = None) -> list[list[str]]:
    """random walks from each root (see the module doc); `reach` scales the number of walks
    per root, `stop_at` are roots a walk must not enter through a ref/table edge."""
    rng = random.Random(seed)
    g, util = fg.graph, fg.utilities
    stop = stop_at or set()
    steer: dict[str, float] = {}

    def leads(n: str) -> float:
        if n not in steer:
            steer[n] = math.log(2 + _reach(fg, n, STEER_CAP))
        return steer[n]

    succ: dict[str, list[tuple[str, float]]] = {}

    def options(n: str) -> list[tuple[str, float]]:
        if n not in succ:
            opts = []
            for s in sorted(g.successors(n)):
                d = g[n][s]
                if s in util or (s in stop and d.get("kind") != "call"):
                    continue
                opts.append((s, d.get("weight", 1.0) * leads(s) / math.log(2 + g.in_degree(s))))
            succ[n] = opts
        return succ[n]

    walks: list[list[str]] = []
    for root in roots:
        n_walks = walks_for(reach.get(root, 0), walks_per_root) if reach else walks_per_root
        for _ in range(n_walks):
            path, seen, stack = [root], {root}, [root]
            while len(path) < max_len and stack:
                cands = [(s, w) for s, w in options(stack[-1]) if s not in seen]
                if not cands:
                    stack.pop()                       # leaf: back to the nearest caller
                    continue
                nxt = rng.choices([s for s, _ in cands], weights=[w for _, w in cands], k=1)[0]
                path.append(nxt)
                seen.add(nxt)
                stack.append(nxt)
            walks.append(path)
    return walks


def caller_in_path(fg: FuncGraph, path: list[str], i: int) -> str | None:
    """the function that calls path[i] in a walk: the nearest earlier node with an edge."""
    for j in range(i - 1, -1, -1):
        if fg.graph.has_edge(path[j], path[i]):
            return path[j]
    return None


EVIDENCE_ROOTS = 12
EVIDENCE_PER_STEP = 6


def _step_positions(path: list[str], assign: dict[str, int]) -> list[tuple[int, str]]:
    """(index of the signature step the node falls in, node) along a walk."""
    out: list[tuple[int, str]] = []
    step, last = -1, None
    for n in path:
        c = assign.get(n)
        if c is None:
            continue
        if c != last:
            step += 1
            last = c
        out.append((step, n))
    return out


def signature(path: list[str], assign: dict[str, int]) -> tuple[int, ...]:
    sig: list[int] = []
    for n in path:
        c = assign.get(n)
        if c is not None and (not sig or sig[-1] != c):
            sig.append(c)
    return tuple(sig)


def _lcp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _spread(buckets: dict[str, Counter], total: Counter, n: int) -> list[tuple[str, int]]:
    """n items taken round-robin over the buckets (largest first), each bucket's most
    frequent first, reported with their overall counts."""
    order = sorted(buckets, key=lambda b: (-sum(buckets[b].values()), b))
    queues = {b: [x for x, _ in sorted(buckets[b].items(), key=lambda kv: (-kv[1], kv[0]))]
              for b in order}
    out: list[str] = []
    while len(out) < n and any(queues.values()):
        for b in order:
            while queues[b] and queues[b][0] in out:
                queues[b].pop(0)
            if queues[b] and len(out) < n:
                out.append(queues[b].pop(0))
    return sorted(((x, total[x]) for x in out), key=lambda kv: (-kv[1], kv[0]))


def min_support(n_walks: int, k: int, min_share: float) -> int:
    """walks a cluster sequence needs to be kept at granularity k."""
    return max(1, math.ceil(min_share * n_walks / max(1, k)))


def deduplicate(fg: FuncGraph, walks: list[list[str]], assign: dict[str, int],
                prefix_subsume: bool = True, max_trajectories: int | None = None,
                min_share: float = 0.02, k: int | None = None,
                group_depth: int = 2) -> tuple[list[Trajectory], dict]:
    """trajectories at this granularity, best supported first, and dedup statistics."""
    groups: dict[tuple[int, ...], list[list[str]]] = {}
    for w in walks:
        sig = signature(w, assign)
        if sig:
            groups.setdefault(sig, []).append(w)
    k = k or len(set(assign.values())) or 1
    ms = min_support(len(walks), k, min_share)
    own = {s: len(ws) for s, ws in groups.items()}

    def rank(s: tuple[int, ...]) -> tuple:
        return (own[s], -len(s), s)

    kept = {s for s in groups if own[s] >= ms}
    for c in sorted({s[0] for s in groups}):       # every first cluster keeps one sequence
        if not any(s[0] == c for s in kept):
            kept.add(max((s for s in groups if s[0] == c), key=rank))
    frequent = len(kept)

    members: dict[tuple[int, ...], list[tuple[list[str], int]]] = {
        s: [(w, len(s)) for w in groups[s]] for s in kept}
    subsumed: Counter = Counter()
    absorbed: set[tuple[int, ...]] = set()
    if prefix_subsume:
        for s in sorted(kept, key=lambda s: (len(s), s)):
            if len(s) < 2:
                continue                              # intra-cluster behaviour stays itself
            ext = [t for t in kept if len(t) > len(s) and t[:len(s)] == s]
            maximal = [t for t in ext if not any(len(u) > len(t) and u[:len(t)] == t
                                                 for u in ext)]
            if len(maximal) != 1:
                continue
            best = maximal[0]
            members[best].extend((w, len(s)) for w, _m in members.pop(s))
            subsumed[best] += 1 + subsumed.pop(s, 0)
            kept.discard(s)
            absorbed.add(s)
    after_subsume = len(kept)

    variants: dict[tuple[int, ...], list[tuple[list[int], int]]] = {s: [] for s in kept}
    folded = 0
    for s in sorted(groups, key=lambda s: (-own[s], s)):
        if s in kept or s in absorbed:
            continue
        target = max(kept, key=lambda t: (_lcp(s, t), own.get(t, 0), -len(t), t))
        m = _lcp(s, target)
        members[target].extend((w, m) for w in groups[s])
        variants[target].append((list(s), own[s]))
        folded += 1

    def doc_score(p: tuple[str, ...]) -> tuple:
        return (sum(bool(fg.nodes[n].summary) for n in p), len(p))

    trajs: list[Trajectory] = []
    for sig in kept:
        mem = members[sig]
        exact = [w for w, m in mem if m == len(sig) and signature(w, assign) == sig]
        counts = Counter(tuple(w) for w in exact)
        # the best documented, longest own walk (a short walk repeated by a narrow root
        # would otherwise win on count)
        rep = max(counts, key=lambda p: (*doc_score(p), counts[p], p))
        buckets: dict[str, list[tuple[list[str], int]]] = {}
        for w, m in mem:
            buckets.setdefault(_group(w[0], group_depth), []).append((w, m))
        root_total = Counter(w[0] for w, _ in mem)
        root_b = {b: Counter(w[0] for w, _ in ms_) for b, ms_ in buckets.items()}
        step_total = [Counter() for _ in sig]
        step_b: list[dict[str, Counter]] = [{} for _ in sig]
        for b, ms_ in buckets.items():
            for w, m in ms_:
                for i, n in _step_positions(w, assign):
                    if i < min(m, len(sig)):
                        step_total[i][n] += 1
                        step_b[i].setdefault(b, Counter())[n] += 1
        trajs.append(Trajectory(
            id="", clusters=list(sig), path=list(rep), support=len(mem),
            roots=sorted(root_total), subsumed=subsumed[sig], own_support=own[sig],
            step_walks=[sum(1 for _w, m in mem if m > i) for i in range(len(sig))],
            variants=sorted(variants.get(sig, []), key=lambda v: (-v[1], v[0])),
            root_counts=_spread(root_b, root_total, EVIDENCE_ROOTS),
            root_groups=sorted(((b, len(root_b[b]), len(ms_)) for b, ms_ in buckets.items()),
                               key=lambda x: (-x[2], x[0])),
            step_functions=[_spread(step_b[i], step_total[i], EVIDENCE_PER_STEP)
                            for i in range(len(sig))]))
    trajs.sort(key=lambda t: (-t.own_support, -t.support, -len(t.clusters), t.clusters))
    dropped: list[Trajectory] = []
    if max_trajectories is not None and len(trajs) > max_trajectories:
        dropped = trajs[max_trajectories:]
        trajs = trajs[:max_trajectories]
    for i, t in enumerate(trajs, start=1):
        t.id = f"T{i:03d}"
    stats = {"walks": len(walks), "distinct_signatures": len(groups), "min_support": ms,
             "frequent_signatures": frequent, "after_prefix_subsume": after_subsume,
             "folded_low_support": folded, "kept": len(trajs),
             "dropped_by_cap": len(dropped),
             "dropped_own_walks": sum(t.own_support for t in dropped)}
    return trajs, stats


def coverage(fg: FuncGraph, candidates: list[tuple[str, int]], roots: list[tuple[str, int]],
             walks: list[list[str]], trajs: list[Trajectory] | None = None,
             depth: int = 1) -> list[dict]:
    """per subsystem: business functions, candidate and walked roots, functions any walk
    visits, and trajectories whose walk or step evidence passes through it."""
    rows: dict[str, dict] = {}

    def row(n: str) -> dict:
        return rows.setdefault(_group(n, depth), {
            "subsystem": _group(n, depth), "functions": 0, "candidate_roots": 0, "roots": 0,
            "visited": 0, "trajectories": 0})

    for n in fg.business():
        row(n)["functions"] += 1
    for n, _ in candidates:
        row(n)["candidate_roots"] += 1
    for n, _ in roots:
        row(n)["roots"] += 1
    biz = set(fg.business())
    for n in {x for w in walks for x in w} & biz:
        row(n)["visited"] += 1
    for t in trajs or []:
        touched = {_group(n, depth) for n in t.path}
        touched |= {_group(n, depth) for step in t.step_functions for n, _ in step}
        for g in touched:
            if g in rows:
                rows[g]["trajectories"] += 1
    return sorted(rows.values(), key=lambda r: (-r["functions"], r["subsystem"]))
