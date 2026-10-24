"""JavaRunner: javac + JUnit Platform launcher + JaCoCo (fast path), or mvn.

the repository is never written: Maven modules are prepared (`test-compile` and the test
classpath) in the sandbox mirror, or, without one, in a mirror under OUT/work/java/mirror;
per-call overlays are compiled into OUT/work/tmp/j*/classes, which shadows target/classes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codocs.golang.gotool import (
    MAX_BUILD_ERRORS,
    OUTPUT_CAP,
    OUTPUT_LIMIT,
    TAIL_CAP,
    CoverBlock,
    _Capture,
    _drain,
    _killpg,
    _Proc,
)
from codocs.lang.base import LangTestResult

log = logging.getLogger(__name__)

OUTER_SLACK = 10
MVN_SLACK = 120
PREP_TIMEOUT = 900
DEFAULT_PLATFORM = "1.10.2"
JACOCO_TABLE = (("0.8.12", 22), ("0.8.13", 24), ("0.8.14", 25))
JACOCO_MAX_RELEASE = dict(JACOCO_TABLE)
DEPENDENCY_PLUGIN = ("maven-dependency-plugin", "3.6.1")
HELP_PLUGIN = ("maven-help-plugin", "3.4.0")
RUNNER_CLASS = "com.codocs.CodocsJUnitRunner"
RUNNER_SOURCE = Path(__file__).resolve().parent / "tools" / "CodocsJUnitRunner.java"
NOT_STARTED = "the test JVM timed out before any test started (class loading or discovery did " \
    "not finish)"
GRADLE_UNSUPPORTED = "Gradle builds are not supported yet; this unit is covered statically only"
MIRROR_SKIP = frozenset({".git", ".hg", ".svn", "target", "node_modules", "__pycache__",
                         ".venv", "venv", ".idea", ".gradle", "build", ".mvn-cache"})
SOFT_SKIP = frozenset({"target", "build", "venv", "node_modules"})   # legal package names

_TOOLCHAIN_ERRORS = ("no usable JDK", "mvn not found", "cannot resolve the Java tool jars",
                     "cannot compile CodocsJUnitRunner")

_JAVAC_ERR_RE = re.compile(r"^(?P<path>.+?\.java):(?P<line>\d+): (?P<kind>error|warning): "
                           r"(?P<msg>.*)$")
_MVN_ERR_RE = re.compile(r"^\[ERROR\]\s+(?P<path>/\S+?\.java):\[(?P<line>\d+),(?P<col>\d+)\]"
                         r"\s*(?P<msg>.*)$")
_VERSION_RE = re.compile(r'version "(?P<v>[^"]+)"')
_PROP_RE = re.compile(r"[@$]\{([^}]+)\}")


# -- jdk -------------------------------------------------------------------------------


def _feature(version: str) -> int:
    """`21.0.5` -> 21, `1.8.0_412` -> 8."""
    parts = re.findall(r"\d+", version)
    if not parts:
        return 0
    if parts[0] == "1" and len(parts) > 1:
        return int(parts[1])
    return int(parts[0])


def _vkey(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", v))


def _probe_jdk(home: Path) -> tuple[str, str]:
    """(java version, "") when <home>/bin/javac and java run, else ("", reason)."""
    javac, java = home / "bin" / "javac", home / "bin" / "java"
    if not javac.is_file() or not java.is_file():
        return "", f"{home}: no bin/javac or bin/java"
    try:
        a = subprocess.run([str(javac), "-version"], capture_output=True, text=True, timeout=60)
        b = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return "", f"{home}: {e}"
    if a.returncode != 0 or b.returncode != 0:
        msg = (b.stderr or a.stderr or "").strip().splitlines()
        return "", f"{home}: {msg[0] if msg else 'exit status ' + str(b.returncode)}"
    m = _VERSION_RE.search(b.stderr + b.stdout)
    return (m.group("v") if m else "0"), ""


def _jdk_candidates(java_home: str, mvn: str | None) -> list[Path]:
    if java_home:
        return [Path(java_home).expanduser()]
    out: list[Path] = []
    if os.environ.get("JAVA_HOME"):
        out.append(Path(os.environ["JAVA_HOME"]))
    if sys.platform == "darwin" and Path("/usr/libexec/java_home").exists():
        for v in ("21", "17"):
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                cp = subprocess.run(["/usr/libexec/java_home", "-v", v], capture_output=True,
                                    text=True, timeout=30)
                if cp.returncode == 0 and cp.stdout.strip():
                    out.append(Path(cp.stdout.strip()))
    for p in ("/opt/homebrew/opt/openjdk@21", "/opt/homebrew/opt/openjdk@17",
              "/opt/homebrew/opt/openjdk"):
        out.append(Path(p))
    for base in (Path("/usr/local/opt"), Path("/usr/lib/jvm")):
        with contextlib.suppress(OSError):
            names = [d.name for d in base.iterdir()
                     if d.is_dir() and (base.name == "jvm" or d.name.startswith("openjdk@"))]
            for name in sorted(names, key=lambda n: _vkey(re.sub(r"\D+", ".", n).strip(".")),
                               reverse=True):
                out.append(base / name)
    if mvn:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            cp = subprocess.run([mvn, "-v"], capture_output=True, text=True, timeout=60)
            m = re.search(r"runtime:\s*(\S.*)$", cp.stdout, re.MULTILINE)
            if m:
                out.append(Path(m.group(1).strip()))
    seen: set[str] = set()
    uniq = []
    for p in out:
        if str(p) not in seen:
            seen.add(str(p))
            uniq.append(p)
    return uniq


_JDK_CACHE: dict[tuple, tuple[Path | None, str, list[str]]] = {}


def select_jdk(java_home: str = "", mvn: str | None = None
               ) -> tuple[Path | None, str, list[str]]:
    """(jdk home, java version, rejected candidates) in _jdk_candidates order (cached)."""
    key = (java_home, mvn)
    if key not in _JDK_CACHE:
        rejected: list[str] = []
        found: tuple[Path | None, str] = (None, "")
        for cand in _jdk_candidates(java_home, mvn):
            version, why = _probe_jdk(cand)
            if version:
                found = (cand.resolve(), version)
                break
            rejected.append(why)
        _JDK_CACHE[key] = (found[0], found[1], rejected)
    return _JDK_CACHE[key]


# -- pure helpers ----------------------------------------------------------------------


def _cap_tail(text: str, n: int) -> str:
    return text if len(text) <= n else text[-n:]


def _argfile_quote(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_argfile(path: Path, args: list[str]) -> Path:
    path.write_text("\n".join(_argfile_quote(a) for a in args) + "\n", encoding="utf-8")
    return path


class PathMap:
    """maps tool paths (call/src, sandbox, mirror, repo) back to repo rels."""

    def __init__(self, prefixes: dict[str, str]):
        pairs: dict[str, str] = {}
        for p, rel in prefixes.items():
            for form in {str(p), str(Path(p).resolve())}:
                pairs[form.rstrip("/") + "/"] = (rel.strip("/") + "/") if rel else ""
        self.pairs = sorted(pairs.items(), key=lambda kv: -len(kv[0]))

    def path(self, p: str) -> str:
        for pre, rel in self.pairs:
            if p.startswith(pre):
                return rel + p[len(pre):]
        return p

    def text(self, s: str) -> str:
        for pre, rel in self.pairs:
            s = s.replace(pre, rel)
        return s


def parse_javac(output: str, mapper: PathMap) -> list[str]:
    """javac errors as `rel:line: message` (symbol/location lines folded in)."""
    out: list[str] = []
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        m = _JAVAC_ERR_RE.match(lines[i])
        if not m or m.group("kind") != "error":
            i += 1
            continue
        msg = m.group("msg").strip()
        extra = []
        j = i + 1
        while j < len(lines) and not _JAVAC_ERR_RE.match(lines[j]) \
                and not re.match(r"^\d+ errors?$", lines[j].strip()):
            s = lines[j].strip()
            if s.startswith(("symbol:", "location:", "required:", "found:", "reason:")):
                extra.append(" ".join(s.split()))
            j += 1
        if extra:
            msg += " (" + "; ".join(extra) + ")"
        out.append(f"{mapper.path(m.group('path'))}:{m.group('line')}: {msg}")
        i = j
    if not out:
        tail = [ln.strip() for ln in output.splitlines() if ln.strip()]
        out = [mapper.text(ln) for ln in tail[-5:]] or ["javac failed"]
    return out[:MAX_BUILD_ERRORS]


_MVN_NOISE = ("-> [Help", "Re-run", "To see", "For more", "[Help", "After correcting",
              "COMPILATION ERROR", "BUILD FAILURE")
_MVN_SUMMARY = ("Failed to execute goal",)      # kept only when no compiler error was parsed


def parse_mvn_errors(output: str, mapper: PathMap) -> list[str]:
    """mvn `[ERROR]` lines; compiler ones normalised to `rel:line:col: msg` with javac's
    symbol/location lines folded in (as parse_javac does); Maven's summary boilerplate and the
    repeated copy of each compiler error are dropped."""
    out: list[str] = []
    summary: list[str] = []
    compiler = False
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        m = _MVN_ERR_RE.match(ln)
        if m:
            msg = m.group("msg").strip()
            extra = []
            j = i + 1
            while j < len(lines):
                s = lines[j].strip()
                s = s[7:].strip() if s.startswith("[ERROR]") else s
                if not s.startswith(("symbol:", "location:", "required:", "found:", "reason:")):
                    break
                extra.append(" ".join(s.split()))
                j += 1
            if extra:
                msg += " (" + "; ".join(extra) + ")"
            out.append(f"{mapper.path(m.group('path'))}:{m.group('line')}:{m.group('col')}: "
                       f"{msg}")
            compiler = True
            i = j
            continue
        if ln.startswith("[ERROR]") and ln[7:].strip():
            body = ln[7:].strip()
            if body.startswith(_MVN_SUMMARY):
                summary.append(mapper.text(body))
            elif not body.startswith(_MVN_NOISE) and not body.startswith(
                    ("symbol:", "location:")):
                out.append(mapper.text(body))
        i += 1
    if not compiler:
        out += summary
    out = list(dict.fromkeys(out))
    if not out:
        tail = [ln.strip() for ln in output.splitlines() if ln.strip()]
        out = [mapper.text(ln) for ln in tail[-5:]] or ["mvn failed"]
    return out[:MAX_BUILD_ERRORS]


_OUTCOME = {"passed": "pass", "failed": "fail", "aborted": "skip", "skipped": "skip"}


def fold_events(events: list[dict], *, requested: list[str] | None = None,
                died: bool = False, outer_timeout: bool = False,
                tail: str = "") -> LangTestResult:
    """runner events -> verdicts per folded id."""
    inv: dict[str, list] = {}          # uid -> [id, verdict | None]
    order: list[str] = []
    texts: dict[str, list[str]] = {}
    ids: list[str] = []
    inner_timeout = ended = False
    disc_err = ""
    for ev in events:
        e = ev.get("e")
        if e == "run":
            uid, tid = ev.get("uid") or ev.get("id"), ev.get("id", "")
            if uid not in inv:
                inv[uid] = [tid, None]
                order.append(uid)
            if tid not in ids:
                ids.append(tid)
        elif e == "result":
            uid, tid = ev.get("uid") or ev.get("id"), ev.get("id", "")
            verdict = _OUTCOME.get(ev.get("outcome", ""), "fail")
            if uid not in inv:
                order.append(uid)
            inv[uid] = [tid, verdict]
            if tid not in ids:
                ids.append(tid)
            if ev.get("text"):
                texts.setdefault(tid, []).append(ev["text"])
        elif e == "timeout":
            inner_timeout = True
        elif e == "end":
            ended = True
        elif e == "discovery_error":
            disc_err = ev.get("text", "") or "test discovery failed"
    timed_out = inner_timeout or outer_timeout
    died = died or not ended or timed_out
    if disc_err:
        return LangTestResult(build_ok=False, build_errors=[disc_err.strip().splitlines()[0]],
                              timed_out=timed_out, tail=tail)
    if outer_timeout and not order:
        return LangTestResult(build_ok=False, build_errors=[NOT_STARTED], timed_out=True,
                              tail=tail)
    explicit_fail = any(v == "fail" for _, v in inv.values())
    pending = [u for u in order if inv[u][1] is None]
    last = pending[-1] if pending else None
    per: dict[str, list[str]] = {}
    crashed: set[str] = set()
    for uid in order:
        tid, v = inv[uid]
        if v is None:
            if uid == last and (timed_out or not explicit_fail):
                v = "fail"
                crashed.add(tid)
            else:
                v = "interrupted"
        per.setdefault(tid, []).append(v)
    res = LangTestResult(build_ok=True, timed_out=timed_out, tail=tail)
    for tid in ids:
        vs = per.get(tid, [])
        if not vs:
            continue
        if "fail" in vs:
            verdict = "fail"
        elif all(v == "skip" for v in vs):
            verdict = "skip"
        elif "interrupted" in vs:
            verdict = "interrupted"
        else:
            verdict = "pass"
        res.tests[tid] = verdict
        body = "\n".join(texts.get(tid, []))
        if tid in crashed:
            why = "test timed out" if timed_out else "the test JVM exited during this test"
            body = (body + "\n" if body else "") + why + "\n" + tail
        res.output[tid] = _cap_tail(body, OUTPUT_CAP)
    if died:
        for name in requested or []:
            if name not in res.tests:
                res.tests[name] = "interrupted"
                res.output.setdefault(name, "")
    return res


def _norm_case(name: str) -> str:
    name = re.sub(r"\[\d+\]$", "", name.strip())
    return re.sub(r"\(.*\)$", "", name).strip()


def parse_surefire(report_dir: Path, classes: set[str] | None = None) -> dict[str, list]:
    """folded id -> [(verdict, text)] from every TEST-*.xml (nested classes included)."""
    out: dict[str, list] = {}
    for f in sorted(report_dir.glob("TEST-*.xml")):
        try:
            root = ET.parse(str(f)).getroot()
        except (OSError, ET.ParseError):
            continue
        for tc in root.iter("testcase"):
            cls = tc.get("classname", "")
            name = _norm_case(tc.get("name", ""))
            if classes is not None and cls.split("$", 1)[0] not in classes:
                continue
            verdict, text = "pass", ""
            for child in tc:
                if child.tag in ("failure", "error"):
                    verdict = "fail"
                    text = (child.get("message", "") + "\n" + (child.text or "")).strip()
                    break
                if child.tag == "skipped":
                    verdict = "skip"
            if not name or name == cls.rsplit(".", 1)[-1] or name == cls:
                out.setdefault(f"container:{cls}", []).append((verdict, text))
                continue
            out.setdefault(f"{cls}#{name}", []).append((verdict, text))
    return out


def jacoco_blocks(xml_text: str, files: dict[str, str]) -> list[CoverBlock]:
    """one block per listed line of each <sourcefile> named in files (pkg/name -> rel)."""
    blocks: list[CoverBlock] = []
    root = ET.fromstring(xml_text)
    for pkg in root.iter("package"):
        pname = pkg.get("name", "")
        for sf in pkg.iter("sourcefile"):
            key = f"{pname}/{sf.get('name', '')}" if pname else sf.get("name", "")
            rel = files.get(key)
            if rel is None:
                continue
            for ln in sf.iter("line"):
                nr = int(ln.get("nr", "0"))
                ci = int(ln.get("ci", "0"))
                blocks.append(CoverBlock(rel, nr, 1, nr, 1, 1, 1 if ci > 0 else 0))
    return blocks


def platform_version(cp: list[str]) -> str:
    """the JUnit Platform version of a test classpath: the junit-platform-engine/commons jar
    (1.x for Jupiter 5, 6.x for JUnit 6), else derived from junit-jupiter-api."""
    for entry in cp:
        m = re.search(r"junit-platform-(?:engine|commons|launcher)-(\d+\.\d+\.\d+)[^/]*\.jar$",
                      entry)
        if m:
            return m.group(1)
    for entry in cp:
        m = re.search(r"junit-jupiter-api-(\d+)\.(\d+\.\d+)[^/]*\.jar$", entry)
        if m:
            return f"1.{m.group(2)}" if m.group(1) == "5" else f"{m.group(1)}.{m.group(2)}"
    return DEFAULT_PLATFORM


def _parse_release(v: str) -> int:
    v = (v or "").strip()
    if not v or "${" in v:
        return 0
    return _feature(v)


# -- effective pom ---------------------------------------------------------------------


@dataclass
class EffectivePom:
    release: int = 0
    explicit_release: bool = False
    source_target: bool = False     # only source/target set: Maven passes -source/-target
    encoding: str = "UTF-8"
    output_dir: str = ""
    test_output_dir: str = ""
    properties: dict[str, str] = field(default_factory=dict)
    processor_paths: list[tuple[str, str, str]] = field(default_factory=list)
    compiler_args: list[str] = field(default_factory=list)
    argline: str = ""
    sysprops: dict[str, str] = field(default_factory=dict)
    envvars: dict[str, str] = field(default_factory=dict)
    workdir: str = ""


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _t(el: ET.Element | None, path: str) -> str:
    if el is None:
        return ""
    x = el.find(path)
    return (x.text or "").strip() if x is not None and x.text else ""


def parse_effective_pom(text: str, artifact: str = "") -> EffectivePom:
    root = ET.fromstring(text)
    _strip_ns(root)
    projects = [root] if root.tag == "project" else list(root.iter("project"))
    proj = next((p for p in projects if artifact and _t(p, "artifactId") == artifact),
                projects[-1] if projects else root)
    eff = EffectivePom()
    props = proj.find("properties")
    if props is not None:
        eff.properties = {p.tag: (p.text or "").strip() for p in props if isinstance(p.tag, str)}
    eff.encoding = eff.properties.get("project.build.sourceEncoding", "") or "UTF-8"
    eff.output_dir = _t(proj, "build/outputDirectory")
    eff.test_output_dir = _t(proj, "build/testOutputDirectory")
    comp = surefire = None
    for pl in proj.findall("build/plugins/plugin"):
        aid = _t(pl, "artifactId")
        if aid == "maven-compiler-plugin":
            comp = pl
        elif aid == "maven-surefire-plugin":
            surefire = pl
    release = target = ""
    if comp is not None:
        conf = comp.find("configuration")
        release = _t(conf, "release")
        target = _t(conf, "target") or _t(conf, "source")
        if conf is not None:
            for path in conf.findall("annotationProcessorPaths/path"):
                eff.processor_paths.append((_t(path, "groupId"), _t(path, "artifactId"),
                                            _t(path, "version")))
            for arg in conf.findall("compilerArgs/arg"):
                if arg.text and arg.text.strip():
                    eff.compiler_args.append(arg.text.strip())
    release = release or eff.properties.get("maven.compiler.release", "")
    target = target or eff.properties.get("maven.compiler.target", "") \
        or eff.properties.get("maven.compiler.source", "")
    if _parse_release(release):
        eff.release, eff.explicit_release = _parse_release(release), True
    elif _parse_release(target):
        eff.release, eff.explicit_release = _parse_release(target), True
        eff.source_target = True
    if surefire is not None:
        conf = surefire.find("configuration")
        eff.argline = _t(conf, "argLine")
        eff.workdir = _t(conf, "workingDirectory")
        if conf is not None:
            for k in ("systemPropertyVariables", "environmentVariables"):
                el = conf.find(k)
                if el is None:
                    continue
                vals = {c.tag: (c.text or "").strip() for c in el if isinstance(c.tag, str)}
                if k == "systemPropertyVariables":
                    eff.sysprops = vals
                else:
                    eff.envvars = vals
    return eff


def expand_argline(argline: str, props: dict[str, str]) -> tuple[list[str], list[str]]:
    """shlex tokens of surefire's argLine with known properties expanded; (tokens, notes)."""
    notes: list[str] = []
    out: list[str] = []
    try:
        tokens = shlex.split(argline or "")
    except ValueError:
        return [], [f"argLine not parsed: {argline!r}"]
    for tok in tokens:
        bad = False

        def sub(m: re.Match, tok=tok) -> str:
            nonlocal bad
            key = m.group(1)
            if key == "argLine":
                return ""
            if key in props:
                return props[key]
            bad = True
            return ""

        val = _PROP_RE.sub(sub, tok).strip()
        if bad:
            notes.append(f"argLine token {tok!r} dropped (unknown property)")
            continue
        if val:
            out.append(val)
    return out, notes


