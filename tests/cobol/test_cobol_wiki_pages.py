"""tests for codocs.cobol.wiki.pages (WikiGenerator) with a fake LLM (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from wiki_fixtures import FakeLLM, build, flat_repo

from codocs.cobol.wiki import mermaid
from codocs.cobol.wiki.pages import (
    WikiGenerator,
    _first_paragraph,
    _mid,
    _short_store,
    _strip_outer_fence,
    tech_rows,
    tech_table,
)
from codocs.cobol.wiki.planner import build_digests, build_skeleton
from codocs.repowiki.core.cache import Cache


@pytest.fixture
async def cache(tmp_path: Path):
    c = Cache(db_path=tmp_path / "cache.db")
    await c.init()
    yield c
    await c.close()


def _setup(tmp_path: Path, llm: FakeLLM, cache: Cache, max_programs: int = 3):
    idx = build(flat_repo(tmp_path / "repo"))
    digests = build_digests(idx)
    plan = build_skeleton(idx, digests, max_programs=max_programs)
    return idx, plan, WikiGenerator(idx, digests, llm, cache)


def test_tech_rows_detect_stack(tmp_path: Path):
    idx = build(flat_repo(tmp_path))
    techs = {t: ev for t, _r, ev in tech_rows(idx)}
    assert "6 programs" in techs["COBOL"]
    assert techs["CICS"].startswith("3 programs") and "3 transactions" in techs["CICS"]
    assert "JCL" in techs and "1 jobs" in techs["JCL"]
    assert "DB2" not in techs and "IMS DB (DL/I)" not in techs
    assert tech_table(idx).startswith("| Technology | Role | Evidence |")


def test_page_context_and_mermaid_skeleton(tmp_path: Path, cache):
    idx, plan, gen = _setup(tmp_path, FakeLLM(), cache)
    gen._bind_plan(plan)
    batch = next(p for p in plan.all_pages() if "ACCT01" in p.programs)
    ctx = gen.page_context(batch)
    assert "### program ACCT01 — batch (JCL)" in ctx
    assert "Purpose: post account balances" in ctx
    assert "Run by JCL jobs: ACCTJOB" in ctx
    assert "- ACCT01 -> DATEUTIL [CALL]" in ctx
    assert "PROD.ACCT.MASTER" in ctx
    diagram = gen.page_mermaid(batch)
    assert not mermaid.check(diagram)
    assert "job_ACCTJOB -->|runs| ACCT01" in diagram
    assert "ACCT01 -->|CALL| DATEUTIL" in diagram

    online = next(p for p in plan.all_pages() if "MENU01" in p.programs)
    od = gen.page_mermaid(online)
    assert "txn_MN01 -->|starts| MENU01" in od and "MENU01 -->|XCTL| VIEW01" in od
    assert not mermaid.check(od)


def test_group_mermaid_is_valid(tmp_path: Path, cache):
    idx, plan, gen = _setup(tmp_path, FakeLLM(), cache)
    gen._bind_plan(plan)
    diagram = gen.group_mermaid()
    assert diagram.startswith("flowchart TD")
    assert "entry_cics" in diagram and "entry_jcl" in diagram
    assert not mermaid.check(diagram)


async def test_generate_full_and_cached(tmp_path: Path, cache):
    llm = FakeLLM()
    idx, plan, gen = _setup(tmp_path, llm, cache)
    docs = await gen.generate(plan)
    assert [d.id for d in docs[:2]] == ["index", "architecture"]
    assert {d.id for d in docs[2:]} == {p.id for p in plan.all_pages()}
    assert docs[2].summary.startswith("This subsystem is run by")
    n = len(llm.calls)
    assert n == len(plan.all_pages()) + 2
    # second run is served from the cache
    again = await WikiGenerator(idx, gen.digests, llm, cache).generate(plan)
    assert len(llm.calls) == n
    assert [d.markdown for d in again] == [d.markdown for d in docs]


async def test_generate_only_pages(tmp_path: Path, cache):
    llm = FakeLLM()
    _idx, plan, gen = _setup(tmp_path, llm, cache)
    first = plan.all_pages()[0].id
    docs = await gen.generate(plan, only_pages=[first])
    assert [d.id for d in docs] == ["index", "architecture", first]
    overview_prompt = llm.calls[-1][-1]["content"]
    assert f"[[page:{first}]]" in overview_prompt
    other = plan.all_pages()[1].id
    assert f"[[page:{other}]]" not in overview_prompt


async def test_mermaid_repair_call(tmp_path: Path, cache):
    llm = FakeLLM(bad_mermaid_once=True)
    _idx, plan, gen = _setup(tmp_path, llm, cache, max_programs=12)
    docs = await gen.generate(plan)
    module = docs[2]
    assert module.markdown.startswith("# Fixed")
    assert any(c[0]["content"].startswith("You fix invalid mermaid") for c in llm.calls)


async def test_refine_plan_renames_but_keeps_assignment(tmp_path: Path, cache):
    idx = build(flat_repo(tmp_path / "repo"))
    digests = build_digests(idx)
    skeleton = build_skeleton(idx, digests, max_programs=3)
    refine = {
        "project_title": "Demo Bank",
        "project_summary": "A demo.",
        "sections": [
            {"id": s.id, "title": s.title.upper(), "pages": [
                {"id": p.id, "title": f"Nice {p.id}", "programs": ["EVIL"],
                 "workflows": ["flow"], "description": "d"} for p in s.pages
            ]} for s in reversed(skeleton.sections)
        ],
    }
    llm = FakeLLM(refine=refine)
    gen = WikiGenerator(idx, digests, llm, cache)
    plan = await gen.refine_plan(skeleton)
    assert plan.project_title == "Demo Bank"
    assert [s.id for s in plan.sections] == [s.id for s in reversed(skeleton.sections)]
    base = {p.id: p.programs for p in skeleton.all_pages()}
    assert all(p.programs == base[p.id] for p in plan.all_pages())
    assert all(p.title.startswith("Nice ") for p in plan.all_pages())
    prompt = llm.calls[0][-1]["content"]
    assert "sample_programs" in prompt and "post account balances" in prompt
    # cached on the second call
    await gen.refine_plan(skeleton)
    assert len(llm.calls) == 1


async def test_refine_plan_bad_json_keeps_skeleton(tmp_path: Path, cache):
    idx = build(flat_repo(tmp_path / "repo"))
    digests = build_digests(idx)
    skeleton = build_skeleton(idx, digests, max_programs=3)

    class Junk(FakeLLM):
        async def complete(self, messages, max_tokens=4096):
            return "sorry, no json"

    plan = await WikiGenerator(idx, digests, Junk(), cache).refine_plan(skeleton)
    assert plan is skeleton


async def test_llm_errors_raise_after_retry(tmp_path: Path, cache, monkeypatch):
    class Broken(FakeLLM):
        async def complete(self, messages, max_tokens=4096):
            return "[LLM Error: boom]"

    async def no_sleep(_s):
        return None

    monkeypatch.setattr("codocs.cobol.wiki.pages.asyncio.sleep", no_sleep)
    _idx, plan, gen = _setup(tmp_path, Broken(), cache)
    with pytest.raises(RuntimeError):
        await gen.generate(plan)


def test_helpers():
    assert _short_store("AWS.M2.CARDDEMO.CARDXREF.VSAM.AIX.PATH") == "CARDXREF.VSAM.AIX.PATH"
    assert _short_store("ACCTDAT") == "ACCTDAT"
    assert _mid("end") == "end_" and _mid("9ABC") == "n9ABC" and _mid("A-B.C") == "A_B_C"
    assert _strip_outer_fence("```markdown\n# T\n\nx\n```") == "# T\n\nx"
    md = "# Title\n\nFirst para\ncontinues.\n\n## Next\n\nother"
    assert _first_paragraph(md) == "First para continues."


def test_label_strips_html_like_text():
    from codocs.cobol.wiki.pages import _label

    assert _label('<USRHLQ>.GENAPP "x" [y]') == "USRHLQ.GENAPP 'x' (y)"


# -- grounding: chains, store ranking, overview skeleton, validators -------------


def _chain_setup(tmp_path: Path, llm, cache, max_programs: int = 4):
    from grounding_fixtures import build_chain

    idx = build_chain(tmp_path / "repo")
    digests = build_digests(idx)
    plan = build_skeleton(idx, digests, max_programs=max_programs)
    gen = WikiGenerator(idx, digests, llm, cache)
    gen._bind_plan(plan)
    return idx, plan, gen


def test_call_chains_in_system_facts(tmp_path: Path, cache):
    _idx, _plan, gen = _chain_setup(tmp_path, FakeLLM(), cache)
    chains = gen.call_chains()
    assert ("- txn FR01 -> FRONT01\n  - FRONT01 -LINK-> BIZ01 -LINK-> DB01 [w CUSTOMER] "
            "-LINK-> VS01 [w CUSTFILE]") in chains
    assert "- txn IQ01 -> INQ01\n  - INQ01 -LINK-> DB02 [r CUSTOMER]" in chains
    assert "- job POSTJOB -> POST01 [w PROD.MASTER.KSDS, r PROD.TRANIN]" in chains
    # the logger hub is named once, not on every chain
    assert "LOGGR -" not in chains and "-> LOGGR" not in chains
    assert "Shared services left out" in chains and "LOGGR [w APPLOG] called by 8" in chains
    assert chains in gen.system_facts()


def test_call_chains_describe_shared_subchains_once(tmp_path: Path, cache):
    _idx, _plan, gen = _chain_setup(tmp_path, FakeLLM(), cache)
    gen.index.transactions["FR02"] = "BIZ01"  # BIZ01 becomes an entry program too
    chains = gen.call_chains()
    assert "FRONT01 -LINK-> BIZ01 (see its chain)" in chains
    assert "- txn FR02 -> BIZ01\n  - BIZ01 -LINK-> DB01 [w CUSTOMER] -LINK-> VS01" in chains


def test_page_mermaid_keeps_written_stores_first(tmp_path: Path, cache):
    from codocs.cobol.wiki.models import PagePlan

    _idx, _plan, gen = _chain_setup(tmp_path, FakeLLM(), cache)
    # IMPORT1 reads PROD.EXPORT and writes PROD.MASTER.IMPORT; alphabetical order kept
    # the input and dropped the output when only one store fit
    page = PagePlan(id="imp", title="Import", programs=["IMPORT1"])
    assert gen.rank_page_stores(["IMPORT1"]) == ["PROD.MASTER.IMPORT", "PROD.EXPORT"]
    diagram = gen.page_mermaid(page, max_nodes=4)
    assert "PROD_MASTER_IMPORT" in diagram and "PROD_EXPORT" not in diagram
    assert not mermaid.check(diagram)


def test_group_mermaid_deemphasises_hub_page(tmp_path: Path, cache):
    _idx, plan, gen = _chain_setup(tmp_path, FakeLLM(), cache)
    diagram = gen.group_mermaid()
    hub_lines = [ln for ln in diagram.splitlines() if "-->|" in ln or "-.->|" in ln]
    into_hub = [ln for ln in hub_lines if ln.rstrip().endswith("shared_services")
                or "shared_services[" in ln.split("|")[-1]]
    assert into_hub and all("-.->" in ln for ln in into_hub)
    assert not mermaid.check(diagram)
    overview = gen.overview_mermaid()
    assert "ds_PROD_MASTER_KSDS" in overview  # one-page stores appear in the overview
    access = gen.store_access_facts()
    assert "- PROD.MASTER.KSDS aka MASTER [dataset, INDEXED]: written by programs POST01" \
           in access and "jobs POSTJOB" in access


class _InventingLLM(FakeLLM):
    """answers the overview / architecture prompts with partly invented diagrams."""

    async def complete(self, messages, max_tokens=4096):
        user = messages[-1]["content"]
        if user.startswith("Write the landing"):
            self.calls.append(messages)
            return (
                "# Demo\n\nIntro.\n\n## System at a Glance\n\n```mermaid\nflowchart TD\n"
                '  job_IMPJOB["IMPJOB"] -->|writes| ds_MASTER[("MASTER VSAM KSDS")]\n'
                '  job_POSTJOB["POSTJOB"] -->|writes| ds_MASTER\n```\n'
            )
        if "Architecture" in user[:80]:
            self.calls.append(messages)
            return (
                "# Architecture\n\nText.\n\n```mermaid\nsequenceDiagram\n"
                "  participant F as FRONT01\n  participant D as DB01\n"
                "  participant K as CUSTFILE\n  participant Q as INQ01\n"
                "  F->>D: LINK add\n  Q->>K: writes record\n```\n"
            )
        return await super().complete(messages, max_tokens)


async def test_generate_grounds_overview_and_architecture(tmp_path: Path, cache):
    llm = _InventingLLM()
    _idx, plan, gen = _chain_setup(tmp_path, llm, cache)
    docs = {d.id: d for d in await gen.generate(plan)}
    ov = docs["index"]
    assert "job_IMPJOB" in ov.markdown and "job_IMPJOB -->|writes| ds_MASTER" not in ov.markdown
    assert 'job_POSTJOB["POSTJOB"] -->|writes| ds_MASTER' in ov.markdown
    assert ov.grounding == ["dropped edge job_IMPJOB -->|writes| ds_MASTER"]
    arch = docs["architecture"]
    assert "F->>D: LINK add (via BIZ01)" in arch.markdown and "Q->>K" not in arch.markdown
    assert len(arch.grounding) == 2
    # the overview prompt carries the verified skeleton and store access list
    overview_prompt = next(c[-1]["content"] for c in llm.calls
                           if c[-1]["content"].startswith("Write the landing"))
    assert "Verified overview skeleton" in overview_prompt
    assert "Verified data store access" in overview_prompt
    arch_prompt = next(c[-1]["content"] for c in llm.calls if "Architecture" in c[-1]["content"][:80])
    assert "Call / LINK / XCTL chains" in arch_prompt


def test_tech_rows_vsam_and_terminal_io(tmp_path: Path):
    from grounding_fixtures import build_chain

    from codocs.cobol.models import DataStore
    from codocs.cobol.wiki.pages import _is_vsam

    idx = build_chain(tmp_path)
    techs = {t: ev for t, _r, ev in tech_rows(idx)}
    # CUSTFILE (CICS file) and PROD.MASTER.KSDS (INDEXED, KSDS qualifier)
    assert techs["VSAM"].startswith("2 ")
    # GenApp: a KSDS qualifier inside the member name, no organization, kind 'dataset'
    assert _is_vsam(DataStore(name="<USRHLQ>.GENAPP.KSDSCUST", kind="dataset"))
    assert _is_vsam(DataStore(name="X.Y", kind="dataset", aliases=["CUSTF"]), {"CUSTF"})
    assert not _is_vsam(DataStore(name="PROD.TRANIN", kind="dataset"))
    assert "Terminal screen I/O" not in techs
    idx.programs["IMPORT1"].uses_terminal_io = True  # extended ACCEPT / DISPLAY, no CICS
    techs = {t: ev for t, _r, ev in tech_rows(idx)}
    assert techs["Terminal screen I/O"] == "1 programs (IMPORT1)"


async def test_cached_pages_are_grounded_again(tmp_path: Path, cache):
    llm = _InventingLLM()
    idx, plan, gen = _chain_setup(tmp_path, llm, cache)
    await gen.generate(plan)
    n = len(llm.calls)
    # the cache holds the page as written; grounding runs on every read
    gen2 = WikiGenerator(idx, gen.digests, llm, cache)
    docs = {d.id: d for d in await gen2.generate(plan)}
    assert len(llm.calls) == n
    assert docs["index"].grounding == ["dropped edge job_IMPJOB -->|writes| ds_MASTER"]
    assert "job_IMPJOB -->|writes| ds_MASTER" not in docs["index"].markdown
