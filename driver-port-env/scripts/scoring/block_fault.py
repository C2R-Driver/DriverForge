"""Restore an unwritten status and zero used length before delivery of a real IRQ."""

import argparse
import ctypes
import json
import os
import re
import socket
import struct
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from four_semantics import host_run, package, storage_profile


def descriptor_chain(read, base, head, count):
    chain, seen = [], set()
    while head not in seen and 0 <= head < count:
        seen.add(head)
        addr, length, flags, next_index = struct.unpack(
            "<QIHH", read(base + head * 16, 16)
        )
        if flags & 4:
            raise ValueError("Unexpected indirect descriptor")
        chain.append((addr, length, flags))
        if not flags & 1:
            return chain
        head = next_index
    raise ValueError("Invalid descriptor chain")


@contextmanager
def paused_vcpu(process, cpus):
    # QMP stop drains all disks, which deadlocks a deliberately suspended read.
    # Ptrace interrupts only the one KVM vCPU thread; the device loop stays live.
    if len(cpus) != 1:
        raise ValueError("Exactly one vCPU thread required")
    tid = cpus[0]["thread-id"]
    if not Path(f"/proc/{process.pid}/task/{tid}").is_dir():
        raise ValueError("QMP vCPU thread not owned by the guest process")
    libc = ctypes.CDLL(None, use_errno=True)

    def ptrace(request):
        if libc.ptrace(request, tid, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Operator vCPU pause failed")

    ptrace(0x4206)  # PTRACE_SEIZE: no process-wide SIGSTOP.
    try:
        ptrace(0x4207)  # PTRACE_INTERRUPT.
        deadline = time.monotonic() + 3
        while True:
            stopped, status = os.waitpid(tid, os.WNOHANG | 0x40000000)
            if stopped:
                if not os.WIFSTOPPED(status):
                    raise ValueError("vCPU did not enter ptrace stop")
                break
            if time.monotonic() >= deadline:
                raise ValueError("vCPU pause observation timed out")
            time.sleep(0.01)
        yield
    finally:
        ptrace(17)  # PTRACE_DETACH resumes the same thread.


def run_case(case, iso):
    from guest import Guest
    from runtime import qemu_argv

    out = Path("/work/results") / case
    out.mkdir(parents=True)
    os.chdir("/root/asterinas")
    with Path("/work/disk.img").open("wb") as stream:
        stream.truncate(8 * 2**20)
    argv = storage_profile(qemu_argv(str(iso)), "grow")
    argv = [
        x.replace("file=/work/disk.img", "file=blkdebug::/work/disk.img") for x in argv
    ]
    # Identical device, PCI location and capabilities; only add an operator QOM ID.
    for index in range(len(argv) - 1):
        if argv[index] == "-device" and "drive=x2" in argv[index + 1].split(","):
            argv[index + 1] += ",id=semantic-block"
    row = {"case": case, "repetition": 1}
    with tempfile.TemporaryDirectory(prefix="c2r-blkfault-", dir="/tmp") as temp:
        address = str(Path(temp) / "qtest.sock")
        argv += [
            "-qtest",
            f"unix:{address},server=on,wait=off",
            "-qtest-log",
            str(out / "qtest.log"),
        ]
        try:
            with Guest(argv, out, 40, ready_text="# ") as guest:
                guest.wait_serial("# ")
                path = "/machine/peripheral/semantic-block/virtio-backend"

                def queue():
                    return guest.command(
                        "x-query-virtio-queue-status", {"path": path, "queue": 0}
                    )

                def monitor(command):
                    response = guest.command(
                        "human-monitor-command", {"command-line": command}
                    )
                    if response.strip():
                        raise ValueError("HMP rejected: " + response)

                before = queue()
                if before["inuse"]:
                    raise ValueError("Queue not idle before injection")
                guest.send("/test/semantic/storage malformed\n")
                guest.wait_serial("BLOCK_MALFORMED_READY")
                monitor('qemu-io x2 "break read_aio semantic-status"')
                guest.send("G\n")
                pending = queue()
                guest.until(
                    lambda: queue()["inuse"] == 1, "one suspended block request"
                )
                pending = queue()
                if pending["inuse"] != 1 or pending["used-idx"] != before["used-idx"]:
                    raise ValueError("Exactly one uncompleted request required")
                cpus = guest.command("query-cpus-fast")
                with (
                    paused_vcpu(guest.process, cpus),
                    socket.socket(socket.AF_UNIX) as test,
                ):
                    test.settimeout(3)
                    test.connect(address)
                    transport = test.makefile("rwb")

                    def command(text):
                        transport.write((text + "\n").encode())
                        transport.flush()
                        reply = transport.readline().decode().strip()
                        if not reply.startswith("OK"):
                            raise ValueError("QTest error " + reply)
                        return reply.split()[1:]

                    def read(addr, size):
                        raw = bytes.fromhex(
                            command(f"read {addr} {size}")[0].removeprefix("0x")
                        )
                        if len(raw) != size:
                            raise ValueError("Short QTest read")
                        return raw

                    def write(addr, raw):
                        command(f"write {addr} {len(raw)} 0x{raw.hex()}")
                        if read(addr, len(raw)) != raw:
                            raise ValueError("Fault write readback mismatch")

                    slot = (pending["last-avail-idx"] - 1) % pending["vring-num"]
                    head = struct.unpack(
                        "<H", read(pending["vring-avail"] + 4 + 2 * slot, 2)
                    )[0]
                    chain = descriptor_chain(
                        read, pending["vring-desc"], head, pending["vring-num"]
                    )
                    status_addr, status_length, status_flags = chain[-1]
                    if status_length != 1 or not status_flags & 2:
                        raise ValueError("Missing one-byte writable status descriptor")
                    initial_status = read(status_addr, 1)
                    monitor('qemu-io x2 "resume semantic-status"')
                    end = time.monotonic() + 3
                    completed = queue()
                    while (
                        completed["used-idx"] == pending["used-idx"]
                        and time.monotonic() < end
                    ):
                        guest.pump(0.01)
                        completed = queue()
                    if (completed["used-idx"] - pending["used-idx"]) & 0xFFFF != 1:
                        raise ValueError("Real completion not uniquely observed")
                    used_addr = (
                        pending["vring-used"]
                        + 4
                        + 8 * (pending["used-idx"] % pending["vring-num"])
                    )
                    used_head, used_length = struct.unpack("<II", read(used_addr, 8))
                    if used_head != head or read(status_addr, 1) != b"\x00":
                        raise ValueError(
                            "Healthy device completion prerequisite failed"
                        )
                    # Restore the byte as if the device never wrote it. Keep the
                    # real completion IRQ pending while making used length zero.
                    write(status_addr, initial_status)
                    malformed_length = 0 if case == "empty-completion" else 1
                    write(used_addr + 4, struct.pack("<I", malformed_length))
                    row.update(
                        injection_verified=True,
                        initial_status=initial_status.hex(),
                        healthy_used_length=used_length,
                        used_length_after=malformed_length,
                        vcpu_pause="ptrace-interrupt-single-KVM-thread",
                        descriptor_head=head,
                    )
                pattern = re.compile(
                    r"BLOCK_MALFORMED_RESULT bytes=(-?\d+) errno=(\d+)\r*\n"
                )
                guest.until(
                    lambda: (
                        pattern.search(guest.serial.decode(errors="replace"))
                        is not None
                    ),
                    "complete BLOCK_MALFORMED_RESULT line",
                )
                result = pattern.findall(
                    (out / "serial.log").read_text(errors="replace")
                )
                if len(result) != 1:
                    raise ValueError("Ambiguous result")
                row.update(
                    bytes_returned=int(result[0][0]),
                    errno=int(result[0][1]),
                    status="PASS" if int(result[0][0]) < 0 else "FAIL",
                    detail="no-false-success"
                    if int(result[0][0]) < 0
                    else "unwritten-status-reported-success",
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
    print(json.dumps(row), flush=True)
    return row


def container_run():
    iso = package()
    rows = [run_case(case, iso) for case in ("unwritten-status", "empty-completion")]
    Path("/work/summary.json").write_text(
        json.dumps({"results": rows}, indent=2) + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", action="store_true")
    parser.add_argument("--driver", default="virtio-blk", choices=["virtio-blk"])
    for name in ("manifest", "factory", "scratch", "output"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.container:
        sys.path[:0] = ["/transport", "/env/scripts/native_experiment"]
        container_run()
    elif not all((args.manifest, args.factory, args.scratch, args.output)):
        parser.error("missing paths")
    else:
        host_run(args, "block_fault.py")


if __name__ == "__main__":
    main()
