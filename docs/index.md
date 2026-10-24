# CoDoc documentation

CoDoc (`codocs` on PyPI and on the command line) generates per-file documentation, unit tests,
a repository wiki and a business-logic analysis for Go, Python, Java, C, C++ and COBOL
repositories. Every doc and every test suite is checked by building and running code.

| Page | Contents |
|---|---|
| [Installation](installation.md) | pip / pipx / uv, the conda environment, per-language toolchains, offline Mermaid bundle |
| [Configuration](configuration.md) | LLM endpoint and key, models and aliases, environment variables, `.env`, cache, resume, sandbox |
| [Usage](usage.md) | the recommended flow: analyze → small trial → full run → translate → check |
| [Unit tests (TeSearch)](testgen.md) | running the test loop on its own |
| [Languages](languages.md) | units, targets, runners, coverage, mutation, suite names and doc signals per language |
| [COBOL](cobol.md) | static index, program modes, test bundles, facts signal, judge, wiki pages |
| [Wiki and translation](wiki.md) | wiki inputs, page plan, HTML export, translation |
| [Business logic](bizlogic.md) | the bizlogic pipeline, its options and domain profiles |
| [Output layout](outputs.md) | every file under `OUT/` |
| [How it works](how-it-works.md) | architecture, the two loops in detail, limits |

The [README](../README.md) has the quickstart, the citation and a comparison with related tools.
