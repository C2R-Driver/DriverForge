"""Inject DEVICE_NEEDS_RESET in the same virtio device, observe actual TX behavior."""

import argparse
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

from four_semantics import host_run, package


def container_run():
    from guest import Guest
    from runtime import qemu_argv

    out = Path("/work/results/device-needs-reset")
    out.mkdir(parents=True)
    iso = package()
    os.chdir("/root/asterinas")
    row = {"case": "device-needs-reset", "repetition": 1}
    with tempfile.TemporaryDirectory(prefix="c2r-netfault-", dir="/tmp") as temp:
        address = str(Path(temp) / "qtest.sock")
        argv = qemu_argv(str(iso)) + [
            "-qtest",
            f"unix:{address},server=on,wait=off",
            "-qtest-log",
            str(out / "qtest.log"),
        ]
        try:
            with Guest(argv, out, 60, ready_text="# ") as guest:
                guest.wait_serial("# ")
                guest.send("/test/semantic/udp-echo &\n")
                guest.wait_serial("UDP_SEMANTIC_READY")
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                    udp.bind(("0.0.0.0", 0))
                    udp.settimeout(0.1)
                    # Calibrate actual bidirectional traffic before injecting status.
                    healthy = False
                    for _ in range(10):
                        udp.sendto(b"HEALTHY-NEEDSRESET", ("10.0.2.15", 5207))
                        try:
                            data, _ = udp.recvfrom(2048)
                            if data == b"HEALTHY-NEEDSRESET":
                                healthy = True
                                break
                        except TimeoutError:
                            guest.pump(0)
                    if not healthy:
                        raise ValueError("Healthy path prerequisite failed")
                    guest.command("stop")
                    devices = guest.command("query-pci")
                    matches = [
                        d
                        for bus in devices
                        for d in bus["devices"]
                        if d["id"]["vendor"] == 0x1AF4 and d["id"]["device"] == 0x1041
                    ]
                    if len(matches) != 1:
                        raise ValueError("VirtIO-net not uniquely located")
                    device = matches[0]
                    with socket.socket(socket.AF_UNIX) as test:
                        test.settimeout(3)
                        test.connect(address)
                        stream = test.makefile("rwb")

                        def command(text):
                            stream.write((text + "\n").encode())
                            stream.flush()
                            reply = stream.readline().decode().strip()
                            if not reply.startswith("OK"):
                                raise ValueError("QTest error " + reply)
                            return (
                                int(reply.split()[1], 16)
                                if len(reply.split()) > 1
                                else None
                            )

                        def cfg(offset, size="b"):
                            pci = (
                                0x80000000
                                | device["bus"] << 16
                                | device["slot"] << 11
                                | device["function"] << 8
                                | offset & ~3
                            )
                            command(f"outl 0xcf8 {pci}")
                            return command(f"in{size} {0xCFC + (offset & 3)}")

                        cap = cfg(0x34)
                        visited = set()
                        status_addr = None
                        while cap and cap not in visited:
                            visited.add(cap)
                            if cfg(cap) == 9 and cfg(cap + 3) == 1:
                                bar = cfg(cap + 4)
                                offset = cfg(cap + 8, "l")
                                base = next(
                                    r["address"]
                                    for r in device["regions"]
                                    if r["bar"] == bar
                                )
                                status_addr = base + offset + 0x14
                                break
                            cap = cfg(cap + 1)
                        if status_addr is None:
                            raise ValueError("Common PCI capability missing")
                        before = command(f"readb {status_addr}")
                        if not before & 4 or before & 64:
                            raise ValueError("Initial status is not operational")
                        command(f"writeb {status_addr} {before | 64}")
                        injected = command(f"readb {status_addr}")
                        if not injected & 64:
                            raise ValueError("NEEDS_RESET not set")
                        row.update(
                            injection_verified=True,
                            status_before=before,
                            status_injected=injected,
                            status_address=status_addr,
                        )
                        guest.command("cont")
                        replies = 0
                        # Require quiescence, not recovery policy or use of a named method.
                        for n in range(20):
                            packet = b"AFTER-NEEDSRESET-" + n.to_bytes(2, "big")
                            udp.sendto(packet, ("10.0.2.15", 5207))
                            try:
                                data, _ = udp.recvfrom(2048)
                                if data == packet:
                                    replies += 1
                            except TimeoutError:
                                pass
                            guest.pump(0)
                        guest.observe(1)
                        guest.command("stop")
                        final = command(f"readb {status_addr}")
                        row.update(
                            status_final=final,
                            replies_after_fault=replies,
                            status="PASS" if replies == 0 else "FAIL",
                            detail="device-quiesced"
                            if replies == 0
                            else "transmitted-after-device-error",
                        )
        except (RuntimeError, ValueError, OSError) as error:
            row.update(
                status="HARNESS_INVALID"
                if not row.get("injection_verified")
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
    parser.add_argument("--driver", default="virtio-net", choices=["virtio-net"])
    for name in ("manifest", "factory", "scratch", "output"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.container:
        sys.path[:0] = ["/transport", "/env/scripts/native_experiment"]
        container_run()
    elif not all((args.manifest, args.factory, args.scratch, args.output)):
        parser.error("missing paths")
    else:
        host_run(args, "network_fault.py")


if __name__ == "__main__":
    main()
