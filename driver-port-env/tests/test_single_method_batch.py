"""Offline batch boundary: Factory-only sealing never opens a Codex trial."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "experiment_batch", ROOT / "scripts/experiment-batch.py"
)
batch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(batch)


def test_factory_only_seals_selected_candidate(tmp_path, monkeypatch):
    import comparison

    work = tmp_path / "trials/dpf/fixture/trial"
    target = work / "run/target"
    iso = target / "target/osdk/asterinas-osdk-bin.iso"
    iso.parent.mkdir(parents=True)
    iso.write_bytes(b"synthetic ISO")
    (target / "driver.rs").write_text("// synthetic source\n")
    accessed = []

    def trial_path(root, driver, trial, method):
        accessed.append(method)
        assert method == "dpf"
        return work

    monkeypatch.setattr(comparison, "trial_path", trial_path)
    monkeypatch.setitem(
        sys.modules,
        "driver_port_factory.composition",
        SimpleNamespace(open_project=lambda *a, **k: object()),
    )
    monkeypatch.setitem(
        sys.modules,
        "driver_port_factory.acquisition.repository",
        SimpleNamespace(
            load_repository_acquisition=lambda _: SimpleNamespace(
                target_worktree=SimpleNamespace(path=Path("target"))
            )
        ),
    )
    monkeypatch.setattr(
        batch,
        "catalog",
        lambda: {"fixture": ("synthetic", {"drivers": {"fixture": {}}})},
    )
    destination = tmp_path / "candidates.json"
    targets = batch.seal(
        tmp_path, "fixture", "trial", tmp_path, destination, methods=("dpf",)
    )
    manifest = json.loads(destination.read_text())
    assert accessed == ["dpf"]
    assert targets == [target]
    assert [c["method"] for c in manifest["candidates"]] == ["dpf"]
    assert manifest["candidates"][0]["source_files"] == {
        "driver.rs": batch.sha(target / "driver.rs")
    }
