"""Suspend one real backend command; observe timeout error and shutdown attempt."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from four_semantics import host_run, package, storage_profile

TRACE_WRITE = re.compile(
    r"pci_nvme_mmio_write addr 0x([0-9a-f]+) data 0x([0-9a-f]+) size \d+"
)
TIMEOUT_RESULT = re.compile(r"NVME_TIMEOUT_RESULT bytes=(-?\d+) errno=(\d+)\r*\n")
READ_COMMAND = re.compile(r"pci_nvme_read cid \d+ nsid 1 nlb 8 count 4096 lba 0x808")


def controller_disabled(trace):
    return any(
        int(addr, 16) == 0x14 and int(value, 16) & 1 == 0
        for addr, value in TRACE_WRITE.findall(trace)
    )


def container_run():
    from guest import Guest
    from runtime import qemu_argv

    out = Path("/work/results/timeout-stop")
    out.mkdir(parents=True)
    iso = package()
    os.chdir("/root/asterinas")
    with Path("/work/disk.img").open("wb") as stream:
        stream.truncate(8 * 2**20)
    argv = storage_profile(qemu_argv(str(iso)), "largeio")
    events = Path("/work/nvme-events.txt")
    events.write_text(
        "pci_nvme_mmio_write\npci_nvme_read\npci_nvme_enqueue_req_completion\n"
    )
    argv += ["-trace", f"events={events},file={out}/device-trace.log"]
    row = {"case": "timeout-stop", "repetition": 1}
    try:
        with Guest(argv, out, 110, ready_text="# ") as guest:
            guest.wait_serial("# ")
            initial = (out / "device-trace.log").read_text(errors="replace")
            if not any(
                int(addr, 16) == 0x14 and int(value, 16) & 1
                for addr, value in TRACE_WRITE.findall(initial)
            ):
                raise ValueError("Trace prerequisite: no enabled-controller write")
            offset = len(initial)
            guest.send("/test/semantic/storage timeout\n")
            guest.wait_serial("NVME_TIMEOUT_READ_READY")
            guest.command(
                "block_set_io_throttle",
                {
                    "device": "nvme0n1",
                    "bps": 0,
                    "bps_rd": 64,
                    "bps_wr": 0,
                    "iops": 0,
                    "iops_rd": 0,
                    "iops_wr": 0,
                },
            )
            row["throttle_bytes_per_second"] = 64
            guest.send("G\n")
            guest.until(
                lambda: (
                    READ_COMMAND.search(
                        (out / "device-trace.log").read_text(errors="replace")[offset:]
                    )
                    is not None
                ),
                "second 4096-byte namespace read submission",
            )
            row["injection_verified"] = True
            guest.until(
                lambda: (
                    TIMEOUT_RESULT.search(guest.serial.decode(errors="replace"))
                    is not None
                ),
                "complete NVME_TIMEOUT_RESULT line",
            )
            result = TIMEOUT_RESULT.findall(
                (out / "serial.log").read_text(errors="replace")
            )
            if len(result) != 1:
                raise ValueError("Missing or duplicate complete timeout result")
            trace = (out / "device-trace.log").read_text(errors="replace")[offset:]
            disabled = controller_disabled(trace)
            row.update(
                bytes_returned=int(result[0][0]),
                errno=int(result[0][1]),
                controller_disable_observed=disabled,
                status="PASS" if int(result[0][0]) < 0 and disabled else "FAIL",
                detail="timeout-error-and-controller-disable"
                if disabled
                else "returned-without-controller-disable",
            )
    except (RuntimeError, ValueError, OSError) as error:
        row.update(
            status="HARNESS_INVALID"
            if isinstance(error, (ValueError, OSError))
            or not row.get("injection_verified")
            else "TIMEOUT"
            if "TIMEOUT:" in str(error)
            else "FAIL",
            detail=str(error),
        )
    (out / "result.json").write_text(json.dumps(row, indent=2) + "\n")
    Path("/work/summary.json").write_text(
        json.dumps({"results": [row]}, indent=2) + "\n"
    )
    print(json.dumps(row), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", action="store_true")
    parser.add_argument("--driver", default="nvme-pci", choices=["nvme-pci"])
    for name in ("manifest", "factory", "scratch", "output"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.container:
        sys.path[:0] = ["/transport", "/env/scripts/native_experiment"]
        container_run()
    elif not all((args.manifest, args.factory, args.scratch, args.output)):
        parser.error("missing paths")
    else:
        host_run(args, "nvme_fault.py")


if __name__ == "__main__":
    main()
