"""codocs settings: defaults, .env / environment, cli overrides."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import asdict, dataclass

from dotenv import find_dotenv, load_dotenv

# short aliases for common model names; any other name is passed through unchanged
MODEL_ALIASES = {
    "gpt": "gpt-5.4",
    "gpt-mini": "gpt-5.4-mini",
    "judge": "gpt-5.5",  # COBOL doc judge (deliberately not the generator's model)
}

_ENV = {
    "api_key": "OPENAI_API_KEY",
    "api_base": "OPENAI_BASE_URL",
    "model": "CODOCS_MODEL",
    "language": "CODOCS_LANG",
    "go": "CODOCS_GO",
    "go_env": "CODOCS_GO_ENV",
    "go_flags": "CODOCS_GO_FLAGS",
    "source_lang": "CODOCS_SOURCE_LANG",
    "test_timeout": "CODOCS_TEST_TIMEOUT",
    "py": "CODOCS_PY",
    "pytest_args": "CODOCS_PYTEST_ARGS",
    "java_home": "CODOCS_JAVA_HOME",
    "mvn": "CODOCS_MVN",
    "java_runner": "CODOCS_JAVA_RUNNER",
    "jacoco_version": "CODOCS_JACOCO_VERSION",
    "cc": "CODOCS_CC",
    "cxx": "CODOCS_CXX",
    "cc_flags": "CODOCS_CC_FLAGS",
    "ld_flags": "CODOCS_LD_FLAGS",
    "cc_db": "CODOCS_CC_DB",
    "cc_root": "CODOCS_CC_ROOT",
    "build_cmd": "CODOCS_BUILD_CMD",
    "test_cmd": "CODOCS_TEST_CMD",
    "gcov": "CODOCS_GCOV",
    "cobc": "CODOCS_COBC",
    "cobc_flags": "CODOCS_COBC_FLAGS",
    "cobol_coverage": "CODOCS_COBOL_COVERAGE",
    "cobol_spec_signal": "CODOCS_COBOL_SPEC_SIGNAL",
    "exclude_paths": "CODOCS_EXCLUDE_PATHS",
    "judge_model": "CODOCS_JUDGE_MODEL",
    "codegen_model": "CODOCS_CODEGEN_MODEL",
}
_ENV_INT = ("test_timeout",)


def resolve_model(name: str) -> str:
    """map a short alias to its full model name; other names pass through."""
    name = name.strip()
    return MODEL_ALIASES.get(name, name)


@dataclass
class Settings:
    """run-wide settings shared by every stage."""

    model: str = "gpt-5.4"
    api_key: str = ""
    api_base: str = ""
    language: str = "en"
    go: str = "go"
    go_env: str = ""                  # extra env for go, "K=V;K2=V2"; {root} = module root go runs in
    go_flags: str = ""                # extra build flags for go list/test, e.g. "-tags=integration"
    concurrency: int = 4
    workers: int = 8
    go_timeout: int = 300
    doc_rounds: int = 3
    doc_threshold: float = 0.9
    test_rounds: int = 3
    test_threshold: float = 0.85
    mutants: int = 20
    mutant_seed: int = 0
    max_file_lines: int = 1500
    temperature: float = 0.2
    max_tokens: int = 16000
    cache_path: str = "~/.codocs/cache.db"
    refresh_llm: bool = False
    sandbox: bool = True
    # multi-language; `language` is the wiki/bizlogic output language
    source_lang: str = "auto"         # auto | all | comma list of go,python,java,c,cpp
    test_timeout: int = 0             # seconds per non-Go test run (0 = go_timeout)
    py: str = ""                      # python interpreter for the tests
    pytest_args: str = ""             # extra pytest arguments (shlex-split)
    java_home: str = ""
    mvn: str = "mvn"
    java_runner: str = "fast"         # fast | mvn
    jacoco_version: str = ""          # "" = picked from the class-file release
    cc: str = "cc"
    cxx: str = "c++"
    cc_flags: str = ""                # extra compile flags (shlex-split)
    ld_flags: str = ""                # extra link flags (shlex-split)
    cc_db: str = ""                   # compile_commands.json
    cc_root: str = ""                 # root for build_cmd/test_cmd ("" = repo root)
    build_cmd: str = ""
    test_cmd: str = ""
    gcov: str = "gcov"
    include_generated: bool = False
    # COBOL
    cobc: str = "cobc"
    cobc_flags: str = ""              # shlex-split; replaces the per-program dialect probing
    cobol_coverage: str = "statement"  # statement | paragraph
    cobol_spec_signal: str = "facts"  # facts | compile: docgen signal of spec programs
    exclude_paths: str = ""           # COBOL scope: globs below the repo root, comma/newline list
    judge_model: str = "gpt-5.5"      # the COBOL doc judge
    codegen_model: str = ""           # COBOL regeneration model ("" = model)

    @classmethod
    def load(cls, **overrides) -> Settings:
        """defaults < .env / environment < non-None overrides."""
        load_dotenv(find_dotenv(usecwd=True))
        values: dict = {}
        for name, var in _ENV.items():
            val = os.environ.get(var)
            if val and name in _ENV_INT:
                if val.strip().isdigit():
                    values[name] = int(val)
            elif val:
                values[name] = val
        values.update({k: v for k, v in overrides.items() if v is not None})
        settings = cls(**values)
        settings.model = resolve_model(settings.model)
        settings.judge_model = resolve_model(settings.judge_model)
        settings.codegen_model = resolve_model(settings.codegen_model)
        if not isinstance(settings.exclude_paths, str):
            settings.exclude_paths = "\n".join(settings.exclude_paths)
        return settings

    def go_env_map(self) -> dict[str, str]:
        """parsed go_env: "K=V;K2=V2" (also newline-separated) -> {K: V}."""
        out: dict[str, str] = {}
        for item in re.split(r"[;\n]", self.go_env or ""):
            key, sep, val = item.strip().partition("=")
            if sep and key.strip():
                out[key.strip()] = val.strip()
        return out

    def go_flag_list(self) -> list[str]:
        return shlex.split(self.go_flags or "")

    def exclude_list(self) -> tuple[str, ...]:
        """exclude_paths as a tuple of globs (comma- or newline-separated, blanks dropped)."""
        raw = self.exclude_paths
        items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,\n]", raw or "")
        out: list[str] = []
        for item in items:
            item = str(item).strip()
            if item and item not in out:
                out.append(item)
        return tuple(out)

    def cobc_flag_list(self) -> list[str]:
        return shlex.split(self.cobc_flags or "")

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("api_key", None)
        return d
