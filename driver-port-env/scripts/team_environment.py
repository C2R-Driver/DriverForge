"""Operator environment installation and on-demand preparation; never calls a model."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from experiment_config import network_required
from team_profile import REPO, catalog, selected_suite


def command(argv, **kwargs):
    return subprocess.run(list(map(str, argv)), check=True, **kwargs)


def git_has(repository, revision):
    return (
        subprocess.run(
            ["git", "-C", str(repository), "cat-file", "-e", revision + "^{commit}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def acquire(repository, url, revisions):
    if not repository.exists():
        repository.mkdir(parents=True)
        command(["git", "init", "-q", repository])
    for revision in revisions:
        if not git_has(repository, revision):
            # Only requested pinned revisions, no full-history clone or toolchain backups.
            command(["git", "-C", repository, "fetch", "--depth=1", url, revision])
            command(
                [
                    "git",
                    "-C",
                    repository,
                    "update-ref",
                    "refs/heads/pin-" + revision,
                    revision,
                ]
            )
    return repository


def install(profile, system=False):
    if system:
        prefix = [] if os.geteuid() == 0 else ["sudo"]
        command([*prefix, "apt-get", "update"])
        command(
            [
                *prefix,
                "apt-get",
                "install",
                "-y",
                "git",
                "docker.io",
                "bubblewrap",
                "nodejs",
                "npm",
                "python3.11-venv",
                "strace",
                "tmux",
            ]
        )
        if shutil.which("systemctl"):
            command([*prefix, "systemctl", "enable", "--now", "docker"])
    factory = profile["factory"]
    if not factory.exists():
        branch = (
            ["--branch", "local-suite"] if profile.get("suite_release") else []
        )
        command(
            [
                "git",
                "clone",
                *branch,
                "https://example.invalid/anonymous/driver-port-factory.git",
                factory,
            ]
        )
    if not (factory / ".venv/bin/python").exists():
        python = "/usr/bin/python3.11"
        command([python, "-m", "venv", factory / ".venv"])
    python = factory / ".venv/bin/python"
    installed = subprocess.run(
        [python, "-c", "import driver_port_factory.cli"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if installed.returncode:
        has_pip = subprocess.run(
            [python, "-m", "pip", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if has_pip.returncode:
            command([python, "-m", "ensurepip", "--upgrade"])
        command([python, "-m", "pip", "install", "-e", factory])
    binary = Path(profile["codex_bin"])
    if binary.is_absolute():
        if binary.parent.name != "bin":
            raise ValueError("An installed codex_bin must be PREFIX/bin/codex")
        current = (
            subprocess.run(
                [binary, "--version"], capture_output=True, text=True, check=False
            )
            if binary.exists()
            else None
        )
        if (
            current is None
            or current.returncode
            or current.stdout.strip() != "codex-cli " + profile["codex_version"]
        ):
            command(
                [
                    "npm",
                    "install",
                    "--prefix",
                    binary.parent.parent,
                    "--global",
                    "@openai/codex@" + profile["codex_version"],
                ]
            )
    config = catalog()["virtio-rng"][1]
    found = subprocess.run(
        ["docker", "image", "inspect", config["image_id"]],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if found.returncode:
        command(["docker", "pull", config["image"]])
    else:
        tagged = subprocess.run(
            ["docker", "image", "inspect", config["image"]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if tagged.returncode:
            command(["docker", "tag", config["image_id"], config["image"]])
    image = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", config["image"]],
        text=True,
    ).strip()
    if image != config["image_id"]:
        raise ValueError(
            "Official Docker tag differs from the frozen image ID; no substitution"
        )
    print(
        "Installed. Run doctor next; KVM and Docker permissions are host responsibilities."
    )


def doctor(profile, driver, require_suite=True):
    checks = []

    def record(name, action):
        try:
            action()
            checks.append({"check": name, "status": "PASS"})
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # Never echo a provider request or credential-bearing environment.
            checks.append({"check": name, "status": "FAIL", "reason": str(error)})

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    config = catalog()[driver][1]
    record(
        "python3.11",
        lambda: command(
            ["python3.11", "-c", "import sys; assert sys.version_info >= (3,11,8)"],
            capture_output=True,
        ),
    )
    record(
        "host-linux-x86_64",
        lambda: require(
            sys.platform == "linux" and os.uname().machine == "x86_64",
            "Use an x86_64 Linux host/VM with KVM",
        ),
    )
    record(
        "KVM device",
        lambda: require(Path("/dev/kvm").is_char_device(), "KVM device is missing"),
    )
    record(
        "bubblewrap namespaces",
        lambda: command(
            [
                shutil.which("bwrap") or str(profile["factory"] / ".local/bin/bwrap"),
                "--unshare-user",
                "--unshare-pid",
                "--ro-bind",
                "/",
                "/",
                "--",
                "/bin/true",
            ],
            capture_output=True,
        ),
    )
    record("git", lambda: command(["git", "--version"], capture_output=True))
    record("strace", lambda: command(["strace", "--version"], capture_output=True))
    record(
        "official Docker image",
        lambda: require(
            subprocess.check_output(
                ["docker", "image", "inspect", "--format", "{{.Id}}", config["image"]],
                text=True,
            ).strip()
            == config["image_id"],
            "Docker image ID differs from the frozen official image",
        ),
    )
    record(
        "Docker KVM access",
        lambda: command(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network",
                "none",
                "--device",
                "/dev/kvm",
                config["image_id"],
                "python3",
                "-c",
                "import fcntl; f=open('/dev/kvm','rb'); assert fcntl.ioctl(f.fileno(),0xAE00,0)==12",
            ],
            capture_output=True,
            timeout=30,
        ),
    )
    record(
        "Codex version",
        lambda: require(
            subprocess.check_output(
                [profile["codex_bin"], "--version"], text=True
            ).strip()
            == "codex-cli " + profile["codex_version"],
            "Install pinned Codex " + profile["codex_version"],
        ),
    )
    record(
        "Factory installation",
        lambda: command(
            [
                profile["factory"] / ".venv/bin/python",
                "-c",
                "import driver_port_factory.cli",
            ],
            capture_output=True,
        ),
    )
    if network_required(config["drivers"][driver]):
        record(
            "TUN",
            lambda: require(
                Path("/dev/net/tun").is_char_device(),
                "The configured TAP network requires /dev/net/tun",
            ),
        )
    if require_suite:
        record("prepared shared assets", lambda: verify_suite(profile, driver))
    print(json.dumps(checks, indent=2, ensure_ascii=False))
    return all(c["status"] == "PASS" for c in checks)


def verify_suite(profile, driver):
    from common import read, sha

    from driver_port_factory.platform.native_runner import runtime_identity

    root = selected_suite(profile, driver)
    task = read(root / "seeds" / driver / "task.json")
    env = read(root / "environment.json")
    # A selected prepared suite is an immutable cohort, including older revisions.
    # New suites use the current common catalog; never relabel archived inputs.
    config = read(root / "config.json")
    for key in (
        "image_id",
        "source_revision",
        "target_revision",
        "qemu_evidence_revision",
    ):
        if key == "qemu_evidence_revision":
            expected = config["qemu_revision"]
        elif key == "image_id":
            # Older suites freeze this field in task.json, not config.json.
            expected = config.get("image_id") or task["environment"]["image_id"]
        else:
            expected = config[key]
        if env[key] != expected:
            raise ValueError("Suite differs from frozen " + key)
    for path, key in (
        ("shared/sdk/cargo-osdk", "sdk_sha256"),
        ("shared/initramfs.cpio.gz", "initramfs_sha256"),
    ):
        if sha(root / path) != env[key]:
            raise ValueError("Shared asset changed: " + path)
    if env["runtime_identity"] != runtime_identity():
        raise ValueError("Suite and operator runner identities differ")
    collectors = dict(env.get("runtime_collectors", {}))
    # Read the first published suite's frozen receipt without rewriting its inputs.
    if env.get("intel_runtime_sha256"):
        collectors["intel_runtime.py"] = env["intel_runtime_sha256"]
    for script, digest in collectors.items():
        runner = profile["factory"] / "scripts/native_experiment" / script
        if sha(runner) != digest:
            raise ValueError("Frozen test collector changed: " + script)
    audit = read(root / "operator" / driver / "seed-audit.json")
    if audit["status"] != "PASS" or audit["root_commit"] != task["target_root_commit"]:
        raise ValueError("Sanitized seed has no matching successful audit")
    source = root / "seeds" / driver / "source"
    if (
        task.get("source_scope") != "complete-linux-tree"
        or task["source_root_commit"] != config["source_revision"]
    ):
        raise ValueError("Task must use the frozen complete Linux input")
    for relative in (
        "Makefile",
        "kernel/sched/core.c",
        "mm/memory.c",
        "drivers/input/input.c",
    ):
        command(
            [
                "git",
                "-C",
                source,
                "cat-file",
                "-e",
                config["source_revision"] + ":" + relative,
            ],
            capture_output=True,
        )
    if not git_has(profile["repositories"]["qemu"], config["qemu_revision"]):
        raise ValueError("Pinned QEMU source is missing")


def prepare_environment(profile, driver):
    if profile.get("suite_release") and catalog()[driver][0] in {"native", "public"}:
        from frozen_suite import prepare_frozen

        prepare_frozen(profile, driver)
        return
    from common import read
    from prepare import create_hollow, prepare, publish

    _, config = catalog()[driver]
    root = selected_suite(profile, driver)
    if (root / "seeds" / driver / "task.json").exists():
        verify_suite(profile, driver)
        print("Reusing prepared driver:", driver)
        return
    repos = profile["repositories"]
    source = acquire(
        repos["linux"],
        "https://github.com/torvalds/linux.git",
        [config["source_revision"]],
    )
    target = acquire(
        repos["asterinas"],
        "https://github.com/asterinas/asterinas.git",
        [config["target_revision"]],
    )
    qemu = acquire(
        repos["qemu"], "https://github.com/qemu/qemu.git", [config["qemu_revision"]]
    )
    if config.get("suite_kind") == "fixed-public-fixture":
        prepare_public(profile, root, source, target, qemu, driver)
        return
    if not root.exists():
        prepare(root, source, target, qemu)
    if (root / "seeds" / driver / "task.json").exists():
        verify_suite(profile, driver)
        print("Reusing prepared driver:", driver)
        return
    receipt = root / "operator" / driver / "positive.json"
    if not receipt.exists():
        operator_check(root, driver, root / "operator/reference", "positive")
    if read(receipt)["status"] != "PASS":
        raise ValueError(
            "Original reference control failed; preserve evidence and stop"
        )
    hollow = root / "operator/hollow" / driver / "target"
    if not hollow.exists():
        create_hollow(root, driver)
    negative = root / "operator" / driver / "negative.json"
    if not negative.exists():
        operator_check(root, driver, hollow, "negative")
    publish(root, driver, source)
    clean_intermediates(root, hollow)
    clean_intermediates(root, root / "operator/reference")
    verify_suite(profile, driver)


def operator_check(root, driver, target, mode):
    from check import check

    result = check(root, driver, target, root / "operator" / driver, mode)
    expected = "NEGATIVE_CONTROL_PASS" if mode == "negative" else "PASS"
    if result["status"] != expected:
        raise ValueError(
            "Control failed; raw receipt retained: " + str(root / "operator" / driver)
        )


def prepare_public(profile, root, source, target, qemu, driver):
    from argparse import Namespace

    from check import check
    from common import read, write
    from public_prepare import prepare

    if not root.exists():
        reuse = profile["suites"]["native"]
        if not (reuse / "environment.json").exists():
            from prepare import prepare as prepare_shared

            native = catalog()["virtio-rng"][1]
            repos = profile["repositories"]
            acquire(
                repos["linux"],
                "https://github.com/torvalds/linux.git",
                [native["source_revision"]],
            )
            acquire(
                repos["asterinas"],
                "https://github.com/asterinas/asterinas.git",
                [native["target_revision"]],
            )
            prepare_shared(reuse, repos["linux"], repos["asterinas"], qemu)
            clean_intermediates(reuse, reuse / "operator/reference")
        prepare(
            Namespace(
                root=root,
                source=source,
                target=target,
                qemu=qemu,
                reuse=reuse,
                config=REPO
                / "configs/experiments"
                / (catalog()[driver][0] + "-drivers.json"),
                assets=REPO / "src/driver_port_factory/data/public-tests",
            )
        )
    negative = root / "operator" / driver / "negative.json"
    if not negative.exists():
        target = root / "seeds" / driver / "target"
        result = check(root, driver, target, negative.parent, "negative")
    else:
        result = read(negative)
    # These fixtures assert absence directly; the native-only collector expects device_present.
    rows = result["tests"]
    good = (
        result["build"]["exit_code"] == 0
        and result.get("boot_passed") is True
        and result.get("source_unchanged") is True
        and len(rows)
        == len(catalog()[driver][1]["drivers"][driver]["tests"])
        + len(catalog()[driver][1]["drivers"][driver]["benchmarks"])
        and all(
            r["status"] == "FAIL" and "GUEST_ASSERT_FAILED" in r.get("error", "")
            for r in rows
        )
    )
    if not good:
        raise ValueError(
            "Driver-absent control was inconclusive; preserve evidence and stop"
        )
    write(
        negative.with_name("negative-interpretation.json"),
        {
            "status": "PASS",
            "raw_collector_status": result["status"],
            "raw_receipt": str(negative),
            "baseline_boot_passed": True,
            "all_three_driver_assertions_failed": True,
            "positive_original_reference": "NOT_APPLICABLE: driver absent upstream",
        },
    )
    clean_intermediates(root, root / "seeds" / driver / "target")
    verify_suite(profile, driver)


def clean_intermediates(root, target):
    from common import read

    if not target.is_relative_to(root) or not (target / "target").exists():
        return
    # Retain final ISO, build lock, all receipts and raw runtime logs.
    command(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "-v",
            str(target / "target") + ":/cache",
            read(root / "environment.json")["image_id"],
            "python3",
            "-c",
            (
                "import shutil; [shutil.rmtree('/cache/'+p, ignore_errors=True) "
                "for p in ['debug','x86_64-unknown-none','osdk/iso_root']]"
            ),
        ]
    )


def preflight(protocol, path):
    """Exercise the actual worker namespace and Docker broker without any model call."""
    import tempfile

    from common import write
    from comparison import now, trial_path
    from worker_isolation import isolated_worker

    rows = []
    for method in protocol.get("methods", ("dpf", "codex")):
        trial = trial_path(
            Path(protocol["root"]), protocol["driver"], protocol["trial"], method
        )
        with tempfile.TemporaryDirectory(
            prefix="preflight-", dir=path.parent
        ) as temporary:
            directory = Path(temporary)
            with isolated_worker(protocol, trial, directory, method) as prefix:
                # No provider settings/key are supplied to this infrastructure-only command.
                probe = (
                    "import os,subprocess; "
                    "assert not os.path.exists(" + repr(str(REPO / "tasks")) + "); "
                    "assert not os.path.exists("
                    + repr(str(Path(protocol["root"]) / "operator"))
                    + "); "
                    "subprocess.run(['docker','run','--rm','--pull=never','--network','none',"
                    "'--device','/dev/kvm',"
                    + repr(protocol["task"]["environment"]["image_id"])
                    + ","
                    "'python3','-c',\"import fcntl; f=open('/dev/kvm','rb'); "
                    'assert fcntl.ioctl(f.fileno(),0xAE00,0)==12"],check=True)'
                )
                result = subprocess.run(
                    [*prefix[:-1], "/usr/bin/python3.11", "-c", probe],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
                rows.append(
                    {
                        "method": method,
                        "exit_code": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
                write(
                    path.parent / "preflight.json",
                    {"checked_at": now(), "checks": rows},
                )
                if result.returncode:
                    raise ValueError(
                        "Worker isolation/Docker preflight failed; see preflight.json; no model started"
                    )
