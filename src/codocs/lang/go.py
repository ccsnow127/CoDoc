"""GoAdapter: a facade over golang/* and the Go helpers of specforge/tesearch/baseline.

every member delegates verbatim to the Go function it wraps, so Go behaviour does not depend
on the adapter. imports are lazy: specforge/tesearch import codocs.lang.registry, and tests stub
the loop modules, so members that need `specforge.loop` / `tesearch.loop` import them only
when called (and only the loops call them).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from codocs.golang import mutate, package
from codocs.golang import source as gosrc
from codocs.lang.base import Decl, Mutant, PromptSpec, TestResult, prompt_spec


class GoAdapter:
    """the context-free Go adapter (a singleton, registry.adapter("go"))."""

    name = "go"
    display = "Go"
    ctx = None

    @property
    def prompts(self) -> PromptSpec:
        return prompt_spec("go")

    def __repr__(self) -> str:
        return "<GoAdapter go>"

    # -- files and parsing ---------------------------------------------------------

    def owns(self, rel: str) -> bool:
        return rel.endswith(".go")

    def is_test_file(self, rel: str, src: str | None = None) -> bool:
        return rel.endswith("_test.go")

    def is_generated(self, src: str, rel: str = "") -> bool:
        return gosrc.is_generated(src)

    def decls(self, src: str) -> list[Decl]:
        return gosrc.top_level_decls(src)

    def imports(self, src: str) -> list:
        return gosrc.imports(src)

    def identifiers(self, src: str) -> set[str]:
        return gosrc.identifiers(src)

    def extract_code(self, reply: str) -> str:
        return gosrc.extract_go_code(reply)

    def preamble(self, src: str) -> str:
        return gosrc.cgo_header(src)

    def apply_preamble(self, code: str, preamble: str) -> str:
        return gosrc.apply_cgo_header(code, preamble)

    # -- tests in source -------------------------------------------------------------

    def test_names(self, src: str, rel: str) -> list[str]:
        return gosrc.test_functions(src)

    def test_lines(self, src: str, rel: str) -> dict[str, int]:
        """test name -> line of its func, exactly the lines tesearch's mark_failing marks."""
        names = set(gosrc.test_functions(src))
        return {d.name: d.line for d in gosrc.top_level_decls(src)
                if d.kind == "func" and d.name in names}

    def remove_tests(self, src: str, names: set[str]) -> str:
        from codocs.tesearch.measure import prune

        return prune(src, names)

    def strip_non_tests(self, src: str) -> str:
        from codocs.tesearch.measure import strip_non_tests

        return strip_non_tests(src)

    def normalize_suite(self, src: str, unit: Any, target_rel: str) -> str:
        return src

    def mutation_sites(self, src: str) -> list[Mutant]:
        return mutate.mutation_sites(src)

    # -- units and targets -------------------------------------------------------------

    async def discover(self, runner: Any, subtree: str | None = None,
                       packages: list[str] | None = None) -> dict[str, package.PackageInfo]:
        return await package.discover(runner, subtree, packages)

    def select_targets(self, root: Path, units: dict[str, Any], *, files: str | None = None,
                       max_files: int | None = None, max_file_lines: int = 1500
                       ) -> list[package.Target]:
        return package.select_targets(root, units, files=files, max_files=max_files,
                                      max_file_lines=max_file_lines)

    def stem(self, rel: str) -> str:
        name = PurePosixPath(rel).name
        return name[:-3] if name.endswith(".go") else name

    # -- context for prompts -------------------------------------------------------------

    def package_context(self, root: Path, unit: Any, target_rel: str,
                        max_chars: int = 12000) -> str:
        return package.package_context(root, unit, target_rel, max_chars)

    def package_identifiers(self, root: Path, unit: Any,
                            exclude_rel: str | None = None) -> set[str]:
        return package.package_identifiers(root, unit, exclude_rel)

    def test_reference(self, root: Path, unit: Any, target_rel: str,
                       max_chars: int = 8000) -> str:
        return package.test_reference(root, unit, target_rel, max_chars)

    def allowed_third_party(self, root: Path, unit: Any) -> list[str]:
        from codocs.tesearch.loop import go_requires

        return go_requires(root)

    # -- tests of a unit -----------------------------------------------------------------

    def test_index(self, root: Path, unit: Any) -> dict[str, dict]:
        return package.test_index(root, unit)

    def declared_tests(self, root: Path, unit: Any) -> set[str]:
        from codocs.baseline import _declared_tests

        return _declared_tests(SimpleNamespace(module_root=Path(root)), unit)

    def relevant_tests(self, root: Path, unit: Any, target_rel: str,
                       candidates: set[str]) -> list[str]:
        return package.relevant_tests(root, unit, target_rel, candidates)

    # -- generated suite placement -------------------------------------------------------

    def suite_rel(self, unit: Any, stem: str) -> str:
        from codocs.tesearch.prompts import suite_path

        return suite_path(unit, stem)

    def suite_basename(self, stem: str) -> str:
        return f"{stem}_codocs_test.go"

    # -- repair and messages ---------------------------------------------------------------

    def fix_imports(self, src: str, build_errors: list[str], file_basename: str,
                    allowed: list) -> str | None:
        return package.fix_imports(src, build_errors, file_basename, allowed)

    def fix_allowed(self, source_imports: list) -> list:
        from codocs.specforge.loop import _fix_allowed

        return _fix_allowed(source_imports)

    def default_allowed(self, runner: Any, unit: Any) -> list:
        from codocs.tesearch.measure import _default_allowed

        return _default_allowed(runner, unit)

    def failure_message(self, output: str) -> str:
        from codocs.tesearch.measure import failure_message

        return failure_message(output)

    def crash_message(self, res: TestResult) -> str:
        from codocs.tesearch.measure import _crash_message

        return _crash_message(res)

    # -- prompt builders (the loops call specforge/tesearch.prompts directly for Go) ---------

    def doc_seed_messages(self, target_rel: str, unit: Any, source: str,
                          context: str) -> tuple[str, str]:
        from codocs.specforge.prompts import seed_messages

        return seed_messages(target_rel, unit, source, context)

    def doc_codegen_messages(self, target_rel: str, unit: Any, doc: str, context: str,
                             allowed: list, *, taken: set[str] | None = None,
                             has_preamble: bool = False) -> tuple[str, str]:
        from codocs.specforge.prompts import codegen_messages

        return codegen_messages(target_rel, unit, doc, context, allowed, taken=taken,
                                cgo=has_preamble)

    def doc_refine_messages(self, target_rel: str, unit: Any, source: str, doc: str, code: str,
                            evaluation: Any, missing: list[str], *, last: Any = None,
                            last_round: int = 0, attempt: int = 0) -> tuple[str, str]:
        from codocs.specforge.prompts import refine_messages

        return refine_messages(target_rel, unit, source, doc, code, evaluation, missing,
                               last=last, last_round=last_round, attempt=attempt)

    def test_seed_messages(self, target_rel: str, unit: Any, source: str, context: str,
                           taken: set[str], third_party: list[str],
                           reference: str = "") -> tuple[str, str]:
        from codocs.tesearch.prompts import seed_messages

        return seed_messages(target_rel, unit, source, context, taken, third_party, reference)

    def test_refine_messages(self, target_rel: str, unit: Any, source: str, context: str,
                             taken: set[str], third_party: list[str], best: Any, last: Any, *,
                             attempt: int = 0, last_round: int = 0,
                             reference: str = "") -> tuple[str, str]:
        from codocs.tesearch.prompts import refine_messages

        return refine_messages(target_rel, unit, source, context, taken, third_party, best,
                               last, attempt=attempt, last_round=last_round,
                               reference=reference)

    # -- resolution (Go wiki/bizlogic keep their own) -------------------------------------

    def resolve_imports(self, rel: str, imports: list, ctx: Any) -> list[str]:
        raise NotImplementedError("Go import resolution stays in the Go wiki and bizlogic code")

    def resolve_include(self, rel: str, header: str, ctx: Any) -> str | None:
        raise NotImplementedError("Go has no includes")

    # -- persistence, runner -----------------------------------------------------------------

    def baseline_path(self, out_dir: Path, unit: Any) -> Path:
        from codocs.baseline import baseline_path

        return baseline_path(out_dir, unit)

    def make_runner(self, ctx: Any) -> Any:
        """pipeline.make_runner for the run context's repo (its errors propagate)."""
        from codocs.pipeline import make_runner

        out_dir = getattr(ctx, "out_dir", None)
        if out_dir is None and getattr(ctx, "work_dir", None) is not None:
            out_dir = Path(ctx.work_dir).parent.parent      # work_dir = OUT/work/tmp
        return make_runner(ctx.repo_root, ctx.settings, out_dir=out_dir)

    # -- static facts (Go wiki and bizlogic keep their own code) -----------------------------

    def wiki_facts(self, root: Path, rel: str, src: str, ctx: Any) -> Any:
        raise NotImplementedError("the Go wiki uses repowiki's GoModuleIndex")

    def graph_facts(self, root: Path, rel: str, src: str, ctx: Any) -> Any:
        raise NotImplementedError("the Go bizlogic graph keeps its own resolution")


GO = GoAdapter()
