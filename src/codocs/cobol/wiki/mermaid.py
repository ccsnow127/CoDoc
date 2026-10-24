"""validation and repair of mermaid blocks in generated wiki pages."""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)

_DIAGRAM_TYPES = (
    "graph",
    "flowchart",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram",
    "stateDiagram-v2",
    "erDiagram",
    "journey",
    "gantt",
    "pie",
    "mindmap",
    "timeline",
)

# a [label] / {label} that contains unquoted special chars mermaid chokes on;
# matched per bracket type so the closer always pairs with its opener
_SQ_LABEL_RE = re.compile(r"\[([^\[\]\"]*[(){}#;&<>][^\[\]\"]*)\]")
_CURLY_LABEL_RE = re.compile(r"\{([^{}\"]*[()\[\]#;&<>][^{}\"]*)\}")

# flowchart edge syntax the real parser rejects (verified against mermaid 11):
# '--|x|' / '==|x|' (a labelled link needs its arrow head: '-->|x|'), a quoted edge label
# padded with spaces inside the pipes ('-->| "x" |'), backslash-escaped quotes inside a
# quoted label, and a label opening with a backtick (parsed as a markdown string that
# must also close with one)
_HEADLESS_LINK_RE = re.compile(r"(?<![-.=])(--|==)\|")
_PADDED_PIPE_RE = re.compile(r"\|\s+(\"[^\"|]*\")\s*\||\|\s*(\"[^\"|]*\")\s+\|")
_ESCAPED_QUOTE = '\\"'


def extract_blocks(markdown: str) -> list[str]:
    """all mermaid code blocks in a markdown document."""
    return [m.group(1).strip() for m in _FENCE_RE.finditer(markdown)]


# statement-opening keywords that make a sequenceDiagram line valid without an arrow
_SEQ_KEYWORDS = (
    "participant", "actor", "note", "loop", "alt", "else", "opt", "par", "and",
    "critical", "end", "break", "rect", "activate", "deactivate", "autonumber",
    "title", "box", "create", "destroy", "link", "links", "properties", "%%",
)
_SEQ_ARROW_RE = re.compile(r"-{1,2}(?:>>?|[)x])")


def check(code: str) -> list[str]:
    """return a list of problems; empty list means the block looks renderable."""
    problems: list[str] = []
    lines = [ln for ln in code.splitlines() if ln.strip()]
    if not lines:
        return ["empty diagram"]

    head = lines[0].strip()
    if not any(head == t or head.startswith(t + " ") for t in _DIAGRAM_TYPES):
        problems.append(f"unknown diagram type in first line: {head!r}")

    if "```" in code:
        problems.append("nested code fence inside mermaid block")

    is_seq = head.startswith("sequenceDiagram")
    is_flow = _is_flowchart(head)
    for i, ln in enumerate(lines[1:], 2):
        stripped = ln.strip()
        if is_flow:
            problems.extend(f"line {i}: {p}: {stripped[:60]!r}" for p in _flowchart_syntax(stripped))
        if stripped.startswith(("#", "<!--")):
            problems.append(f"line {i}: markdown comment inside diagram")
        if not _balanced(stripped):
            problems.append(f"line {i}: unbalanced brackets: {stripped[:60]!r}")
        if is_seq and not _valid_sequence_line(stripped):
            problems.append(f"line {i}: not a sequenceDiagram statement: {stripped[:60]!r}")

    if _is_flowchart(head):
        depth = _subgraph_depth(lines[1:])
        if depth < 0:
            problems.append("'end' without an open subgraph")
        elif depth > 0:
            problems.append(f"{depth} subgraph(s) never closed with 'end'")
    return problems


def _flowchart_syntax(line: str) -> list[str]:
    """flowchart statements the real parser rejects although brackets balance."""
    out: list[str] = []
    if "`" in line:
        out.append("backtick in a flowchart label")
    if _ESCAPED_QUOTE in line:
        out.append('backslash-escaped quote (\\") in a label')
    if _HEADLESS_LINK_RE.search(line):
        out.append("labelled link without an arrow head ('--|x|')")
    if _PADDED_PIPE_RE.search(line):
        out.append("spaces around a quoted edge label inside |...|")
    return out


def _fix_flowchart_line(line: str) -> str:
    """mechanical repair of the flowchart statements _flowchart_syntax flags."""
    line = line.replace(_ESCAPED_QUOTE, "'").replace("`", "")
    line = _HEADLESS_LINK_RE.sub(lambda m: m.group(1) + ">|", line)
    return _PADDED_PIPE_RE.sub(lambda m: f"|{m.group(1) or m.group(2)}|", line)


def _is_flowchart(head: str) -> bool:
    return head.split(" ", 1)[0] in ("graph", "flowchart")


def _subgraph_depth(lines: list[str]) -> int:
    """open subgraphs left at the end (negative as soon as an 'end' has nothing to close)."""
    depth = 0
    for ln in lines:
        word = ln.strip().split(" ", 1)[0]
        if word == "subgraph":
            depth += 1
        elif word == "end":
            depth -= 1
            if depth < 0:
                return depth
    return depth


def _valid_sequence_line(line: str) -> bool:
    """a sequence line must open with a keyword or contain a message arrow."""
    first = line.split(" ", 1)[0].lower().rstrip(":")
    if first in _SEQ_KEYWORDS:
        return True
    return bool(_SEQ_ARROW_RE.search(line))


def _balanced(line: str) -> bool:
    """bracket balance check that ignores quoted strings and arrow syntax."""
    line = re.sub(r'"[^"]*"', '""', line)
    # strip message arrows first: the async/cross arrows -) --) -x --x contain
    # a lone paren that is arrow syntax, not a bracket
    line = re.sub(r"-{1,2}(?:>>?|[)x])", "-", line)
    # -- / --> / --o etc. are not brackets; pipes delimit edge labels
    counts = {c: line.count(c) for c in "[](){}"}
    return (
        counts["["] == counts["]"]
        and counts["("] == counts[")"]
        and counts["{"] == counts["}"]
    )


def sanitize(code: str) -> str:
    """apply mechanical fixes for the most common LLM mermaid mistakes."""
    out_lines: list[str] = []
    lines = code.splitlines()
    head = next((ln.strip() for ln in lines if ln.strip()), "")
    depth = 0
    for ln in lines:
        stripped = ln.rstrip().rstrip(";")
        # flowcharts: drop a stray 'end' that closes no subgraph (a parse error)
        if _is_flowchart(head):
            stripped = _fix_flowchart_line(stripped)
            word = stripped.strip().split(" ", 1)[0]
            if word == "subgraph":
                depth += 1
            elif word == "end":
                if depth == 0:
                    continue
                depth -= 1
        # quote node labels containing characters mermaid can't parse bare
        stripped = _SQ_LABEL_RE.sub(lambda m: f'["{m.group(1)}"]', stripped)
        stripped = _CURLY_LABEL_RE.sub(lambda m: f'{{"{m.group(1)}"}}', stripped)
        out_lines.append(stripped)
    return "\n".join(out_lines).strip()


def sanitize_page(markdown: str) -> tuple[str, list[str]]:
    """sanitize every mermaid block in a page; return (new markdown, remaining problems)."""
    problems: list[str] = []

    def _replace(m: re.Match) -> str:
        fixed = sanitize(m.group(1))
        problems.extend(check(fixed))
        return f"```mermaid\n{fixed}\n```"

    return _FENCE_RE.sub(_replace, markdown), problems