# -- module preparation ------------------------------------------------------------------


@dataclass
class ModulePrep:
    key: str
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    mode: str = "maven"
    exec_module: Path | None = None
    classes: Path | None = None
    test_classes: Path | None = None
    cp: list[str] = field(default_factory=list)
    release: int = 0
    explicit_release: bool = False
    source_target: bool = False
    encoding: str = "UTF-8"
    platform: str = DEFAULT_PLATFORM
    jacoco: str = JACOCO_TABLE[0][0]
    jars: dict[str, Path] = field(default_factory=dict)
    runner_classes: Path | None = None
    processor_path: list[str] = field(default_factory=list)
    compiler_args: list[str] = field(default_factory=list)
    argline: list[str] = field(default_factory=list)
    sysprops: dict[str, str] = field(default_factory=dict)
    envvars: dict[str, str] = field(default_factory=dict)
    workdir: Path | None = None
    notes: list[str] = field(default_factory=list)
    generation: int = 0

    @property
    def coverage_error(self) -> str:
        top = JACOCO_MAX_RELEASE.get(self.jacoco, 0)
        if self.release and top and self.release > top:
            return f"JaCoCo {self.jacoco} cannot instrument class-file release {self.release}"
        return ""


def _jar_path(repo: Path, g: str, a: str, v: str, classifier: str = "") -> Path:
    name = f"{a}-{v}" + (f"-{classifier}" if classifier else "") + ".jar"
    return repo / g.replace(".", "/") / a / v / name


