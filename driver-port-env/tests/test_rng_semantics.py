"""Offline checks of packaging, environment boundaries and verdict parsing."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "rng_semantics",
    Path(__file__).resolve().parents[1] / "scripts/scoring/rng_semantics.py",
)
rng = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rng)


def names(raw):
    found = []
    pos = 0
    while pos < len(raw):
        head = raw[pos : pos + 110]
        size, length = int(head[54:62], 16), int(head[94:102], 16)
        name = raw[pos + 110 : pos + 110 + length - 1].decode()
        found.append(name)
        if name == "TRAILER!!!":
            break
        pos = (((pos + 110 + length + 3) & ~3) + size + 3) & ~3
    return found


def test_probe_precedes_single_trailer_and_preserves_existing_entries():
    existing = rng.entry("test/original", b"existing")
    raw = existing + rng.entry("TRAILER!!!") + b"\0" * 17
    changed = rng.insert_probe(raw, b"binary")
    assert changed.startswith(existing)
    assert changed.endswith(raw[len(existing) :])
    assert names(changed) == [
        "test/original",
        "test/semantic",
        "test/semantic/rng",
        "TRAILER!!!",
    ]
    with pytest.raises(ValueError, match="already present"):
        rng.insert_probe(changed, b"replacement")


def test_invalid_archive_is_not_silently_repaired():
    with pytest.raises(ValueError):
        rng.insert_probe(rng.entry("test/original"), b"probe")
    with pytest.raises(ValueError):
        rng.insert_probe(b"bad archive", b"probe")


def test_rate_profile_only_changes_rng_and_keeps_feature_flags():
    argv = [
        "qemu",
        "-device",
        "virtio-blk-pci,drive=x0",
        "-device",
        "virtio-rng-pci,rng=rng0,event_idx=off,queue_reset=off",
        "-smp",
        "1",
    ]
    limited = rng.rng_profile(argv, True)
    assert limited[:4] == argv[:4]
    assert limited[4] == argv[4] + ",max-bytes=8,period=60000"
    assert limited[5:] == argv[5:]
    assert rng.rng_profile(argv, False) == argv
    assert "max-bytes" not in argv[4]
    with pytest.raises(ValueError):
        rng.rng_profile(["qemu", "-device", "virtio-net-pci"], True)


def test_unsupported_and_missing_results_are_not_passes():
    assert (
        rng.status_from_log(
            "selection",
            "RNG_SEMANTIC case=selection status=UNSUPPORTED detail=no-control\n",
        )["status"]
        == "UNSUPPORTED"
    )
    assert (
        rng.status_from_log("selection", "read succeeded")["status"]
        == "HARNESS_INVALID"
    )
    line = "RNG_SEMANTIC case=partial status=PASS detail=short\n"
    assert rng.status_from_log("partial", line + line)["status"] == "HARNESS_INVALID"


def test_sealed_iso_change_is_rejected(tmp_path):
    iso = tmp_path / "candidate.iso"
    iso.write_bytes(b"original")
    candidate = {"iso": str(iso), "iso_sha256": rng.sha(iso)}
    rng.verify(candidate)
    iso.write_bytes(b"changed")
    with pytest.raises(ValueError, match="Frozen ISO changed"):
        rng.verify(candidate)
