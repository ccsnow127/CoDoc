"""Go module static analysis: package discovery, imports, symbols, dependency graph."""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

import networkx as nx

from codocs.repowiki.deepdoc.models import GoPackage, GoSymbol

_SKIP_DIRS = {"vendor", "testdata", "node_modules", "markdown_docs", "helm", "chart", "charts"}

_MODULE_RE = re.compile(r"^module\s+(\S+)", re.MULTILINE)
_PACKAGE_RE = re.compile(r"^package\s+(\w+)", re.MULTILINE)
_IMPORT_BLOCK_RE = re.compile(r"^import\s*\(([^)]*)\)", re.MULTILINE)
_IMPORT_SINGLE_RE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.MULTILINE)
_IMPORT_PATH_RE = re.compile(r'"([^"]+)"')
# func Name( / func Name[T any]( / func (r *Recv) Name(
_FUNC_RE = re.compile(
    r"^func\s+(?:\(\s*\w*\s*\*?\s*([A-Za-z_]\w*)[^)]*\)\s*)?([A-Za-z_]\w*)\s*[\[(]",
    re.MULTILINE,
)
_TYPE_RE = re.compile(r"^type\s+([A-Za-z_]\w*)\s+\S", re.MULTILINE)

# well-known external dependency prefixes -> (display name, category)
TECH_STACK_MAP: list[tuple[str, str, str]] = [
    # web / rpc
    ("github.com/gin-gonic/gin", "Gin", "HTTP framework"),
    ("github.com/labstack/echo", "Echo", "HTTP framework"),
    ("github.com/go-chi/chi", "chi", "HTTP router"),
    ("github.com/gorilla/mux", "gorilla/mux", "HTTP router"),
    ("github.com/gofiber/fiber", "Fiber", "HTTP framework"),
    ("google.golang.org/grpc", "gRPC", "RPC framework"),
    ("github.com/golang/protobuf", "Protocol Buffers", "serialization"),
    ("google.golang.org/protobuf", "Protocol Buffers", "serialization"),
    # data
    ("gorm.io/gorm", "GORM", "ORM"),
    ("github.com/jmoiron/sqlx", "sqlx", "database"),
    ("github.com/jackc/pgx", "pgx", "database (PostgreSQL)"),
    ("github.com/lib/pq", "lib/pq", "database (PostgreSQL)"),
    ("github.com/go-sql-driver/mysql", "MySQL driver", "database"),
    ("github.com/mattn/go-sqlite3", "go-sqlite3", "database"),
    ("go.mongodb.org/mongo-driver", "MongoDB driver", "database"),
    ("github.com/redis/go-redis", "Redis client", "cache / KV store"),
    ("github.com/go-redis/redis", "Redis client", "cache / KV store"),
    ("go.etcd.io/etcd", "etcd client", "KV store"),
    ("github.com/coocood/freecache", "freecache", "in-memory cache"),
    # messaging / networking
    ("github.com/segmentio/kafka-go", "kafka-go", "messaging"),
    ("github.com/IBM/sarama", "Sarama", "messaging"),
    ("github.com/Shopify/sarama", "Sarama", "messaging"),
    ("github.com/nats-io/nats.go", "NATS client", "messaging"),
    ("github.com/rabbitmq/amqp091-go", "AMQP client", "messaging"),
    ("github.com/gorilla/websocket", "gorilla/websocket", "networking"),
    ("github.com/miekg/dns", "DNS library", "networking"),
    ("github.com/ishidawataru/sctp", "SCTP", "transport"),
    # observability
    ("github.com/prometheus/client_golang", "Prometheus client", "metrics"),
    ("go.opentelemetry.io/otel", "OpenTelemetry", "tracing"),
    ("go.uber.org/zap", "zap", "logging"),
    ("github.com/sirupsen/logrus", "logrus", "logging"),
    ("github.com/rs/zerolog", "zerolog", "logging"),
    ("github.com/InVisionApp/go-health", "go-health", "health checking"),
    # cloud native
    ("k8s.io/client-go", "Kubernetes client-go", "orchestration"),
    ("k8s.io/api", "Kubernetes API", "orchestration"),
    ("sigs.k8s.io/controller-runtime", "controller-runtime", "operator framework"),
    ("github.com/operator-framework", "Operator SDK", "operator framework"),
    ("github.com/aws/aws-sdk-go-v2", "AWS SDK", "cloud SDK"),
    ("github.com/aws/aws-sdk-go", "AWS SDK", "cloud SDK"),
    # cli / config / misc
    ("github.com/spf13/cobra", "Cobra", "CLI framework"),
    ("github.com/urfave/cli", "urfave/cli", "CLI framework"),
    ("github.com/spf13/viper", "Viper", "configuration"),
    ("gopkg.in/yaml.v3", "yaml.v3", "serialization"),
    ("github.com/google/uuid", "uuid", "identifiers"),
    ("github.com/alitto/pond", "pond", "worker pool"),
    ("github.com/stretchr/testify", "testify", "testing"),
]