def _tool_coords(platform: str, jacoco: str) -> dict[str, tuple[str, str, str, str]]:
    return {
        "standalone": ("org.junit.platform", "junit-platform-console-standalone", platform, ""),
        "agent": ("org.jacoco", "org.jacoco.agent", jacoco, "runtime"),
        "cli": ("org.jacoco", "org.jacoco.cli", jacoco, "nodeps"),
    }


def _settings_local_repo() -> Path:
    s = Path.home() / ".m2" / "settings.xml"
    with contextlib.suppress(OSError, ET.ParseError):
        root = ET.parse(str(s)).getroot()
        _strip_ns(root)
        val = _t(root, "localRepository")
        if val:
            return Path(val.replace("${user.home}", str(Path.home()))).expanduser()
    return Path.home() / ".m2" / "repository"


def _plugin_version(repo: Path, artifact: str, default: str) -> str:
    d = repo / "org" / "apache" / "maven" / "plugins" / artifact
    with contextlib.suppress(OSError):
        vers = [p.name for p in d.iterdir()
                if p.is_dir() and (p / f"{artifact}-{p.name}.jar").is_file()]
        if vers:
            return max(vers, key=_vkey)
    return default


class JavaRunner:
    """runs a Java unit's tests (fast path by default, `java_runner=mvn` for surefire)."""

    lang = "java"

    def __init__(self, repo_root: str | Path, *, adapter: Any = None, settings: Any = None,
                 out_dir: str | Path | None = None, work_dir: str | Path | None = None,
                 tools_dir: str | Path | None = None, exec_root: str | Path | None = None,
                 run_ctx: Any = None, workers: int = 4, timeout: int | None = None):
        self.module_root = Path(repo_root).resolve()
        self.settings = settings
        self.run_ctx = run_ctx
        if adapter is None:
            from codocs.lang.base import RepoContext
            from codocs.lang.registry import adapter as get_adapter

            ctx = RepoContext.build(self.module_root, ["java"], settings, out_dir)
            adapter = get_adapter("java", ctx)
        self.adapter = adapter
        if out_dir is not None:
            out = Path(out_dir).resolve()
            base_work = out / "work"
        else:
            base_work = Path(tempfile.gettempdir()).resolve() / "codocs-java"
        self.work_dir = Path(work_dir).resolve() if work_dir else base_work / "tmp"
        self.tools_dir = Path(tools_dir).resolve() if tools_dir else base_work / "tools"
        self.java_dir = self.work_dir.parent / "java"
        if exec_root is not None and Path(exec_root).resolve() != self.module_root:
            self.exec_root = Path(exec_root).resolve()
            self.sandboxed = True
        else:
            self.exec_root = self.java_dir / "mirror"
            self.sandboxed = False
        self.module = self.module_root.name
        t = timeout if timeout is not None else (
            self._setting("test_timeout", 0) or self._setting("go_timeout", 300))
        self.timeout = int(t or 300)
        self.workers = max(1, int(workers))
        self.java_home = self._setting("java_home", "") or os.environ.get("CODOCS_JAVA_HOME", "")
        mvn = self._setting("mvn", "") or os.environ.get("CODOCS_MVN", "") or "mvn"
        self.mvn = shutil.which(mvn) or (mvn if Path(mvn).is_file() else None)
        self.mode = (self._setting("java_runner", "") or os.environ.get("CODOCS_JAVA_RUNNER", "")
                     or "fast").lower()
        self.jacoco_pin = self._setting("jacoco_version", "") or \
            os.environ.get("CODOCS_JACOCO_VERSION", "")
        self._sem: asyncio.Semaphore | None = None
        self._sem_loop = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._preps: dict[tuple[str, int], ModulePrep] = {}
        self._mirrored: set[str] = set()
        self._version: str | None = None
        self._local_repo: Path | None = None
        self._tools_lock: asyncio.Lock | None = None
        self._warned: set[str] = set()

    @classmethod
    def from_context(cls, ctx: Any, adapter: Any = None) -> JavaRunner:
        """build from a pipeline.RunContext (attributes read defensively)."""
        repo_ctx = getattr(ctx, "repo_ctx", None)
        if adapter is None and repo_ctx is not None:
            from codocs.lang.registry import adapter as get_adapter

            adapter = get_adapter("java", repo_ctx)
        out_dir = getattr(ctx, "out_dir", None)
        work_dir = getattr(ctx, "work_dir", None)
        if out_dir is None and work_dir is not None:
            out_dir = Path(work_dir).parent.parent
        return cls(getattr(ctx, "repo_root"), adapter=adapter,
                   settings=getattr(ctx, "settings", None), out_dir=out_dir, work_dir=work_dir,
                   tools_dir=getattr(ctx, "tools_dir", None),
                   exec_root=getattr(ctx, "exec_root", None), run_ctx=ctx,
                   workers=getattr(ctx, "workers", 4) or 4,
                   timeout=getattr(ctx, "timeout", None))

    def _setting(self, name: str, default: Any) -> Any:
        return getattr(self.settings, name, default) if self.settings is not None else default

    # -- toolchain -----------------------------------------------------------------------

    def jdk(self) -> tuple[Path | None, str, list[str]]:
        return select_jdk(self.java_home, self.mvn)

    def _jdk_reason(self) -> str:
        home, _, rejected = self.jdk()
        if home is None:
            return "no usable JDK (javac and java): " + ("; ".join(rejected) or "no candidates")
        return ""

    def unavailable_reason(self) -> str:
        why = self._jdk_reason()
        if why:
            return why
        if not self.mvn and self._has_maven_modules():
            return "mvn not found (set --mvn or CODOCS_MVN); the Maven modules need it"
        return ""

    @staticmethod
    def toolchain_failure(build_errors: list[str]) -> bool:
        """whether a failed build is the toolchain's (no JDK/mvn, tool jars unresolvable):
        such a baseline is not cached, so a later run with a working toolchain recomputes."""
        return any(e.startswith(_TOOLCHAIN_ERRORS) for e in build_errors)

    def _has_maven_modules(self) -> bool:
        try:
            lay = self.adapter.layout()
        except Exception:
            return False
        return any(m.mode == "maven" for m in lay.modules.values())

    def available(self) -> bool:
        return not self.unavailable_reason()

    def version(self) -> str:
        if self._version is None:
            home, ver, _ = self.jdk()
            if home is None:
                self._version = ""
                return ""
            parts = [f"Java {ver} ({home})"]
            if self.mvn:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    cp = subprocess.run([self.mvn, "-v"], capture_output=True, text=True,
                                        timeout=60, env=self._env())
                    first = cp.stdout.strip().splitlines()[:1]
                    if first:
                        parts.append(first[0].split("(")[0].strip())
            # the platform follows each module's test classpath (1.x for JUnit 5, 6.x for 6)
            parts.append(f"JUnit Platform per module (default {DEFAULT_PLATFORM}), "
                         f"JaCoCo {self._jacoco_for(17)}")
            self._version = ", ".join(parts)
        return self._version

    def _java(self, tool: str = "java") -> str:
        home = self.jdk()[0]
        return str((home or Path("/nonexistent")) / "bin" / tool)

    def _feature(self) -> int:
        return _feature(self.jdk()[1])

    def _jacoco_for(self, release: int) -> str:
        if self.jacoco_pin:
            return self.jacoco_pin
        for ver, top in JACOCO_TABLE:
            if release <= top:
                return ver
        return JACOCO_TABLE[-1][0]

    def _env(self, tmp: Path | None = None, extra: dict[str, str] | None = None
             ) -> dict[str, str]:
        env = dict(os.environ)
        home = self.jdk()[0]
        if home is not None:
            env["JAVA_HOME"] = str(home)
            env["PATH"] = str(home / "bin") + os.pathsep + env.get("PATH", "")
        if tmp is not None:
            env["TMPDIR"] = str(tmp)
        env.update(extra or {})
        return env

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._sem is None or self._sem_loop is not loop:
            self._sem = asyncio.Semaphore(self.workers)
            self._sem_loop = loop
            self._locks = {}
            self._tools_lock = asyncio.Lock()
        return self._sem

    def _lock(self, key: str) -> asyncio.Lock:
        self._semaphore()
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    async def _exec(self, args: list[str], *, cwd: Path, env: dict[str, str] | None = None,
                    timeout: float) -> _Proc:
        """run one process in its own session, bounded by the semaphore; kill on timeout."""
        async with self._semaphore():
            return await self._exec_raw(args, cwd=cwd, env=env, timeout=timeout)

    async def _exec_raw(self, args: list[str], *, cwd: Path, env: dict[str, str] | None,
                        timeout: float) -> _Proc:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=str(cwd), env=env or self._env(), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
        except OSError as e:
            return _Proc(127, "", f"{args[0]}: {e}", False)
        out = _Capture(OUTPUT_LIMIT, lambda: _killpg(proc))
        err = _Capture(OUTPUT_LIMIT, lambda: _killpg(proc))
        readers = asyncio.gather(_drain(proc.stdout, out), _drain(proc.stderr, err))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=max(timeout, 0.01))
        except asyncio.TimeoutError:
            timed_out = True
            _killpg(proc)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=10)
        except BaseException:
            _killpg(proc)
            readers.cancel()
            raise
        try:
            await asyncio.wait_for(readers, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _killpg(proc)
            readers.cancel()
            with contextlib.suppress(BaseException):
                await readers
        rc = proc.returncode if proc.returncode is not None else -1
        return _Proc(rc, out.text(), err.text(), timed_out, out.overflow or err.overflow)

    # -- tool jars -----------------------------------------------------------------------

    def local_repo(self) -> Path:
        if self._local_repo is None:
            self._local_repo = _settings_local_repo()
        return self._local_repo

    def _tools_json(self) -> Path:
        return self.tools_dir / "java" / "tools.json"

    async def tool_jars(self, platform: str, jacoco: str) -> tuple[dict[str, Path], str]:
        """({standalone, agent, cli} -> jar, "") or ({}, reason); cached in tools.json."""
        self._semaphore()
        assert self._tools_lock is not None
        async with self._tools_lock:
            key = f"{platform}|{jacoco}"
            cache: dict = {}
            with contextlib.suppress(OSError, ValueError):
                cache = json.loads(self._tools_json().read_text(encoding="utf-8"))
            hit = cache.get(key)
            if hit and all(Path(p).is_file() for p in hit.values()):
                return {k: Path(v) for k, v in hit.items()}, ""
            coords = _tool_coords(platform, jacoco)
            repo = self.local_repo()
            found = {k: _jar_path(repo, *c) for k, c in coords.items()}
            missing = [k for k, p in found.items() if not p.is_file()]
            if missing and self.mvn:
                await self._resolve_jars([coords[k] for k in missing])
                if any(not found[k].is_file() for k in missing):
                    real = await self._mvn_local_repo()
                    if real is not None and real != repo:
                        self._local_repo = repo = real
                        found = {k: _jar_path(repo, *c) for k, c in coords.items()}
                        missing = [k for k, p in found.items() if not p.is_file()]
                        if missing:
                            await self._resolve_jars([coords[k] for k in missing])
            missing = [k for k, p in found.items() if not p.is_file()]
            if missing:
                names = ", ".join(":".join(x for x in coords[k] if x) for k in missing)
                return {}, (f"cannot resolve the Java tool jars ({names}); put them into the "
                            f"local Maven repository {repo}")
            cache[key] = {k: str(v) for k, v in found.items()}
            self._tools_json().parent.mkdir(parents=True, exist_ok=True)
            self._tools_json().write_text(json.dumps(cache, indent=2), encoding="utf-8")
            return found, ""

    async def _mvn_local_repo(self) -> Path | None:
        if not self.mvn:
            return None
        p = await self._exec([self.mvn, "-q", "help:evaluate",
                              "-Dexpression=settings.localRepository", "-DforceStdout"],
                             cwd=self.tools_dir.parent, timeout=PREP_TIMEOUT)
        val = p.stdout.strip().splitlines()[-1:] if p.returncode == 0 else []
        return Path(val[0].strip()) if val and val[0].strip().startswith("/") else None

    async def _resolve_jars(self, coords: list[tuple[str, str, str, str]]) -> None:
        d = self.tools_dir / "java" / "resolve"
        d.mkdir(parents=True, exist_ok=True)
        deps = "".join(
            f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId>"
            f"<version>{v}</version>" + (f"<classifier>{c}</classifier>" if c else "")
            + "<exclusions><exclusion><groupId>*</groupId><artifactId>*</artifactId>"
            "</exclusion></exclusions></dependency>" for g, a, v, c in coords)
        (d / "pom.xml").write_text(
            '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0'
            "</modelVersion><groupId>codocs</groupId><artifactId>codocs-resolve</artifactId>"
            f"<version>1</version><dependencies>{deps}</dependencies></project>\n",
            encoding="utf-8")
        plugin = _plugin_version(self.local_repo(), *DEPENDENCY_PLUGIN)
        goal = f"org.apache.maven.plugins:maven-dependency-plugin:{plugin}:resolve"
        for offline in (True, False):
            args = [self.mvn, "-q", "-B", *(["-o"] if offline else []), "-f",
                    str(d / "pom.xml"), goal]
            p = await self._exec(args, cwd=d, timeout=PREP_TIMEOUT)
            if p.returncode == 0:
                return

    async def _runner_classes(self, standalone: Path) -> tuple[Path | None, str]:
        """CodocsJUnitRunner compiled once per platform jar into OUT/work/tools/java."""
        src = RUNNER_SOURCE.read_bytes()
        digest = hashlib.sha1(src + str(standalone).encode()).hexdigest()[:12]
        out = self.tools_dir / "java" / f"classes-{digest}"
        marker = out / "com" / "codocs" / "CodocsJUnitRunner.class"
        async with self._lock(f"runner:{digest}"):
            if marker.is_file():
                return out, ""
            srcdir = self.tools_dir / "java" / "src" / "com" / "codocs"
            srcdir.mkdir(parents=True, exist_ok=True)
            (srcdir / "CodocsJUnitRunner.java").write_bytes(src)
            tmp = Path(tempfile.mkdtemp(prefix="rc", dir=self.tools_dir / "java"))
            major = standalone.parent.name.split(".", 1)[0]
            release = "17" if major.isdigit() and int(major) >= 6 else "11"   # JUnit 6: Java 17
            p = await self._exec([self._java("javac"), "--release", release, "-nowarn",
                                  "-encoding",
                                  "UTF-8", "-d", str(tmp), "-cp", str(standalone),
                                  str(srcdir / "CodocsJUnitRunner.java")],
                                 cwd=self.tools_dir, timeout=PREP_TIMEOUT)
            if p.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                return None, "cannot compile CodocsJUnitRunner: " + \
                    " ".join((p.stderr or p.stdout).strip().splitlines()[-3:])
            if out.exists():
                shutil.rmtree(out, ignore_errors=True)
            os.replace(tmp, out)
            return out, ""

    # -- sync / mirror -------------------------------------------------------------------

    async def _generation(self) -> int:
        if self.sandboxed:
            ctx = self.run_ctx
            ensure = getattr(ctx, "ensure_synced", None)
            if ensure is not None:
                await ensure()
            return int(getattr(ctx, "sync_generation", 1) or 1)
        return 1

    def _mirror(self, rel: str) -> None:
        """copy repo_root/rel into the private mirror (changed files only; target/ kept)."""
        if self.sandboxed or rel in self._mirrored:
            return
        if any(rel == m or not m or rel.startswith(m + "/") for m in self._mirrored):
            return
        src = self.module_root / rel if rel else self.module_root
        dst = self.exec_root / rel if rel else self.exec_root
        _mirror_tree(src, dst, exclude={self.work_dir.parent.parent, self.exec_root},
                     keep_dirs=self._keep_dirs(rel))
        self._mirrored.add(rel)

    def _keep_dirs(self, base: str) -> set[str] | None:
        """dirs (relative to base) holding the layout's Java files: a package named build,
        target or venv (com/acme/build) is never skipped like a build output dir."""
        try:
            lay = self.adapter.layout()
        except Exception:
            return None
        out: set[str] = set()
        for f in lay.file_module:
            if base and not f.startswith(base + "/"):
                continue
            cur = _parent(f[len(base) + 1:] if base else f)
            while cur and cur not in out:
                out.add(cur)
                cur = _parent(cur)
        return out

    def _exec_path(self, rel: str, mode: str) -> Path:
        if mode == "maven" or self.sandboxed:
            return self.exec_root / rel
        return self.module_root / rel

    def _mapper(self, call: Path | None = None) -> PathMap:
        pre = {str(self.exec_root): "", str(self.module_root): ""}
        if call is not None:
            pre[str(call / "src")] = ""
        return PathMap(pre)

    # -- module preparation ------------------------------------------------------------

    def _module(self, unit: Any):
        return self.adapter.layout().modules.get(unit.extra.get("module", ""))

    async def prepare(self, unit: Any) -> ModulePrep:
        """prepare the unit's module once per process and sync generation."""
        mod = self._module(unit)
        key = unit.extra.get("module", "")
        gen = await self._generation()
        hit = self._preps.get((key, gen))
        if hit is not None:
            return hit
        async with self._lock(f"prep:{key}"):
            hit = self._preps.get((key, gen))
            if hit is not None:
                return hit
            prep = ModulePrep(key=key, mode=mod.mode if mod is not None else "plain",
                              generation=gen)
            reason = self._jdk_reason()
            if reason:
                prep.errors = [reason]
            elif mod is None:
                prep.errors = [f"no Java module for {unit.rel}"]
            elif mod.mode == "gradle":
                prep.errors = [GRADLE_UNSUPPORTED]
            elif mod.mode == "maven":
                await self._prepare_maven(mod, prep)
            else:
                await self._prepare_plain(mod, prep)
            self._preps[(key, gen)] = prep
            return prep

    def _key_dir(self, key: str) -> Path:
        d = self.java_dir / (key.replace("/", "__") or "_root")
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _mvn(self, args: list[str], *, cwd: Path, timeout: float = PREP_TIMEOUT
                   ) -> _Proc:
        """an mvn command offline, retried online once on failure."""
        p = await self._exec([self.mvn, "-B", "-o", *args], cwd=cwd, env=self._env(),
                             timeout=timeout)
        if p.returncode == 0 or p.timed_out:
            return p
        return await self._exec([self.mvn, "-B", *args], cwd=cwd, env=self._env(),
                                timeout=timeout)

    async def _prepare_maven(self, mod: Any, prep: ModulePrep) -> None:
        if not self.mvn:
            prep.errors = ["mvn not found (set --mvn or CODOCS_MVN)"]
            return
        await asyncio.to_thread(self._mirror, mod.reactor)
        reactor = self.exec_root / mod.reactor if mod.reactor else self.exec_root
        module_dir = self.exec_root / mod.rel if mod.rel else self.exec_root
        prep.exec_module = module_dir
        kd = self._key_dir(mod.rel)
        eff_path, cp_path = kd / "effective-pom.xml", kd / "cp.txt"
        for stale in (eff_path, cp_path):
            with contextlib.suppress(OSError):
                stale.unlink()
        pl: list[str] = []
        if mod.rel != mod.reactor:
            sub = mod.rel[len(mod.reactor) + 1:] if mod.reactor else mod.rel
            pl = ["-pl", sub]
        mapper = self._mapper()
        repo = self.local_repo()
        help_v = _plugin_version(repo, *HELP_PLUGIN)
        p = await self._mvn(["-q", f"org.apache.maven.plugins:maven-help-plugin:{help_v}:"
                             "effective-pom", f"-Doutput={eff_path}", *pl], cwd=reactor)
        try:
            eff = parse_effective_pom(eff_path.read_text(encoding="utf-8"), mod.artifact)
        except (OSError, ET.ParseError) as e:
            prep.errors = parse_mvn_errors(p.stdout + "\n" + p.stderr, mapper) \
                if p.returncode != 0 else [f"cannot read the effective pom: {e}"]
            return
        release = eff.release or min(self._feature() or 17, JACOCO_MAX_RELEASE[
            JACOCO_TABLE[0][0]])
        prep.release, prep.explicit_release = release, eff.explicit_release
        prep.source_target = eff.source_target
        prep.encoding = eff.encoding
        rel_flag = [] if eff.explicit_release else [f"-Dmaven.compiler.release={release}"]
        dep_v = _plugin_version(repo, *DEPENDENCY_PLUGIN)
        args = ["-q", "-DskipTests", *rel_flag, *([*pl, "-am"] if pl else []), "test-compile",
                f"org.apache.maven.plugins:maven-dependency-plugin:{dep_v}:build-classpath",
                f"-Dmdep.outputFile={cp_path}", "-Dmdep.includeScope=test"]
        p = await self._mvn(args, cwd=reactor)
        if p.returncode != 0:
            prep.errors = parse_mvn_errors(p.stdout + "\n" + p.stderr, mapper)
            return
        try:
            cp_text = cp_path.read_text(encoding="utf-8").strip()
        except OSError:
            cp_text = ""
        prep.cp = [x for x in cp_text.split(os.pathsep) if x]
        prep.classes = Path(eff.output_dir) if eff.output_dir else module_dir / "target" / \
            "classes"
        prep.test_classes = Path(eff.test_output_dir) if eff.test_output_dir else \
            module_dir / "target" / "test-classes"
        prep.classes.mkdir(parents=True, exist_ok=True)
        prep.test_classes.mkdir(parents=True, exist_ok=True)
        prep.platform = platform_version(prep.cp)
        for g, a, v in eff.processor_paths:
            jar = _jar_path(repo, g, a, v)
            if jar.is_file():
                prep.processor_path.append(str(jar))
        prep.compiler_args = _safe_compiler_args(eff.compiler_args)
        props = dict(eff.properties)
        props.setdefault("project.basedir", str(module_dir))
        props.setdefault("basedir", str(module_dir))
        prep.argline, notes = expand_argline(eff.argline, props)
        prep.notes.extend(notes)
        prep.sysprops = {k: _PROP_RE.sub(lambda m: props.get(m.group(1), ""), v)
                         for k, v in eff.sysprops.items()}
        prep.envvars = dict(eff.envvars)
        prep.workdir = Path(eff.workdir) if eff.workdir else module_dir
        await self._finish_prep(prep)

    async def _finish_prep(self, prep: ModulePrep) -> None:
        prep.jacoco = self._jacoco_for(prep.release)
        jars, why = await self.tool_jars(prep.platform, prep.jacoco)
        if not jars and prep.platform != DEFAULT_PLATFORM:
            prep.notes.append(f"JUnit Platform {prep.platform} jars unavailable; using "
                              f"{DEFAULT_PLATFORM}")
            prep.platform = DEFAULT_PLATFORM
            jars, why = await self.tool_jars(prep.platform, prep.jacoco)
        if not jars:
            prep.errors = [why]
            return
        prep.jars = jars
        runner, why = await self._runner_classes(jars["standalone"])
        if runner is None:
            prep.errors = [why]
            return
        prep.runner_classes = runner
        if prep.coverage_error and "jacoco" not in self._warned:
            self._warned.add("jacoco")
            log.warning("%s: Java coverage is unavailable", prep.coverage_error)
        prep.ok = True

    async def _prepare_plain(self, mod: Any, prep: ModulePrep) -> None:
        """compile every source of a plain source root into OUT/work/java/<key>/."""
        lay = self.adapter.layout()
        kd = self._key_dir(mod.rel)
        prep.release = min(self._feature() or 17, JACOCO_MAX_RELEASE[JACOCO_TABLE[0][0]])
        prep.exec_module = self._exec_path(mod.source_root, "plain")
        prep.workdir = prep.exec_module
        await self._finish_prep(prep)
        if not prep.ok:
            return
        prep.ok = False
        mains = [r for r, k in lay.file_module.items()
                 if k == mod.rel and lay.file_role.get(r) == "main"]
        tests = [r for r, k in lay.file_module.items()
                 if k == mod.rel and lay.file_role.get(r) in ("test", "support")]
        # a plain src/ + test/ layout: the module's units own tests of other plain roots
        others = {lay.file_module.get(t) for u in lay.units.values()
                  if u.extra.get("module") == mod.rel for t in u.test_files}
        others = {k for k in others if k and k != mod.rel and k in lay.modules
                  and lay.modules[k].mode == "plain"}
        tests += [r for r, k in sorted(lay.file_module.items())
                  if k in others and lay.file_role.get(r) in ("test", "support")
                  and r not in tests]
        classes, test_classes = kd / "classes", kd / "test-classes"
        for d in (classes, test_classes):
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True)
        mapper = self._mapper()
        for files, out, cp in ((mains, classes, [str(prep.jars["standalone"])]),
                               (tests, test_classes, [str(classes),
                                                      str(prep.jars["standalone"])])):
            if not files:
                continue
            argfile = write_argfile(kd / "javac.args", [
                "-d", str(out), "-encoding", prep.encoding, "-nowarn", "-g", "--release",
                str(prep.release), "-cp", os.pathsep.join(cp),
                *[str(self._exec_path(f, "plain")) for f in sorted(files)]])
            p = await self._exec([self._java("javac"), f"@{argfile}"], cwd=kd,
                                 timeout=PREP_TIMEOUT)
            if p.returncode != 0:
                prep.errors = parse_javac(p.stdout + "\n" + p.stderr, mapper)
                return
        prep.classes, prep.test_classes = classes, test_classes
        prep.ok = True

    # -- per-call helpers ----------------------------------------------------------------

    def _call_dir(self) -> Path:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="j", dir=self.work_dir)).resolve()

    def _read(self, rel: str) -> str:
        return self.adapter.read(rel)

    def _codec(self, rel: str, prep: ModulePrep) -> str:
        """the module's sourceEncoding for Java sources (javac and Maven read every file of a
        call with -encoding <sourceEncoding>, so an ASCII original detected as utf-8 must not
        decide), else the existing file's codec."""
        if rel.endswith(".java") or self.adapter.ctx is None or \
                not (self.module_root / rel).exists():
            return _java_codec(prep.encoding)
        return self.adapter.ctx.codec(rel)

    def _write_overlay(self, call: Path, rel: str, content: str,
                       prep: ModulePrep | None = None) -> Path:
        path = call / "src" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        codec = self._codec(rel, prep) if prep is not None else "utf-8"
        path.write_bytes(encode_java(content, codec))
        return path

    def _selection(self, unit: Any, overlay: dict[str, str | None]
                   ) -> tuple[list[str], dict[str, str]]:
        """(test files after the overlay, test class FQCN -> file)."""
        files = [f for f in unit.test_files if not (f in overlay and overlay[f] is None)]
        for rel, content in overlay.items():
            if content is not None and rel not in unit.test_files and rel.endswith(".java") \
                    and self.adapter.is_test_file(rel, content):
                files.append(rel)
        classes: dict[str, str] = {}
        for f in files:
            src = overlay[f] if overlay.get(f) is not None else self._read(f)
            for fq in self.adapter.test_classes(src or ""):
                classes.setdefault(fq, f)
        return files, classes

    async def list_tests(self, unit_rel: str) -> list[str] | None:
        """runner discovery of the unit's existing tests (folded ids), None on failure."""
        unit = self.adapter.unit(unit_rel)
        if unit is None:
            return None
        if not unit.test_files:
            return []
        prep = await self.prepare(unit)
        if not prep.ok:
            return None
        _, classes = self._selection(unit, {})
        call = self._call_dir()
        try:
            res, events, _ = await self._launch(unit, prep, call, [f"c:{c}" for c in classes],
                                                cover=False, failfast=False, t=self.timeout,
                                                extra=["--discover-only"], use_call=False)
            if not any(e.get("e") == "end" for e in events):
                return None
            return sorted({e["id"] for e in events if e.get("e") == "discovered"})
        finally:
            shutil.rmtree(call, ignore_errors=True)

    # -- test -------------------------------------------------------------------------

    async def test(self, unit_rel: str, *, overlay: dict[str, str | None] | None = None,
                   run: list[str] | None = None, cover: bool = False, failfast: bool = False,
                   compile_only: bool = False, timeout: int | None = None) -> LangTestResult:
        assert not (cover and failfast), "failfast is never combined with cover"
        t = int(timeout if timeout is not None else self.timeout)
        unit = self.adapter.unit(unit_rel)
        if unit is None:
            return LangTestResult(build_ok=False, build_errors=[f"no Java unit {unit_rel!r}"])
        prep = await self.prepare(unit)
        if not prep.ok:
            return LangTestResult(build_ok=False, build_errors=list(prep.errors)
                                  or ["module preparation failed"])
        overlay = dict(overlay or {})
        use_mvn = self.mode == "mvn" and prep.mode == "maven" and not compile_only and (
            cover or (not overlay and run is None))
        if use_mvn:
            return await self._test_mvn(unit, prep, overlay, run, cover, failfast, t)
        return await self._test_fast(unit, prep, overlay, run, cover, failfast, compile_only, t)

    async def _test_fast(self, unit: Any, prep: ModulePrep, overlay: dict[str, str | None],
                         run: list[str] | None, cover: bool, failfast: bool,
                         compile_only: bool, t: int) -> LangTestResult:
        lay = self.adapter.layout()
        mod = self._module(unit)
        call = self._call_dir()
        try:
            for d in ("classes", "gen", "tmp", "src"):
                (call / d).mkdir(parents=True, exist_ok=True)
            mapper = self._mapper(call)
            written: dict[str, Path] = {}
            hidden: set[str] = set()
            for rel, content in overlay.items():
                if content is None:
                    hidden.add(rel)
                elif rel.endswith(".java"):
                    written[rel] = self._write_overlay(call, rel, content, prep)
            main_ov = [r for r in written if _under(r, mod.source_root)
                       and not (mod.mode == "plain" and self.adapter.is_test_file(
                           r, overlay[r]))]
            test_ov = [r for r in written if r not in main_ov]
            files, classes = self._selection(unit, overlay)
            if compile_only:
                selectors: list[str] = []
            elif run is None:
                selectors = [f"c:{c}" for c in classes]
            else:
                selectors = [f"m:{n}" for n in dict.fromkeys(run)
                             if "#" in n and n.split("#", 1)[0].split("$", 1)[0] in classes]
            compile_set = [written[r] for r in main_ov + test_ov]
            # the original classes of an overlaid file must not stay on the classpath: a type
            # the regenerated file leaves out would silently come from target/classes
            stale_main, stale_test, _ = self._stale_classes(main_ov, test_ov, overlay)
            for r in main_ov:
                if not self.adapter.top_level_names(overlay[r] or "") and \
                        self.adapter.top_level_names(self._read(r)):
                    return LangTestResult(build_ok=False, build_errors=[
                        f"{r}:1: the file declares no type (the original declares "
                        f"{', '.join(self.adapter.top_level_names(self._read(r)))})"])
            if main_ov:
                declared: set[str] = set()
                dirs = {_parent(r) for r in main_ov}
                for r in main_ov:
                    for text in (overlay[r] or "", self._read(r)):
                        declared.update(d.name.rsplit(".", 1)[-1]
                                        for d in self.adapter.decls(text) if d.kind == "type")
                for r, role in sorted(lay.file_role.items()):
                    if role != "main" or lay.file_module.get(r) != mod.rel or r in written \
                            or r in hidden:
                        continue
                    if _parent(r) in dirs or declared & self.adapter.identifiers(self._read(r)):
                        compile_set.append(self._exec_path(r, prep.mode))
                if selectors:
                    run_classes = {s[2:].split("#", 1)[0].split("$", 1)[0] for s in selectors}
                    for fq in sorted(run_classes):
                        f = classes.get(fq)
                        if f is not None and f not in written:
                            compile_set.append(self._exec_path(f, prep.mode))
            compile_set = list(dict.fromkeys(compile_set))
            test_cp = prep.test_classes
            hidden_fq = {fq for f in hidden if f.endswith(".java")
                         for fq in self.adapter.test_classes(self._read(f))}
            added_fq = {fq for r in test_ov for fq in self.adapter.test_classes(overlay[r] or "")}
            drop_test = (hidden_fq & added_fq) | stale_test
            if drop_test and prep.test_classes is not None:
                test_cp = call / "test-classes"
                _copy_classes(prep.test_classes, test_cp, drop_test)
            main_cp = prep.classes
            if stale_main and prep.classes is not None:
                main_cp = call / "main-classes"
                _copy_classes(prep.classes, main_cp, stale_main)
            cp = [str(x) for x in (test_cp, main_cp) if x is not None] + prep.cp + \
                [str(prep.jars["standalone"])]
            if compile_set:
                args = ["-d", str(call / "classes"), "-s", str(call / "gen"), "-encoding",
                        prep.encoding, "-nowarn", "-g", *_level_flags(prep)]
                if self._feature() >= 21:
                    args.append("-proc:full")
                if prep.processor_path:
                    args += ["-processorpath", os.pathsep.join(prep.processor_path)]
                args += [*prep.compiler_args, "-cp", os.pathsep.join(cp),
                         *[str(p) for p in compile_set]]
                argfile = write_argfile(call / "javac.args", args)
                p = await self._exec([self._java("javac"), f"@{argfile}"], cwd=call,
                                     env=self._env(call / "tmp"), timeout=max(self.timeout, 60))
                if p.returncode != 0 or p.timed_out:
                    errs = parse_javac(p.stdout + "\n" + p.stderr, mapper)
                    if p.timed_out:
                        errs = ["javac timed out"] + errs
                    return LangTestResult(build_ok=False, build_errors=errs,
                                          timed_out=p.timed_out, returncode=p.returncode,
                                          tail=_cap_tail(mapper.text(p.stdout + p.stderr),
                                                         TAIL_CAP))
            if compile_only or not selectors:
                return LangTestResult(build_ok=True)
            requested = [s[2:] for s in selectors] if run is not None else None
            res, _, proc = await self._launch(unit, prep, call, selectors, cover=cover,
                                              failfast=failfast, t=t, cp=cp,
                                              requested=requested)
            if cover and res.build_ok:
                res.coverage, res.coverage_error = await self._coverage(
                    unit, prep, call, proc.stderr if proc is not None else "", main_cp)
            return res
        finally:
            shutil.rmtree(call, ignore_errors=True)

    def _stale_classes(self, main_ov: list[str], test_ov: list[str],
                       overlay: dict[str, str | None]) -> tuple[set[str], set[str], bool]:
        """(main, test) binary class names the original versions of overlaid files declare
        that the overlays do not (their nested classes go with them), and whether any."""
        from codocs.lang.java import package_of

        def binary(text: str) -> set[str]:
            pkg = package_of(text)
            return {(f"{pkg}." if pkg else "") + d.name.replace(".", "$")
                    for d in self.adapter.decls(text) if d.kind == "type"}

        out: list[set[str]] = [set(), set()]
        for i, rels in enumerate((main_ov, test_ov)):
            for r in rels:
                if not (self.module_root / r).is_file():
                    continue
                old = binary(self._read(r))
                out[i] |= old - binary(overlay[r] or "")
        return out[0], out[1], bool(out[0] or out[1])

    async def _launch(self, unit: Any, prep: ModulePrep, call: Path, selectors: list[str], *,
                      cover: bool, failfast: bool, t: int, cp: list[str] | None = None,
                      requested: list[str] | None = None, extra: list[str] | None = None,
                      use_call: bool = True) -> tuple[LangTestResult, list[dict], _Proc | None]:
        """run CodocsJUnitRunner once; (result, raw events, process)."""
        (call / "tmp").mkdir(parents=True, exist_ok=True)
        if cp is None:
            cp = [str(x) for x in (prep.test_classes, prep.classes) if x is not None] + \
                prep.cp + [str(prep.jars["standalone"])]
        events_path = call / "events.jsonl"
        select = call / "select.txt"
        select.write_text("\n".join(selectors) + "\n", encoding="utf-8")
        jvm = list(prep.argline)
        classpath = ([str(call / "classes")] if use_call else []) + cp + \
            [str(prep.runner_classes)]
        jvm += ["-cp", os.pathsep.join(classpath)]
        if cover and not prep.coverage_error:
            pkg = unit.import_path
            inc = f",includes={pkg}.*" if pkg else ""
            jvm.append(f"-javaagent:{prep.jars['agent']}=destfile={call / 'jacoco.exec'}"
                       f"{inc},output=file")
        jvm.append(f"-Djava.io.tmpdir={call / 'tmp'}")
        jvm += [f"-D{k}={v}" for k, v in prep.sysprops.items()]
        jvm += [RUNNER_CLASS, "--events", str(events_path), "--timeout", str(t),
                "--select-file", str(select), *(extra or [])]
        if failfast:
            jvm.append("--failfast")
        argfile = write_argfile(call / "java.args", jvm)
        cwd = prep.workdir or prep.exec_module or call
        cwd.mkdir(parents=True, exist_ok=True)
        p = await self._exec([self._java("java"), f"@{argfile}"], cwd=cwd,
                             env=self._env(call / "tmp", prep.envvars),
                             timeout=t + OUTER_SLACK)
        events = _read_events(events_path)
        mapper = self._mapper(call)
        tail = _cap_tail(mapper.text(p.stdout + p.stderr), TAIL_CAP)
        if p.overflow:
            tail = _cap_tail(tail + "\ntest output exceeded the output limit; the run was "
                             "killed\n", TAIL_CAP)
        res = fold_events(events, requested=requested,
                          died=p.returncode not in (0, 1), outer_timeout=p.timed_out
                          or p.overflow, tail=tail)
        if res.build_ok and not events and p.returncode not in (0, 1):
            lines = [ln.strip() for ln in (p.stderr or p.stdout).splitlines() if ln.strip()]
            res = LangTestResult(build_ok=False, build_errors=[
                mapper.text(ln) for ln in lines[:5]] or [f"java exited with {p.returncode}"],
                tail=tail)
        res.returncode = p.returncode
        return res, events, p

    async def _coverage(self, unit: Any, prep: ModulePrep, call: Path, stderr: str,
                        classes: Path | None = None) -> tuple[list[CoverBlock] | None, str]:
        if prep.coverage_error:
            return None, prep.coverage_error
        if "IllegalClassFormatException" in stderr:
            return None, (f"JaCoCo {prep.jacoco} cannot instrument class-file release "
                          f"{prep.release}")
        exec_file = call / "jacoco.exec"
        if not exec_file.is_file() or exec_file.stat().st_size == 0:
            return None, "JaCoCo wrote no execution data"
        pkg_path = unit.import_path.replace(".", "/")
        cov = call / "covclasses"
        dst = cov / pkg_path if pkg_path else cov
        dst.mkdir(parents=True, exist_ok=True)
        for base in (call / "classes", classes or prep.classes):
            if base is None:
                continue
            d = base / pkg_path if pkg_path else base
            with contextlib.suppress(OSError):
                for f in d.iterdir():
                    if f.suffix == ".class" and not (dst / f.name).exists():
                        shutil.copy2(f, dst / f.name)
        xml_path = call / "cov.xml"
        p = await self._exec([self._java("java"), "-jar", str(prep.jars["cli"]), "report",
                              str(exec_file), "--classfiles", str(cov), "--xml", str(xml_path),
                              "--quiet"], cwd=call, env=self._env(call / "tmp"),
                             timeout=max(self.timeout, 60))
        if p.returncode != 0 or not xml_path.is_file():
            msg = " ".join((p.stderr or p.stdout).strip().splitlines()[-2:])
            return None, f"JaCoCo report failed: {msg[:300]}"
        files = {f"{pkg_path}/{Path(f).name}" if pkg_path else Path(f).name: f
                 for f in unit.files}
        try:
            return jacoco_blocks(xml_path.read_text(encoding="utf-8"), files), ""
        except (OSError, ET.ParseError) as e:
            return None, f"JaCoCo report unreadable: {e}"

    # -- mvn path ----------------------------------------------------------------------

    async def _test_mvn(self, unit: Any, prep: ModulePrep, overlay: dict[str, str | None],
                        run: list[str] | None, cover: bool, failfast: bool, t: int
                        ) -> LangTestResult:
        mod = self._module(unit)
        call = self._call_dir()
        try:
            reactor_src = self.exec_root / mod.reactor if mod.reactor else self.exec_root
            farm = call / "farm"
            ov_abs: dict[str, str | None] = {}
            for rel, content in overlay.items():
                if mod.reactor and not (rel == mod.reactor or rel.startswith(mod.reactor + "/")):
                    continue
                ov_abs[rel[len(mod.reactor) + 1:] if mod.reactor else rel] = content
            await asyncio.to_thread(_symlink_farm, reactor_src, farm, ov_abs,
                                    lambda full: self._codec(full, prep), mod.reactor,
                                    self._keep_dirs(mod.reactor))
            files, classes = self._selection(unit, overlay)
            if run is None:
                tests = sorted(classes)
            else:
                by_cls: dict[str, list[str]] = {}
                for n in dict.fromkeys(run):
                    if "#" in n and n.split("#", 1)[0].split("$", 1)[0] in classes:
                        c, m = n.split("#", 1)
                        by_cls.setdefault(c.split("$", 1)[0], []).append(m)
                tests = [f"{c}#{'+'.join(ms)}" for c, ms in sorted(by_cls.items())]
            if not tests:
                return LangTestResult(build_ok=True)
            args = ["-q", "-B", "-o", "-Dmaven.test.failure.ignore=true", "-DfailIfNoTests=false",
                    "-Dsurefire.failIfNoSpecifiedTests=false", f"-Dtest={','.join(tests)}",
                    f"-Dsurefire.timeout={t}"]
            if not prep.explicit_release:
                args.append(f"-Dmaven.compiler.release={prep.release}")
            if failfast:
                args.append("-Dsurefire.skipAfterFailureCount=1")
            jac = f"org.jacoco:jacoco-maven-plugin:{prep.jacoco}"
            use_cov = cover and not prep.coverage_error
            goals = ([f"{jac}:prepare-agent"] if use_cov else []) + ["test"] + \
                ([f"{jac}:report"] if use_cov else [])
            pl = []
            if mod.rel != mod.reactor:
                pl = ["-pl", mod.rel[len(mod.reactor) + 1:] if mod.reactor else mod.rel, "-am"]
            p = await self._exec([self.mvn, *args, *pl, *goals], cwd=farm,
                                 env=self._env(call / "tmp"), timeout=t + MVN_SLACK)
            if p.returncode != 0 and not p.timed_out:
                p = await self._exec([self.mvn, *[a for a in args if a != "-o"], *pl, *goals],
                                     cwd=farm, env=self._env(call / "tmp"),
                                     timeout=t + MVN_SLACK)
            module_farm = farm / (mod.rel[len(mod.reactor) + 1:] if mod.reactor else mod.rel) \
                if mod.rel != mod.reactor else farm
            mapper = PathMap({str(farm): mod.reactor, str(self.exec_root): "",
                              str(self.module_root): ""})
            tail = _cap_tail(mapper.text(p.stdout + p.stderr), TAIL_CAP)
            reports = module_farm / "target" / "surefire-reports"
            verdicts = parse_surefire(reports, set(classes)) if reports.is_dir() else {}
            if not verdicts and p.returncode != 0:
                return LangTestResult(build_ok=False, build_errors=parse_mvn_errors(
                    p.stdout + "\n" + p.stderr, mapper), timed_out=p.timed_out,
                    returncode=p.returncode, tail=tail)
            res = LangTestResult(build_ok=True, timed_out=p.timed_out, returncode=p.returncode,
                                 tail=tail)
            containers = {k[len("container:"):]: v for k, v in verdicts.items()
                          if k.startswith("container:")}
            for tid, vs in verdicts.items():
                if tid.startswith("container:"):
                    continue
                vals = [v for v, _ in vs]
                verdict = "fail" if "fail" in vals else ("skip" if all(v == "skip" for v in vals)
                                                         else "pass")
                res.tests[tid] = verdict
                res.output[tid] = _cap_tail("\n".join(x for _, x in vs if x), OUTPUT_CAP)
            for cls, vs in containers.items():
                if any(v == "fail" for v, _ in vs):
                    text = "\n".join(x for _, x in vs if x)
                    for name in run or []:
                        if name.split("#", 1)[0] == cls or name.startswith(cls + "$"):
                            res.tests[name] = "fail"
                            res.output[name] = _cap_tail(text, OUTPUT_CAP)
            if p.timed_out or not reports.is_dir():
                for name in run or []:
                    res.tests.setdefault(name, "interrupted")
                    res.output.setdefault(name, "")
            if cover:
                if prep.coverage_error:
                    res.coverage_error = prep.coverage_error
                else:
                    xml_path = module_farm / "target" / "site" / "jacoco" / "jacoco.xml"
                    pkg_path = unit.import_path.replace(".", "/")
                    files_map = {f"{pkg_path}/{Path(f).name}" if pkg_path else Path(f).name: f
                                 for f in unit.files}
                    try:
                        res.coverage = jacoco_blocks(xml_path.read_text(encoding="utf-8"),
                                                     files_map)
                    except (OSError, ET.ParseError) as e:
                        res.coverage_error = f"JaCoCo report unavailable: {e}"
            return res
        finally:
            shutil.rmtree(call, ignore_errors=True)


