"""Post-hoc semantic scoring; immutable candidates, no model calls or repairs."""

import argparse
import gzip
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from rng_semantics import entry, sha, verify

ENV = Path(__file__).resolve().parents[2]
FIXTURE = "kernel/core/comps/network/src/dpf_ne2k_public.rs"


def insert_files(raw, files):
    pos = 0
    while pos < len(raw):
        head = raw[pos : pos + 110]
        if len(head) != 110 or head[:6] != b"070701":
            raise ValueError("Invalid newc archive")
        size, namesize = int(head[54:62], 16), int(head[94:102], 16)
        name = raw[pos + 110 : pos + 110 + namesize - 1].decode()
        start = (pos + 110 + namesize + 3) & ~3
        end = (start + size + 3) & ~3
        if end > len(raw):
            raise ValueError("Truncated archive")
        if (
            name.removeprefix("./") in files
            or name.removeprefix("./") == "test/semantic"
        ):
            raise ValueError("Probe already present")
        if name == "TRAILER!!!":
            return (
                raw[:pos]
                + entry("test/semantic", directory=True)
                + b"".join(entry(n, data) for n, data in files.items())
                + raw[pos:]
            )
        pos = end
    raise ValueError("Missing trailer")


def storage_profile(argv, case):
    result = list(argv)
    drive = "x2" if case in {"grow", "shrink", "readonly"} else "nvme0n1"
    matches = [
        i + 1
        for i in range(len(result) - 1)
        if result[i] == "-drive" and ("id=" + drive) in result[i + 1].split(",")
    ]
    if len(matches) != 1:
        raise ValueError("Missing/ambiguous disk")
    i = matches[0]
    result[i] = (
        ",".join(x for x in result[i].split(",") if not x.startswith("file="))
        + ",file=/work/disk.img"
    )
    if case == "readonly":
        result[i] += ",readonly=on"
    if case == "namespaces":
        devices = [
            i + 1
            for i in range(len(result) - 1)
            if result[i] == "-device" and result[i + 1].startswith("nvme,")
        ]
        if len(devices) != 1:
            raise ValueError("Exactly one NVMe controller required")
        j = devices[0]
        result[j] = (
            ",".join(x for x in result[j].split(",") if not x.startswith("drive="))
            + ",id=semantic-nvme"
        )
        result += [
            "-device",
            "nvme-ns,drive=nvme0n1,bus=semantic-nvme,nsid=1",
            "-drive",
            "if=none,format=raw,id=extra-ns,file=/work/extra.img",
            "-device",
            "nvme-ns,drive=extra-ns,bus=semantic-nvme,nsid=2",
        ]
    return result


def parse_probe(case, text):
    rows = re.findall(
        r"STORAGE_SEMANTIC case=(\w+) status=(\w+) detail=([^\r\n]*)", text
    )
    if (
        len(rows) != 1
        or rows[0][0] != case
        or rows[0][1] not in {"PASS", "FAIL", "UNSUPPORTED"}
    ):
        return {
            "status": "HARNESS_INVALID",
            "detail": "missing-or-ambiguous-probe-result",
        }
    return {"status": rows[0][1], "detail": rows[0][2]}


def probe(guest, command, case):
    offset = (guest.output / "serial.log").stat().st_size
    try:
        guest.assert_guest("/test/semantic/storage " + command)
    except RuntimeError as error:
        if "GUEST_ASSERT_FAILED" not in str(error):
            raise
    data = (guest.output / "serial.log").read_bytes()[offset:].decode(errors="replace")
    return parse_probe(case, data)


