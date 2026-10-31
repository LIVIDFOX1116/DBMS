"""Renders docs/PROJECT_PROPOSAL.md to docs/gantt.png, .pdf and .docx.

    uv run --no-project --with pypandoc_binary python scripts/build_proposal.py

Build-time only, so nothing here is in requirements.txt: pandoc comes from the
pypandoc_binary wheel, the Mermaid CLI from npx, and the PDF and the chart are
drawn by the Edge or Chrome already on the machine (scripts/er_png.py finds it).
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pypandoc

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
from er_png import find_browser  # noqa: E402

DOCS = REPO / "docs"
SOURCE = DOCS / "PROJECT_PROPOSAL.md"
CSS = """body{font:11pt/1.45 Georgia,serif;max-width:46em;margin:2em auto;padding:0 1em;color:#111}
h1,h2{font-family:Arial,sans-serif}h2{margin-top:1.6em;border-bottom:1px solid #ccc;padding-bottom:.2em}
table{border-collapse:collapse;width:100%;font-size:10pt}td,th{border:1px solid #bbb;padding:4px 7px;text-align:left;vertical-align:top}
img{max-width:100%}code{font-size:.92em}"""


def main() -> None:
    browser = find_browser()
    text = SOURCE.read_text(encoding="utf-8")
    chart = re.search(r"```mermaid\n(.*?)```", text, re.S).group(1)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "gantt.mmd").write_text(chart, encoding="utf-8")
        (tmp / "pp.json").write_text(json.dumps({"executablePath": browser, "headless": "new"}), encoding="utf-8")
        npx = "npx.cmd" if sys.platform == "win32" else "npx"
        subprocess.run([npx, "--yes", "@mermaid-js/mermaid-cli@11", "-i", str(tmp / "gantt.mmd"), "-o", str(DOCS / "gantt.png"),
                        "-p", str(tmp / "pp.json"), "-w", "1400", "-H", "700", "-b", "white", "-s", "2"],
                       check=True, env={**__import__("os").environ, "PUPPETEER_SKIP_DOWNLOAD": "1"})
        # The printed versions show the image; the Markdown keeps the source too.
        printable = re.sub(r"```mermaid\n.*?```\n", "", text, flags=re.S)
        printable = re.sub(r"<!--.*?-->", "", printable)
        (tmp / "p.md").write_text(printable, encoding="utf-8")
        (tmp / "s.css").write_text(CSS, encoding="utf-8")
        args = [f"--resource-path={DOCS}", "--standalone", "--metadata", "title=FrameCheck: Project Proposal"]
        pypandoc.convert_file(str(tmp / "p.md"), "docx", outputfile=str(DOCS / "PROJECT_PROPOSAL.docx"), extra_args=args)
        html = tmp / "p.html"
        pypandoc.convert_file(str(tmp / "p.md"), "html", outputfile=str(html),
                              extra_args=args + ["--embed-resources", f"--css={tmp / 's.css'}"])
        subprocess.run([browser, "--headless=new", "--disable-gpu", f"--user-data-dir={tmp / 'edge'}", "--no-pdf-header-footer",
                        f"--print-to-pdf={DOCS / 'PROJECT_PROPOSAL.pdf'}", html.as_uri()],
                       check=True, capture_output=True, timeout=120)
    for name in ("gantt.png", "PROJECT_PROPOSAL.pdf", "PROJECT_PROPOSAL.docx"):
        print(f"wrote docs/{name} ({(DOCS / name).stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