class GoModuleIndex:
    """index of every package in a Go module, with a package-level import graph."""

    def __init__(self, root: Path, module: str):
        self.root = root
        self.module = module
        self.packages: dict[str, GoPackage] = {}
        self.graph = nx.DiGraph()

    # -- construction -----------------------------------------------------

    @classmethod
    def build(cls, root: str | Path, subtree: str | None = None) -> GoModuleIndex:
        """scan a Go module rooted at *root*; optionally restrict to a subtree dir."""
        root = Path(root).resolve()
        module = _read_module_name(root)
        index = cls(root, module)

        scan_root = root / subtree if subtree else root
        for dirpath, dirnames, filenames in os.walk(scan_root):
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and d not in _SKIP_DIRS
            )
            go_files = [f for f in filenames if f.endswith(".go")]
            if not go_files:
                continue
            rel = str(Path(dirpath).relative_to(root)).replace(os.sep, "/")
            if rel == ".":
                rel = ""
            pkg = index._parse_package(Path(dirpath), rel, go_files)
            if pkg is not None:
                index.packages[pkg.path] = pkg

        index._build_graph()
        return index

    def _parse_package(self, pkg_dir: Path, rel: str, go_files: list[str]) -> GoPackage | None:
        pkg = GoPackage(path=rel)
        internal: set[str] = set()
        external: set[str] = set()
        stdlib: set[str] = set()
        doc_candidates: list[tuple[str, str]] = []  # (filename, comment)

        for fname in sorted(go_files):
            fpath = pkg_dir / fname
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if fname.endswith("_test.go"):
                pkg.test_files += 1
                continue

            rel_file = f"{rel}/{fname}" if rel else fname
            pkg.files.append(rel_file)
            pkg.loc += content.count("\n") + 1

            m = _PACKAGE_RE.search(content)
            if m:
                pkg.name = m.group(1)
                if pkg.name == "main":
                    pkg.is_main = True
                doc = _package_doc_comment(content, m.start())
                if doc:
                    doc_candidates.append((fname, doc))

            for imp in _parse_imports(content):
                kind, target = _classify_import(imp, self.module)
                if kind == "internal":
                    internal.add(target)
                elif kind == "external":
                    external.add(target)
                else:
                    stdlib.add(target)

            for rmatch in _FUNC_RE.finditer(content):
                receiver, name = rmatch.group(1) or "", rmatch.group(2)
                pkg.symbols.append(
                    GoSymbol(
                        name=name,
                        kind="method" if receiver else "func",
                        receiver=receiver,
                        file=rel_file,
                        exported=name[0].isupper(),
                    )
                )
            for tmatch in _TYPE_RE.finditer(content):
                name = tmatch.group(1)
                pkg.symbols.append(
                    GoSymbol(name=name, kind="type", file=rel_file, exported=name[0].isupper())
                )

        if not pkg.files:
            return None  # test-only directory

        # prefer doc.go for the package comment, else the longest candidate
        doc_candidates.sort(key=lambda c: (c[0] != "doc.go", -len(c[1])))
        if doc_candidates:
            pkg.doc_comment = doc_candidates[0][1]

        pkg.imports_internal = sorted(internal - {pkg.path})
        pkg.imports_external = sorted(external)
        pkg.imports_stdlib = sorted(stdlib)
        return pkg

    def _build_graph(self) -> None:
        for path, pkg in self.packages.items():
            self.graph.add_node(path, loc=pkg.loc, is_main=pkg.is_main)
        for path, pkg in self.packages.items():
            for target in pkg.imports_internal:
                if target in self.packages:
                    self.graph.add_edge(path, target)

    # -- queries ----------------------------------------------------------

    def rank_packages(self) -> list[tuple[str, float]]:
        """packages ranked by PageRank (most depended-upon first)."""
        if not self.graph.nodes:
            return []
        try:
            scores = nx.pagerank(self.graph, alpha=0.85)
        except Exception:
            scores = {n: 1.0 / len(self.graph) for n in self.graph}
        return sorted(scores.items(), key=lambda x: -x[1])

    def entrypoints(self) -> list[str]:
        """paths of `package main` packages (binaries / services)."""
        return sorted(p for p, pkg in self.packages.items() if pkg.is_main)

    def group_of(self, pkg_path: str, depth: int = 1) -> str:
        """top-level group a package belongs to (first *depth* path segments)."""
        if not pkg_path:
            return "(root)"
        return "/".join(pkg_path.split("/")[:depth])

    def group_edges(self, depth: int = 1) -> dict[tuple[str, str], int]:
        """cross-group dependency edges aggregated to *depth* path segments."""
        edges: Counter[tuple[str, str]] = Counter()
        for src, dst in self.graph.edges:
            gs, gd = self.group_of(src, depth), self.group_of(dst, depth)
            if gs != gd:
                edges[(gs, gd)] += 1
        return dict(edges)

    def subgraph_edges(self, packages: list[str]) -> list[tuple[str, str]]:
        """import edges among *packages* (both endpoints inside the set)."""
        pkgs = set(packages)
        return sorted((s, d) for s, d in self.graph.edges if s in pkgs and d in pkgs)

    def outbound_edges(self, packages: list[str]) -> dict[str, list[str]]:
        """imports from *packages* to internal packages outside the set."""
        pkgs = set(packages)
        out: dict[str, set[str]] = {}
        for s, d in self.graph.edges:
            if s in pkgs and d not in pkgs:
                out.setdefault(s, set()).add(d)
        return {k: sorted(v) for k, v in sorted(out.items())}

    def external_deps(self, packages: list[str] | None = None) -> Counter[str]:
        """external import path -> number of packages using it."""
        counts: Counter[str] = Counter()
        selected = packages if packages is not None else list(self.packages)
        for path in selected:
            pkg = self.packages.get(path)
            if pkg:
                for imp in pkg.imports_external:
                    counts[imp] += 1
        return counts

    def tech_stack(self) -> list[tuple[str, str, int]]:
        """(display name, category, using-package count) for known dependencies."""
        counts = self.external_deps()
        rows: dict[tuple[str, str], int] = {}
        for imp, n in counts.items():
            for prefix, name, category in TECH_STACK_MAP:
                if imp == prefix or imp.startswith(prefix + "/"):
                    key = (name, category)
                    rows[key] = rows.get(key, 0) + n
                    break
        return sorted(
            [(name, cat, n) for (name, cat), n in rows.items()], key=lambda r: -r[2]
        )


