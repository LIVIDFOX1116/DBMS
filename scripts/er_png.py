"""Rasterises the ER diagram SVG to PNG for the written report, using a
headless Edge or Chrome that is already installed -- no new dependency.

    python scripts/er_png.py docs/ER_Diagram_TransactionGraphAnalyzer.svg docs/ER_Diagram_TransactionGraphAnalyzer.png

Graphviz sizes the SVG in points, so the browser window has to be set to the
equivalent CSS pixels (pt * 4/3) or the screenshot silently crops the right
and bottom of the diagram. Rendered at half scale: the diagram's 32pt text
stays legible and the file stays around 1 MB.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCALE = 0.5

CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser() -> str:
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for path in CANDIDATES:
        if os.path.exists(path):
            return path
    sys.exit("No Edge/Chrome/Chromium found -- install one, or export the PNG from the SVG by hand.")


def svg_size_px(svg: Path) -> tuple[int, int]:
    m = re.search(r'<svg width="([\d.]+)pt" height="([\d.]+)pt"', svg.read_text(encoding="utf-8"))
    if not m:
        sys.exit(f"{svg}: expected Graphviz's width/height in pt")
    return round(float(m.group(1)) * 4 / 3) + 1, round(float(m.group(2)) * 4 / 3) + 1


def main(svg: Path, png: Path) -> None:
    width, height = svg_size_px(svg)
    with tempfile.TemporaryDirectory() as profile:
        subprocess.run([
            find_browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--force-device-scale-factor={SCALE}", "--default-background-color=FFFFFFFF",
            f"--user-data-dir={profile}", f"--screenshot={png.resolve()}",
            f"--window-size={width},{height}", svg.resolve().as_uri(),
        ], check=True, capture_output=True, timeout=180)
    print(f"wrote {png} ({png.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
