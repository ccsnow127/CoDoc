"""the bizlogic wiki re-rendered from stored json (no llm): checks on a synthetic run with a
scripted llm and the 3gpp-5gc example profile, the bizlogic-render cli, and a headless-chrome
render check of the exported html (CODOCS_CHROME; skips without chrome or
~/.repowiki/mermaid.min.js)."""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from click.testing import CliRunner

from codocs.bizlogic import actors as A
from codocs.bizlogic import diagrams as G
from codocs.bizlogic import domain as D
from codocs.bizlogic.domain import SELF
from codocs.bizlogic.profile import load_profile
from codocs.bizlogic.run import BizParams, load_results, render_from_json, run_bizlogic
from codocs.config import Settings

CHROME = Path(os.environ.get("CODOCS_CHROME")
              or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
BUNDLE = Path.home() / ".repowiki" / "mermaid.min.js"
MAX_WIDTH = 4000            # natural svg width (px) above which a diagram is unreadable
WARN_WIDTH = 800            # the content column

# ---- helpers -------------------------------------------------------------------------------

_MERMAID = re.compile(r'<div class="mermaid">(.*?)</div>', re.S)


def html_diagrams(path: Path) -> list[tuple[str, str]]:
    """(page id, mermaid source) of every diagram in an exported wiki html."""
    text = Path(path).read_text(encoding="utf-8")
    start = text.index('<div class="content" id="content">')
    body = text[start:text.index("<script>", start)]
    out = []
    for chunk in body.split('<div id="page-')[1:]:
        pid = chunk.split('"', 1)[0]
        out += [(pid, html.unescape(src)) for src in _MERMAID.findall(chunk)]
    return out


def md_diagrams(text: str) -> list[str]:
    return re.findall(r"^```mermaid\n(.*?)\n```$", text, re.S | re.M)


def _chrome(page: str, budget: int = 120000, timeout: int = 900) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "page.html"
        f.write_text(page, encoding="utf-8")
        return subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--no-sandbox",
                               f"--user-data-dir={tmp}/prof", f"--virtual-time-budget={budget}",
                               "--window-size=1400,1000", "--dump-dom", f.as_uri()],
                              capture_output=True, timeout=timeout).stdout.decode()


def _render_chunk(srcs: list[str]) -> list[dict]:
    data = json.dumps(srcs).replace("<", "\\u003c")
    page = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            "<pre id='results'>PENDING</pre><script>" + BUNDLE.read_text() + "</script><script>"
            "const S=" + data + ";"
            "mermaid.initialize({startOnLoad:false,theme:'default'});"
            "(async()=>{const out=[];for(let i=0;i<S.length;i++){"
            "try{await mermaid.parse(S[i]);const r=await mermaid.render('d'+i,S[i]);"
            "const bad=/Syntax error in text|Maximum text size/.test(r.svg);"
            "const vb=(r.svg.match(/viewBox=\"([^\"]+)\"/)||[0,'0 0 0 0'])[1].split(/[ ,]+/);"
            "out.push({ok:!bad,err:bad?'error svg':'',w:Math.round(+vb[2]),h:Math.round(+vb[3])});"
            "}catch(e){out.push({ok:false,err:String(e&&e.message||e).slice(0,300),w:0,h:0});}}"
            "document.getElementById('results').textContent=JSON.stringify(out);})();"
            "</script></body></html>")
    dom = _chrome(page)
    m = re.search(r'<pre id="results">(.*?)</pre>', dom, re.S)
    raw = html.unescape(m.group(1)) if m else ""
    if not raw.startswith("["):
        raise RuntimeError(f"no render results: {raw[:200]!r}")
    return json.loads(raw)


