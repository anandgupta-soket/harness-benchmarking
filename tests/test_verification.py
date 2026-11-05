from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bench import verification


def test_rejects_symlink_but_ignores_candidate_tests(tmp_path: Path) -> None:
    submit = tmp_path / "submission"
    shutil.copytree(verification.FIXTURES_DIR / "mini-calc-buggy", submit)
    (submit / "src/lib.rs").unlink()
    (submit / "src/lib.rs").symlink_to(Path("/etc/passwd"))
    with pytest.raises(ValueError, match="symlink"):
        verification._safe_overlay("BUG-01", submit, tmp_path / "stage")

    (submit / "src/lib.rs").unlink()
    shutil.copyfile(verification.FIXTURES_DIR / "mini-calc-buggy/src/lib.rs", submit / "src/lib.rs")
    (submit / "tests").mkdir()
    (submit / "tests/fake.rs").write_text("#[test] fn fake() {}")
    stage = tmp_path / "stage"
    # A rejected overlay may already have copied earlier regular files before
    # directory traversal encounters the symlink. Start the next assertion
    # from a clean destination instead of depending on filesystem traversal order.
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(verification.FIXTURES_DIR / "mini-calc-buggy", stage)
    verification._safe_overlay("BUG-01", submit, stage)
    assert not (stage / "tests/fake.rs").exists()


def test_accepts_new_source_helpers_but_rejects_deleted_production_files(tmp_path: Path) -> None:
    fixture = verification.FIXTURES_DIR / "mini-calc"
    submit = tmp_path / "submission"
    shutil.copytree(fixture, submit)
    (submit / "src/helper.rs").write_text("pub fn identity(x: i32) -> i32 { x }\n")
    stage = tmp_path / "stage"
    shutil.copytree(fixture, stage)
    verification._safe_overlay("REF-01", submit, stage)
    assert (stage / "src/helper.rs").is_file()
    (submit / "src/main.rs").unlink()
    with pytest.raises(ValueError, match="deleted trusted production files"):
        verification._safe_overlay("REF-01", submit, stage)


def test_verifier_uses_pristine_tests_not_submission_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    submit = tmp_path / "submission"
    shutil.copytree(verification.FIXTURES_DIR / "mini-web", submit)
    (submit / "js/todo.js").write_text("// intentionally incorrect\n")
    (submit / "tests/remaining.test.js").write_text("process.exit(0)\n")
    result = verification.verify("WEB-BUG-01", submit, tmp_path / "out")
    assert result["passed"] is False
    assert (tmp_path / "out/verification.json").exists()


@pytest.mark.parametrize("task_id,fixture", [("BUG-01", "mini-calc-buggy"), ("AGENT-01", "mini-calc-buggy")])
def test_rust_baseline_is_a_negative_control(tmp_path: Path, task_id: str, fixture: str) -> None:
    # Empty overlay means the checked-in, intentionally broken source is tested.
    submission = tmp_path / task_id
    submission.mkdir()
    result = verification.verify(task_id, submission, tmp_path / f"out-{task_id}")
    assert result["passed"] is False
