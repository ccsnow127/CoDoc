"""step 2: hierarchical clustering of the business (non-utility) functions.

base communities come from Louvain on the undirected dependency graph (plus a weak chain
between consecutive functions of a file, so undocumented leaves stay with their file);
oversized communities are split again at a higher resolution. each community is embedded by
structure (diffusion coordinates of the normalised inter-community affinity, made connected
by a weak teleport so the result does not depend on rounding), text (SVD of TF-IDF over doc
summaries, identifier words and package path) and the package tree (subsystem boundaries);
bisecting 2-means weighted by function count then splits the heaviest cluster that has a
balanced split, again and again. the first K - 1 splits give K clusters, and coarser levels
are always ancestors of finer ones: system -> subsystems -> components as K grows.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

import networkx as nx
import numpy as np

from codocs.bizlogic.graph import FuncGraph

_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOP = frozenset("the a an of to and or in for is on by with this that it as be are from at "
                  "function returns return value values if not set get func method".split())


@dataclass
class Hierarchy:
    nodes: list[str]
    base: dict[str, int]                 # node -> base community
    communities: list[list[str]]
    splits: list[tuple[int, list[int]]] = field(default_factory=list)
    # splits[i] = (cluster that was split, communities moved to the new cluster i + 1)

    @property
    def max_k(self) -> int:
        return 1 + len(self.splits) if self.communities else 0

    def community_labels(self, k: int) -> list[int]:
        """cluster of each base community after the first k - 1 splits (nested in k)."""
        labels = [0] * len(self.communities)
        for i, (_parent, moved) in enumerate(self.splits[: max(0, k - 1)]):
            for c in moved:
                labels[c] = i + 1
        return labels

    def assign(self, k: int) -> dict[str, int]:
        """node -> cluster id in 0..k'-1 (k' <= k), ids ordered by cluster size, largest first."""
        if not self.communities:
            return {}
        labels = self.community_labels(max(1, min(int(k), self.max_k)))
        sizes: Counter[int] = Counter()
        for ci, members in enumerate(self.communities):
            sizes[labels[ci]] += len(members)
        order = {lab: i for i, (lab, _) in enumerate(sorted(sizes.items(), key=lambda x: (-x[1], x[0])))}
        return {n: order[labels[c]] for n, c in self.base.items()}

    def parents(self, k_fine: int, k_coarse: int) -> dict[int, int]:
        """fine cluster -> its ancestor at the coarse level (partitions are nested)."""
        fine, coarse = self.assign(k_fine), self.assign(k_coarse)
        return {c: coarse[n] for n, c in fine.items()}


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")
            if len(w) > 2 and w.lower() not in _STOP]


def build_hierarchy(fg: FuncGraph, resolution: float = 1.0, alpha: float = 0.6,
                    seed: int = 0, file_weight: float = 0.3, pkg_weight: float = 0.5,
                    max_community: int | None = None, min_share: float = 0.05) -> Hierarchy:
    """base communities + dendrogram; alpha weighs structure against text (1 = structure only),
    pkg_weight adds the package tree (subsystem boundaries) to the embedding."""
    nodes = sorted(fg.business())
    keep = set(nodes)
    ug = nx.Graph()
    ug.add_nodes_from(nodes)
    for u, v, d in sorted(fg.graph.edges(data=True), key=lambda e: (e[0], e[1])):
        if u in keep and v in keep:
            w = d.get("weight", 1.0) + (ug[u][v]["weight"] if ug.has_edge(u, v) else 0.0)
            ug.add_edge(u, v, weight=w)
    by_file: dict[str, list[tuple[int, str]]] = {}
    for n in nodes:
        node = fg.nodes[n]
        by_file.setdefault(node.file, []).append((node.line, n))
    for items in by_file.values():
        items.sort()
        for (_, a), (_, b) in zip(items, items[1:]):
            if not ug.has_edge(a, b):
                ug.add_edge(a, b, weight=file_weight)

    if not nodes:
        return Hierarchy([], {}, [])
    # louvain on integer labels: sets of ints iterate the same way in every process, so the
    # communities do not depend on PYTHONHASHSEED
    ig = nx.convert_node_labels_to_integers(ug, ordering="sorted")
    comms = nx.community.louvain_communities(ig, weight="weight", resolution=resolution,
                                             seed=seed)
    cap = max_community if max_community is not None else max(50, len(nodes) // 50)
    comms = _refine(ig, [sorted(c) for c in comms], cap, resolution, seed)
    communities = sorted((sorted(nodes[i] for i in c) for c in comms),
                         key=lambda c: (-len(c), c[0]))
    base = {n: i for i, c in enumerate(communities) for n in c}
    C = len(communities)
    if C < 2:
        return Hierarchy(nodes, base, communities)

    W = np.zeros((C, C))
    vol = np.zeros(C)
    for u, v, d in ug.edges(data=True):
        a, b, w = base[u], base[v], d["weight"]
        vol[a] += w
        vol[b] += w
        if a != b:
            W[a, b] += w
            W[b, a] += w
    S = W / np.sqrt(np.outer(vol + 1e-9, vol + 1e-9))
    np.fill_diagonal(S, 0.0)
    if S.max() > 0:
        S = S / S.max()

    docs_tokens: list[Counter] = []
    for members in communities:
        cnt: Counter = Counter()
        for n in members:
            node = fg.nodes[n]
            cnt.update(_words(node.summary))
            cnt.update(_words(node.name))
            cnt.update(_words(node.pkg.replace("/", " ")) * 2)
        docs_tokens.append(cnt)
    df: Counter = Counter()
    for cnt in docs_tokens:
        df.update(cnt.keys())
    vocab = {w: i for i, w in enumerate(sorted(df))}
    X = np.zeros((C, len(vocab)), dtype=np.float64)
    for i, cnt in enumerate(docs_tokens):
        for w, tf in cnt.items():
            X[i, vocab[w]] = (1 + math.log(tf)) * math.log((1 + C) / (1 + df[w]))
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    X = X / np.where(norms == 0, 1, norms)

    # embedding of each community: diffusion coordinates of the community graph (structure),
    # an SVD of the TF-IDF matrix (text) and the package tree (subsystem); then bisecting
    # k-means weighted by function count, splitting the heaviest cluster that has a
    # balanced split, so every coarser partition is an ancestor of the finer ones.
    emb = _embed(S, X, _package_matrix(fg, communities), alpha, pkg_weight)
    weights = np.array([len(c) for c in communities], dtype=float)
    splits = _bisect_all(emb, weights, seed, min_share)
    return Hierarchy(nodes, base, communities, splits)


def _refine(ug: nx.Graph, comms: list[list[int]], cap: int, resolution: float,
            seed: int) -> list[list[int]]:
    """split communities larger than `cap` (louvain on the induced subgraph, rising
    resolution), so no K is limited by one oversized base community."""
    out: list[list[int]] = []
    todo = [(c, resolution) for c in comms]
    while todo:
        c, res = todo.pop()
        if len(c) <= cap or res > resolution * 16:
            out.append(c)
            continue
        parts = nx.community.louvain_communities(ug.subgraph(c), weight="weight",
                                                 resolution=res * 2, seed=seed)
        parts = [sorted(p) for p in parts]
        if len(parts) < 2:
            todo.append((c, res * 2))
        else:
            todo.extend((p, res * 2) for p in parts)
    return out


def _package_matrix(fg: FuncGraph, communities: list[list[str]]) -> np.ndarray:
    """share of each community's functions per top-level directory and per 2-level package
    prefix (unit rows), so coarse splits follow subsystem boundaries."""
    keys: dict[str, int] = {}
    rows: list[Counter] = []
    for members in communities:
        cnt: Counter = Counter()
        for n in members:
            parts = (fg.nodes[n].pkg or "(root)").split("/")
            cnt["1:" + parts[0]] += 1.0
            cnt["2:" + "/".join(parts[:2])] += 0.5
        rows.append(cnt)
        for k in cnt:
            keys.setdefault(k, len(keys))
    P = np.zeros((len(communities), len(keys)))
    for i, cnt in enumerate(rows):
        for k, v in cnt.items():
            P[i, keys[k]] = v
    return _rownorm(P)


def _embed(S: np.ndarray, X: np.ndarray, P: np.ndarray | None, alpha: float,
           pkg_weight: float = 0.0) -> np.ndarray:
    """[structure | text | package] blocks. a community with no link to any other gets no
    structure coordinates (they would be noise); its place in the package tree takes the
    structure's weight instead (or its text does, without a package block)."""
    C = len(S)
    struct = _spectral(S)
    text = _svd(X, dims=min(64, C - 1, X.shape[1]))
    linked = np.linalg.norm(struct, axis=1) > 0
    blocks = [math.sqrt(alpha) * struct]
    if P is not None and pkg_weight > 0:
        blocks.append(math.sqrt(1.0 - alpha) * text)
        blocks.append(np.where(linked, math.sqrt(pkg_weight),
                               math.sqrt(pkg_weight + alpha))[:, None] * P)
    else:
        blocks.append(np.where(linked, math.sqrt(1.0 - alpha), 1.0)[:, None] * text)
    return np.hstack(blocks)


