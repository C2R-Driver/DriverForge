"""Offline checks for the new baseline's isolation and accounting boundaries."""

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/native_experiment")]
spec = importlib.util.spec_from_file_location(
    "openhands_experiment", ROOT / "scripts/openhands-experiment.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_prepare_delivers_historical_scope_without_expanded_material(
    tmp_path, monkeypatch
):
    import behavior_scope
    from experiment_config import load_config

    config = load_config(ROOT / "configs/experiments/public-drivers.json")
    task = config["drivers"]["evbug"] | {
        "driver": "evbug",
        "source_root_commit": "source-revision",
        "target_root_commit": "target-revision",
        "environment": config,
        "behavior_scope": {"required": ["EXPANDED_SCOPE_SENTINEL"]},
    }
    root = tmp_path / "suite"
    runner.write(root / "seeds/evbug/task.json", task)
    runner.write(root / "config.json", config)
    runner.write(root / "environment.json", config)
    runner.write(
        root / "operator/evbug/seed-audit.json",
        {"status": "PASS", "root_commit": "target-revision"},
    )
    prices = tmp_path / "prices.json"
    runner.write(prices, {"models": {"test-model": {}}})
    runtime = {
        "bwrap": "/bin/bwrap",
        "python": str(tmp_path / "venv/bin/python"),
        "base": str(tmp_path / "python"),
    }
    monkeypatch.setattr(runner, "doctor", lambda _: runtime)
    monkeypatch.setattr(runner, "selected_suite", lambda *_: root)
    monkeypatch.setattr(runner, "configure_factory", lambda _: None)
    monkeypatch.setattr(
        runner.subprocess, "check_output", lambda *_a, **_k: config["image_id"]
    )
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda argv, **_k: Path(argv[-1]).mkdir(parents=True),
    )
    monkeypatch.setattr(runner, "git", lambda *_: "fixture-revision")
    monkeypatch.setattr(runner, "export", lambda *_: None)
    monkeypatch.setattr(runner, "root_commit", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "setup_assets", lambda *_: None)

    def forbid_registry_read(_):
        raise AssertionError("Historical scope must not import the expanded registry")

    monkeypatch.setattr(behavior_scope, "read", forbid_registry_read)
    output = runner.prepare(
        Namespace(driver="evbug", trial="historical-scope"),
        {
            "factory": tmp_path / "factory",
            "repositories": {"qemu": tmp_path / "qemu"},
            "pricing": prices,
            "model": "test-model",
            "reasoning_effort": "medium",
            "llm": {},
        },
    )
    protocol = runner.read(output / "protocol.json")
    trial = Path(protocol["workspace"])
    prompt = (trial / "PROMPT.md").read_text()
    assert task["scope"] in prompt
    assert "EXPANDED_SCOPE_SENTINEL" not in prompt
    assert "Required behaviors:" not in prompt
    assert "behavior-scope.json" not in prompt
    assert "behavior_scope" not in protocol["task"]
    assert (
        protocol["completion_policy"] == "public-runtime-self-test-before-native-finish"
    )
    assert not (trial / "behavior-scope.json").exists()
    runner.namespace(protocol, output, tmp_path / "broker.sock")
    view = runner.read(output / "namespace.json")
    assert str(trial / "behavior-scope.json") not in view["readonly"]
    assert str(trial / "source") in view["readonly"]
    assert str(trial / "target") in view["writable"]


def test_openhands_docker_version_query_does_not_open_daemon_administration(tmp_path):
    from docker_broker import DockerBroker

    with DockerBroker(
        tmp_path / "broker.sock", "fixed-image", [], [], allow_cli_version=True
    ) as broker:
        assert broker.command(["--version"]) == [broker.docker, "--version"]
        assert broker.command(["-v"]) == [broker.docker, "-v"]
        with pytest.raises(ValueError, match="not a Docker outage"):
            broker.command(["system", "prune", "-a"])
    with (
        DockerBroker(tmp_path / "legacy.sock", "fixed-image", [], []) as broker,
        pytest.raises(ValueError),
    ):
        broker.command(["--version"])