def render_sources(srcs: list[str], chunk: int = 20, workers: int = 6) -> list[dict]:
    """{ok, err, w, h} per mermaid source, rendered by the bundled mermaid in chrome."""
    parts = [srcs[i:i + chunk] for i in range(0, len(srcs), chunk)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [r for rs in pool.map(_render_chunk, parts) for r in rs]


# rendered into the page (so that layout can be measured): lane titles taller than their
# header band, sequence text with pieces of mermaid's entity placeholders (a wrap inside
# one), the left edge of every subgraph, and edge labels drawn on top of each other
_MEASURE = r"""
function box(e) { return e.getBoundingClientRect(); }
function inter(a, b) {
  const x = Math.min(a.right, b.right) - Math.max(a.left, b.left);
  const y = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
  return (x > 2 && y > 2) ? Math.round(x * y) : 0;
}
function measure(s) {
  const r = {lanes: [], junk: [], clusters: {}, labels: []};
  s.querySelectorAll('g.cluster.swimlane, g.swimlane').forEach(g => {
    const tr = g.querySelector('rect.swimlane-title');
    const fo = g.querySelector('.swimlane-label foreignObject');
    if (tr && fo && box(fo).height > box(tr).height + 2)
      r.lanes.push([(fo.textContent || '').trim(), Math.round(box(tr).height),
                    Math.round(box(fo).height)]);
  });
  if ((s.getAttribute('aria-roledescription') || '') === 'sequence')
    s.querySelectorAll('text').forEach(t => {
      if (/[ﬂ°¶ß]|#\d/.test(t.textContent)) r.junk.push(t.textContent);
    });
  const sb = box(s);
  s.querySelectorAll('g.cluster').forEach(g => {
    const id = (g.getAttribute('id') || '').replace(/^.*?-?(p\d+|p_none)$/, '$1');
    if (/^p(\d+|_none)$/.test(id)) r.clusters[id] = Math.round(box(g).left - sb.left);
  });
  const labs = [...s.querySelectorAll('g.edgeLabel')]
    .map(g => [(g.textContent || '').trim(), box(g.querySelector('foreignObject div, text') || g)])
    .filter(x => x[0] && x[1].width > 0);
  for (let i = 0; i < labs.length; i++) for (let j = i + 1; j < labs.length; j++)
    if (inter(labs[i][1], labs[j][1]) > 20) r.labels.push([labs[i][0], labs[j][0]]);
  return r;
}
"""


def _measure_chunk(srcs: list[str]) -> list[dict]:
    data = json.dumps(srcs).replace("<", "\\u003c")
    page = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            "<pre id='results'>PENDING</pre><div id='stage'></div><script>"
            + BUNDLE.read_text() + "</script><script>" + _MEASURE + "const S=" + data + ";"
            "mermaid.initialize({startOnLoad:false,theme:'default'});"
            "(async()=>{const out=[];const st=document.getElementById('stage');"
            "for(let i=0;i<S.length;i++){try{const r=await mermaid.render('m'+i,S[i]);"
            "const div=document.createElement('div');div.innerHTML=r.svg;st.appendChild(div);"
            "const s=div.querySelector('svg');"
            "const bad=/Syntax error in text|Maximum text size/.test(r.svg);"
            "out.push(Object.assign({ok:!bad,err:bad?'error svg':''},measure(s)));"
            "st.removeChild(div);"
            "}catch(e){out.push({ok:false,err:String(e&&e.message||e).slice(0,300),lanes:[],"
            "junk:[],clusters:{},labels:[]});}}"
            "document.getElementById('results').textContent=JSON.stringify(out);})();"
            "</script></body></html>")
    dom = _chrome(page)
    m = re.search(r'<pre id="results">(.*?)</pre>', dom, re.S)
    raw = html.unescape(m.group(1)) if m else ""
    if not raw.startswith("["):
        raise RuntimeError(f"no measure results: {raw[:200]!r}")
    return json.loads(raw)


