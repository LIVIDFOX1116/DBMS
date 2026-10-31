#!/usr/bin/env bash
# Regenerates every artefact derived from docs/schema_v2.dbml:
#   docs/ER_Diagram_TransactionGraphAnalyzer.svg  (dbml-renderer, then the no-FK callouts)
#   docs/ER_Diagram_TransactionGraphAnalyzer.png  (headless Chrome/Chromium/Edge, for the written report)
#   docs/DATA_DICTIONARY.md
#
# Needs Node (for npx) and an installed Chrome, Chromium or Edge. Run from the repo root:
#   bash scripts/render_er.sh
# Then run tests/test_er_coverage.py, which fails if any of the three fell behind the DDL.
set -euo pipefail
PYTHON="${PYTHON:-python3}"
DBML=docs/schema_v2.dbml
SVG=docs/ER_Diagram_TransactionGraphAnalyzer.svg
PNG=docs/ER_Diagram_TransactionGraphAnalyzer.png

npx --yes @softwaretechnik/dbml-renderer@1.0.31 -i "$DBML" -o "$SVG"
"$PYTHON" scripts/annotate_er_svg.py "$DBML" "$SVG"
"$PYTHON" scripts/er_png.py "$SVG" "$PNG"
"$PYTHON" scripts/gen_data_dictionary.py
