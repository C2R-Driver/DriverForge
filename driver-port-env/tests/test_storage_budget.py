"""Offline storage admission regressions; no Docker, model or kernel builds."""

import json
import multiprocessing
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import storage_budget as budget


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv(budget.TOKEN_ENV, raising=False)
    root = budget.state_root()
    root.mkdir(parents=True)
    policy = {
        "max_used_bytes": 100 * budget.GIB,
        "experiment_bytes": 16 * budget.GIB,
        "safety_bytes": 8 * budget.GIB,
    }
    budget.write(root / "policy.json", policy)
    monkeypatch.setattr(
        budget.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(
            total=1000 * budget.GIB, used=60 * budget.GIB, free=940 * budget.GIB
        ),
    )
    return tmp_path, root


def test_virtual_capacity_does_not_allow_second_parallel_experiment(setup, monkeypatch):
    workspace, root = setup
    with budget.reserve(workspace, "first"):
        monkeypatch.delenv(budget.TOKEN_ENV)
        with budget.reserve(workspace, "second"):
            monkeypatch.delenv(budget.TOKEN_ENV)
            with (
                pytest.raises(ValueError, match="Storage admission denied"),
                budget.reserve(workspace, "third"),
            ):
                pytest.fail("Started with exhausted reservation budget")
            state = json.loads((root / "reservations.json").read_text())
            assert sum(r["bytes"] for r in state["leases"].values()) == 32 * budget.GIB
    assert json.loads((root / "reservations.json").read_text())["leases"] == {}


def test_nested_worker_shares_batch_reservation_and_failure_releases(setup):
    workspace, root = setup
    with (
        pytest.raises(RuntimeError),
        budget.reserve(workspace, "pair incl private scoring", 2) as token,
    ):
        with budget.reserve(workspace, "method") as child:
            assert token == child
            lease = json.loads((root / "reservations.json").read_text())["leases"][
                token
            ]
            assert lease["bytes"] == 32 * budget.GIB and len(lease["owners"]) == 2
        raise RuntimeError("synthetic launcher failure")
    assert budget.TOKEN_ENV not in os.environ
    assert json.loads((root / "reservations.json").read_text())["leases"] == {}


def test_reboot_and_pid_reuse_reclaim_but_live_worker_is_retained(setup):
    _workspace, root = setup
    live = budget.identity()
    old = live | {"boot": "previous boot"}
    reused = live | {"start": "wrong start time"}
    state = {
        "leases": {
            "dead": {"owners": [old]},
            "reused": {"owners": [reused]},
            "worker": {"owners": [old, live]},
        }
    }
    budget.write(root / "reservations.json", state)
    with budget.locked() as (_, current):
        assert set(current["leases"]) == {"worker"}
        assert current["leases"]["worker"]["owners"] == [live]


def test_wsl_without_fixed_ceiling_refuses_virtual_df_capacity(setup, monkeypatch):
    workspace, root = setup
    (root / "policy.json").unlink()
    monkeypatch.setattr(budget.platform, "release", lambda: "microsoft-standard-WSL2")
    with (
        pytest.raises(ValueError, match="WSL storage ceiling is not configured"),
        budget.reserve(workspace, "fixture"),
    ):
        pytest.fail("Started without a no-growth ceiling")


def test_denial_uses_physical_filesystem_free_space_as_well(setup, monkeypatch):
    workspace, _ = setup
    monkeypatch.setattr(
        budget.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(
            total=100 * budget.GIB, used=60 * budget.GIB, free=10 * budget.GIB
        ),
    )
    with (
        pytest.raises(ValueError, match="Storage admission denied"),
        budget.reserve(workspace, "fixture"),
    ):
        pytest.fail("Started on a full filesystem")


def contender(workspace, queue, release):
    try:
        with budget.reserve(workspace, "concurrent fixture"):
            queue.put("ADMITTED")
            release.wait(10)
    except ValueError:
        queue.put("DENIED")


def test_parallel_admission_is_atomic_under_global_lock(setup):
    workspace, root = setup
    policy = json.loads((root / "policy.json").read_text())
    policy["max_used_bytes"] = (
        84 * budget.GIB
    )  # Exactly one 16 GiB lease + 8 GiB margin.
    budget.write(root / "policy.json", policy)
    ctx = multiprocessing.get_context("fork")
    queue, release = ctx.Queue(), ctx.Event()
    processes = [
        ctx.Process(target=contender, args=(workspace, queue, release))
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        assert sorted([queue.get(timeout=10), queue.get(timeout=10)]) == [
            "ADMITTED",
            "DENIED",
        ]
    finally:
        release.set()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    assert json.loads((root / "reservations.json").read_text())["leases"] == {}