def measure_sources(srcs: list[str], chunk: int = 15, workers: int = 6) -> list[dict]:
    """{ok, err, lanes, junk, clusters, labels} per mermaid source (see _MEASURE)."""
    parts = [srcs[i:i + chunk] for i in range(0, len(srcs), chunk)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [r for rs in pool.map(_measure_chunk, parts) for r in rs]


PROBE = """<script>
(async () => {
  const r = {showPage: typeof showPage, first: null, nav: [], diagrams: 0, errors: [],
             loadErrors: (window.__errs || []).slice(),
             active: [...document.querySelectorAll('.nav-item.active')]
               .map(a => (a.getAttribute('onclick').match(/showPage\\('([^']+)'\\)/) || [])[1])};
  const vis = [...document.querySelectorAll('.wiki-page')].filter(p => p.style.display === 'block');
  r.first = vis.length === 1 ? vis[0].id.slice(5) : vis.map(p => p.id).join(',');
  const items = [...document.querySelectorAll('.sidebar .nav-item')];
  for (const a of items.filter((_, i) => i < 3 || i % 25 === 0)) {
    a.click();
    const shown = [...document.querySelectorAll('.wiki-page')].filter(p => p.style.display === 'block');
    const want = (a.getAttribute('onclick').match(/showPage\\('([^']+)'\\)/) || [])[1];
    r.nav.push([want, shown.length === 1 && shown[0].id === 'page-' + want,
                a.classList.contains('active')]);
  }
  const links = [...document.querySelectorAll('.wiki-page a[onclick]')];
  r.links = links.length;
  if (links.length) {                  // a link inside a page keeps the sidebar highlight
    links[0].click();
    const want = (links[0].getAttribute('onclick').match(/showPage\\('([^']+)'\\)/) || [])[1];
    r.linkActive = [...document.querySelectorAll('.nav-item.active')].some(
      a => (a.getAttribute('onclick') || '').includes("showPage('" + want + "')"));
    r.linkMarked = links[0].classList.contains('active');
  }
  r.dangling = links.map(a => (a.getAttribute('onclick').match(/showPage\\('([^']+)'\\)/) || [])[1])
    .filter(id => id && !document.getElementById('page-' + id)).slice(0, 20);
  document.querySelectorAll('.wiki-page').forEach(p => p.style.display = 'block');
  try { await mermaid.run({querySelector: '.mermaid', suppressErrors: true}); }
  catch (e) { r.runError = String(e); }
  await new Promise(res => setTimeout(res, 2000));
  document.querySelectorAll('.mermaid').forEach(d => {
    r.diagrams++;
    const s = d.querySelector('svg');
    const page = (d.closest('.wiki-page') || {id: '?'}).id.slice(5);
    if (!s) { r.errors.push([page, 'no svg']); return; }
    if (/Syntax error in text|Maximum text size/.test(s.textContent)
        || (s.getAttribute('aria-roledescription') || '') === 'error') {
      r.errors.push([page, 'error svg']);
    }
  });
  const pre = document.createElement('pre'); pre.id = 'probe';
  pre.textContent = JSON.stringify(r); document.body.appendChild(pre);
})();
</script>"""


def probe_html(path: Path) -> dict:
    """load an exported wiki html as is, click some sidebar entries and a page link, then
    show every page and render every diagram: {showPage, first, nav, links, dangling,
    diagrams, errors, loadErrors (uncaught on load), active (highlighted on load),
    linkActive, linkMarked}."""
    text = Path(path).read_text(encoding="utf-8")
    catch = ("<script>window.__errs=[];window.addEventListener('error',"
             "e=>window.__errs.push(String(e.message)));</script>")
    text = text.replace("<head>", "<head>" + catch, 1)
    i = text.rfind("</body>")                 # the mermaid bundle itself contains </body>
    dom = _chrome(text[:i] + PROBE + text[i:], budget=600000, timeout=1800)
    m = re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S)
    if not m:
        raise RuntimeError("probe did not finish")
    return json.loads(html.unescape(m.group(1)))


