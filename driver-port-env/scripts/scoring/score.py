"""Score old immutable candidates; retain receipts and delete disposable media per driver."""

import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ENV = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ENV / "scripts"))
from storage_budget import reserve

IMAGE = "sha256:LOCAL_INPUT_FINGERPRINT"


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def copytree(source, dest):
    # Only one ephemeral source tree, never a git/object/toolchain backup.
    shutil.copytree(
        source,
        dest,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git", "target", ".dpf-output", "__pycache__", "build"
        ),
    )


def verify(candidate):
    assert sha(candidate["iso"]) == candidate["iso_sha256"], "Sealed ISO changed"
    for rel, digest in candidate["source_files"].items():
        assert sha(Path(candidate["target"]) / rel) == digest, (
            "Sealed source changed: " + rel
        )


def verify_copied_sources(candidate, target, fixture):
    # Git worktree pointers are provenance, not kernel inputs; scratch has no object store.
    for relative, digest in candidate["source_files"].items():
        if relative != fixture and ".git" not in Path(relative).parts:
            assert sha(target / relative) == digest, relative


def docker(args, work, target, iso, shared, command, log, network=False):
    name = "c2r-score-" + uuid.uuid4().hex[:10]
    argv = [
        "docker",
        "run",
        "--rm",
        "--pull=never",
        "--name",
        name,
        "--device",
        "/dev/kvm",
        "--network",
        "bridge" if network else "none",
    ]
    mounts = [
        (ENV, "/env", "ro"),
        (args.factory / "src/driver_port_factory/platform", "/transport", "ro"),
        (work, "/work", "rw"),
        (target, "/root/asterinas", "rw"),
        (iso, "/candidate.iso", "ro"),
        (shared, "/dpf-shared", "ro"),
        (
            work / "public" if (work / "public").exists() else shared / "public-tests",
            "/public",
            "ro",
        ),
    ]
    for host, guest, mode in mounts:
        if host.exists():
            argv += ["-v", f"{host}:{guest}:{mode}"]
    argv += [
        "-v",
        "dpf-native-cargo-registry:/root/.cargo/registry",
        "-v",
        "dpf-native-cargo-git:/root/.cargo/git",
    ]
    if network:
        argv += ["--device", "/dev/net/tun", "--cap-add", "NET_ADMIN"]
    values = {
        "CONSOLE": "ttyS0",
        "MEM": "2G",
        "SMP": "1",
        "OVMF": "on",
        "CARGO_BUILD_JOBS": "4",
        "CARGO_INCREMENTAL": "0",
        "CARGO_NET_OFFLINE": "true",
        "CARGO_PROFILE_DEV_DEBUG": "0",
        "RUSTUP_TOOLCHAIN": "nightly-2026-07-21",
        "OSDK_LOCAL_DEV": "1",
        "VDSO_LIBRARY_DIR": "/root/linux_vdso",
        "NETDEV": "tap" if network else "user",
    }
    for key, value in values.items():
        argv += ["-e", f"{key}={value}"]
    argv += [
        "-w",
        "/root/asterinas/kernel",
        IMAGE,
        "bash",
        "-lc",
        'export PATH=/dpf-shared/sdk:/usr/local/qemu/bin:/root/.rustup/toolchains/nightly-2026-07-21-x86_64-unknown-linux-gnu/bin:$PATH; exec "$@"',
        "score",
        *command,
    ]
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        with log.open("w") as stream:
            code = subprocess.run(
                argv, stdout=stream, stderr=subprocess.STDOUT, timeout=1800, check=False
            ).returncode
    except subprocess.TimeoutExpired:
        code = 124
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
    write(
        log.with_suffix(".receipt.json"),
        {
            "argv": argv,
            "exit_code": code,
            "seconds": round(time.time() - started, 3),
            "log_sha256": sha(log),
        },
    )
    if code:
        raise RuntimeError(f"Operator command failed ({code}): {log}")


def media(shared, target):
    dest = target / "test/initramfs/build"
    dest.mkdir(parents=True, exist_ok=True)
    for src in (shared / "disks").glob("*.img"):
        subprocess.run(
            ["cp", "--sparse=always", "--reflink=auto", str(src), str(dest / src.name)],
            check=True,
        )


def preserve(work, out):
    for name in [
        "results",
        "packaging.json",
        "summary.json",
        "summary-A.json",
        "summary-B.json",
        "fixture-inputs.json",
    ]:
        src = work / name
        if src.is_dir():
            shutil.copytree(src, out / name, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, out / name)