def test_frozen_tool_errors_return_actionable_feedback_without_blocking_native_commands():
    from docker_policy import validated_run

    options = {
        "tool_dirs": ("/image/cargo/bin", "/image/qemu/bin"),
        "accelerator": "kvm",
    }
    with pytest.raises(ValueError, match="Docker PATH hides image-provided tools"):
        validated_run(
            [
                "run",
                "-e",
                "PATH=/experiment-assets/sdk:/usr/bin",
                "image",
                "cargo",
                "build",
            ],
            "image",
            [],
            [],
            **options,
        )
    valid = [
        "run",
        "-e",
        "PATH=/experiment-assets/sdk:/image/cargo/bin:/image/qemu/bin:/usr/bin",
        "image",
        "cargo",
        "build",
    ]
    assert validated_run(valid, "image", [], [], **options) is None
    assert (
        validated_run(
            [
                "run",
                "image",
                "bash",
                "-lc",
                "export PATH=/experiment-assets/sdk:$PATH; cargo osdk build",
            ],
            "image",
            [],
            [],
            **options,
        )
        is None
    )
    for command, message in [
        (
            "apt-get update -qq && apt-get install -y -qq qemu-system-x86",
            "QEMU is already installed",
        ),
        ('cargo osdk run --qemu-args="-machine q35 -accel tcg"', "uses KVM"),
    ]:
        with pytest.raises(ValueError, match=message):
            validated_run(
                ["run", "image", "bash", "-lc", command], "image", [], [], **options
            )
    for command in [
        "apt-get install -y tcpdump",
        'echo "apt-get install qemu-system-x86"',
        'cargo osdk run --qemu-args="-machine q35 -accel kvm"',
        "command -v qemu-system-x86_64",
    ]:
        assert (
            validated_run(
                ["run", "image", "bash", "-lc", command], "image", [], [], **options
            )
            is None
        )
    # Existing Factory/Codex policy stays unchanged without the OpenHands option.
    assert (
        validated_run(
            ["run", "-e", "PATH=/usr/bin", "image", "cargo", "build"], "image", [], []
        )
        is None
    )


def test_actual_qemu_defaults_report_runtime_mismatch_not_environment_outage():
    from docker_policy import qemu_runtime_error

    runtime = {"accelerator": "kvm", "cpus": 1, "memory": "2G"}
    observed = ["qemu-system-x86_64", "-machine", "q35", "-smp", "1", "-m", "8G"]
    error = qemu_runtime_error(observed, runtime)
    assert "add -accel kvm" in error
    assert "use -m 2G" in error
    assert "Docker and QEMU are available" in error
    for args in [
        ["-accel", "kvm", "-smp", "1", "-m", "2G"],
        ["-enable-kvm", "-smp", "cpus=1", "-m", "2048M"],
        ["-machine", "pc,accel=kvm", "-smp", "1", "-m", "size=2G"],
    ]:
        assert qemu_runtime_error(["qemu-system-x86_64", *args], runtime) is None


