"""render-check every mermaid block of a markdown wiki dir with the real mermaid parser.

usage: python scripts/cobol/check_mermaid.py WIKI_DIR [--chrome PATH]

Builds a throwaway HTML page that runs mermaid.parse() on each block (bundle from
~/.repowiki/mermaid.min.js) and loads it in headless Chrome; prints one line per failing block.
Dev tool only (needs a local Chrome); not part of the test suite.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from codocs.cobol.wiki.mermaid import extract_blocks
from codocs.repowiki.export.html import load_local_mermaid

_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

_PAGE = """<!DOCTYPE html><html><body><pre id="out">pending</pre>
<script>__MERMAID__</script>
<script>
const blocks = __BLOCKS__;
mermaid.initialize({startOnLoad: false});
(async () => {
  const res = [];
  for (const [name, code] of blocks) {
    try { await mermaid.parse(code); res.push([name, "ok"]); }
    catch (e) { res.push([name, String(e.message || e).split("\\n").slice(0, 3).join(" ")]); }
  }
  document.getElementById("out").textContent = "RESULT" + JSON.stringify(res);
})();
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wiki_dir")
    ap.add_argument("--chrome", default=_CHROME)
    args = ap.parse_args()
    js = load_local_mermaid()
    if js is None:
        print("no local mermaid bundle (~/.repowiki/mermaid.min.js)")
        return 2
    blocks = []
    for md in sorted(Path(args.wiki_dir).glob("*.md")):
        for i, code in enumerate(extract_blocks(md.read_text(encoding="utf-8")), 1):
            blocks.append([f"{md.name}#{i}", code])
    page = _PAGE.replace("__BLOCKS__", json.dumps(blocks).replace("</", "<\\/"))
    page = page.replace("__MERMAID__", js)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "check.html"
        path.write_text(page, encoding="utf-8")
        dom = subprocess.run(
            [args.chrome, "--headless=new", "--disable-gpu", "--virtual-time-budget=20000",
             "--dump-dom", path.as_uri()],
            capture_output=True, text=True, timeout=120,
        ).stdout
    m = re.search(r"RESULT(\[.*?\])</pre>", dom, re.DOTALL)
    if not m:
        print("no result from chrome")
        return 2
    results = json.loads(html.unescape(m.group(1)))
    bad = [(n, r) for n, r in results if r != "ok"]
    for n, r in bad:
        print(f"FAIL {n}: {r}")
    print(f"{len(results) - len(bad)}/{len(results)} mermaid blocks parse")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
