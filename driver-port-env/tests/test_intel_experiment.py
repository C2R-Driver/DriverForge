"""Offline configuration and admission checks for new Intel tasks."""

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/native_experiment")]
from experiment_config import load_config, network_required
from public_prepare import freeze_tests, runtime_profile
from team_profile import catalog


def test_new_family_uses_complete_source_pin_and_same_original_target():
    old = load_config(ROOT / "configs/experiments/native-drivers.json")
    for driver in ("igb", "e1000e"):
        group, config = catalog()[driver]
        assert group == "intel"
        assert config["target_revision"] == old["target_revision"]
        assert config["image_id"] == old["image_id"]
        assert config["source_revision"] == "LOCAL_INPUT_REVISION"
        task = config["drivers"][driver]
        assert task["suite_kind"] == "fixed-public-fixture"
        assert task["runtime_script"] == "intel_runtime.py"
        assert "-accel" in runtime_profile(task)["qemu_args"]
        assets = ROOT / "src/driver_port_factory/data/public-tests" / driver
        rows = json.loads((assets / "cases.json").read_text())
        assert [r["id"] for r in rows] == [*task["tests"], *task["benchmarks"]]
        assert len(rows) == 6


def test_declared_network_route_passes_tun_to_factory(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT.parent / "driver-port-factory/src"))
    from driver_port_factory.platform import native_runner, profile

    calls = []
    monkeypatch.setattr(
        native_runner,
        "container_argv",
        lambda *args, **kwargs: calls.append(kwargs) or [],
    )
    for driver, task, expected in [
        ("igb", {"network_required": True}, True),
        ("evbug", {}, False),
        ("virtio-net", {"benchmarks": ["iperf3/tcp_virtio_bw"]}, True),
    ]:
        profile.container_argv(
            {
                "native": {
                    "environment": {},
                    "root": tmp_path,
                    "driver": driver,
                    "task": task,
                }
            },
            tmp_path,
            "offline-only",
            [],
            build=False,
            cache_key="fixture",
        )
        assert calls[-1]["network"] == expected


def test_intel_scopes_are_accepted_by_factory_before_paid_launch():
    sys.path.insert(0, str(ROOT.parent / "driver-port-factory/src"))
    from driver_port_factory.intake.behavior_scope import validate

    data = json.loads((ROOT / "configs/experiments/behavior-scopes.json").read_text())
    for name in ["igb", "e1000e"]:
        validate(data["drivers"][name])


def test_shared_runtime_applies_to_every_catalog_group_and_new_nic_profile():
    common = load_config(ROOT / "configs/experiments/common-runtime.json")
    for group in ("native", "public", "intel"):
        config = load_config(ROOT / f"configs/experiments/{group}-drivers.json")
        assert all(config[key] == value for key, value in common.items())
    configs = catalog()
    profiles = []
    for driver in ("igb", "e1000e"):
        config = configs[driver][1]
        task = config["drivers"][driver]
        profile = runtime_profile(task, config)
        # Only the declared device arguments vary, not machine/CPU/firmware.
        profiles.append(profile["qemu_args"][: -len(task["runtime_qemu_args"])])
    assert profiles[0] == profiles[1]


def test_common_traffic_cases_keep_each_tasks_device_profile(tmp_path):
    config = load_config(ROOT / "configs/experiments/intel-drivers.json")
    assets = ROOT / "src/driver_port_factory/data/public-tests"
    freeze_tests(tmp_path, config, assets)
    index = json.loads((tmp_path / "shared/public-tests/index.json").read_text())
    assert len(index) == 14
    for driver, task in config["drivers"].items():
        for case in [*task["tests"], *task["benchmarks"], task["boot_case"]]:
            args = index[driver + "/" + case]["profile"]["qemu_args"]
            assert args[-1] == task["qemu_model"]


def test_network_capability_does_not_depend_on_driver_name():
    assert network_required({"network_required": True})
    assert network_required({"benchmarks": ["iperf3/example"]})
    assert not network_required({"tests": ["probe"]})


def test_private_scoring_routes_an_arbitrary_task_by_metadata(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "configured_scorer", ROOT / "scripts/scoring/score.py"
    )
    scorer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scorer)
    source, suite = tmp_path / "source", tmp_path / "suite"
    for relative in ("tools", "test/initramfs/src/benchmark", "target/osdk"):
        (source / relative).mkdir(parents=True)
    (source / ".native-task.json").write_text('{"driver":"arbitrary-nic"}')
    iso = source / "target/osdk/asterinas-osdk-bin.iso"
    iso.write_bytes(b"synthetic offline artifact; not a bootable kernel")
    (suite / "shared").mkdir(parents=True)
    commands = []
    monkeypatch.setattr(scorer, "media", lambda *a: None)
    monkeypatch.setattr(scorer, "docker", lambda *a, **kw: commands.append((a[5], kw)))
    scoring = {
        "script": "scripts/scoring/example.py",
        "network_required": True,
        "copy_paths": ["test/initramfs/src/benchmark", ".native-task.json"],
    }
    candidate = {
        "driver": "arbitrary-nic",
        "method": "dpf",
        "target": str(source),
        "suite": str(suite),
        "iso": str(iso),
        "iso_sha256": scorer.sha(iso),
        "source_files": {},
        "private_scoring": scoring,
    }
    (tmp_path / "scratch").mkdir()
    scorer.run_candidate(Namespace(), candidate, tmp_path / "scratch", tmp_path / "out")
    assert commands == [
        (
            ["python3", "/env/scripts/scoring/example.py", "arbitrary-nic"],
            {"network": True},
        )
    ]
