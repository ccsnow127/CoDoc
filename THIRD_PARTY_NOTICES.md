# Third-party notices

CoDoc includes or builds on the following third-party work.

## RepoWiki

`src/codocs/repowiki/` is a modified fork of [RepoWiki](https://github.com/he-yufeng/RepoWiki)
by Yufeng He, released under the MIT License. CoDoc extends it with the `deepdoc` pipeline
(planner, generator, Mermaid validation and repair, translator), Go static analysis and
multi-language indexes. The original license follows.

```text
MIT License

Copyright (c) 2026 Yufeng He

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## COBOL stub copybooks

`src/codocs/cobol/stubs/*.cpy` are minimal, independently written compile-only stubs that
mirror the publicly documented layouts of IBM CICS, Db2, IMS and MQ interface copybooks
(for example `DFHEIBLK`, `SQLCA`, `CMQV`). They contain no IBM source code. IBM, CICS, Db2,
IMS and MQ are trademarks of International Business Machines Corporation.

## Test data

- `tests/cobol/test_cobol_jcl.py` embeds short JCL excerpts from
  [AWS Mainframe Modernization CardDemo](https://github.com/aws-samples/aws-mainframe-modernization-carddemo)
  (Copyright Amazon.com, Inc. or its affiliates, Apache License 2.0); their copyright lines are kept.
- `datasets/manifest.json` and `scripts/cobol/fetch_datasets.sh` reference public COBOL
  repositories (CardDemo, Apache-2.0; IBM cics-genapp, EPL-2.0; shamrice/cbi, GPL-3.0) that are
  downloaded on demand for benchmarks and are not redistributed here. `tests/cobol/golden/index/dataset-*.json`
  hold only derived metadata (names, counts, graph edges, hashes) of those repositories.
