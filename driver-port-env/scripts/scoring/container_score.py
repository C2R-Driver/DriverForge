"""Operator-side scoring on sealed kernels; no migrator or repair feedback."""

import gzip
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path[:0] = ["/transport", "/env/scripts/native_experiment"]
import network_peer
from guest import Guest, run
from oracle import summaries

ROOT = Path("/work")
SPEC = json.loads(Path("/env/scoring/variants/parameter-variants.json").read_text())


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def command(argv, **kwargs):
    subprocess.run(argv, check=True, **kwargs)


def extract_iso(iso, relative, output):
    command(
        [
            "xorriso",
            "-osirrox",
            "on",
            "-indev",
            str(iso),
            "-extract",
            relative,
            str(output),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def package(driver):
    """Replace test binaries in the original newc archive; preserve kernel/GRUB bytes."""
    base = ROOT / "base-initramfs.cpio.gz"
    extract_iso("/candidate.iso", "/boot/initramfs.cpio.gz", base)
    native = {
        "virtio-rng": ["device/hwrng.c"],
        "virtio-blk": [
            "io/file_io/block_device.c",
            "fs/ext2/file_io.c",
            "fs/ext2/short_rw.c",
        ],
        "nvme-pci": ["device/nvme.c"],
    }
    output = {}
    for variant in SPEC["profiles"]:
        overlay = ROOT / variant / "overlay"
        overlay.mkdir(parents=True)
        binaries = []
        for relative in native[driver]:
            src = Path("/env/scoring/generated") / driver / variant / relative
            dest = overlay / "test" / Path(relative).with_suffix("")
            dest.parent.mkdir(parents=True, exist_ok=True)
            command(
                [
                    "gcc",
                    "-static",
                    "-O2",
                    "-pthread",
                    "-D__asterinas__",
                    str(src),
                    "-o",
                    str(dest),
                ]
            )
            binaries.append(
                {
                    "source": str(src),
                    "source_sha256": sha(src),
                    "binary_sha256": sha(dest),
                }
            )
        # Asterinas stops at the first CPIO trailer: replace entries in a single archive.
        replacements = {
            str(f.relative_to(overlay)): f.read_bytes()
            for f in overlay.rglob("*")
            if f.is_file()
        }
        raw = gzip.decompress(base.read_bytes())
        pos = 0
        chunks = []
        replaced = set()
        while pos < len(raw):
            head = bytearray(raw[pos : pos + 110])
            if head[:6] != b"070701":
                raise ValueError("Unexpected CPIO format")
            size = int(head[54:62], 16)
            namesize = int(head[94:102], 16)
            name = raw[pos + 110 : pos + 110 + namesize - 1].decode()
            datastart = (pos + 110 + namesize + 3) & ~3
            end = (datastart + size + 3) & ~3
            key = name.removeprefix("./")
            if key in replacements:
                data = replacements[key]
                head[54:62] = f"{len(data):08x}".encode()
                chunks.append(
                    bytes(head)
                    + raw[pos + 110 : datastart]
                    + data
                    + b"\0" * ((-len(data)) % 4)
                )
                replaced.add(key)
            else:
                chunks.append(raw[pos:end])
            pos = end
            if name == "TRAILER!!!":
                break
        if replaced != replacements.keys():
            raise ValueError(
                "Missing packaged test paths: " + str(replacements.keys() - replaced)
            )
        initramfs = ROOT / variant / "initramfs.cpio.gz"
        initramfs.write_bytes(gzip.compress(b"".join(chunks), compresslevel=1, mtime=0))
        iso = ROOT / variant / "scoring.iso"
        import shutil

        boot = ROOT / "iso-tree" / "boot"
        if not boot.exists():
            boot.parent.mkdir(exist_ok=True)
            extract_iso("/candidate.iso", "/boot", boot)
        shutil.copyfile(initramfs, boot / "initramfs.cpio.gz")
        command(
            ["grub-mkrescue", "-o", str(iso), str(boot.parent)],
            stdout=subprocess.DEVNULL,
        )
        checks = {}
        for relative in ["/boot/asterinas-osdk-bin", "/boot/grub/grub.cfg"]:
            before = ROOT / variant / ("before-" + Path(relative).name)
            after = ROOT / variant / ("after-" + Path(relative).name)
            extract_iso("/candidate.iso", relative, before)
            extract_iso(iso, relative, after)
            assert sha(before) == sha(after), "Kernel or GRUB changed"
            checks[relative] = sha(before)
            before.unlink()
            after.unlink()
        output[variant] = {
            "iso": str(iso),
            "iso_sha256": sha(iso),
            "kernel_and_grub_unchanged": checks,
            "initramfs_sha256": sha(initramfs),
            "test_binaries": binaries,
        }
    return output


def native_args(iso):
    from runtime import qemu_argv

    # This uses only the sealed target's original QEMU profile and new temporary media.
    return qemu_argv(str(iso))


def native_case(driver, variant, case, iso, out):
    result = {
        "driver": driver,
        "variant": variant,
        "case": case,
        "status": "FAIL",
        "boot_passed": False,
    }
    started = time.monotonic()
    try:
        with Guest(native_args(iso), out, 150, ready_text="# ") as guest:
            guest.wait_serial("# ")
            guest.assert_guest("sh /test/boot_hello.sh")
            result["boot_passed"] = True
            device = {
                "virtio-rng": "test -e /dev/hwrng",
                "virtio-blk": "test -e /dev/vda",
                "nvme-pci": "test -e /dev/nvme0n1",
                "virtio-net": "ip link show eth0",
            }[driver]
            guest.assert_guest(device)
            result["device_present"] = True
            if driver == "virtio-net":
                benchmark(guest, variant, case, out, result)
            else:
                binary = "/test/" + str(Path(case).with_suffix(""))
                ok = True
                try:
                    guest.assert_guest(binary)
                except RuntimeError as error:
                    if "GUEST_ASSERT_FAILED" not in str(error):
                        raise
                    result["guest_error"] = str(error)
                    ok = False
                source = (
                    Path("/env/scoring/generated") / driver / variant / case
                ).read_text()
                details = summaries(
                    source, (out / "serial.log").read_text(errors="replace")
                )
                result.update(details)
                result["status"] = "PASS" if ok and details["passed"] else "FAIL"
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        result["error"] = str(error)
        result["status"] = (
            "TIMEOUT"
            if "TIMEOUT:" in str(error)
            else (
                "HARNESS_INVALID"
                if isinstance(error, (OSError, subprocess.SubprocessError))
                or "QMP_UNAVAILABLE" in str(error)
                else "FAIL"
            )
        )
    result["seconds"] = round(time.monotonic() - started, 3)
    result["iso_sha256"] = sha(iso)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def benchmark(guest, variant, case, out, result):
    p = SPEC["profiles"][variant]["virtio-net"]
    guest.send(
        "/benchmark/common/bench_runner.sh " + shlex.quote(case) + " asterinas\n"
    )
    guest.wait_serial("The VM is ready for the benchmark.")
    guest.observe(1)
    udp = "udp_" in case
    argv = [
        "iperf3",
        "-c",
        "10.0.2.15",
        "-t",
        str(p["seconds"]),
        "-f",
        "m",
        "-l",
        str(p["udp_bytes"] if udp else p["tcp_bytes"]),
    ]
    if udp:
        argv += ["-u", "-b", p["udp_rate"]]
    if case.endswith("_tx"):
        argv += ["-R"]
    (out / "host-argv.json").write_text(json.dumps(argv) + "\n")
    with (out / "host.log").open("w") as stream:
        host = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 45
        try:
            while host.poll() is None and time.monotonic() < deadline:
                guest.pump()
            if host.poll() is None:
                result["host_timeout"] = True
                host.terminate()
            host.wait(timeout=5)
        finally:
            if host.poll() is None:
                host.kill()
                host.wait()
    rates = re.findall(
        r"(\d+(?:\.\d+)?)\s+Mbits/sec.*\b(receiver|sender)\b",
        (out / "host.log").read_text(),
    )
    result.update(rates_mbits=rates, host_exit=host.returncode)
    result["status"] = (
        "PASS"
        if host.returncode == 0
        and {r for v, r in rates if float(v) > 0} == {"sender", "receiver"}
        else "FAIL"
    )


def fixture_case(driver, variant, iso, out):
    rows = json.loads(
        (Path("/env/scoring/generated") / driver / variant / "cases.json").read_text()
    )
    index = json.loads(Path("/public/index.json").read_text())
    if driver == "ne2k-pci":
        p = SPEC["profiles"][variant][driver]
        network_peer.MAC = bytes.fromhex(p["mac"].replace(":", ""))
        network_peer.PEER = bytes.fromhex(p["peer_mac"].replace(":", ""))

        def request(sequence, length):
            return (
                network_peer.PEER
                + network_peer.MAC
                + b"\x88\xb5"
                + sequence.to_bytes(2, "big")
                + bytes(
                    (sequence + i * p["pattern_stride"]) & 255
                    for i in range(length - 16)
                )
            )

        def response(frame):
            return (
                frame[6:12]
                + frame[:6]
                + b"\x88\xb6"
                + frame[14:16]
                + bytes(x ^ p["response_xor"] for x in frame[16:])
            )

        network_peer.request = request
        network_peer.response = response
    results = []
    for row in rows:
        directory = out / row["id"]
        directory.mkdir(parents=True)
        value = run(index[row["id"]]["profile"], iso, row["case"], directory)
        value.update(
            driver=driver, variant=variant, case=row["id"], iso_sha256=sha(iso)
        )
        (directory / "result.json").write_text(json.dumps(value, indent=2) + "\n")
        results.append(value)
    return results


def main():
    driver = sys.argv[1]
    mode = sys.argv[2]
    results = []
    packaging = {}
    if mode == "prepare-fixture":
        extract_iso(
            "/candidate.iso",
            "/boot/initramfs.cpio.gz",
            ROOT / "original-initramfs.cpio.gz",
        )
        extract_iso("/candidate.iso", "/boot/grub/grub.cfg", ROOT / "original-grub.cfg")
        (ROOT / "fixture-inputs.json").write_text(
            json.dumps(
                {
                    "initramfs_sha256": sha(ROOT / "original-initramfs.cpio.gz"),
                    "original_grub": (ROOT / "original-grub.cfg").read_text(),
                },
                indent=2,
            )
            + "\n"
        )
        return
    if mode == "package":
        packaging = package(driver)
        (ROOT / "packaging.json").write_text(json.dumps(packaging, indent=2) + "\n")
        return
    if mode == "fixture":
        variant = sys.argv[3]
        import shutil

        lock = Path("/root/asterinas/target/osdk/asterinas-run-base/Cargo.lock")
        directory = ROOT / "results" / variant
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(lock, directory / "scoring-build-Cargo.lock")
        (directory / "dependency-provenance.json").write_text(
            json.dumps(
                {
                    "resolved_lock_sha256": sha(lock),
                    "sealed_source_lock_sha256": sha("/root/asterinas/Cargo.lock"),
                    "original_generated_build_lock_retained": False,
                    "limitation": "fixture-only source reconstruction; exact original generated dependency lock unavailable after historical cache cleanup",
                },
                indent=2,
            )
            + "\n"
        )
        results = fixture_case(
            driver, variant, "/candidate.iso", ROOT / "results" / variant
        )
    else:
        os.chdir("/root/asterinas")
        paths = {
            "virtio-rng": ["device/hwrng.c"],
            "virtio-blk": [
                "io/file_io/block_device.c",
                "fs/ext2/file_io.c",
                "fs/ext2/short_rw.c",
            ],
            "nvme-pci": ["device/nvme.c"],
            "virtio-net": [
                "iperf3/tcp_virtio_bw",
                "iperf3/udp_virtio_bw_rx",
                "iperf3/udp_virtio_bw_tx",
            ],
        }[driver]
        for variant in SPEC["profiles"]:
            iso = (
                "/candidate.iso"
                if driver == "virtio-net"
                else ROOT / variant / "scoring.iso"
            )
            for case in paths:
                out = ROOT / "results" / variant / Path(case).stem
                out.mkdir(parents=True, exist_ok=True)
                value = native_case(driver, variant, case, iso, out)
                results.append(value)
                print(
                    json.dumps(
                        {
                            "driver": driver,
                            "variant": variant,
                            "case": case,
                            "status": value["status"],
                        }
                    ),
                    flush=True,
                )
    (
        ROOT
        / ("summary-" + sys.argv[3] + ".json" if mode == "fixture" else "summary.json")
    ).write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
