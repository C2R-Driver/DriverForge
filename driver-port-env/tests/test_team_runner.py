"""Offline boundary checks; no Docker, kernel build, network or paid model calls."""

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "scripts"),
    str(ROOT / "scripts/native_experiment"),
    str(ROOT / "src"),
]

import comparison
import experiment
import prepare
import team_environment
import team_profile


def test_legacy_suite_uses_its_frozen_task_image_without_relabeling(
    monkeypatch, tmp_path
):
    import common

    from driver_port_factory.platform import native_runner

    config = {
        "source_revision": "source",
        "target_revision": "target",
        "qemu_revision": "qemu",
    }
    env = {
        "image_id": "frozen-image",
        "source_revision": "source",
        "target_revision": "target",
        "qemu_evidence_revision": "qemu",
        "sdk_sha256": "hash",
        "initramfs_sha256": "hash",
        "runtime_identity": "runtime",
    }
    task = {
        "environment": {"image_id": "frozen-image"},
        "source_scope": "complete-linux-tree",
        "source_root_commit": "source",
        "target_root_commit": "seed",
    }
    documents = {
        "config.json": config,
        "environment.json": env,
        "task.json": task,
        "seed-audit.json": {"status": "PASS", "root_commit": "seed"},
    }
    monkeypatch.setattr(common, "read", lambda path: documents[path.name])
    monkeypatch.setattr(common, "sha", lambda path: "hash")
    monkeypatch.setattr(native_runner, "runtime_identity", lambda: "runtime")
    monkeypatch.setattr(team_environment, "selected_suite", lambda *args: tmp_path)
    monkeypatch.setattr(team_environment, "command", lambda *args, **kwargs: None)
    monkeypatch.setattr(team_environment, "git_has", lambda *args: True)
    profile = {"factory": tmp_path, "repositories": {"qemu": tmp_path}}
    team_environment.verify_suite(profile, "driver")
    assert "image_id" not in config
    config["image_id"] = "different-image"
    with pytest.raises(ValueError, match="image_id"):
        team_environment.verify_suite(profile, "driver")


def test_provider_catalog_and_requested_effort_are_shared():
    protocol = {
        "provider": {"name": "deepseek", "base_url": "https://api.deepseek.com"},
        "reasoning_effort": "medium",
        "compact_token_limit": 224000,
        "model_catalog_json": "/frozen/model-catalog.json",
    }
    args = comparison.settings(protocol)
    config = {v.split("=", 1)[0]: json.loads(v.split("=", 1)[1]) for v in args[1::2]}
    assert config["model_provider"] == "deepseek"
    assert config["model_providers.deepseek.base_url"] == "https://api.deepseek.com"
    assert config["model_reasoning_effort"] == "medium"
    assert config["model_catalog_json"] == "/frozen/model-catalog.json"
    assert config["model_reasoning_summary"] == "none"
    assert not any(k.startswith("model_providers.example-provider.") for k in config)


def test_profile_credentials_and_paths(tmp_path, monkeypatch):
    path = tmp_path / "team.json"
    team_profile.initialize(path)
    config = team_profile.read_profile(path)
    assert config["factory"].is_absolute()
    key_file = tmp_path / "secret"
    key_file.write_text("example-only-secret\n")
    key_file.chmod(0o600)
    config["api_key_file"] = key_file
    monkeypatch.delenv(config["api_key_env"], raising=False)
    assert (
        team_profile.model_environment(config)["example-provider_API_KEY"]
        == "example-only-secret"
    )
    assert "example-only-secret" not in path.read_text()
    with pytest.raises(ValueError, match="exists"):
        team_profile.initialize(path)
    key_file.chmod(0o644)
    with pytest.raises(ValueError, match="600"):
        team_profile.model_environment(config)


@pytest.mark.parametrize(
    "url", ["https://user:secret@relay/v1", "https://relay/v1?key=secret", "file:///x"]
)
def test_secret_bearing_url_rejected(url):
    with pytest.raises(ValueError):
        team_profile.validate_url(url)