_DECL = re.compile(r"^\s*([a-z][a-z0-9_]*)(?:\[|\(\[|@\{|\[\(|\[\[)", re.M)
_ASSOC = re.compile(r"^\s*[a-z][a-z0-9_]* (?:---|-\.-|-->|-\.->)", re.M)


def caps(src: str) -> list[str]:
    """node caps of the diagram rules that a diagram source breaks."""
    errs = []
    decl = _DECL.findall(src)
    actors = [x for x in decl if x.startswith("a_") and x != "a_other"]
    if src.splitlines()[1:2] == ["sequenceDiagram"]:
        n = len(re.findall(r"^\s*(participant|actor) ", src, re.M))
        if n > G.MAX_SEQ_LIFELINES:
            errs.append(f"{n} lifelines")
        if re.search(r"^\s*(participant|actor) c\d+ ", src, re.M):
            errs.append("cluster lifeline")
    elif re.search(r"^\s*subgraph sys \[", src, re.M):                       # D1
        if len(actors) > G.MAX_D1_EXTERNALS:
            errs.append(f"D1: {len(actors)} externals")
    elif any(x.startswith("g_") for x in decl):                             # D2
        if len([x for x in decl if x.startswith("g_") and x != "g_more"]) > G.MAX_UC:
            errs.append("D2: too many ovals")
        if len(actors) > G.MAX_ACTORS:
            errs.append(f"D2: {len(actors)} actors")
    elif any(x.startswith("u_") for x in decl):                             # D7 / D9
        if len([x for x in decl if x.startswith("u_") and x != "u_more"]) > G.MAX_UC:
            errs.append("D7: too many ovals")
        if len(actors) > G.MAX_ACTORS + 1:              # D9 may add the timer
            errs.append(f"D7: {len(actors)} actors")
        if len(_ASSOC.findall(src)) > G.MAX_ASSOC:
            errs.append("D7: too many associations")
    elif "classDef focus" in src:                                           # D13
        if len([x for x in decl if re.match(r"c\d+$", x)]) > 2 * G.MAX_EGO + 1:
            errs.append("D13: too many neighbours")
    elif re.search(r'-->\|"\d+( / \d+)?"\|', src):                          # D4 / D8
        n = len(re.findall(r'-->\|"\d+(?: / \d+)?"\|', src))
        if n > G.MAX_EDGES:
            errs.append(f"call map: {n} edges")
    return errs


# ---- a synthetic run, re-rendered ---------------------------------------------------------

CLUSTER_REPLY = {"name": "Registration", "summary": "Handles UE registration.",
                 "kind": "business"}


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """the synthetic module of test_bizlogic run with a scripted llm and the built-in
    3gpp-5gc profile, then re-rendered from json: {k: (kdir, html path)}."""
    import test_bizlogic as B

    from codocs import llm as llm_mod

    async def fake(self, system, user, *, tag="", temperature=None, max_tokens=None,
                   validate=None):
        return json.dumps(CLUSTER_REPLY if "cluster" in tag or "subsystem" in tag
                          else B.NEW_REPLY)

    tmp = tmp_path_factory.mktemp("biz")
    saved, llm_mod.LLM.complete = llm_mod.LLM.complete, fake
    try:
        asyncio.run(run_bizlogic(B._module(tmp / "m"), tmp / "out",
                                 Settings(cache_path=str(tmp / "cache.db")),
                                 BizParams(clusters=(2, 3), utility_fanin=4, min_reach=1,
                                           walks_per_root=8, walk_length=6),
                                 profile=load_profile("3gpp-5gc")))
    finally:
        llm_mod.LLM.complete = saved
    base = tmp / "out" / "bizlogic"
    ks = sorted(int(p.name[1:]) for p in base.glob("k*") if p.name[1:].isdigit())
    assert ks and (base / "domain_profile.json").is_file()
    return {k: (base / f"k{k}", render_from_json(base / f"k{k}", k)) for k in ks}


