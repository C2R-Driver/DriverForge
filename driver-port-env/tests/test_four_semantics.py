"""Offline integrity/oracle checks, not real driver validation."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts/scoring"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location(
    "four_semantics", SCRIPTS / "four_semantics.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_single_archive_and_duplicate_refusal():
    raw = module.entry("init", b"old") + module.entry("TRAILER!!!")
    updated = module.insert_files(raw, {"test/semantic/storage": b"new"})
    assert updated.count(b"TRAILER!!!") == 1
    assert updated.index(b"new") < updated.index(b"TRAILER!!!")
    with pytest.raises(ValueError, match="already present"):
        module.insert_files(updated, {"test/semantic/storage": b"bad"})
    with pytest.raises(ValueError):
        module.insert_files(raw[:-3], {"test/semantic/storage": b"new"})


def test_no_false_pass_from_missing_or_duplicate_markers():
    line = "STORAGE_SEMANTIC case=readonly status=PASS detail=rejected\n"
    assert module.parse_probe("readonly", line)["status"] == "PASS"
    for text in (
        "",
        line + line,
        line.replace("readonly", "capacity"),
        line.replace("PASS", "UNKNOWN"),
    ):
        assert module.parse_probe("readonly", text)["status"] == "HARNESS_INVALID"


def test_profiles_preserve_other_devices():
    old = [
        "qemu",
        "-drive",
        "if=none,id=x2,format=raw,file=old.img",
        "-device",
        "virtio-blk-pci,drive=x2",
        "-device",
        "virtio-net-pci,netdev=n",
    ]
    new = module.storage_profile(old, "readonly")
    assert old[2].endswith("old.img")
    assert new[2].endswith("file=/work/disk.img,readonly=on")
    assert old[3:] == new[3:]
    with pytest.raises(ValueError):
        module.storage_profile(["qemu"], "readonly")


def test_namespaces_use_one_controller_two_separate_disks():
    old = [
        "qemu",
        "-drive",
        "if=none,id=nvme0n1,format=raw,file=old.img",
        "-device",
        "nvme,drive=nvme0n1,serial=fixed",
    ]
    new = module.storage_profile(old, "namespaces")
    assert new.count("-device") == 3
    assert "nvme,serial=fixed,id=semantic-nvme" in new
    assert "nvme-ns,drive=nvme0n1,bus=semantic-nvme,nsid=1" in new
    assert "nvme-ns,drive=extra-ns,bus=semantic-nvme,nsid=2" in new


def test_fault_trace_requires_real_disable_and_complete_serial_line():
    import nvme_fault

    assert not nvme_fault.controller_disabled(
        "pci_nvme_mmio_write addr 0x14 data 0x460001 size 4\n"
    )
    assert nvme_fault.controller_disabled(
        "pci_nvme_mmio_write addr 0x14 data 0x0 size 4\n"
    )
    assert not nvme_fault.TIMEOUT_RESULT.search("NVME_TIMEOUT_RESULT bytes=-1")
    assert nvme_fault.TIMEOUT_RESULT.search(
        "NVME_TIMEOUT_RESULT bytes=-1 errno=5\r\r\n"
    )
    assert nvme_fault.READ_COMMAND.search(
        "pci_nvme_read cid 4 nsid 1 nlb 8 count 4096 lba 0x808"
    )


def test_fault_descriptors_reject_cycle_and_indirect_chain():
    import struct

    import block_fault

    memory = struct.pack("<QIHH", 4096, 16, 1, 1) + struct.pack("<QIHH", 8192, 1, 2, 0)
    assert block_fault.descriptor_chain(lambda a, n: memory[a : a + n], 0, 0, 2)[
        -1
    ] == (8192, 1, 2)
    for flags in (1, 4):
        bad = struct.pack("<QIHH", 4096, 16, flags, 0)
        with pytest.raises(ValueError):
            block_fault.descriptor_chain(lambda a, n, bad=bad: bad[a : a + n], 0, 0, 1)