def udp_measure(guest, out, case):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(0.15)
    if case == "after-load":
        for n in range(4000):
            sock.sendto(
                b"LOAD" + n.to_bytes(4, "big") + bytes(1392), ("10.0.2.15", 5207)
            )
            if n % 64 == 0:
                guest.pump(0)
        guest.observe(2)
        sock.setblocking(False)
        while True:
            try:
                sock.recvfrom(4096)
            except BlockingIOError:
                break
        sock.settimeout(0.15)
    rows = []
    count = 256 if case == "delivery" else 64
    for n in range(count):
        length = (60, 512, 1400)[n % 3]
        expected = (
            b"C2RSEM"
            + (0x713 if case == "delivery" else 0x819).to_bytes(2, "big")
            + n.to_bytes(4, "big")
            + bytes((n + i * 31) & 255 for i in range(length - 12))
        )
        started = time.monotonic()
        sock.sendto(expected, ("10.0.2.15", 5207))
        received = False
        corrupt = False
        deadline = started + 0.15
        while time.monotonic() < deadline:
            guest.pump(0)
            try:
                actual, peer = sock.recvfrom(4096)
            except TimeoutError:
                break
            if actual[:8] != expected[:8] or actual[8:12] != expected[8:12]:
                continue
            corrupt = actual != expected or peer != ("10.0.2.15", 5207)
            received = not corrupt
            break
        rows.append(
            {"sequence": n, "length": length, "received": received, "corrupt": corrupt}
        )
        guest.observe(max(0.001, 0.02 - (time.monotonic() - started)))
    sock.close()
    passed = sum(r["received"] for r in rows)
    value = {
        "status": "PASS"
        if passed / count >= 0.99 and not any(r["corrupt"] for r in rows)
        else "FAIL",
        "delivered": passed,
        "sent": count,
        "minimum_ratio": 0.99,
        "payloads": rows,
    }
    (out / "datagrams.json").write_text(json.dumps(value, indent=2) + "\n")
    return {k: v for k, v in value.items() if k != "payloads"}


