"""Import the published public inputs of the frozen six-driver study; no model calls."""

import errno
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from team_profile import REPO


def share_file(source, destination):
    try:
        os.link(source, destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        subprocess.run(
            ["cp", "--sparse=always", "--reflink=auto", source, destination], check=True
        )


def extract(archive, destination):
    # Inspect every member before delegating sparse-file restoration to GNU tar.
    with tarfile.open(archive) as bundle:
        for member in bundle:
            path = Path(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.parts[0] != "shared"
                or member.issym()
                or member.islnk()
                or member.isdev()
            ):
                raise ValueError("Unsafe frozen runtime member: " + member.name)
    subprocess.run(
        ["tar", "--sparse", "-xzf", str(archive), "-C", str(destination)], check=True
    )


def prepare_frozen(profile, driver):
    from common import sha, write
    from team_environment import acquire, verify_suite

    release = profile["suite_release"]
    if release != "local-suite":
        raise ValueError("Unknown frozen suite release: " + release)
    assets = REPO / "assets" / release
    manifest = json.loads((assets / "manifest.json").read_text())
    group = next(
        name for name, suite in manifest["suites"].items() if driver in suite["drivers"]
    )
    suite = manifest["suites"][group]
    root = profile["suites"][group]
    if (root / "seeds" / driver / "task.json").exists():
        verify_suite(profile, driver)
        if json.loads((root / "config.json").read_text()) != suite["config"]:
            raise ValueError("Existing suite belongs to a different release")
        return
    if root.exists():
        raise ValueError(
            "Frozen import requires a new suite path; retain partial evidence"
        )
    if (
        sha(REPO / "scripts/native_experiment/baseline-prompt.md")
        != manifest["baseline_prompt_sha256"]
    ):
        raise ValueError("Codex prompt differs from the frozen study")
    files = [manifest["runtime"], manifest["public_tests"]]
    files += [
        {"file": item["bundle"], "sha256": item["bundle_sha256"]}
        for item in suite["drivers"].values()
    ]
    for item in files:
        if sha(assets / item["file"]) != item["sha256"]:
            raise ValueError("Frozen distribution hash mismatch: " + item["file"])
    config = suite["config"]
    repos = profile["repositories"]
    source = acquire(
        repos["linux"],
        "https://github.com/torvalds/linux.git",
        [config["source_revision"]],
    )
    acquire(
        repos["qemu"], "https://github.com/qemu/qemu.git", [config["qemu_revision"]]
    )
    # Public fixtures reuse the exact native runtime bytes, stored once in the release.
    if group == "public":
        prepare_frozen(profile, "virtio-rng")
    root.mkdir(parents=True)
    if group == "native":
        extract(assets / manifest["runtime"]["file"], root)
    else:
        shutil.copytree(
            profile["suites"]["native"] / "shared",
            root / "shared",
            copy_function=share_file,
        )
        # Replace this native hard link before overlaying the public oracle manifest.
        (root / "shared/oracle-sources.json").unlink()
        extract(assets / manifest["public_tests"]["file"], root)
    write(root / "config.json", config)
    write(root / "environment.json", suite["environment"])
    for name, item in suite["drivers"].items():
        seed = root / "seeds" / name
        target = seed / "target"
        target.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(
            ["git", "-C", str(target), "fetch", str(assets / item["bundle"]), "HEAD"],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "checkout",
                "-q",
                "-b",
                "baseline",
                "FETCH_HEAD",
            ],
            check=True,
        )
        head = subprocess.check_output(
            ["git", "-C", str(target), "rev-parse", "HEAD"], text=True
        ).strip()
        if head != item["task"]["target_root_commit"]:
            raise ValueError("Frozen target root commit mismatch: " + name)
        commits = subprocess.check_output(
            ["git", "-C", str(target), "rev-list", "--all"], text=True
        ).splitlines()
        if commits != [head]:
            raise ValueError("Sanitized seed contains additional commits")
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "fsck",
                "--full",
                "--no-reflogs",
                "--unreachable",
            ],
            check=True,
            capture_output=True,
        )
        (seed / "source").symlink_to(source.resolve(), target_is_directory=True)
        write(seed / "task.json", item["task"])
        write(root / "operator" / name / "seed-audit.json", item["seed_audit"])
        verify_suite(profile, name)
    write(
        root / "frozen-release.json",
        {"release": release, "manifest_sha256": sha(assets / "manifest.json")},
    )
    print("Prepared exact frozen suite; no model or rebuild:", root)