def _read_module_name(root: Path) -> str:
    gomod = root / "go.mod"
    if gomod.exists():
        m = _MODULE_RE.search(gomod.read_text(encoding="utf-8", errors="ignore"))
        if m:
            return m.group(1)
    return root.name


def _parse_imports(content: str) -> list[str]:
    imports: list[str] = []
    for block in _IMPORT_BLOCK_RE.finditer(content):
        imports.extend(_IMPORT_PATH_RE.findall(block.group(1)))
    imports.extend(_IMPORT_SINGLE_RE.findall(content))
    return imports


def _classify_import(imp: str, module: str) -> tuple[str, str]:
    """classify an import path -> (kind, target); internal targets are package paths."""
    if imp == module:
        return "internal", ""
    if imp.startswith(module + "/"):
        return "internal", imp[len(module) + 1 :]
    first = imp.split("/")[0]
    if "." not in first:
        return "stdlib", imp
    return "external", imp


def _package_doc_comment(content: str, package_pos: int) -> str:
    """the // comment block immediately preceding the package clause."""
    lines = content[:package_pos].splitlines()
    doc: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("//"):
            text = stripped.removeprefix("//").strip()
            if text.startswith(("go:build", "+build", "Code generated")):
                continue
            doc.append(text)
        elif stripped == "":
            if doc:
                break
        else:
            break
    return " ".join(reversed(doc)).strip()
