"""
Phase 0 exit criterion from the project bible: run this on the RTX 4070 and
GTX 1650 laptops (inside WSL2 Ubuntu, after installing RAPIDS via conda) to
confirm the GPU path is actually usable before writing a line of pipeline
code against it.

    python scripts/verify_gpu_env.py

This deliberately does NOT run in this sandbox (no NVIDIA GPU here) -- that's
expected. graph/backend.py falls back to pandas/NetworkX automatically
wherever this check fails, so development isn't blocked by it; this script
just tells you, plainly, which path you're on.
"""
import sys


def main():
    print("Checking for a working RAPIDS (cudf + cugraph) installation...\n")

    try:
        import cudf
    except ImportError as e:
        print(f"[FAIL] cudf not importable: {e}")
        print("       -> Expected on the Zenbook / anywhere without an NVIDIA GPU + RAPIDS.")
        print("       -> On the GTX 1650 / RTX 4070 laptops: install RAPIDS via conda inside")
        print("          WSL2 Ubuntu first. See README.md 'GPU laptop setup'.")
        sys.exit(1)

    try:
        series = cudf.Series([1, 2, 3])
        assert list(series.to_pandas()) == [1, 2, 3]
        print("[OK]   cudf.Series([1, 2, 3]) round-trips correctly")
    except Exception as e:
        print(f"[FAIL] cudf imported but basic operation failed: {e}")
        sys.exit(1)

    try:
        import cugraph
        print(f"[OK]   cugraph importable (version {cugraph.__version__})")
    except ImportError as e:
        print(f"[FAIL] cugraph not importable: {e}")
        sys.exit(1)

    try:
        import cupy
        device = cupy.cuda.Device(0)
        free_mem, total_mem = device.mem_info
        print(f"[OK]   GPU 0 visible: {total_mem / 1e9:.1f} GB total VRAM, {free_mem / 1e9:.1f} GB free")
        if total_mem < 5e9:
            print("       Note: under ~5GB VRAM (e.g. the GTX 1650's 4GB) -- keep the")
            print("       case-study subgraph small; this is the real budget constraint,")
            print("       not an afterthought. See the bible's Critical Risks (8.1).")
    except Exception as e:
        print(f"[WARN] Could not query GPU memory via cupy: {e}")
        print("       cudf/cugraph work but double-check `nvidia-smi` shows the GPU.")

    print("\nPHASE 0 GPU CHECK: COMPLETE")


if __name__ == "__main__":
    main()