def run_candidate(args, c, driver_work, output):
    verify(c) if c.get("source_files") else None
    work = driver_work / c["method"]
    work.mkdir()
    target = work / "target"
    shared = Path(c["suite"]) / "shared"
    out = output / c["method"]
    out.mkdir(parents=True)
    fixture = c["driver"] in {"evbug", "ne2k-pci"}
    if fixture:
        copytree(c["target"], target)
    else:
        target.mkdir()
        shutil.copytree(Path(c["target"]) / "tools", target / "tools", symlinks=True)
        (target / "kernel").mkdir()
    media(shared, target)
    iso = Path(c["iso"])
    write(out / "candidate.json", {k: v for k, v in c.items() if k != "source_files"})
    if c.get("private_scoring"):
        scoring = c["private_scoring"]
        source = Path(c["target"])
        for relative in scoring.get("copy_paths", []):
            src, dest = source / relative, target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dest)
            else:
                shutil.copy2(src, dest)
        docker(
            args,
            work,
            target,
            iso,
            shared,
            ["python3", "/env/" + scoring["script"], c["driver"]],
            out / "execute.log",
            network=scoring.get("network_required", False),
        )
        preserve(work, out)
        verify(c)
        return
    if not fixture:
        if c["driver"] != "virtio-net":
            docker(
                args,
                work,
                target,
                iso,
                shared,
                [
                    "python3",
                    "/env/scripts/scoring/container_score.py",
                    c["driver"],
                    "package",
                ],
                out / "package.log",
            )
        docker(
            args,
            work,
            target,
            iso,
            shared,
            [
                "python3",
                "/env/scripts/scoring/container_score.py",
                c["driver"],
                "native",
            ],
            out / "execute.log",
            network=c["driver"] == "virtio-net",
        )
    else:
        fixturefile = {
            "evbug": "kernel/core/comps/input/src/dpf_evbug_public.rs",
            "ne2k-pci": "kernel/core/comps/network/src/dpf_ne2k_public.rs",
        }[c["driver"]]
        asset = {"evbug": "evbug_public.rs", "ne2k-pci": "ne2k_public.rs"}[c["driver"]]
        docker(
            args,
            work,
            target,
            iso,
            shared,
            [
                "python3",
                "/env/scripts/scoring/container_score.py",
                c["driver"],
                "prepare-fixture",
            ],
            out / "prepare-fixture.log",
        )
        for variant in ["A", "B"]:
            shutil.copyfile(
                ENV / "scoring/generated" / c["driver"] / variant / asset,
                target / fixturefile,
            )
            # Verify every sealed relevant source byte except the operator-owned test fixture.
            verify_copied_sources(c, target, fixturefile)
            initramfs = "/work/original-initramfs.cpio.gz"
            cmd = [
                "cargo",
                "osdk",
                "build",
                "--initramfs=" + initramfs,
                "--kcmd-args=console=ttyS0",
                "--kcmd-args=earlycon",
                "--kcmd-args=loglevel=info",
            ]
            docker(args, work, target, iso, shared, cmd, out / f"build-{variant}.log")
            verify_copied_sources(c, target, fixturefile)
            derived = target / "target/osdk/asterinas-osdk-bin.iso"
            write(
                out / f"fixture-{variant}.json",
                {
                    "original_iso_sha256": c["iso_sha256"],
                    "derived_iso_sha256": sha(derived),
                    "fixture": fixturefile,
                    "fixture_sha256": sha(target / fixturefile),
                    "all_other_sealed_sources_unchanged": True,
                    "build_command": cmd,
                },
            )
            # Legacy NE2000 has its own profile, not the q35 profile of the new suite.
            if c["driver"] == "ne2k-pci":
                local = work / "public"
                local.mkdir(exist_ok=True)
                index = json.loads(
                    (
                        ENV
                        / "src/driver_port_factory/data/public-tests/ne2k-pci/cases.json"
                    ).read_text()
                )
                profile = {
                    "qemu": "qemu-system-x86_64",
                    "ready_text": "# ",
                    "qemu_args": [
                        "-machine",
                        "pc,kernel-irqchip=split",
                        "-accel",
                        "kvm",
                        "-cpu",
                        "host,+x2apic",
                        "-smp",
                        "1",
                        "-m",
                        "2G",
                        "-no-reboot",
                        "-display",
                        "none",
                        "-monitor",
                        "none",
                        "-serial",
                        "stdio",
                        "-boot",
                        "d",
                        "-bios",
                        "/root/ovmf/release/OVMF.fd",
                        "-device",
                        "isa-debug-exit,iobase=0xf4,iosize=0x04",
                    ],
                }
                write(
                    local / "index.json",
                    {row["id"]: {"profile": profile} for row in index},
                )
            docker(
                args,
                work,
                target,
                derived,
                shared,
                [
                    "python3",
                    "/env/scripts/scoring/container_score.py",
                    c["driver"],
                    "fixture",
                    variant,
                ],
                out / f"execute-{variant}.log",
            )
    preserve(work, out)
    if c.get("source_files"):
        verify(c)
    write(
        out / "integrity.json",
        {
            "original_iso_unchanged": sha(iso) == c["iso_sha256"],
            "original_sealed_sources_unchanged": True,
            "producer_feedback": "NONE",
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factory", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ENV / "results/scoring/candidates-20261006.json",
    )
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--driver", required=True)
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    args.reference = args.reference.resolve() if args.reference else None
    args.factory = args.factory.resolve()
    args.output = args.output.resolve()
    args.scratch = args.scratch.resolve()
    generated = json.loads(
        (ENV / "scoring/variants/generated-manifest.json").read_text()
    )
    assert (
        sha(ENV / "scoring/variants/parameter-variants.json")
        == generated["parameters_sha256"]
    )
    for relative, digest in generated["generated_files"].items():
        assert sha(ENV / relative) == digest, relative
    manifest = json.loads(args.manifest.read_text())
    for relative, digest in manifest.get("private_oracle_files", {}).items():
        if sha(ENV / relative) != digest:
            raise ValueError("Frozen private oracle changed: " + relative)
    # The first published Intel batch seals this older field. Preserve its receipt.
    if manifest.get("intel_private_oracle_sha256"):
        if (
            sha(ENV / "scripts/scoring/intel_score.py")
            != manifest["intel_private_oracle_sha256"]
        ):
            raise ValueError("Frozen private oracle changed")
        for candidate in manifest["candidates"]:
            candidate["private_scoring"] = {
                "script": "scripts/scoring/intel_score.py",
                "network_required": True,
                "copy_paths": ["test/initramfs/src/benchmark", ".native-task.json"],
            }
    candidates = [c for c in manifest["candidates"] if c["driver"] == args.driver]
    if args.reference:
        candidates = [
            {
                "driver": args.driver,
                "method": "reference",
                "target": str(args.reference),
                "iso": str(args.reference / "target/osdk/asterinas-osdk-bin.iso"),
                "iso_sha256": sha(
                    args.reference / "target/osdk/asterinas-osdk-bin.iso"
                ),
                "suite": str(args.reference.parents[1]),
                "provenance": {"classification": "original-reference-only"},
            }
        ]
    with reserve(args.scratch.parent, f"private/{args.driver}"):
        work = args.scratch / (args.driver + "-" + uuid.uuid4().hex[:8])
        work.mkdir(parents=True)
        out = args.output / args.driver
        out.mkdir(parents=True, exist_ok=True)
        start = datetime.datetime.now(datetime.timezone.utc).isoformat()
        try:
            for c in candidates:
                print("SCORING " + args.driver + " " + c["method"], flush=True)
                candidate_start = time.time()
                try:
                    run_candidate(args, c, work, out)
                    print("COMPLETED " + args.driver + " " + c["method"], flush=True)
                except (
                    RuntimeError,
                    OSError,
                    ValueError,
                    subprocess.SubprocessError,
                ) as error:
                    write(
                        out / c["method"] / "operator-error.json",
                        {
                            "status": "BLOCKED",
                            "error": str(error),
                            "candidate_repaired": False,
                        },
                    )
                    print(
                        "BLOCKED " + args.driver + " " + c["method"] + " " + str(error),
                        flush=True,
                    )
                finally:
                    preserve(work / c["method"], out / c["method"])
                    write(
                        out / c["method"] / "timing.json",
                        {
                            "started_at": datetime.datetime.fromtimestamp(
                                candidate_start, datetime.timezone.utc
                            ).isoformat(),
                            "ended_at": datetime.datetime.now(
                                datetime.timezone.utc
                            ).isoformat(),
                            "total_seconds": round(time.time() - candidate_start, 3),
                            "model_calls": 0,
                        },
                    )
        finally:
            allocated = sum(
                p.stat().st_blocks * 512
                for p in work.rglob("*")
                if p.is_file() and not p.is_symlink()
            )
            # Docker-created scratch is root-owned; delete only this explicit per-driver mount.
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "-v",
                    f"{work}:/cleanup",
                    IMAGE,
                    "bash",
                    "-c",
                    "find /cleanup -mindepth 1 -delete",
                ],
                check=True,
            )
            work.rmdir()
            write(
                out / "cleanup.json",
                {
                    "started_at": start,
                    "ended_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "temporary_allocated_bytes_removed": allocated,
                    "scratch_removed": not work.exists(),
                    "original_candidates_retained": True,
                },
            )
            print(
                "CLEANED "
                + args.driver
                + " "
                + str(round(allocated / (1 << 20), 1))
                + " MiB",
                flush=True,
            )


if __name__ == "__main__":
    main()