def package():
    from container_score import extract_iso

    files = {}
    for name in ("storage", "udp-echo"):
        binary = Path("/work") / name
        subprocess.run(
            [
                "gcc",
                "-static",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                str(ENV / "scoring/semantic" / (name + ".c")),
                "-o",
                str(binary),
            ],
            check=True,
        )
        files["test/semantic/" + name] = binary.read_bytes()
    tree = Path("/work/iso-tree")
    extract_iso("/candidate.iso", "/boot", tree / "boot")
    preserved = {
        n: sha(tree / "boot" / n) for n in ("asterinas-osdk-bin", "grub/grub.cfg")
    }
    ramfs = tree / "boot/initramfs.cpio.gz"
    ramfs.write_bytes(
        gzip.compress(insert_files(gzip.decompress(ramfs.read_bytes()), files), mtime=0)
    )
    derived = Path("/work/semantic.iso")
    subprocess.run(
        ["grub-mkrescue", "-o", str(derived), str(tree)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    for n, digest in preserved.items():
        dest = Path("/work") / ("verify-" + Path(n).name)
        extract_iso(derived, "/boot/" + n, dest)
        if sha(dest) != digest:
            raise ValueError("Frozen kernel or GRUB changed")
    Path("/work/packaging.json").write_text(
        json.dumps(
            {
                "kernel_and_grub_unchanged": preserved,
                "derived_iso_sha256": sha(derived),
            },
            indent=2,
        )
        + "\n"
    )
    return derived


def native_run(driver):
    from guest import Guest
    from runtime import qemu_argv

    derived = package()
    os.chdir("/root/asterinas")
    cases = {
        "virtio-blk": ("grow", "shrink", "readonly"),
        "nvme-pci": ("largeio", "namespaces"),
        "virtio-net": ("delivery", "after-load"),
    }[driver]
    rows = []
    for repetition, case in ((1, c) for c in cases):
        out = Path("/work/results") / str(repetition) / case
        out.mkdir(parents=True)
        row = {
            "case": case,
            "repetition": repetition,
        }
        try:
            argv = qemu_argv(str(derived))
            if driver != "virtio-net":
                for disk in ("disk.img", "extra.img"):
                    with (Path("/work") / disk).open("wb") as stream:
                        stream.truncate(8 * 2**20)
                argv = storage_profile(argv, case)
            with Guest(argv, out, 90, ready_text="# ") as guest:
                guest.wait_serial("# ")
                guest.assert_guest("sh /test/boot_hello.sh")
                if case in {"grow", "shrink"}:
                    initial = probe(guest, "capacity 8388608", "capacity")
                    if initial["status"] != "PASS":
                        raise ValueError(
                            "Initial capacity prerequisite failed: " + str(initial)
                        )
                    size = (16 if case == "grow" else 4) * 2**20
                    guest.command("block_resize", {"device": "x2", "size": size})
                    guest.observe(1)
                    row.update(probe(guest, "capacity " + str(size), "capacity"))
                elif driver == "virtio-net":
                    guest.send("/test/semantic/udp-echo &\n")
                    guest.wait_serial("UDP_SEMANTIC_READY")
                    row.update(udp_measure(guest, out, case))
                else:
                    row.update(probe(guest, case, case))
        except (RuntimeError, ValueError, OSError) as error:
            row.update(
                status="TIMEOUT"
                if "TIMEOUT:" in str(error)
                else "HARNESS_INVALID"
                if isinstance(error, (ValueError, OSError)) or "QMP_" in str(error)
                else "FAIL",
                detail=str(error),
            )
        (out / "result.json").write_text(json.dumps(row, indent=2) + "\n")
        print(json.dumps(row), flush=True)
        rows.append(row)
    Path("/work/summary.json").write_text(
        json.dumps({"results": rows}, indent=2) + "\n"
    )


def ne2k_run():
    import socket
    import tempfile

    from guest import Guest
    from network_peer import WirePeer

    out = Path("/work/results/tx-completion-loss")
    peer = WirePeer(out)
    profile = json.loads(Path("/public/index.json").read_text())["ne2k-traffic"][
        "profile"
    ]
    row = {"case": "tx-completion-loss", "repetition": 1}
    with tempfile.TemporaryDirectory(prefix="c2r-fault-", dir="/tmp") as temp:
        address = str(Path(temp) / "qtest.sock")
        argv = [
            profile["qemu"],
            *profile["qemu_args"],
            "-cdrom",
            "/candidate.iso",
            "-nic",
            "none",
            "-netdev",
            f"socket,id=dpf-peer,connect=127.0.0.1:{peer.port}",
            "-device",
            "ne2k_pci,netdev=dpf-peer,mac=52:54:00:12:34:56,addr=5",
            "-qtest",
            f"unix:{address},server=on,wait=off",
            "-qtest-log",
            str(out / "qtest.log"),
        ]
        try:
            with Guest(argv, out, 45, ready_text="# ") as guest:
                guest.wait_serial("# ")
                # Asynchronous invocation permits host control while fixture is running.
                guest.send("printf '%s\\n' 'traffic' > /proc/ne2k_test\n")
                guest.wait_serial("NE2K_TX_FAULT_READY")
                guest.command("stop")
                devices = guest.command("query-pci")
                matches = [
                    d
                    for bus in devices
                    for d in bus["devices"]
                    if d["id"]["vendor"] == 0x10EC and d["id"]["device"] == 0x8029
                ]
                if len(matches) != 1:
                    raise ValueError("Cannot locate injected NE2000")
                base = next(
                    r["address"]
                    for r in matches[0]["regions"]
                    if r["bar"] == 0 and r["type"] == "io"
                )
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.settimeout(3)
                    sock.connect(address)
                    stream = sock.makefile("rwb")

                    def command(text):
                        stream.write((text + "\n").encode())
                        stream.flush()
                        reply = stream.readline().decode().strip()
                        if not reply.startswith("OK"):
                            raise ValueError("QTest injection failed: " + reply)
                        return reply

                    before = int(command(f"inb {base + 7}").split()[1], 16)
                    if not before & 0x0A:
                        raise ValueError("No hardware TX completion to remove")
                    command(f"outb {base + 7} 10")
                    after = int(command(f"inb {base + 7}").split()[1], 16)
                    if after & 0x0A:
                        raise ValueError("TX completion not removed")
                    row.update(
                        injection_verified=True,
                        isr_before=before,
                        isr_after=after,
                        io_base=base,
                    )
                guest.command("cont")
                guest.until(
                    lambda: (
                        b"NE2K_TX_FAULT_PASS" in guest.serial
                        or b"NE2K_TX_FAULT_FAIL" in guest.serial
                    ),
                    "TX fault result",
                )
                if b"NE2K_TX_FAULT_PASS" in guest.serial:
                    peer.assert_expected([[5000, 1514]])
                    row.update(
                        status="PASS", detail="bounded-recovery-and-fresh-wire-exchange"
                    )
                else:
                    row.update(
                        status="FAIL", detail="stuck-busy-after-lost-TX-completion"
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
        finally:
            peer.close()
    (out / "result.json").write_text(json.dumps(row, indent=2) + "\n")
    Path("/work/summary.json").write_text(
        json.dumps({"results": [row]}, indent=2) + "\n"
    )
    print(json.dumps(row), flush=True)


def host_run(args):
    import score

    sys.path.insert(0, str(ENV / "scripts"))
    from storage_budget import reserve

    args.factory = args.factory.resolve()
    args.output, args.scratch = args.output.resolve(), args.scratch.resolve()
    if args.output.exists() or args.scratch.exists():
        raise ValueError("Use fresh output and scratch paths")
    if Path("/mnt/d").is_mount() and shutil.disk_usage("/mnt/d").free < 6 * 2**30:
        raise ValueError("Backing D drive has less than 6 GiB free")
    spec = json.loads(args.manifest.read_text())
    inputs = []
    for item in spec["inputs"]:
        if item["driver"] != args.driver:
            continue
        path = (args.manifest.parent / item["manifest"]).resolve()
        obj = json.loads(path.read_text())
        rows = obj.get("candidates", [obj])
        matches = [
            r
            for r in rows
            if r["method"] == item["method"] and r["driver"] == args.driver
        ]
        if len(matches) != 1:
            raise ValueError("Ambiguous input")
        verify(matches[0])
        inputs.append((item["label"], matches[0], path))
    if not inputs:
        raise ValueError("No candidates")
    with reserve(args.scratch, "post-hoc-four-semantics", instances=1):
        args.output.mkdir(parents=True)
        score.write(
            args.output / "freeze.json",
            {
                "evaluation_mode": "DEVELOPER_EVIDENCE",
                "active_role": "CURATOR",
                "test_author_candidate_exposure": "NON_INDEPENDENT",
                "candidate_freeze_relation": "candidates-predate-test-design",
                "producer_private_feedback": "NONE",
                "driver": args.driver,
                "spec_sha256": sha(args.manifest),
                "files": {
                    str(p.relative_to(ENV)): sha(p)
                    for p in [
                        Path(__file__),
                        ENV / "scripts/scoring/semantic_peer.py",
                        *sorted((ENV / "scoring/semantic").glob("*")),
                    ]
                },
            },
        )
        score.write(
            args.output / "phase.json",
            {
                "active_role": "EVALUATOR",
                "freeze_sha256": sha(args.output / "freeze.json"),
            },
        )
        try:
            for label, candidate, manifest in inputs:
                work = args.scratch / label
                target = work / "target"
                shared = Path(candidate["suite"]) / "shared"
                if args.driver == "ne2k-pci":
                    score.copytree(candidate["target"], target)
                else:
                    target.mkdir(parents=True)
                    shutil.copytree(
                        Path(candidate["target"]) / "tools",
                        target / "tools",
                        symlinks=True,
                    )
                    (target / "kernel").mkdir()
                score.write(
                    work / "owner.json", {"uid": os.getuid(), "gid": os.getgid()}
                )
                score.media(shared, target)
                output = args.output / label
                output.mkdir()
                score.write(
                    output / "candidate.json",
                    {
                        **{k: v for k, v in candidate.items() if k != "source_files"},
                        "source_manifest": str(manifest),
                        "source_manifest_sha256": sha(manifest),
                    },
                )
                try:
                    iso = Path(candidate["iso"])
                    if args.driver == "ne2k-pci":
                        score.docker(
                            args,
                            work,
                            target,
                            iso,
                            shared,
                            [
                                "python3",
                                "/env/scripts/scoring/container_score.py",
                                args.driver,
                                "prepare-fixture",
                            ],
                            output / "prepare.log",
                        )
                        shutil.copyfile(
                            ENV / "scoring/semantic/ne2k-tx-fault.rs", target / FIXTURE
                        )
                        score.verify_copied_sources(candidate, target, FIXTURE)
                        score.docker(
                            args,
                            work,
                            target,
                            iso,
                            shared,
                            [
                                "cargo",
                                "osdk",
                                "build",
                                "--initramfs=/work/original-initramfs.cpio.gz",
                                "--kcmd-args=console=ttyS0",
                                "--kcmd-args=earlycon",
                                "--kcmd-args=loglevel=info",
                            ],
                            output / "build.log",
                        )
                        score.verify_copied_sources(candidate, target, FIXTURE)
                        iso = target / "target/osdk/asterinas-osdk-bin.iso"
                        score.write(
                            output / "fixture.json",
                            {
                                "fixture": FIXTURE,
                                "fixture_sha256": sha(target / FIXTURE),
                                "all_other_sealed_sources_unchanged": True,
                                "derived_iso_sha256": sha(iso),
                            },
                        )
                    score.docker(
                        args,
                        work,
                        target,
                        iso,
                        shared,
                        [
                            "python3",
                            "/env/scripts/scoring/ne2k_fault.py",
                            "--container",
                            "--driver",
                            args.driver,
                        ],
                        output / "execute.log",
                        network=args.driver == "virtio-net",
                    )
                    score.preserve(work, output)
                    verify(candidate)
                    score.write(
                        output / "integrity.json",
                        {"original_iso_and_sealed_sources_unchanged": True},
                    )
                finally:
                    score.preserve(work, output)
                    # On build failure Docker can leave root-owned build directories.
                    subprocess.run(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "--pull=never",
                            "-v",
                            f"{work}:/cleanup",
                            score.IMAGE,
                            "python3",
                            "-c",
                            'import os,stat;\nfor r,ds,fs in os.walk("/cleanup",followlinks=False):\n for p in [r]+[os.path.join(r,n) for n in ds+fs]:\n  os.chown(p,'
                            + str(os.getuid())
                            + ","
                            + str(os.getgid())
                            + ",follow_symlinks=False)\n  if os.path.isdir(p) and not os.path.islink(p): os.chmod(p,os.stat(p).st_mode|stat.S_IRWXU)",
                        ],
                        check=True,
                    )
                    shutil.rmtree(work)
                    score.write(
                        output / "cleanup.json",
                        {
                            "scratch_removed": not work.exists(),
                            "shared_caches_retained": True,
                        },
                    )
        finally:
            args.scratch.rmdir()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--container", action="store_true")
    p.add_argument(
        "--driver",
        required=True,
        choices=["virtio-blk", "virtio-net", "nvme-pci", "ne2k-pci"],
    )
    for name in ("manifest", "factory", "scratch", "output"):
        p.add_argument("--" + name, type=Path)
    args = p.parse_args()
    if args.container:
        sys.path[:0] = ["/transport", "/env/scripts/native_experiment"]
        if args.driver == "ne2k-pci":
            ne2k_run()
        else:
            native_run(args.driver)
    elif not all((args.manifest, args.factory, args.scratch, args.output)):
        p.error("manifest, factory, scratch, output required")
    else:
        host_run(args)


if __name__ == "__main__":
    main()
