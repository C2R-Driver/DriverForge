"""Offline disclosure/collector parity; synthetic artifacts, no models or QEMU."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts/native_experiment"), str(ROOT / "scripts")]
from baseline_prompt import render
from experiment_config import load_config
from public_prepare import freeze_tests


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def published(tmp_path):
    config = load_config(ROOT / "configs/experiments/intel-drivers.json")
    freeze_tests(tmp_path, config, ROOT / "src/driver_port_factory/data/public-tests")
    return tmp_path, config


@pytest.mark.parametrize("driver", ["igb", "e1000e"])
def test_full_definition_reaches_both_methods_and_baseline_prompt(published, driver):
    root, config = published
    (root / "config.json").write_text(json.dumps(config))
    fixture = load_module(
        "disclosure_fixture",
        ROOT / "src/driver_port_factory/platform/public_fixture.py",
    )
    files = fixture.prepared_files(root, driver)
    interface = (root / "shared/public-tests" / driver / "INTERFACE.md").read_text()
    # These are the files inserted into Factory; baseline's seed copies the same asset.
    assert files[".dpf-output/harness/public/INTERFACE.md"] == interface
    index = json.loads((root / "shared/public-tests/index.json").read_text())
    for case in [
        *config["drivers"][driver]["tests"],
        *config["drivers"][driver]["benchmarks"],
    ]:
        assert index[driver + "/" + case]["case"]["acceptance"]["version"] == 2
    task = config["drivers"][driver] | {
        "driver": driver,
        "environment": config,
    }
    prompt = render(task, root / "trial", root / "qemu", config, root / "shared")
    assert "each case's acceptance object" in prompt
    assert "You may execute these public procedures" in prompt
    assert "No\nFactory-written build.sh, check.sh, test runner" in prompt
    assert f"QEMU machine: `{config['machine']}`" in prompt


@pytest.mark.parametrize(
    "driver,case",
    [
        (driver, case)
        for driver in ("igb", "e1000e")
        for case in (
            driver + "-probe",
            driver + "-recovery",
            driver + ("-rss" if driver == "igb" else "-diagnostics"),
            "iperf3/tcp_virtio_bw",
            "iperf3/udp_virtio_bw_rx",
            "iperf3/udp_virtio_bw_tx",
        )
    ],
)
def test_published_procedure_matches_actual_collector(
    published, monkeypatch, driver, case
):
    root, _ = published
    contract = json.loads((root / "shared/public-tests/index.json").read_text())[
        driver + "/" + case
    ]["case"]["acceptance"]
    runtime, observed, artifact = configured_runtime(root, monkeypatch, driver)
    assert runtime.execute(case, str(artifact), str(root / "output")) == 0
    assert observed["timeout"] == contract["guest_boot"]["timeout_seconds"]
    assert observed["asserts"][0] == contract["guest_boot"]["guest_assert"]
    if fixture := contract.get("fixture"):
        assert observed["asserts"][1] == fixture["guest_assert"]
        if "wait_serial" in fixture:
            assert fixture["wait_serial"] in observed["waits"]
    if traffic := contract.get("traffic"):
        assert observed["host_command"] == traffic["host_command"]
        assert observed["host_environment"]["GUEST_SERVER_IP_ADDRESS"] == "10.0.2.15"
        assert observed["asserts"][-1] == traffic["guest_assert"]
        assert observed["send"] == traffic["guest_command"] + "\n"
        assert traffic["wait_serial"] in observed["waits"]
        assert observed["settle"] == traffic["settle_seconds"]
    else:
        assert "host_command" not in observed
    if trace := contract.get("qemu_trace"):
        assert observed["argv"][-4:] == trace["args"]
        result = json.loads((root / "output/result.json").read_text())
        assert result["hardware_rx_queues"] == trace["require_rx_queues"]


@pytest.mark.parametrize(
    "trace,host_log,exit_code",
    [
        ("Received packet written to guest (queue 0)\nigb_rx_metadata_rss", None, 0),
        (
            "Received packet written to guest (queue 0)\nReceived packet written to guest (queue 1)",
            None,
            0,
        ),
        (None, "2 Mbits/sec sender\n0 Mbits/sec receiver\n", 0),
        (None, "2 Mbits/sec sender\n1 Mbits/sec receiver\n", 1),
    ],
)
def test_rss_needs_workload_and_trace_jointly(
    published, monkeypatch, trace, host_log, exit_code
):
    root, _ = published
    runtime, _, artifact = configured_runtime(
        root, monkeypatch, "igb", trace=trace, host_log=host_log, exit_code=exit_code
    )
    assert runtime.execute("igb-rss", str(artifact), str(root / "output")) == 1
    assert json.loads((root / "output/result.json").read_text())["status"] == "FAIL"


def configured_runtime(
    root, monkeypatch, driver, *, trace=None, host_log=None, exit_code=0
):
    platform = ROOT.parent / "driver-port-factory/src/driver_port_factory/platform"
    monkeypatch.syspath_prepend(str(platform))
    runtime = load_module(
        "disclosure_runtime", ROOT / "scripts/native_experiment/intel_runtime.py"
    )
    target = root / "target"
    target.mkdir()
    (target / ".native-task.json").write_text(json.dumps({"driver": driver}))
    artifact = target / "synthetic.iso"
    artifact.write_bytes(b"synthetic fixture; not a bootable kernel")
    benchmark = target / "test/initramfs/src/benchmark/iperf3"
    commands = {
        "tcp_virtio_bw": "iperf3 -c $GUEST_SERVER_IP_ADDRESS -f m",
        "udp_virtio_bw_rx": "iperf3 -u -b 0 -l 1460 -c $GUEST_SERVER_IP_ADDRESS -f m",
        "udp_virtio_bw_tx": "iperf3 -R -u -b 0 -l 1460 -c $GUEST_SERVER_IP_ADDRESS -f m",
    }
    for case, command in commands.items():
        directory = benchmark / case
        directory.mkdir(parents=True)
        (directory / "host.sh").write_text(command + "\n")
    observed = {"asserts": [], "waits": []}

    class Guest:
        def __init__(self, argv, output, timeout, **kwargs):
            observed.update(argv=argv, timeout=timeout)
            (output / "qemu-stderr.log").write_text(
                trace
                if trace is not None
                else "Received packet written to guest (queue 0)\n"
                "Received packet written to guest (queue 1)\nigb_rx_metadata_rss\n"
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def assert_guest(self, command):
            observed["asserts"].append(command)

        def wait_serial(self, marker):
            observed["waits"].append(marker)

        def send(self, command):
            observed["send"] = command

        def observe(self, seconds):
            observed["settle"] = seconds

    class Host:
        returncode = exit_code

        def __init__(self, argv, stdout, env, **kwargs):
            observed.update(host_command=argv[-1], host_environment=env)
            stdout.write(
                host_log
                if host_log is not None
                else "2 Mbits/sec sender\n1 Mbits/sec receiver\n"
            )
            stdout.flush()

        def poll(self):
            return self.returncode

        def wait(self, **kwargs):
            return self.returncode

    monkeypatch.setattr(runtime, "ROOT", target)
    monkeypatch.setattr(runtime, "Guest", Guest)
    monkeypatch.setattr(runtime.subprocess, "Popen", Host)
    monkeypatch.setattr(runtime.os, "chdir", lambda _: None)
    monkeypatch.setattr(
        runtime,
        "Path",
        lambda path: (
            root / "shared/public-tests/index.json"
            if str(path) == "/dpf-shared/public-tests/index.json"
            else Path(path)
        ),
    )
    return runtime, observed, artifact