def _bisect_all(emb: np.ndarray, w: np.ndarray, seed: int,
                min_share: float = 0.05) -> list[tuple[int, list[int]]]:
    """split the heaviest cluster whose best 2-means split gives the smaller side at least
    `min_share` of its weight; lopsided splits (an outlier community) only come last."""
    rng = np.random.default_rng(seed)
    clusters: list[list[int]] = [list(range(len(emb)))]
    splits: list[tuple[int, list[int]]] = []
    cache: dict[int, tuple[list[int], list[int], bool]] = {}
    while True:
        cand = [i for i, c in enumerate(clusters) if len(c) > 1]
        for i in cand:
            if i not in cache:
                cache[i] = _two_means(emb[clusters[i]], w[clusters[i]], rng, min_share)
        cand = [i for i in cand if cache[i][1]]
        if not cand:
            return splits
        balanced = [i for i in cand if cache[i][2]]
        i = max(balanced or cand, key=lambda j: (w[clusters[j]].sum(), -j))
        left, right, _ok = cache.pop(i)
        members = clusters[i]
        clusters[i] = [members[x] for x in left]
        moved = [members[x] for x in right]
        clusters.append(moved)
        splits.append((i, moved))


def _wsse(X: np.ndarray, w: np.ndarray, lab: np.ndarray) -> float:
    total = 0.0
    for k in (0, 1):
        m = lab == k
        if m.any():
            c = (w[m, None] * X[m]).sum(0) / w[m].sum()
            total += float((w[m] * ((X[m] - c) ** 2).sum(1)).sum())
    return total


