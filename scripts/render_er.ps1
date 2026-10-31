# Regenerates every artefact derived from docs/schema_v2.dbml:
#   docs/ER_Diagram_TransactionGraphAnalyzer.svg  (dbml-renderer, then the no-FK callouts)
#   docs/ER_Diagram_TransactionGraphAnalyzer.png  (headless Edge/Chrome, for the written report)
#   docs/DATA_DICTIONARY.md
#
# Needs Node (for npx) and an installed Edge or Chrome. Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File scripts\render_er.ps1
# Then run tests\test_er_coverage.py, which fails if any of the three fell behind the DDL.
$ErrorActionPreference = "Stop"
$Python = if (Test-Path ".venv-win\Scripts\python.exe") { ".venv-win\Scripts\python.exe" } else { "python" }
$Dbml = "docs\schema_v2.dbml"
$Svg = "docs\ER_Diagram_TransactionGraphAnalyzer.svg"
$Png = "docs\ER_Diagram_TransactionGraphAnalyzer.png"

npx --yes @softwaretechnik/dbml-renderer@1.0.31 -i $Dbml -o $Svg
if ($LASTEXITCODE -ne 0) { throw "dbml-renderer failed" }
& $Python scripts\annotate_er_svg.py $Dbml $Svg
if ($LASTEXITCODE -ne 0) { throw "annotate_er_svg.py failed" }
& $Python scripts\er_png.py $Svg $Png
if ($LASTEXITCODE -ne 0) { throw "er_png.py failed" }
& $Python scripts\gen_data_dictionary.py
if ($LASTEXITCODE -ne 0) { throw "gen_data_dictionary.py failed" }
