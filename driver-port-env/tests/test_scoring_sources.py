"""Offline regression for Git worktree metadata in private scoring scratch."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "scoring_sources",
    Path(__file__).resolve().parents[1] / "scripts/scoring/score.py",
)
scoring = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scoring)


def test_scratch_omits_git_but_checks_candidate_sources(tmp_path):
    source = tmp_path / "driver.rs"
    source.write_text("sealed driver")
    candidate = {
        "source_files": {
            ".git": "original-worktree-pointer",
            "driver.rs": scoring.sha(source),
            "fixture.rs": "operator-owned-fixture",
        }
    }
    scoring.verify_copied_sources(candidate, tmp_path, "fixture.rs")
    source.write_text("changed driver")
    with pytest.raises(AssertionError, match="driver.rs"):
        scoring.verify_copied_sources(candidate, tmp_path, "fixture.rs")
