"""Post-hoc RNG probes on immutable ISOs; never edits driver sources or old scores."""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parents[2]
CASES = ("partial", "interrupt", "selection", "quality")


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(candidate):
    if sha(candidate["iso"]) != candidate["iso_sha256"]:
        raise ValueError("Frozen ISO changed")
    for relative, digest in candidate.get("source_files", {}).items():
        if sha(Path(candidate["target"]) / relative) != digest:
            raise ValueError("Frozen source changed: " + relative)


def entry(name, data=b"", directory=False):
    encoded = name.encode() + b"\0"
    fields = [
        0,
        (stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | 0o755),
        0,
        0,
        1,
        0,
        len(data),
        0,
        0,
        0,
        0,
        len(encoded),
        0,
    ]
    head = b"070701" + b"".join(f"{value:08x}".encode() for value in fields)
    named = head + encoded
    return named + b"\0" * (-len(named) % 4) + data + b"\0" * (-len(data) % 4)


def insert_probe(raw, executable):
    """Insert before the sole newc trailer; never append a second ignored archive."""
    position = 0
    while position < len(raw):
        head = raw[position : position + 110]
        if len(head) != 110 or head[:6] != b"070701":
            raise ValueError("Invalid newc archive")
        size, namesize = int(head[54:62], 16), int(head[94:102], 16)
        name = raw[position + 110 : position + 110 + namesize - 1].decode()
        start = (position + 110 + namesize + 3) & ~3
        end = (start + size + 3) & ~3
        if end > len(raw):
            raise ValueError("Truncated newc entry")
        if name.removeprefix("./") in {"test/semantic", "test/semantic/rng"}:
            raise ValueError("Probe path already present")
        if name == "TRAILER!!!":
            return (
                raw[:position]
                + entry("test/semantic", directory=True)
                + entry("test/semantic/rng", executable)
                + raw[position:]
            )
        position = end
    raise ValueError("Missing newc trailer")


def rng_profile(argv, limited):
    result, count = list(argv), 0
    for i in range(len(result) - 1):
        if result[i] == "-device" and result[i + 1].split(",")[0] == "virtio-rng-pci":
            count += 1
            if limited:
                if any(s in result[i + 1] for s in ("max-bytes=", "period=")):
                    raise ValueError("Original RNG profile already has rate parameters")
                result[i + 1] += ",max-bytes=8,period=60000"
    if count != 1:
        raise ValueError("Exactly one frozen RNG device is required")
    return result


def status_from_log(case, text):
    rows = re.findall(r"RNG_SEMANTIC case=(\w+) status=(\w+) detail=([^\r\n]*)", text)
    matches = [row for row in rows if row[0] == case]
    if len(matches) != 1:
        return {"status": "HARNESS_INVALID", "detail": "missing-or-duplicate-result"}
    _, status, detail = matches[0]
    if status not in {"PASS", "FAIL", "UNSUPPORTED", "HARNESS_INVALID"}:
        return {"status": "HARNESS_INVALID", "detail": "unknown-result"}
    return {"status": status, "detail": detail}


