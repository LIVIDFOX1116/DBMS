"""
graph_snapshot.file_path is stored relative so a snapshot row is portable
between machines. "Relative to what" used to be the process's working
directory, which is the repo root in a terminal but the install directory
in the packaged desktop app -- where every audit died with a bare
FileNotFoundError naming a path that looked right.
"""
import os

os.environ["FRAMECHECK_API_MODE"] = "mock"

import pathlib  # noqa: E402

import pytest  # noqa: E402

from ingestion import config  # noqa: E402


class TestResolveDataPath:
    def test_relative_paths_resolve_against_the_repo_not_the_cwd(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FRAMECHECK_DATA_DIR", raising=False)
        monkeypatch.chdir(tmp_path)  # a working directory that is not the repo
        resolved = config.resolve_data_path("snapshots/example.parquet")
        assert resolved == config.REPO_ROOT / "snapshots" / "example.parquet"
        assert resolved.is_absolute()

    def test_the_data_dir_env_var_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FRAMECHECK_DATA_DIR", str(tmp_path))
        assert config.resolve_data_path("snapshots/x.parquet") == tmp_path / "snapshots" / "x.parquet"

    def test_absolute_paths_pass_through_untouched(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FRAMECHECK_DATA_DIR", str(tmp_path / "ignored"))
        absolute = pathlib.Path(tmp_path / "elsewhere" / "x.parquet")
        assert config.resolve_data_path(str(absolute)) == absolute

    def test_forward_slashed_storage_still_resolves_on_windows(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FRAMECHECK_DATA_DIR", str(tmp_path))
        assert config.resolve_data_path("snapshots/sub/x.parquet").name == "x.parquet"


class TestRequireDataPath:
    def test_missing_file_names_the_path_and_how_to_fix_it(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FRAMECHECK_DATA_DIR", str(tmp_path))
        with pytest.raises(FileNotFoundError) as excinfo:
            config.require_data_path("snapshots/nope.parquet")
        message = str(excinfo.value)
        assert "nope.parquet" in message
        assert "FRAMECHECK_DATA_DIR" in message

    def test_existing_file_is_returned(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FRAMECHECK_DATA_DIR", str(tmp_path))
        target = tmp_path / "snapshots" / "real.parquet"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"not really parquet")
        assert config.require_data_path("snapshots/real.parquet") == target


def test_every_snapshot_read_goes_through_the_resolver():
    # A new pd.read_parquet(file_path) added later would reintroduce the
    # packaged-app bug silently, so pin the call shape.
    for rel in ("graph/audit.py", "graph/run_analysis.py", "scripts/benchmark.py"):
        source = (config.REPO_ROOT / rel).read_text(encoding="utf-8")
        for line in source.splitlines():
            if "read_parquet(" in line and "require_data_path" not in line:
                raise AssertionError(f"{rel}: raw read_parquet without require_data_path -> {line.strip()}")


def test_every_runtime_data_file_is_bundled_into_the_sidecar():
    """The packaged app reads these off disk at runtime, and PyInstaller
    only ships what it is told to. Each one of these was found the hard
    way: mock_data.json, then benchmarks.json (empty Benchmarks tab), then
    evidence.sql (every audit failed). Pin them so the next addition is
    caught by a test rather than by a user."""
    build_script = (config.REPO_ROOT / "scripts" / "build_backend_sidecar.py").read_text(encoding="utf-8")
    for needed in ("mock_data.json", "benchmarks.json", "evidence.sql"):
        assert needed in build_script, f"{needed} is read at runtime but not bundled by the sidecar build"
