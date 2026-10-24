"""export wiki as a self-contained HTML file."""

from __future__ import annotations

import html
from pathlib import Path

from codocs.repowiki.core.wiki_builder import Wiki


def export_html(
    wiki: Wiki,
    output_path: str | Path,
    branding: bool = True,
    mermaid_js: str | None = None,
) -> None:
    """generate a single self-contained HTML file with sidebar navigation.

    branding=False removes the RepoWiki footer and title suffix, for
    output that should not reference the generating tool.
    mermaid_js, when given, is embedded inline instead of loading mermaid
    from the CDN, making the file fully offline and free of external URLs.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    pages_html = []
    nav_html = []

    for item in wiki.sidebar:
        if item.page_id:
            nav_html.append(
                f'<a class="nav-item" href="#" onclick="showPage(\'{item.page_id}\')">'
                f'{html.escape(item.title)}</a>'
            )
        else:
            nav_html.append(f'<div class="nav-group">{html.escape(item.title)}</div>')
        for child in item.children:
            nav_html.append(
                f'<a class="nav-item nav-child" href="#" onclick="showPage(\'{child.page_id}\')">'
                f'{html.escape(child.title)}</a>'
            )

    for page in wiki.pages:
        # basic markdown to HTML (just the essentials, mermaid handled by JS)
        content = _markdown_to_html(page.content)
        pages_html.append(
            f'<div id="page-{page.id}" class="wiki-page" style="display:none">'
            f'{content}</div>'
        )

    template = _HTML_TEMPLATE.format(
        title=html.escape(wiki.project_name),
        title_suffix=" - RepoWiki" if branding else "",
        footer=_FOOTER if branding else "",
        nav="".join(nav_html),
        pages="".join(pages_html),
        first_page=wiki.pages[0].id if wiki.pages else "index",
    )
    # the mermaid bundle contains { } — splice it in after .format(), never through it
    if mermaid_js is not None:
        script_tag = f"<script>{mermaid_js}</script>"
    else:
        script_tag = _MERMAID_CDN_TAG
    template = template.replace("__MERMAID_SCRIPT__", script_tag)

    out.write_text(template, encoding="utf-8")


def _markdown_to_html(md: str) -> str:
    """minimal markdown to HTML conversion (no dependencies)."""
    import re
    lines = md.split("\n")
    result = []
    in_code = False
    code_lang = ""
    code_lines: list[str] = []
    list_type: str | None = None  # "ul", "ol", or None when not inside a list
    table_rows: list[str] = []

    def close_list() -> None:
        nonlocal list_type
        if list_type is not None:
            result.append(f"</{list_type}>")
            list_type = None

    def close_table() -> None:
        if not table_rows:
            return
        rows_html = []
        for i, row in enumerate(table_rows):
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            tag = "th" if i == 0 else "td"
            rows_html.append(
                "<tr>" + "".join(f"<{tag}>{_inline_md(c)}</{tag}>" for c in cells) + "</tr>"
            )
        result.append(f"<table>{''.join(rows_html)}</table>")
        table_rows.clear()

    for line in lines:
        # fenced code blocks
        if line.startswith("```"):
            if in_code:
                code = html.escape("\n".join(code_lines))
                if code_lang == "mermaid":
                    result.append(f'<div class="mermaid">{code}</div>')
                else:
                    result.append(f'<pre><code class="language-{code_lang}">{code}</code></pre>')
                code_lines = []
                in_code = False
            else:
                close_list()
                in_code = True
                code_lang = line[3:].strip() or "text"
            continue

        if in_code:
            code_lines.append(line)
            continue

        # markdown tables: buffer consecutive | rows, skip the ---- separator
        if line.strip().startswith("|"):
            close_list()
            if not re.match(r"^\s*\|[\s\-:|]+\|\s*$", line):
                table_rows.append(line)
            continue
        close_table()

        # list items: wrap consecutive items in a single <ul>/<ol> so ordered
        # lists keep their numbers and the markup stays valid HTML
        if line.startswith("- "):
            if list_type != "ul":
                close_list()
                result.append("<ul>")
                list_type = "ul"
            result.append(f"<li>{_inline_md(line[2:])}</li>")
            continue
        if re.match(r"^\d+\. ", line):
            if list_type != "ol":
                close_list()
                result.append("<ol>")
                list_type = "ol"
            text = re.sub(r"^\d+\. ", "", line)
            result.append(f"<li>{_inline_md(text)}</li>")
            continue

        # any other line ends an open list
        close_list()

        # headings
        if line.startswith("# "):
            result.append(f"<h1>{_inline_md(line[2:])}</h1>")
        elif line.startswith("## "):
            result.append(f"<h2>{_inline_md(line[3:])}</h2>")
        elif line.startswith("### "):
            result.append(f"<h3>{_inline_md(line[4:])}</h3>")
        elif line.startswith("#### "):
            result.append(f"<h4>{_inline_md(line[5:])}</h4>")
        elif line.startswith("> "):
            result.append(f"<blockquote>{_inline_md(line[2:])}</blockquote>")
        elif line.strip() == "":
            result.append("<br>")
        else:
            result.append(f"<p>{_inline_md(line)}</p>")

    close_list()
    close_table()
    return "\n".join(result)


def _inline_md(text: str) -> str:
    """handle inline markdown: bold, code, links."""
    import re
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    def _link(m: re.Match) -> str:
        label, target = m.group(1), m.group(2)
        # internal wiki links (page.md) navigate within the single-file viewer
        if target.endswith(".md") and "/" not in target:
            page_id = target[:-3]
            return (
                f'<a href="#" onclick="showPage(\'{page_id}\'); return false;">{label}</a>'
            )
        return f'<a href="{target}">{label}</a>'

    text = re.sub(r"\[(.+?)\]\((.+?)\)", _link, text)
    return text


_FOOTER = (
    '<div style="margin-top: 24px; padding-top: 12px; border-top: 1px solid #e5e7eb;\n'
    '    font-size: 11px; color: #9ca3af;">\n'
    '    Generated by <a href="https://github.com/he-yufeng/RepoWiki" '
    'style="color:#6b7280">RepoWiki</a>\n'
    "  </div>"
)

_MERMAID_CDN_TAG = (
    '<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>'
)


def load_local_mermaid() -> str | None:
    """a locally cached mermaid bundle (~/.repowiki/mermaid.min.js), if present.

    When available it is inlined into exported HTML so the file renders offline
    with no external requests.
    """
    path = Path.home() / ".repowiki" / "mermaid.min.js"
    try:
        if path.exists() and path.stat().st_size > 100_000:
            return path.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}{title_suffix}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  display: flex; height: 100vh; color: #1a1a1a; background: #fff; }}
.sidebar {{ width: 260px; border-right: 1px solid #e5e7eb; padding: 16px;
  overflow-y: auto; flex-shrink: 0; background: #fafafa; }}
.sidebar h2 {{ font-size: 16px; margin-bottom: 12px; color: #111; }}
.nav-item {{ display: block; padding: 6px 12px; color: #374151; text-decoration: none;
  border-radius: 6px; font-size: 14px; cursor: pointer; }}
.nav-item:hover {{ background: #e5e7eb; }}
.nav-item.active {{ background: #dbeafe; color: #1d4ed8; font-weight: 500; }}
.nav-child {{ padding-left: 28px; font-size: 13px; }}
.nav-group {{ padding: 8px 12px 4px; font-size: 12px; font-weight: 600;
  text-transform: uppercase; color: #6b7280; margin-top: 8px; }}
.content {{ flex: 1; padding: 32px 48px; overflow-y: auto; max-width: 900px; }}
h1 {{ font-size: 28px; margin-bottom: 16px; border-bottom: 1px solid #e5e7eb; padding-bottom: 8px; }}
h2 {{ font-size: 22px; margin: 24px 0 12px; }}
h3 {{ font-size: 18px; margin: 20px 0 8px; }}
p {{ margin: 8px 0; line-height: 1.7; }}
li {{ margin: 4px 0 4px 24px; line-height: 1.6; }}
blockquote {{ border-left: 3px solid #3b82f6; padding: 8px 16px; margin: 12px 0;
  background: #eff6ff; color: #1e40af; border-radius: 0 4px 4px 0; }}
pre {{ background: #1e293b; color: #e2e8f0; padding: 16px; border-radius: 8px;
  overflow-x: auto; margin: 12px 0; font-size: 13px; line-height: 1.5; }}
code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }}
pre code {{ background: none; padding: 0; }}
strong {{ font-weight: 600; }}
a {{ color: #2563eb; }}
.mermaid {{ margin: 16px 0; text-align: center; }}
table {{ border-collapse: collapse; margin: 16px 0; width: 100%; font-size: 14px; }}
th, td {{ border: 1px solid #e5e7eb; padding: 8px 12px; text-align: left; }}
th {{ background: #f9fafb; }}
</style>
</head>
<body>
<div class="sidebar">
  <h2>{title}</h2>
  {nav}
  {footer}
</div>
<div class="content" id="content">
  {pages}
</div>
__MERMAID_SCRIPT__
<script>
mermaid.initialize({{ startOnLoad: false, theme: 'default' }});
function showPage(id) {{
  document.querySelectorAll('.wiki-page').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.nav-item').forEach(a => a.classList.remove('active'));
  const page = document.getElementById('page-' + id);
  if (page) {{
    page.style.display = 'block';
    mermaid.run({{ nodes: page.querySelectorAll('.mermaid') }});
  }}
  event.target.classList.add('active');
}}
showPage('{first_page}');
</script>
</body>
</html>"""