# -- file helpers -----------------------------------------------------------------------


def _under(rel: str, root: str) -> bool:
    return not root or rel == root or rel.startswith(root + "/")


def _parent(rel: str) -> str:
    p = os.path.dirname(rel)
    return "" if p == "." else p


def _level_flags(prep: ModulePrep) -> list[str]:
    """javac's language level as Maven's compiler plugin passes it: --release N when the pom
    sets release, else -source N -target N (compiled against the running JDK's library)."""
    if prep.source_target:
        return ["-source", str(prep.release), "-target", str(prep.release), "-Xlint:-options"]
    return ["--release", str(prep.release)]


def _java_codec(name: str) -> str:
    """the Python codec of a Java/Maven encoding name (utf-8 when unknown)."""
    import codecs

    for cand in (name, name.replace("_", "-"), {"windows-31j": "cp932", "ms932": "cp932"}.get(
            name.lower(), "")):
        if not cand:
            continue
        with contextlib.suppress(LookupError):
            return codecs.lookup(cand).name
    return "utf-8"


def encode_java(text: str, codec: str) -> bytes:
    """Java source in the given codec; characters it cannot hold become \\uXXXX escapes (valid
    anywhere in Java source, UTF-16 code units for characters beyond the BMP)."""
    try:
        return text.encode(codec, errors="surrogateescape")
    except (UnicodeEncodeError, LookupError):
        pass
    try:
        "".encode(codec)
    except LookupError:
        codec = "utf-8"
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode(codec, errors="surrogateescape")
        except UnicodeEncodeError:
            units = ch.encode("utf-16-be")
            for i in range(0, len(units), 2):
                out += f"\\u{units[i] << 8 | units[i + 1]:04x}".encode("ascii")
    return bytes(out)