def test_docker_runtime_check_requires_pid_column(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from docker_broker import DockerBroker

    with DockerBroker(tmp_path / "broker.sock", "image", [], []) as broker:
        broker.runtime = {"accelerator": "kvm", "cpus": 1, "memory": "2G"}
        monkeypatch.setattr(
            runner.subprocess, "check_output", lambda *_a, **_k: "owned-container\n"
        )

        def top(argv, **_kwargs):
            assert argv == [broker.docker, "top", "owned-container", "-eo", "pid,args"]
            return SimpleNamespace(
                stdout="PID COMMAND\n42 bash -lc cargo osdk run\n"
                "43 /usr/local/qemu/bin/qemu-system-x86_64 -m 8G\n"
            )

        monkeypatch.setattr(runner.subprocess, "run", top)
        container, error = broker.runtime_violation()
        assert container == "owned-container"
        assert "add -accel kvm" in error
        assert "use -m 2G" in error


def test_venv_executable_is_not_resolved_to_base_python(tmp_path, monkeypatch):
    base = tmp_path / "base-python"
    base.touch()
    executable = tmp_path / "venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(base)
    config = {
        "openhands_python": str(executable),
        "base_url": "https://example.org/v1",
        "model": "test",
        "reasoning_effort": "medium",
        "llm": {
            "model": "openai/test",
            "base_url": "https://example.org/v1",
            "reasoning_effort": "medium",
        },
    }
    monkeypatch.setattr(runner, "read_profile", lambda _: config)
    assert runner.load(tmp_path / "settings.json")["openhands_python"] == executable


def test_agent_and_condenser_incremental_usage_counted_once(tmp_path):
    home = tmp_path / "model-home"
    home.mkdir()
    agent = {
        "response_id": "a",
        "prompt_tokens": 100,
        "cache_read_tokens": 80,
        "completion_tokens": 10,
        "cache_write_tokens": 0,
        "reasoning_tokens": 7,
    }
    condenser = agent | {
        "response_id": "b",
        "prompt_tokens": 50,
        "cache_read_tokens": 20,
    }
    runner.write(
        home / "metrics.json",
        {
            "agent": {"token_usages": [agent, agent]},
            "condenser": {"token_usages": [condenser]},
        },
    )
    value = runner.usage(tmp_path)
    assert value["input_tokens"] == 150
    assert value["cached_input_tokens"] == 100
    assert value["output_tokens"] == 20  # Reasoning already included, not added twice.
    assert value["requests_with_usage"] == 2


def test_failed_attempt_missing_usage_remains_unknown(tmp_path):
    protocol = {
        "driver": "test",
        "trial": "sample-alpha",
        "model": "test",
        "reasoning_effort": "medium",
        "task": {"tests": ["case"], "benchmarks": []},
        "pricing": {
            "currency": "CNY",
            "models": {"test": {"input": 1, "cached_input": 0.1, "output": 2}},
        },
    }
    runner.write(tmp_path / "protocol.json", protocol)
    runner.write(tmp_path / "run.json", {"state": "ERROR", "exit_code": 1})
    result = runner.collect(tmp_path)
    assert result["cost_estimate"] is None
    assert result["cost_status"] == "unknown"
    assert result["cases_passed"] == 0


def test_worker_view_excludes_answers_and_inherited_credentials(tmp_path):
    trial = tmp_path / "trial"
    output = tmp_path / "output"
    output.mkdir()
    protocol = {
        "workspace": str(trial),
        "root": str(tmp_path / "suite"),
        "qemu": str(tmp_path / "qemu"),
        "llm": {},
        "runtime": {
            "bwrap": "/bin/bwrap",
            "python": str(tmp_path / "venv/bin/python"),
            "base": str(tmp_path / "base"),
        },
    }
    runner.namespace(protocol, output, tmp_path / "docker.sock")
    view = json.loads((output / "namespace.json").read_text())
    assert str(trial / "target") in view["writable"]
    assert str(trial / "source") in view["readonly"]
    paths = view["readonly"] + view["writable"]
    assert str(tmp_path / "suite/operator") not in paths
    assert str(ROOT) not in paths
    assert "/home/anonymous/.codex" not in paths
    assert "/var/run/docker.sock" not in paths
    environment = runner.worker_environment("fixture-only")
    assert set(environment) <= {
        "PATH",
        "LANG",
        "C2R_OPENHANDS_API_KEY",
        "LITELLM_LOCAL_MODEL_COST_MAP",
        "DO_NOT_TRACK",
        "OPENHANDS_SUPPRESS_BANNER",
    }


def test_private_results_are_separate_from_public_cases(tmp_path):
    runner.write(
        tmp_path / "protocol.json",
        {
            "driver": "test",
            "trial": "sample-alpha",
            "model": "test",
            "reasoning_effort": "medium",
            "task": {"tests": ["public-case"], "benchmarks": []},
            "pricing": {
                "models": {"test": {"input": 1, "cached_input": 0.1, "output": 2}}
            },
        },
    )
    runner.write(tmp_path / "run.json", {"state": "FINISHED", "exit_code": 0})
    runner.write(
        tmp_path / "private/test/openhands/summary.json",
        [
            {"variant": "A", "status": "PASS"},
            {"variant": "B", "status": "FAIL"},
        ],
    )
    result = runner.collect(tmp_path)
    assert result["private_A_passed"] == 1 and result["private_A_observed"] == 1
    assert result["private_B_passed"] == 0 and result["private_B_observed"] == 1
    assert (
        result["cases_passed"] == 0
    )  # No public receipt, private PASS cannot replace it.