def container_run():
    sys.path[:0] = ["/transport", "/env/scripts/native_experiment"]
    from container_score import extract_iso
    from guest import Guest
    from runtime import qemu_argv

    work = Path("/work")
    binary = work / "rng"
    subprocess.run(
        [
            "gcc",
            "-static",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(ENV / "scoring/semantic/virtio-rng.c"),
            "-o",
            str(binary),
        ],
        check=True,
    )
    tree = work / "iso-tree"
    extract_iso("/candidate.iso", "/boot", tree / "boot")
    preserved = {
        name: sha(tree / "boot" / name)
        for name in ("asterinas-osdk-bin", "grub/grub.cfg")
    }
    ramfs = tree / "boot/initramfs.cpio.gz"
    ramfs.write_bytes(
        gzip.compress(
            insert_probe(gzip.decompress(ramfs.read_bytes()), binary.read_bytes()),
            mtime=0,
        )
    )
    derived = work / "semantic.iso"
    subprocess.run(
        ["grub-mkrescue", "-o", str(derived), str(tree)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    for name, digest in preserved.items():
        exported = work / ("verify-" + Path(name).name)
        extract_iso(derived, "/boot/" + name, exported)
        if sha(exported) != digest:
            raise ValueError("Kernel or GRUB changed")
    os.chdir("/root/asterinas")
    results = []
    for case in CASES:
        output = work / "results" / case
        row = {
            "case": case,
            "classification": "data-path-challenge"
            if case in CASES[:2]
            else "extended-control-capability",
        }
        try:
            argv = rng_profile(qemu_argv(str(derived)), case in CASES[:2])
            with Guest(argv, output, 45, ready_text="# ") as guest:
                guest.wait_serial("# ")
                guest.assert_guest("sh /test/boot_hello.sh")
                guest.observe(0.3)
                try:
                    guest.assert_guest("/test/semantic/rng " + case)
                except RuntimeError as error:
                    if "GUEST_ASSERT_FAILED" not in str(error):
                        raise
                row.update(
                    status_from_log(
                        case, (output / "serial.log").read_text(errors="replace")
                    )
                )
        except RuntimeError as error:
            row.update(
                status="TIMEOUT" if "TIMEOUT:" in str(error) else "HARNESS_INVALID",
                detail=str(error),
            )
        row["derived_iso_sha256"] = sha(derived)
        (output / "result.json").write_text(json.dumps(row, indent=2) + "\n")
        results.append(row)
        print(json.dumps(row), flush=True)
    (work / "summary.json").write_text(
        json.dumps(
            {
                "results": results,
                "kernel_and_grub_unchanged": preserved,
                "producer_feedback": "NONE",
            },
            indent=2,
        )
        + "\n"
    )


def host_run(args):
    sys.path.insert(0, str(ENV / "scripts"))
    import score
    from storage_budget import reserve

    args.factory = args.factory.resolve()
    args.output = args.output.resolve()
    args.scratch = args.scratch.resolve()
    if args.output.exists() or args.scratch.exists():
        raise ValueError(
            "Use new output and scratch paths; results are never overwritten"
        )
    # WSL virtual df capacity does not represent the backing Windows drive.
    if Path("/mnt/d").is_mount() and shutil.disk_usage("/mnt/d").free < 2 * 2**30:
        raise ValueError(
            "Backing D drive has less than 2 GiB free; no evaluation started"
        )
    spec = json.loads(args.manifest.read_text())
    candidates = []
    for item in spec["inputs"]:
        path = (args.manifest.parent / item["manifest"]).resolve()
        value = json.loads(path.read_text())
        rows = value.get("candidates", [value])
        selected = [
            c
            for c in rows
            if c["method"] == item["method"] and c["driver"] == "virtio-rng"
        ]
        if len(selected) != 1:
            raise ValueError("Ambiguous or missing frozen candidate: " + str(path))
        candidate = selected[0]
        verify(candidate)
        candidates.append((item["label"], candidate, path))
    labels = [label for label, _, _ in candidates]
    if len(set(labels)) != len(labels) or any(
        not re.fullmatch(r"[a-z0-9-]+", label) for label in labels
    ):
        raise ValueError("Candidate labels must be unique safe path components")
    with reserve(args.scratch, "post-hoc-rng-semantics", instances=1):
        args.output.mkdir(parents=True)
        frozen = {
            "evaluation_mode": "DEVELOPER_EVIDENCE",
            "active_role": "CURATOR",
            "test_author_candidate_exposure": "NON_INDEPENDENT",
            "producer_private_feedback": "NONE",
            "candidate_freeze_relation": "candidates-predate-test-design",
            "spec_sha256": sha(args.manifest),
            "probe_sha256": sha(ENV / "scoring/semantic/virtio-rng.c"),
            "runner_sha256": sha(Path(__file__)),
            "cases": CASES,
        }
        score.write(args.output / "freeze.json", frozen)
        score.write(
            args.output / "phase.json",
            {
                "active_role": "EVALUATOR",
                "freeze_sha256": sha(args.output / "freeze.json"),
                "producer_feedback": "NONE",
            },
        )
        try:
            for label, candidate, original_manifest in candidates:
                work = args.scratch / label
                target = work / "target"
                target.mkdir(parents=True)
                score.write(
                    work / "owner.json", {"uid": os.getuid(), "gid": os.getgid()}
                )
                shutil.copytree(
                    Path(candidate["target"]) / "tools", target / "tools", symlinks=True
                )
                (target / "kernel").mkdir()
                shared = Path(candidate["suite"]) / "shared"
                score.media(shared, target)
                output = args.output / label
                output.mkdir()
                score.write(
                    output / "candidate.json",
                    {
                        **{k: v for k, v in candidate.items() if k != "source_files"},
                        "source_manifest": str(original_manifest),
                        "source_manifest_sha256": sha(original_manifest),
                    },
                )
                try:
                    score.docker(
                        args,
                        work,
                        target,
                        Path(candidate["iso"]),
                        shared,
                        [
                            "python3",
                            "/env/scripts/scoring/rng_semantics.py",
                            "--container",
                        ],
                        output / "execute.log",
                    )
                    score.preserve(work, output)
                    verify(candidate)
                    score.write(
                        output / "integrity.json",
                        {"original_iso_and_sealed_sources_unchanged": True},
                    )
                finally:
                    # Preserve diagnostics even if packaging/infrastructure fails.
                    if (work / "results").exists() and not (
                        output / "results"
                    ).exists():
                        shutil.copytree(work / "results", output / "results")
                    shutil.rmtree(work)
                    score.write(
                        output / "cleanup.json",
                        {
                            "scratch_removed": not work.exists(),
                            "candidate_and_shared_caches_retained": True,
                        },
                    )
        finally:
            shutil.rmtree(args.scratch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--factory", type=Path)
    parser.add_argument("--scratch", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.container:
        try:
            container_run()
        finally:
            # Docker owns newly-created files until it hands the scoring scratch
            # back to the host. Never follows tools symlinks outside this tree.
            owner = json.loads(Path("/work/owner.json").read_text())
            for root, directories, files in os.walk("/work", followlinks=False):
                for path in [
                    Path(root),
                    *(Path(root) / name for name in directories + files),
                ]:
                    os.chown(path, owner["uid"], owner["gid"], follow_symlinks=False)
                    if path.is_dir() and not path.is_symlink():
                        os.chmod(path, path.stat().st_mode | stat.S_IRWXU)
    elif not all((args.manifest, args.factory, args.scratch, args.output)):
        parser.error("manifest, factory, scratch and output are required")
    else:
        host_run(args)


if __name__ == "__main__":
    main()