@pytest.fixture
def gpp():
    prev = D.use_profile(load_profile("3gpp-5gc"))
    yield
    D.use_profile(prev)


def _model(kdir: Path) -> G.Model:
    r = load_results(kdir)
    return G.build_model(r.k, r.clusters, r.parents, r.parent_infos, r.trajs, r.logic, "demo")


def _pages(kdir: Path, k: int) -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8")
            for p in (kdir / "wiki").glob(f"bizlogic-k{k}*.md")}


def test_rendered_pages_layout(rendered):
    for k, (kdir, path) in rendered.items():
        pages = _pages(kdir, k)
        logic = {pid: t for pid, t in pages.items() if re.match(rf"bizlogic-k{k}-T\d+$", pid)}
        assert logic and all(f"bizlogic-k{k}-p" in t for t in logic.values())
        for pid, text in logic.items():
            assert "Evidence:" not in text and "## Where in the code" in text, pid
            assert "Cluster flow" not in text and "Entry roots:" not in text, pid
            assert len(text.encode()) < 12_000, (pid, len(text.encode()))
            where = text.split("## Where in the code", 1)[1].strip().splitlines()
            assert 2 <= len(where) <= 3 and where[-1].startswith("- Based on "), pid
            # activity lanes are deployables the page says the logic runs in
            runs = re.search(r"^\| \*\*Runs in\*\* \| (.+) \|$", text, re.M).group(1).split(" + ")
            lanes = set(re.findall(r'subgraph l_(?!src)\w+ \["([^"]*)"\]', text))
            assert lanes <= {G.lane_name(x) for x in runs}, (pid, lanes, runs)
            assert "| **Implements** | Initial registration (TS 23.502" in text, pid
        d1 = md_diagrams(pages[f"bizlogic-k{k}"])[0]
        thick = re.search(r"linkStyle ([\d,]+) stroke-width:3.5px", d1)
        edges = [ln.strip() for ln in d1.splitlines()
                 if re.match(r"^\s*\w+ (-->|-\.->|-\.-|~~~)", ln)]
        for i in (thick.group(1).split(",") if thick else []):
            assert '"? ·' not in edges[int(i)], edges[int(i)]      # thick = known interface
        for pid, text in pages.items():
            assert "autonumber" not in text and "> Diagram omitted" not in text, pid
            for src in md_diagrams(text):
                assert not caps(src), (pid, caps(src))
                assert all(line.count('"') % 2 == 0 for line in src.splitlines()), pid
        assert path.is_file()
        sidebar = (kdir / "wiki" / "_sidebar.md").read_text()
        section = sidebar.split(f"**Business Logic (K={k})**", 1)[1]
        assert section.splitlines()[1].strip() == f"- [Overview](bizlogic-k{k}.md)"


def test_rendered_overview(rendered):
    for k, (kdir, path) in rendered.items():
        ov = (kdir / "wiki" / f"bizlogic-k{k}.md").read_text()
        for head in ("## System context", "## Procedures", "## Who takes part in which group",
                     "## Code components (clustered)", "## Component call map",
                     "## All business logic", "## Run"):
            assert head in ov, head
        assert "3GPP procedure families" not in ov and "Initial registration" in ov
        text = path.read_text(encoding="utf-8")
        assert "Evidence: entry roots" not in text and "overflow-x: auto" in text[:20000]


def test_rendered_model_semantics(rendered, gpp):
    for k, (kdir, _path) in rendered.items():
        m = _model(kdir)
        unknown = sorted({a.name for lv in m.logics for a in lv.actors if a.kind == "unknown"})
        assert not unknown, unknown
        for lv in m.logics:
            for a in lv.actors:
                assert SELF.get(a.name) not in lv.deployables, (lv.tid, a.name)
            assert lv.initiator.name == "UE" and lv.bl.implements == ["Initial registration"]
            src = G.flow_diagram(m, lv)[0].src()
            assert not re.search(r"^\s*(participant|actor) c\d+ ", src, re.M)
        llm_names = {p.name for p in m.parent_infos.values() if p.name}
        sidebar = (kdir / "wiki" / "_sidebar.md").read_text()
        section = sidebar.split(f"**Business Logic (K={k})**", 1)[1]
        for name in llm_names:
            assert f"[{name}]" not in section, name
            assert not set(m.group_labels.values()) & {name}