def _safe_compiler_args(args: list[str]) -> list[str]:
    """compilerArgs that do not fight the fast path's own options."""
    out: list[str] = []
    skip_next = False
    with_value = {"-d", "-s", "-cp", "-classpath", "--class-path", "--release", "-source",
                  "-target", "-processorpath", "--processor-path", "-encoding", "-sourcepath"}
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a in with_value:
            skip_next = True
            continue
        if a.startswith(("-proc:", "-Werror", "--release=", "-implicit")):
            continue
        out.append(a)
    return out


def _read_events(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        with contextlib.suppress(ValueError):
            ev = json.loads(line)
            if isinstance(ev, dict):
                out.append(ev)
    return out


def _copy_classes(src: Path, dst: Path, drop_fqcns: set[str]) -> None:
    """hardlinked copy of a class dir without the given classes (and their nested ones)."""
    drop = {fq.replace(".", "/") for fq in drop_fqcns}
    for dirpath, _, filenames in os.walk(src):
        d = Path(dirpath)
        out = dst / d.relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            rel = (d / name).relative_to(src).as_posix()
            stem = rel[:-6] if rel.endswith(".class") else rel
            if stem in drop or any(stem.startswith(x + "$") for x in drop):
                continue
            try:
                os.link(d / name, out / name)
            except OSError:
                shutil.copy2(d / name, out / name)


def _mirror_tree(src: Path, dst: Path, exclude: set[Path], keep_dirs: set[str] | None = None
                 ) -> None:
    """copy src into dst (size/mtime check); skips MIRROR_SKIP dirs (not the package dirs in
    keep_dirs, relative to src); never deletes target/."""
    dst.mkdir(parents=True, exist_ok=True)
    excl = {p.resolve() for p in exclude}
    for dirpath, dirnames, filenames in os.walk(src):
        d = Path(dirpath)
        out = dst / d.relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        here = d.relative_to(src).as_posix()
        here = "" if here == "." else here
        keep = []
        for name in dirnames:
            p = d / name
            if _skipped(name, f"{here}/{name}" if here else name, keep_dirs) or \
                    p.resolve() in excl or (p / "pyvenv.cfg").exists():
                continue
            keep.append(name)
        dirnames[:] = keep
        wanted = set(keep) | set(filenames)
        for name in filenames:
            p, q = d / name, out / name
            try:
                st = p.stat()
                if q.exists():
                    qs = q.stat()
                    if qs.st_size == st.st_size and qs.st_mtime_ns == st.st_mtime_ns:
                        continue
                shutil.copy2(p, q)
            except OSError:
                continue
        with contextlib.suppress(OSError):
            for extra in os.listdir(out):
                if extra in wanted or extra in MIRROR_SKIP:
                    continue
                q = out / extra
                if q.is_dir() and not q.is_symlink():
                    shutil.rmtree(q, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        q.unlink()


def _skipped(name: str, rel: str, keep: set[str] | None) -> bool:
    """MIRROR_SKIP by name, except a source package dir (com/acme/build) the layout knows."""
    if name.startswith("."):
        return True
    if name not in MIRROR_SKIP:
        return False
    return not (name in SOFT_SKIP and keep is not None and rel in keep)


def _symlink_farm(src: Path, dst: Path, overlay: dict[str, str | None], codec_of: Any,
                  base_rel: str, keep: set[str] | None = None) -> None:
    """real dirs, symlinked files, overlay written as real files, hidden files absent."""
    for dirpath, dirnames, filenames in os.walk(src):
        d = Path(dirpath)
        out = dst / d.relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        here = d.relative_to(src).as_posix()
        here = "" if here == "." else here
        dirnames[:] = [n for n in dirnames
                       if not _skipped(n, f"{here}/{n}" if here else n, keep)]
        for name in filenames:
            rel = (d / name).relative_to(src).as_posix()
            if rel in overlay:
                continue
            os.symlink(d / name, out / name)
    for rel, content in overlay.items():
        if content is None:
            continue
        q = dst / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        full = f"{base_rel}/{rel}" if base_rel else rel
        codec = "utf-8"
        if codec_of is not None:
            with contextlib.suppress(Exception):
                codec = codec_of(full)
        q.write_bytes(encode_java(content, codec) if rel.endswith(".java") else
                      content.encode(codec, errors="replace"))


__all__ = ["JavaRunner", "select_jdk", "fold_events", "parse_javac", "parse_mvn_errors",
           "parse_surefire", "jacoco_blocks", "parse_effective_pom", "expand_argline",
           "ModulePrep", "PathMap", "JACOCO_TABLE", "OUTER_SLACK"]
