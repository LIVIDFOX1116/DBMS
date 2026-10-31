"""Adds a visible callout beside every table that has no foreign key by
design, in the SVG dbml-renderer produced.

Why this exists: dbml-renderer shows a table's Note only as a hover tooltip,
so in the PNG (and on paper) a table like app_user just looks unconnected,
as if an FK had been forgotten. The DBML says why each one has none -- a line
beginning "No FK by design:" in the table's Note -- and this copies that line
onto the canvas next to the table.

Pure text processing of Graphviz's own SVG; no layout of its own beyond
"to the right of the table's cluster", where the rendered diagram has empty
space. Run by scripts/render_er.ps1 / render_er.sh after the renderer:

    python scripts/annotate_er_svg.py docs/schema_v2.dbml docs/ER_Diagram_TransactionGraphAnalyzer.svg
"""
import html
import re
import sys
import textwrap
from pathlib import Path

MARKER = "No FK by design:"
FONT_SIZE = 26
LINE_HEIGHT = 34
WRAP_CHARS = 52
BOX_WIDTH = 820
GAP = 50


def no_fk_reasons(dbml: str) -> dict[str, str]:
    """table -> the "No FK by design: ..." line from its Note."""
    reasons = {}
    for m in re.finditer(r"^Table\s+\"?(\w+)\"?[^{\n]*\{(.*?)^\}", dbml, re.M | re.S):
        for line in m.group(2).splitlines():
            if MARKER in line:
                reasons[m.group(1)] = line[line.index(MARKER):].strip().rstrip("'").strip()
    return reasons


def _points(fragment: str) -> list[tuple[float, float]]:
    pts = []
    for attr in re.findall(r'points="([^"]+)"', fragment):
        for pair in attr.split():
            x, y = pair.split(",")
            pts.append((float(x), float(y)))
    return pts


def node_box(svg: str, table: str):
    m = re.search(rf'<g id="[^"]*" class="node">\s*<title>{re.escape(table)}</title>(.*?)</g>\s*</g>', svg, re.S)
    if not m:
        raise SystemExit(f"table {table!r} not found in the SVG -- render it first")
    pts = _points(m.group(1))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def cluster_right_edge(svg: str, box) -> float:
    """Right edge of the cluster (TableGroup) the table sits in, so the note
    lands outside the grey group box rather than across it."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for m in re.finditer(r'<g id="clust\d+" class="cluster">(.*?)</g>', svg, re.S):
        pts = _points(m.group(1))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        if min(xs) <= cx <= max(xs) and min(ys) <= cy <= max(ys):
            return max(xs)
    return x1


def callout(table: str, reason: str, box, left: float) -> str:
    x0, y0, x1, y1 = box
    lines = textwrap.wrap(reason, WRAP_CHARS)
    height = 24 + LINE_HEIGHT * len(lines)
    top = y0
    bold = ' font-weight="bold"'
    texts = "\n".join(
        f'<text x="{left + 20:.2f}" y="{top + 12 + LINE_HEIGHT * (i + 1) - 8:.2f}" '
        f'font-family="Helvetica,sans-Serif" font-size="{FONT_SIZE}" fill="#29235c"'
        f'{bold if i == 0 else ""}>{html.escape(line)}</text>'
        for i, line in enumerate(lines))
    return (
        f'<g class="no-fk-note" id="no_fk_{table}">\n'
        f'<title>no-fk note: {table}</title>\n'
        f'<line x1="{x1:.2f}" y1="{top + 30:.2f}" x2="{left:.2f}" y2="{top + 30:.2f}" '
        f'stroke="#d9781c" stroke-width="2" stroke-dasharray="10,8"/>\n'
        f'<rect x="{left:.2f}" y="{top:.2f}" width="{BOX_WIDTH}" height="{height}" rx="10" '
        f'fill="#fff8e6" stroke="#d9781c" stroke-width="2" stroke-dasharray="10,8"/>\n'
        f"{texts}\n</g>\n")


def annotate(dbml_path: Path, svg_path: Path) -> int:
    svg = svg_path.read_text(encoding="utf-8")
    svg = re.sub(r'<g class="no-fk-note".*?</g>\n', "", svg, flags=re.S)   # idempotent
    notes = []
    for table, reason in no_fk_reasons(dbml_path.read_text(encoding="utf-8")).items():
        box = node_box(svg, table)
        notes.append(callout(table, reason, box, cluster_right_edge(svg, box) + GAP))
    # Inside the graph's own <g>, so the notes share its coordinate system.
    head, sep, tail = svg.rpartition("</g>\n</svg>")
    if not sep:
        raise SystemExit("unexpected SVG structure: no closing graph group")
    svg_path.write_text(head + "".join(notes) + sep + tail, encoding="utf-8")
    return len(notes)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    count = annotate(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"annotated {count} no-FK tables in {sys.argv[2]}")
