# GPU setup (RAPIDS in WSL2)

FrameCheck's GPU path is `graph/backend.py`'s cudf + cuGraph branch. **cuGraph has no native
Windows build**, so on a Windows machine the GPU path only exists inside WSL2. Everything in
FrameCheck keeps working without any of this — the CPU (pandas + NetworkX) fallback is the
default, and GPU is opt-in behind one environment variable.

## This machine

| | |
|---|---|
| GPU | NVIDIA GeForce GTX 1650, 4 GB |
| Windows driver | 581.80 (this is what exposes the GPU to WSL; no driver is installed *inside* WSL) |
| Compute capability | 7.5 (Turing) — RAPIDS requires 7.0+ |
| WSL distro | Ubuntu-24.04, Python 3.12.3, 7 GB RAM |
| RAPIDS | 26.8 (`cudf-cu12`, `cugraph-cu12`) |

The Phase 4 brief named an RTX 4070; the machine this was actually set up on has the GTX 1650
(`scripts/benchmark.py` has always mentioned both laptops). That matters for reading
`docs/benchmark_results.md`: 4 GB of VRAM shared with the Windows desktop, and a Turing part at
that, is a modest GPU. Treat the numbers as "what this hardware does", never as what cuGraph can
do in general.

## Install

Postgres already lives in this WSL distro, so nothing moves. Run everything from a WSL shell:

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip

python3 -m venv ~/rapids-venv
~/rapids-venv/bin/pip install --upgrade pip wheel
~/rapids-venv/bin/pip install --extra-index-url=https://pypi.nvidia.com \
    cudf-cu12 cugraph-cu12 pandas pyarrow psycopg2-binary networkx scipy python-dotenv "sqlalchemy>=2.1"
```

Notes that save time:

- **No CUDA toolkit install.** The pip wheels bring their own CUDA runtime, and the Windows driver
  provides `libcuda.so` under `/usr/lib/wsl/lib`. Installing a CUDA toolkit inside WSL is a
  common wrong turn.
- **It is a big download** — roughly 4-5 GB of wheels (`libcugraph` alone is ~1 GB), and pip
  fetches everything before it installs anything, so the venv looks empty for a long time. That is
  normal, not a hang.
- `scipy` and `python-dotenv` are easy to forget: `networkx.pagerank` needs the first and
  `ingestion/config.py` needs the second, so the **CPU** half of the benchmark fails without them.

## Verify

```bash
~/rapids-venv/bin/python -c "import cudf, cugraph; print(cudf.__version__, cugraph.__version__)"
cd /mnt/e/DBMS && PYTHONPATH=. ~/rapids-venv/bin/python scripts/verify_gpu_env.py
```

## Using the GPU path

GPU is opt-in twice over: RAPIDS has to be importable **and** `FRAMECHECK_USE_GPU_GRAPH=1` has to
be set (`gpu_available()` in `graph/backend.py`). Without both, every entry point silently uses
the CPU backend — that is the designed behaviour, not a misconfiguration.

```bash
cd /mnt/e/DBMS
export PYTHONPATH=/mnt/e/DBMS FRAMECHECK_USE_GPU_GRAPH=1
~/rapids-venv/bin/python scripts/benchmark.py --snapshot-id 2 --seed-wallet-id 2 \
    --repeats 3 --json docs/perf/benchmarks.json
```

The repo is reached through `/mnt/e/DBMS` and snapshot paths in `graph_snapshot.file_path` are
stored **relative** (`snapshots/<name>.parquet`), so they resolve from WSL with no translation —
as long as the working directory is the repo root.

`scripts/benchmark.py` runs the CPU pass and the GPU pass in one process, so the only difference
between the two columns is the backend. Run it from WSL for both; timing CPU on Windows against
GPU in WSL would compare two different Pythons and two different filesystems, not two backends.

## Limits worth knowing

- **4 GB of VRAM, shared with the Windows desktop.** Snapshot 3 (698,273 edges) is the largest
  here and fits comfortably; much larger graphs would not.
- **Small graphs can be slower on the GPU.** Host-to-device transfer and setup cost is fixed, so
  on a 35k-edge snapshot the CPU often wins. That is a real result and
  `docs/benchmark_results.md` reports it as measured rather than hiding it.
- **Postgres is reachable only while a WSL session is attached.** If every WSL shell exits, the
  distro shuts down and `localhost:5432` stops answering from Windows.
