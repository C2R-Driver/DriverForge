"""Offline regression for scope loss between protocol, launch and isolated workers."""

import json
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts/native_experiment"), str(ROOT / "src")]

import behavior_scope
import launch
import worker_isolation
from experiment_config import load_config


def task_for(driver):
    name = "public" if driver in {"evbug", "ne2k-pci"} else "native"
    config = load_config(ROOT / f"configs/experiments/{name}-drivers.json")
    task = config["drivers"][driver] | {
        "driver": driver,
        "source_root_commit": config["source_revision"],
        "target_root_commit": config["target_revision"],
        "environment": {
            "image_id": config["image_id"],
            "qemu_evidence_revision": config["qemu_revision"],
            **config,
        },
    }
    return behavior_scope.scoped_task(task), config


@pytest.mark.parametrize("driver", ["nvme-pci", "virtio-net", "ne2k-pci"])
def test_both_launches_deliver_the_same_frozen_scope(tmp_path, monkeypatch, driver):
    task, config = task_for(driver)
    monkeypatch.setattr(launch, "git", lambda *args: "")
    monkeypatch.setattr(launch, "read", lambda path: config)
    factory = tmp_path / "factory"
    (factory / "scripts").mkdir(parents=True)
    (factory / "scripts/run-experiment.sh").touch()
    root = tmp_path / "suite"
    for method in ["dpf", "codex"]:
        trial = tmp_path / method
        trial.mkdir()
        launch.prepare_launch(root, driver, method, trial, tmp_path, factory, task=task)
        assert (
            json.loads((trial / "behavior-scope.json").read_text())
            == task["behavior_scope"]
        )
    args = shlex.split((tmp_path / "dpf/launch.sh").read_text().splitlines()[-1])
    scope_file = Path(args[args.index("--behavior-scope") + 1])
    assert scope_file == tmp_path / "dpf/behavior-scope.json"
    prompt = (tmp_path / "codex/PROMPT.md").read_text()
    for item in task["behavior_scope"]["required"] + task["behavior_scope"]["excluded"]:
        assert item in prompt
    assert str(tmp_path / "codex/behavior-scope.json") in prompt


@pytest.mark.parametrize("method", ["dpf", "codex"])
def test_scope_file_is_visible_readonly_in_worker_namespace(
    tmp_path, monkeypatch, method
):
    monkeypatch.setattr(
        worker_isolation, "codex_paths", lambda _: (tmp_path / "codex", [])
    )
    trial = tmp_path / "trial"
    trial.mkdir()
    directory = tmp_path / "record"
    directory.mkdir()
    protocol = {
        "factory": str(tmp_path / "factory"),
        "root": str(tmp_path / "suite"),
        "qemu": str(tmp_path / "qemu"),
        "codex_bin": "codex",
        "driver": "nvme-pci",
        "task": {"behavior_scope": task_for("nvme-pci")[0]["behavior_scope"]},
    }
    manifest = worker_isolation.prepare_view(
        protocol, trial, directory, method, tmp_path / "bwrap", tmp_path / "broker"
    )
    scope_file = str(trial / "behavior-scope.json")
    assert scope_file in manifest["readonly"]
    assert scope_file not in manifest["writable"]


def test_frozen_task_scope_survives_registry_change_and_unknown_driver():
    task, _ = task_for("nvme-pci")
    custom = dict(task, driver="new-driver")
    frozen = behavior_scope.scoped_task(custom)
    assert frozen["behavior_scope"] == task["behavior_scope"]
    frozen["behavior_scope"]["required"].append("new local item")
    assert frozen["behavior_scope"] != task["behavior_scope"]
    with pytest.raises(ValueError, match="Configure a shared behavior scope"):
        behavior_scope.scoped_task({"driver": "unconfigured-driver"})