def _lloyd(X: np.ndarray, w: np.ndarray, centers: np.ndarray, iters: int) -> np.ndarray:
    lab = np.full(len(X), -1)
    for _ in range(iters):
        d = ((X[:, None, :] - centers[None, :, :]) ** 2).sum(2)
        new = d.argmin(1)
        if (new == lab).all():
            break
        lab = new
        for k in (0, 1):
            m = lab == k
            if m.any():
                centers[k] = (w[m, None] * X[m]).sum(0) / w[m].sum()
    return lab


def _two_means(X: np.ndarray, w: np.ndarray, rng, min_share: float = 0.05,
               restarts: int = 8, iters: int = 25) -> tuple[list[int], list[int], bool]:
    """weighted 2-means (principal-axis split plus k-means++ restarts, lowest weighted SSE
    among the balanced ones); the smaller side is returned second, then whether the
    smaller side holds at least `min_share` of the weight."""
    n = len(X)
    if n < 2:
        return list(range(n)), [], False
    mean = (w[:, None] * X).sum(0) / w.sum()
    Xc = X - mean
    labels: list[np.ndarray] = []
    if np.abs(Xc).max() > 1e-12:
        axis = np.linalg.svd(Xc * np.sqrt(w)[:, None], full_matrices=False)[2][0]
        proj = Xc @ axis
        order = np.argsort(proj, kind="stable")
        cum = np.cumsum(w[order])
        lab = np.zeros(n, dtype=int)
        lab[order[np.searchsorted(cum, cum[-1] / 2, side="right"):]] = 1
        labels.append(lab)
        labels.append(_lloyd(X, w, np.stack([X[lab == 0].mean(0) if (lab == 0).any() else mean,
                                             X[lab == 1].mean(0) if (lab == 1).any() else mean]),
                             iters))
        p = w / w.sum()
        for _ in range(restarts):
            a = int(rng.choice(n, p=p))
            d = ((X - X[a]) ** 2).sum(1) * w
            if d.sum() <= 0:
                continue
            b = int(rng.choice(n, p=d / d.sum()))
            labels.append(_lloyd(X, w, np.stack([X[a], X[b]]), iters))
    else:                                    # identical points: split by weight
        order = rng.permutation(n)
        lab = np.zeros(n, dtype=int)
        lab[order[n // 2:]] = 1
        labels.append(lab)
    best = None
    for lab in labels:
        if lab.min() == lab.max():
            continue
        share = min(w[lab == 0].sum(), w[lab == 1].sum()) / w.sum()
        key = (share < min_share, _wsse(X, w, lab))
        if best is None or key < best[0]:
            best = (key, lab, share)
    if best is None:
        return list(range(n)), [], False
    _key, lab, share = best
    side0, side1 = np.where(lab == 0)[0].tolist(), np.where(lab == 1)[0].tolist()
    if w[side0].sum() < w[side1].sum():
        side0, side1 = side1, side0
    return side0, side1, share >= min_share


def _rownorm(M: np.ndarray, tol: float = 1e-8) -> np.ndarray:
    """unit rows; rows that are (numerically) zero stay zero instead of becoming noise."""
    n = np.linalg.norm(M, axis=1, keepdims=True)
    scale = n.max() if n.size and n.max() > 0 else 1.0
    return np.where(n > tol * scale, M / np.where(n == 0, 1, n), 0.0)


def _spectral(S: np.ndarray, min_eig: float = 0.05, teleport: float = 1e-3) -> np.ndarray:
    """diffusion coordinates of the linked communities: eigenvectors of the normalised
    affinity, made connected by a weak teleport so eigenvalue 1 is simple, scaled by their
    eigenvalue (near-degenerate eigenvectors are kept together, so the embedding does not
    depend on rounding); communities with no link get zero rows."""
    C = len(S)
    out = np.zeros((C, 0))
    linked = np.where(S.sum(1) > 1e-12)[0]
    if len(linked) < 3:
        return np.zeros((C, 1))
    A = S[np.ix_(linked, linked)]
    A = A + teleport * A.sum() / len(linked) ** 2
    d = A.sum(1)
    L = A / np.sqrt(np.outer(d, d))
    vals, vecs = np.linalg.eigh((L + L.T) / 2)
    order = np.argsort(vals)[::-1][1:]
    order = order[vals[order] > min_eig]
    if not len(order):
        return np.zeros((C, 1))
    coords = vecs[:, order] / np.sqrt(d / d.mean())[:, None] * vals[order]
    out = np.zeros((C, len(order)))
    out[linked] = _rownorm(coords)
    return out


def _svd(X: np.ndarray, dims: int) -> np.ndarray:
    if dims <= 0 or not X.any():
        return np.zeros((len(X), max(dims, 0)))
    U, sv, _ = np.linalg.svd(X, full_matrices=False)
    return _rownorm(U[:, :dims] * sv[:dims])
