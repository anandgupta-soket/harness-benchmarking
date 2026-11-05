"""Independent regression: inspect decoded archives, not compressed bytes."""
import tarfile

from .test_worker import FakeCg, SECRET, fake_build, params
from bench.worker import run_trial


def test_credential_written_into_workspace_is_not_exported_in_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_API_KEY", SECRET)
    script = """
import os, pathlib
pathlib.Path('src/lib.rs').write_text(os.environ['BENCH_API_KEY'])
"""
    result = run_trial(params(tmp_path), cg=FakeCg(), build=fake_build(script))
    assert not result.get("worker_error"), result
    archive = tmp_path / "out" / "workspace-full.tar.gz"
    with tarfile.open(archive) as tf:
        files = [m for m in tf.getmembers() if m.isfile()]
        assert files
        for member in files:
            assert SECRET.encode() not in tf.extractfile(member).read(), member.name
    assert SECRET not in (tmp_path / "out" / "submission" / "src" / "lib.rs").read_text()