def test_prepare_single_method_and_freeze_configuration(tmp_path, monkeypatch):
    root = tmp_path / "suite"
    task = root / "seeds/virtio-rng/task.json"
    task.parent.mkdir(parents=True)
    task.write_text("{}")
    pricing = tmp_path / "rates.json"
    pricing.write_text(
        json.dumps(
            {"models": {"test-model": {"input": 1, "cached_input": 0.1, "output": 2}}}
        )
    )
    created = []
    monkeypatch.setattr(comparison, "configure_factory", lambda _: None)
    monkeypatch.setattr(
        comparison.subprocess, "check_output", lambda *a, **k: "fixture-version\n"
    )
    monkeypatch.setattr(
        prepare, "create_trial", lambda *args, **kwargs: created.append(args[2])
    )
    monkeypatch.setenv("example-provider_API_KEY", "example-only-secret")
    args = argparse.Namespace(
        root=root,
        driver="virtio-rng",
        trial="paper-01",
        factory=tmp_path,
        qemu=tmp_path,
        pricing=pricing,
        model="test-model",
        reasoning_effort="medium",
        codex_bin="codex",
        methods=["codex"],
        base_url="https://example.org/v1",
        max_cost_usd=None,
        max_seconds=None,
    )
    comparison.prepare(args)
    protocol_file = root / "comparisons/paper-01/virtio-rng/protocol.json"
    protocol = json.loads(protocol_file.read_text())
    assert created == ["codex"]
    assert protocol["methods"] == ["codex"]
    assert protocol["provider"]["base_url"] == args.base_url
    assert protocol["model"] == args.model
    assert protocol["limits"] == {"usd": None, "seconds": None}
    assert "example-only-secret" not in protocol_file.read_text()
    with pytest.raises(ValueError, match="already exists"):
        comparison.prepare(args)
    with pytest.raises(ValueError, match="not prepared"):
        comparison.execute(argparse.Namespace(protocol=protocol_file, method="dpf"))
    assert not (protocol_file.parent / "dpf").exists()


def test_prepared_environment_reused_without_fetch(tmp_path, monkeypatch):
    task = tmp_path / "seeds/evbug/task.json"
    task.parent.mkdir(parents=True)
    task.write_text("{}")
    verified = []
    monkeypatch.setattr(team_environment, "selected_suite", lambda *a: tmp_path)
    monkeypatch.setattr(
        team_environment, "verify_suite", lambda *a: verified.append(a[1])
    )
    monkeypatch.setattr(
        team_environment,
        "acquire",
        lambda *a: pytest.fail("Unexpected repository download"),
    )
    team_environment.prepare_environment({}, "evbug")
    assert verified == ["evbug"]


def test_run_environment_failure_does_not_prepare_or_launch(monkeypatch):
    monkeypatch.setattr(experiment, "doctor", lambda *a: False)
    monkeypatch.setattr(
        experiment,
        "prepare_run",
        lambda *a: pytest.fail("Prepared after failed environment"),
    )
    monkeypatch.setattr(
        experiment.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("Started paid model"),
    )
    with pytest.raises(ValueError, match="no model started"):
        experiment.run({}, argparse.Namespace(driver="evbug"))


def test_storage_denial_happens_before_workspace_preparation_or_paid_launch(
    tmp_path, monkeypatch
):
    @contextmanager
    def deny(*args):
        raise ValueError("Storage admission denied")
        yield  # A context manager fixture; the body must never execute.

    monkeypatch.setattr(experiment, "doctor", lambda *args: True)
    monkeypatch.setattr(experiment, "selected_suite", lambda *args: tmp_path)
    monkeypatch.setattr(experiment, "reserve", deny)
    monkeypatch.setattr(
        experiment, "run_reserved", lambda *args: pytest.fail("Started after denial")
    )
    with pytest.raises(ValueError, match="Storage admission denied"):
        experiment.run(
            {}, argparse.Namespace(driver="evbug", trial="fixture", method="factory")
        )


def test_frozen_model_override_rejected_before_launch(tmp_path, monkeypatch):
    path = tmp_path / "protocol.json"
    path.write_text(
        json.dumps(
            {
                "methods": ["codex"],
                "model": "original",
                "reasoning_effort": "medium",
                "provider": {"base_url": "https://example.org/v1"},
                "limits": {"usd": None, "seconds": None},
            }
        )
    )
    monkeypatch.setattr(experiment, "doctor", lambda *a: True)
    monkeypatch.setattr(experiment, "model_environment", lambda *a: os.environ.copy())
    monkeypatch.setattr(experiment, "protocol_path", lambda *a: path)
    monkeypatch.setattr(
        experiment.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("Started with changed protocol"),
    )
    args = argparse.Namespace(
        driver="evbug",
        method="codex",
        model="changed",
        reasoning_effort=None,
        base_url=None,
        pricing=None,
        max_cost_usd=None,
        max_seconds=None,
    )
    with pytest.raises(ValueError, match="frozen protocol"):
        experiment.run_reserved({}, args)


def test_trial_cannot_escape_suite():
    with pytest.raises(argparse.ArgumentTypeError):
        experiment.trial_id("../../other")