def test_rendered_raw_actor_strings_normalise(rendered, gpp):
    for _k, (kdir, _path) in rendered.items():
        r = load_results(kdir)
        raw = sorted({a.name for bl in r.logic.values() for a in bl.actors})
        assert raw and not [x for x in raw if any(c.kind == "unknown" for c in A.normalize(x))]


def test_render_without_the_stored_profile_is_domain_neutral(rendered, tmp_path):
    k, (kdir, _path) = next(iter(rendered.items()))
    base = tmp_path / "bizlogic"
    shutil.copytree(kdir, base / f"k{k}")              # no domain_profile.json next to it
    render_from_json(base / f"k{k}", k)
    ov = (base / f"k{k}" / "wiki" / f"bizlogic-k{k}.md").read_text()
    assert "## Procedures" not in ov
    page = next((base / f"k{k}" / "wiki").glob(f"bizlogic-k{k}-T*.md")).read_text()
    assert "| **Implements** |" not in page or "Initial registration" not in page
    assert D.active().empty                               # the render restored the default


def test_cli_bizlogic_render(rendered, tmp_path):
    from codocs.cli import cli
    k, (kdir, _path) = next(iter(rendered.items()))
    base = tmp_path / "bizlogic"
    shutil.copytree(kdir, base / f"k{k}")
    shutil.copy(kdir.parent / "domain_profile.json", base / "domain_profile.json")
    res = CliRunner().invoke(cli, ["bizlogic-render", str(tmp_path), "-k", str(k)],
                             catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert f"business-logic-k{k}.html" in res.output.replace("\n", "")
    ov = base / f"k{k}" / "wiki" / f"bizlogic-k{k}.md"
    assert "## Procedures" in ov.read_text()               # the stored profile
    res = CliRunner().invoke(cli, ["bizlogic-render", str(tmp_path), "-k", str(k),
                                   "--domain-profile", "none"], catch_exceptions=False)
    assert res.exit_code == 0, res.output
    assert "## Procedures" not in ov.read_text()
    res = CliRunner().invoke(cli, ["bizlogic-render", str(tmp_path), "--domain-profile",
                                   "no-such-profile"])
    assert res.exit_code == 1 and "unknown domain profile 'no-such-profile'" in res.output
    res = CliRunner().invoke(cli, ["bizlogic-render", str(tmp_path / "nope")])
    assert res.exit_code == 1


# ---- browser render check (slow) -----------------------------------------------------------

@pytest.mark.slow
def test_rendered_html_renders_in_chrome(rendered):
    """every diagram of the re-rendered html parses and renders with the bundled mermaid,
    none is wider than MAX_WIDTH, and the sidebar navigates."""
    if not CHROME.exists() or not BUNDLE.is_file():
        pytest.skip("chrome or ~/.repowiki/mermaid.min.js missing")
    srcs: list[str] = []
    where: list[str] = []
    for k, (_kdir, path) in rendered.items():
        for pid, src in html_diagrams(path):
            srcs.append(src)
            where.append(pid)
    joined = "\n".join(srcs)
    # (the other constructs are rendered by test_bizlogic_diagrams' synthetic check)
    for construct in ("%%{init:", "sequenceDiagram", "stateDiagram-v2", "flowchart LR"):
        assert construct in joined, construct
    res = render_sources(srcs)
    bad = [(w, r["err"]) for w, r in zip(where, res) if not r["ok"]]
    hint = " (if swimlane shapes fail, set USE_SWIMLANE_BETA = False)" \
        if any("swimlane" in s for s, r in zip(srcs, res) if not r["ok"]) else ""
    assert not bad, f"{len(bad)} diagrams failed to render{hint}: {bad[:5]}"
    # only our diagrams: merged wiki pages keep useMaxWidth and shrink to the column
    ours = [(w, r) for w, r in zip(where, res) if w.startswith("bizlogic-")]
    wide = [(w, r["w"]) for w, r in ours if r["w"] > MAX_WIDTH]
    assert not wide, wide
    over = sum(r["w"] > WARN_WIDTH for _, r in ours)
    if over:
        print(f"{over} of {len(ours)} diagrams are wider than {WARN_WIDTH}px (they scroll)")
    # laid out in the page: no lane title taller than its header band, no sequence text
    # with pieces of an entity placeholder, group 0 left of group 1 in D3
    mine = [(w, s) for w, s in zip(where, srcs) if w.startswith("bizlogic-")]
    lay = measure_sources([s for _, s in mine])
    lanes = [(w, r["lanes"]) for (w, _), r in zip(mine, lay) if r["lanes"]]
    assert not lanes, lanes[:5]
    junk = [(w, r["junk"]) for (w, _), r in zip(mine, lay) if r["junk"]]
    assert not junk, junk[:5]
    for (w, s), r in zip(mine, lay):
        c = r["clusters"]
        if re.fullmatch(r"bizlogic-k\d+", w) and "~~~" in s and "p0" in c and "p1" in c:
            assert c["p0"] < c["p1"], (w, c)
    k = next(iter(rendered))
    probe = probe_html(rendered[k][1])
    assert probe["showPage"] == "function"
    assert probe["first"] == f"bizlogic-k{k}"              # the html opens on the overview
    assert probe["nav"] and all(ok for _, ok, _ in probe["nav"]), probe["nav"]
    assert not probe["dangling"], probe["dangling"]
    assert not probe["errors"], probe["errors"][:5]
    assert not probe["loadErrors"], probe["loadErrors"]     # showPage() without an event
    assert probe["active"] == [f"bizlogic-k{k}"]            # its sidebar entry is marked
    assert probe["linkActive"] and not probe["linkMarked"]  # also after a link in a page


@pytest.mark.slow
def test_synthetic_layout_in_chrome():
    """sequence text with ; # < > and long words, the initiator lane, D3 with six groups
    and reciprocal call edges, measured in the page."""
    if not CHROME.exists() or not BUNDLE.is_file():
        pytest.skip("chrome or ~/.repowiki/mermaid.min.js missing")
    import test_bizlogic_diagram_rules as V
    import test_bizlogic_diagrams as T

    prev = D.use_profile(T.gpp_profile())
    try:
        _synthetic_layout(V, T)
    finally:
        D.use_profile(prev)


def _synthetic_layout(V, T) -> None:

    old_seq = G.SEQ_INIT + (
        "\nsequenceDiagram\n  participant a as UE\n  participant b as AMF (this code)\n"
        "  a->>b: Npcf_AMPolicyControl_Create#59; Npcf_AMPolicyControl_UpdateNotify")
    m6 = T.synthetic(n_groups=6)
    srcs = [V.hostile_sequence(), G.component_view(m6)[0].src(), old_seq]
    srcs += [G.flow_diagram(m, lv)[0].src() for m in (T.synthetic(6), T.synthetic(20))
             for lv in m.logics]
    srcs += [G.call_map(T.synthetic(20))[0].src()]
    res = measure_sources(srcs)
    assert all(r["ok"] for r in res), [r["err"] for r in res if not r["ok"]][:3]
    assert not res[0]["junk"], res[0]["junk"]
    assert res[2]["junk"], "the junk check no longer sees an entity split by a wrap"
    c = res[1]["clusters"]
    assert c["p0"] < c["p1"] < c["p2"] < c["p3"] and c["p4"] < c["p1"], c
    assert not [r["lanes"] for r in res[3:-1] if r["lanes"]]
