"""Offline public-asset import checks; no Docker, model or network calls."""

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "scripts"),
    str(Path(__file__).resolve().parents[1] / "scripts/native_experiment"),
]
import frozen_suite
import team_environment


@pytest.mark.parametrize("name", ["../outside", "/absolute", "shared/../../outside"])
def test_runtime_path_escape_is_rejected_before_extraction(tmp_path, name):
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo(name)
        member.size = 1
        bundle.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unsafe"):
        frozen_suite.extract(archive, tmp_path)


def test_runtime_symlink_is_rejected(tmp_path):
    archive = tmp_path / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo("shared/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        bundle.addfile(member)
    with pytest.raises(ValueError, match="Unsafe"):
        frozen_suite.extract(archive, tmp_path)


def test_existing_different_cohort_is_not_relabelled(tmp_path, monkeypatch):
    root = tmp_path / "suite"
    seed = root / "seeds/virtio-rng/task.json"
    seed.parent.mkdir(parents=True)
    seed.write_text("{}")
    (root / "config.json").write_text("{}")
    monkeypatch.setattr(team_environment, "verify_suite", lambda *args: None)
    profile = {"suite_release": "local-suite", "suites": {"native": root}}
    with pytest.raises(ValueError, match="different release"):
        frozen_suite.prepare_frozen(profile, "virtio-rng")
    assert json.loads((root / "config.json").read_text()) == {}


def test_corrupt_assets_stop_before_fetch_or_directory_creation(tmp_path, monkeypatch):
    import common

    monkeypatch.setattr(common, "sha", lambda path: "wrong")
    fetched = []
    monkeypatch.setattr(team_environment, "acquire", lambda *args: fetched.append(args))
    root = tmp_path / "suite"
    profile = {"suite_release": "local-suite", "suites": {"native": root}}
    with pytest.raises(ValueError, match="prompt differs"):
        frozen_suite.prepare_frozen(profile, "virtio-rng")
    assert not root.exists()
    assert not fetched
