#!/usr/bin/env python3.11
"""Prepare and run a separate OpenHands baseline against the unchanged suites."""

import argparse
import csv
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NATIVE = REPO / "scripts/native_experiment"
sys.path[:0] = [str(NATIVE), str(REPO / "scripts")]

from baseline_prompt import render
from behavior_scope import digest
from common import git, read, sha, write
from comparison import configure_factory
from comparison_metrics import empty_outcome, native_outcome, quote
from docker_broker import DockerBroker
from experiment import trial_id
from prepare import export, root_commit, setup_assets
from storage_budget import reserve
from team_profile import catalog, read_profile, selected_suite, validate_url

PINS = REPO / "configs/experiments/openhands-pins-20261010.json"
LOCK = REPO / "configs/experiments/openhands-dependencies-20261010.txt"


def now():
    return datetime.now(UTC).isoformat()


def load(path):
    config = read_profile(path)
    # Preserve the venv executable path: resolving its symlink selects base Python.
    config["openhands_python"] = Path(
        os.path.abspath(path.parent / config["openhands_python"])
    )
    validate_url(config["llm"]["base_url"])
    if "api_key" in config["llm"]:
        raise ValueError("Keep API credentials in api_key_file or api_key_env")
    if config["llm"]["model"].split("/", 1)[-1] != config["model"]:
        raise ValueError("LLM transport model must identify the same configured model")
    if config["llm"]["reasoning_effort"] != config["reasoning_effort"]:
        raise ValueError("LLM effort differs from the experiment configuration")
    if config["llm"]["base_url"] != config["base_url"]:
        raise ValueError("LLM URL differs from the experiment configuration")
    return config


def init(args):
    original = read_profile(args.from_profile.resolve())
    original["openhands_python"] = str(REPO / ".local/openhands-venv/bin/python")
    original["llm"] = {
        "model": "openai/" + original["model"],
        "base_url": original["base_url"],
        "api_mode": "responses",
        "reasoning_effort": original["reasoning_effort"],
        "usage_id": "agent",
        "capability_overrides": {
            "supports_reasoning_effort": True,
            "supports_responses_api": True,
            "supports_sampling_params": False,
            "supports_vision": False,
        },
    }
    # read_profile resolves paths to Path values; serialize only non-secret settings.
    args.profile.parent.mkdir(parents=True, exist_ok=True)
    with args.profile.open("x") as stream:
        json.dump(original, stream, indent=2, default=str)
        stream.write("\n")
    args.profile.chmod(0o600)
    if args.ask_key:
        key = getpass.getpass("OpenHands API key（隐藏输入）：").strip()
        if not key or "\n" in key or "\r" in key:
            raise ValueError("API key must be a nonempty single line")
        key_path = args.profile.with_suffix(".key")
        with key_path.open("x") as stream:
            stream.write(key + "\n")
        key_path.chmod(0o600)
        original["api_key_file"] = str(key_path.resolve())
        args.profile.write_text(json.dumps(original, indent=2, default=str) + "\n")
    print("Edit your non-secret model configuration:", args.profile)


def setup(args):
    pins = read(PINS)
    for name, item in pins["repos"].items():
        checkout = args.checkout_parent / (
            "OpenHands" if name == "canvas" else "openhands-software-agent-sdk"
        )
        if not checkout.exists():
            subprocess.run(["git", "init", str(checkout)], check=True)
            subprocess.run(
                ["git", "-C", str(checkout), "remote", "add", "origin", item["url"]],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "fetch",
                    "--depth=1",
                    "origin",
                    item["commit"],
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(checkout), "checkout", "--detach", "FETCH_HEAD"],
                check=True,
            )
        if git(checkout, "rev-parse", "HEAD") != item["commit"]:
            raise ValueError(
                "Checkout differs from frozen OpenHands version: " + str(checkout)
            )
    uv = shutil.which("uv")
    if not uv:
        raise ValueError(
            "Install uv first; no changes to the existing Codex/Factory environments"
        )
    environment = REPO / ".local/openhands-venv"
    if not environment.exists():
        subprocess.run([uv, "venv", "--python", "3.12", str(environment)], check=True)
    python = environment / "bin/python"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), "-r", str(LOCK)], check=True
    )
    sdk = args.checkout_parent / "openhands-software-agent-sdk"
    subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-deps",
            "--reinstall",
            str(sdk / "openhands-sdk"),
            str(sdk / "openhands-tools"),
        ],
        check=True,
    )
    print("OpenHands runtime:", python)


def doctor(config):
    python = config["openhands_python"]
    if not python.is_file():
        raise ValueError("Run setup first: missing OpenHands Python")
    output = subprocess.check_output(
        [
            str(python),
            "-c",
            (
                "import importlib.metadata as m,json,sys; "
                "print(json.dumps({'base':sys.base_prefix,'sdk':m.version('openhands-sdk'),"
                "'tools':m.version('openhands-tools')}))"
            ),
        ],
        text=True,
    )
    runtime = json.loads(output)
    if (
        runtime["sdk"] != read(PINS)["sdk_version"]
        or runtime["tools"] != runtime["sdk"]
    ):
        raise ValueError("OpenHands version differs from frozen pins")
    if not shutil.which("tmux"):
        raise ValueError("OpenHands TerminalTool requires tmux")
    bwrap = shutil.which("bwrap") or str(config["factory"] / ".local/bin/bwrap")
    if not Path(bwrap).is_file():
        raise ValueError("Missing namespace launcher; no host-permission fallback")
    if not Path("/dev/kvm").is_char_device():
        raise ValueError("KVM unavailable; do not use TCG")
    runtime.update(
        python=str(python), bwrap=bwrap, pins=read(PINS), lock_sha256=sha(LOCK)
    )
    return runtime


def prepare(args, config):
    runtime = doctor(config)
    root = selected_suite(config, args.driver)
    task = read(root / "seeds" / args.driver / "task.json")
    # Reuse the historical task.scope, without importing the later expanded registry.
    task.pop("behavior_scope", None)
    environment = read(root / "environment.json")
    actual_image = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", environment["image_id"]],
        text=True,
    ).strip()
    if actual_image != environment["image_id"]:
        raise ValueError("Frozen Asterinas image unavailable")
    task_env = task["environment"]
    if task_env["image_id"] != environment["image_id"]:
        raise ValueError("Task and suite image differ")
    seed = root / "seeds" / args.driver
    audit = read(root / "operator" / args.driver / "seed-audit.json")
    if audit["status"] != "PASS" or audit["root_commit"] != task["target_root_commit"]:
        raise ValueError("Sanitized seed is not audited")
    trial = root / "trials/openhands" / args.driver / args.trial
    output = root / "comparisons" / args.trial / args.driver / "openhands"
    if trial.exists() or output.exists():
        raise ValueError("Use a new trial ID; never overwrite an existing attempt")
    configure_factory(config["factory"])
    trial.mkdir(parents=True)
    output.mkdir(parents=True)
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--no-hardlinks",
            str(seed / "target"),
            str(trial / "target"),
        ],
        check=True,
    )
    git(trial / "target", "remote", "remove", "origin")
    export(seed / "source", task["source_root_commit"], trial / "source")
    root_commit(trial / "source", force=True)
    setup_assets(root, trial / "target")
    # Keep the historical short scope; retain approved TAP/public execution guidance.
    prompt = render(
        task,
        trial,
        config["repositories"]["qemu"],
        read(root / "config.json"),
        root / "shared",
        include_behavior_scope=False,
        require_public_validation=True,
    )
    prompt = (
        prompt.replace("# Pure Codex + goal:", "# OpenHands:")
        .replace(
            "Pursue the native persistent goal established for this session.",
            "Pursue this task using the native OpenHands agent execution loop.",
        )
        .replace("Goal completion alone is not", "Agent completion alone is not")
    )
    (trial / "PROMPT.md").write_text(prompt)
    pricing = read(config["pricing"])
    if config["model"] not in pricing["models"]:
        raise ValueError("Add official prices for this model before preparation")
    protocol = {
        "schema_version": 1,
        "method": "openhands",
        "driver": args.driver,
        "trial": args.trial,
        "created_at": now(),
        "root": str(root),
        "workspace": str(trial),
        "task": task,
        "environment": environment,
        "task_scope_sha256": digest(task["scope"]),
        "task_scope_policy": "historical-task.scope-without-expanded-behavior-scope",
        "completion_policy": "public-runtime-self-test-before-native-finish",
        "model": config["model"],
        "reasoning_effort": config["reasoning_effort"],
        "llm": config["llm"],
        "pricing": pricing,
        "runtime": runtime,
        "qemu": str(config["repositories"]["qemu"]),
        "prompt_sha256": sha(trial / "PROMPT.md"),
        "factory_operator_revision": git(config["factory"], "rev-parse", "HEAD"),
        "env_revision": git(REPO, "rev-parse", "HEAD"),
        "limits": {"seconds": None, "cost": None, "tokens": None},
        "evaluation_mode": "DEVELOPER_EVIDENCE",
        "producer_private_feedback": "NONE",
        "knowledge_condition": "no Factory tools, KB, Skill or reviewer",
        "tools": ["TerminalTool", "FileEditorTool", "TaskTrackerTool"],
        "condenser": "official default CLI preset; all summarization usage counted",
        "source_identity": {"root_commit": git(trial / "source", "rev-parse", "HEAD")},
        "target_identity": {"root_commit": git(trial / "target", "rev-parse", "HEAD")},
    }
    write(output / "protocol.json", protocol)
    print("Prepared; no model called:", output / "protocol.json")
    return output


def namespace(protocol, output, socket):
    trial = Path(protocol["workspace"])
    runtime = protocol["runtime"]
    home = output / "model-home"
    home.mkdir(exist_ok=False)
    entry = home / "worker.py"
    shutil.copyfile(REPO / "scripts/openhands_baseline/worker.py", entry)
    write(
        home / "worker-config.json",
        {
            "llm": protocol["llm"],
            "workspace": str(trial),
            "prompt": str(trial / "PROMPT.md"),
        },
    )
    venv = Path(runtime["python"]).parent.parent
    manifest = {
        "method": "openhands",
        "bwrap": runtime["bwrap"],
        "cwd": str(trial),
        "home": str(home),
        "broker": str(socket),
        "docker_proxy": str(NATIVE / "docker_proxy.py"),
        "readonly": list(
            map(
                str,
                [
                    trial / "source",
                    trial / "PROMPT.md",
                    *(
                        [trial / "behavior-scope.json"]
                        if "behavior_scope" in protocol.get("task", {})
                        else []
                    ),
                    Path(protocol["root"]) / "shared",
                    Path(protocol["qemu"]),
                    venv,
                    Path(runtime["base"]),
                ],
            )
        ),
        "writable": [str(trial / "target"), str(home)],
    }
    write(output / "namespace.json", manifest)
    return [
        sys.executable,
        str(NATIVE / "worker_sandbox.py"),
        "--manifest",
        str(output / "namespace.json"),
        "--",
        runtime["python"],
        str(entry),
        str(home / "worker-config.json"),
    ]


def usage(output):
    path = output / "model-home/metrics.json"
    if not path.exists():
        return None
    records = []
    seen = set()
    for slot, metrics in read(path).items():
        for row in metrics["token_usages"]:
            # Incremental records, never sum cumulative snapshots. Slots are distinct.
            identity = (slot, row["response_id"])
            if row["response_id"] and identity in seen:
                continue
            seen.add(identity)
            records.append(row)
    if not records:
        return None
    fields = {
        "input_tokens": "prompt_tokens",
        "cached_input_tokens": "cache_read_tokens",
        "output_tokens": "completion_tokens",
        "cache_write_input_tokens": "cache_write_tokens",
        "reasoning_output_tokens": "reasoning_tokens",
    }
    total = {name: sum(r[field] for r in records) for name, field in fields.items()}
    total["requests_with_usage"] = len(records)
    if total["cached_input_tokens"] > total["input_tokens"]:
        raise ValueError("Invalid cache accounting; preserve metrics for audit")
    return total


def run(args, config):
    with reserve(
        selected_suite(config, args.driver), f"{args.trial}/{args.driver}/openhands"
    ):
        run_reserved(args, config)


def run_reserved(args, config):
    root = selected_suite(config, args.driver)
    output = root / "comparisons" / args.trial / args.driver / "openhands"
    if not (output / "protocol.json").exists():
        output = prepare(args, config)
    protocol = read(output / "protocol.json")
    if protocol["llm"] != config["llm"] or protocol["model"] != config["model"]:
        raise ValueError("Model config changed after prepare; use a new trial ID")
    if (output / "run.json").exists():
        raise ValueError("Attempt already started; no automatic resume or retry")
    key = os.environ.get(config["api_key_env"], "")
    if not key:
        key_path = config.get("api_key_file")
        if not key_path or key_path.stat().st_mode & 0o077:
            raise ValueError("Supply API key environment or a mode-600 key file")
        key = key_path.read_text().strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("Missing valid API key")
    trial = Path(protocol["workspace"])
    record = {"method": "openhands", "state": "RUNNING", "started_at": now()}
    write(output / "run.json", record)
    start = time.monotonic()
    env = worker_environment(key)
    try:
        with tempfile.TemporaryDirectory(prefix="c2r-openhands-") as temporary:
            socket = Path(temporary) / "docker.sock"
            broker = DockerBroker(
                socket,
                protocol["environment"]["image_id"],
                [trial / "target"],
                [root / "shared", trial / "source", Path(protocol["qemu"])],
                allow_cli_version=True,
                enforce_frozen_tools=True,
                accelerator=protocol["environment"]["accelerator"],
                runtime=protocol["environment"],
            )
            thread = threading.Thread(target=broker.serve_forever, daemon=True)
            thread.start()
            try:
                argv = namespace(protocol, output, socket)
                with (output / "controller.log").open("x") as log:
                    result = subprocess.run(
                        argv, stdout=log, stderr=subprocess.STDOUT, env=env, check=False
                    )
                worker_state = output / "model-home/worker-result.json"
                agent_status = read(worker_state) if worker_state.exists() else {}
                record.update(
                    exit_code=result.returncode,
                    agent_status=agent_status,
                    stop_reason="AGENT_RETURNED"
                    if not result.returncode
                    else "MODEL_FAILED",
                )
            finally:
                broker.shutdown()
                broker.server_close()
                thread.join(timeout=5)
                containers = broker.known()
                if containers:
                    subprocess.run(
                        [broker.docker, "rm", "-f", *containers],
                        capture_output=True,
                        check=False,
                    )
        record.update(
            translation_ended_at=now(),
            translation_seconds=round(time.monotonic() - start, 3),
        )
        configure_factory(config["factory"])
        from check import check

        evaluation = time.monotonic()
        check(root, args.driver, trial / "target", output / "evaluation", "check")
        record.update(
            evaluation_seconds=round(time.monotonic() - evaluation, 3), state="FINISHED"
        )
    except Exception as error:
        record.update(state="ERROR", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        record.update(
            ended_at=now(),
            total_seconds=round(time.monotonic() - start, 3),
            usage=usage(output),
        )
        write(output / "run.json", record)
        collect(output)
    if args.private_score:
        score(args, config, protocol, output)
    clean(protocol, output)
    print("Finished:", output / "result.json")


def collect(output):
    protocol = read(output / "protocol.json")
    record = read(output / "run.json")
    record["usage"] = usage(output)
    observation = output / "evaluation/check.json"
    cases = [*protocol["task"]["tests"], *protocol["task"]["benchmarks"]]
    outcome = (
        native_outcome(read(observation), cases)
        if observation.exists()
        else empty_outcome(len(cases))
    )
    rates = protocol["pricing"]["models"][protocol["model"]]
    known = record["usage"]
    cost = quote(known, rates)
    # Missing responses or failed transport can leave usage partial; never claim zero cost.
    status = (
        "unknown"
        if known is None
        else ("partial" if record.get("exit_code", 1) else "estimated")
    )
    result = (
        record
        | outcome
        | (known or {})
        | {
            "driver": protocol["driver"],
            "trial": protocol["trial"],
            "model": protocol["model"],
            "reasoning_effort": protocol["reasoning_effort"],
            "cost_estimate": cost,
            "cost_status": status,
            "cost_currency": protocol["pricing"].get("currency", "USD"),
            "sdk_cost_is_paper_cost": False,
        }
    )
    observations = [
        row
        for path in sorted(
            (output / "private" / protocol["driver"] / "openhands").glob(
                "summary*.json"
            )
        )
        for row in read(path)
    ]
    for variant in ("A", "B"):
        group = [row for row in observations if row.get("variant") == variant]
        result["private_" + variant + "_passed"] = (
            sum(row["status"] == "PASS" for row in group) if group else None
        )
        result["private_" + variant + "_observed"] = len(group) if group else None
    write(output / "result.json", result)
    fields = [k for k, v in result.items() if not isinstance(v, (dict, list))]
    with (output / "measurements.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerow({k: result[k] for k in fields})
    return result


def worker_environment(key):
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "C2R_OPENHANDS_API_KEY": key,
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "DO_NOT_TRACK": "1",
        "OPENHANDS_SUPPRESS_BANNER": "1",
    }


def preflight(args, config):
    """Real SDK terminal and Docker route, with no LLM request or kernel build."""
    runtime = doctor(config)
    root = selected_suite(config, args.driver)
    environment = read(root / "environment.json")
    image = environment["image_id"]
    receipt = REPO / ".local/openhands-preflight.json"
    # UNIX sockets have a short path limit; keep this disposable scratch under /tmp.
    with tempfile.TemporaryDirectory(prefix="openhands-preflight-") as temporary:
        trial = Path(temporary)
        (trial / "target").mkdir()
        (trial / "source").mkdir()
        (trial / "source/context.txt").write_text("public context\n")
        (trial / "PROMPT.md").write_text(
            "Offline infrastructure preflight. No model request.\n"
        )
        protocol = {
            "workspace": str(trial),
            "runtime": runtime,
            "root": str(root),
            "qemu": str(config["repositories"]["qemu"]),
            "llm": config["llm"],
        }
        output = trial / "observations"
        output.mkdir()
        socket = trial / "docker.sock"
        broker = DockerBroker(
            socket,
            image,
            [trial / "target"],
            [root / "shared"],
            allow_cli_version=True,
            enforce_frozen_tools=True,
            accelerator=environment["accelerator"],
            runtime=environment,
        )
        thread = threading.Thread(target=broker.serve_forever, daemon=True)
        thread.start()
        try:
            argv = namespace(protocol, output, socket)
            checks = (
                "from pathlib import Path; "
                "assert not Path('/home/anonymous/.codex').exists(); "
                "assert not Path('/var/run/docker.sock').exists(); "
                f"assert not Path({str(config['factory'])!r}).exists(); "
                f"assert not Path({str(root / 'operator')!r}).exists(); "
                f"assert Path({str(trial / 'source/context.txt')!r}).read_text() == 'public context\\n'; "
                f"Path({str(trial / 'target/terminal.txt')!r}).write_text('SDK terminal works'); "
                "print('NAMESPACE_PASS')"
            )
            docker_checks = (
                "import os,shutil,subprocess; assert os.path.exists('/dev/kvm'); "
                "qemu=shutil.which('qemu-system-x86_64'); assert qemu; "
                "print(subprocess.check_output([qemu,'--version'],text=True)); "
                "print('DOCKER_KVM_PASS')"
            )
            # Empty paused QEMU, no kernel: verify real runtime-error delivery.
            rejected_guest = shlex.join(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network",
                    "none",
                    image,
                    "qemu-system-x86_64",
                    "-S",
                    "-display",
                    "none",
                    "-nodefaults",
                    "-m",
                    "8G",
                ]
            )
            command = (
                "python3 -c "
                + shlex.quote(checks)
                + " && docker --version && "
                + "( "
                + rejected_guest
                + "; test $? -eq 125 ) && "
                + shlex.join(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--pull=never",
                        "--network",
                        "none",
                        "--device",
                        "/dev/kvm",
                        image,
                        "python3",
                        "-c",
                        docker_checks,
                    ]
                )
            )
            worker_config = output / "model-home/worker-config.json"
            value = read(worker_config)
            value["preflight_command"] = command
            write(worker_config, value)
            result = subprocess.run(
                argv,
                env=worker_environment("offline-not-a-credential"),
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            observation = output / "model-home/preflight.json"
            terminal = read(observation) if observation.exists() else {}
            transcript = json.dumps(terminal)
            passed = (
                result.returncode == 0
                and "NAMESPACE_PASS" in transcript
                and "DOCKER_KVM_PASS" in transcript
                and "Fixed experiment environment error" in transcript
                and (trial / "target/terminal.txt").read_text() == "SDK terminal works"
            )
            write(
                receipt,
                {
                    "at": now(),
                    "status": "PASS" if passed else "FAIL",
                    "image_id": image,
                    "runtime": runtime,
                    "paid_model_calls": 0,
                    "kernel_builds": 0,
                    "observation": terminal,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
            )
            if not passed:
                raise ValueError("OpenHands preflight failed; see " + str(receipt))
        finally:
            broker.shutdown()
            broker.server_close()
            thread.join(timeout=5)
            containers = broker.known()
            if containers:
                subprocess.run(
                    [broker.docker, "rm", "-f", *containers],
                    capture_output=True,
                    check=False,
                )
    print(
        "SDK terminal, namespace and fixed Docker/KVM: PASS; no model calls.", receipt
    )


def score(args, config, protocol, output):
    from check import source_files

    target = Path(protocol["workspace"]) / "target"
    iso = target / "target/osdk/asterinas-osdk-bin.iso"
    manifest = {
        "schema_version": 1,
        "evaluation_mode": "DEVELOPER_EVIDENCE",
        "producer_feedback": "NONE",
        "candidates": [],
        "missing": [],
    }
    if iso.exists():
        manifest["candidates"].append(
            {
                "driver": args.driver,
                "method": "openhands",
                "target": str(target),
                "iso": str(iso),
                "iso_sha256": sha(iso),
                "suite": protocol["root"],
                "source_files": {
                    name: value
                    for name, value in source_files(target).items()
                    if value is not None
                    and not set(Path(name).parts).intersection(
                        {".git", "target", ".dpf-output", "__pycache__", "build"}
                    )
                },
                "provenance": {"trial": args.trial},
            }
        )
    else:
        manifest["missing"].append(
            {"driver": args.driver, "method": "openhands", "reason": "NO_FINAL_ISO"}
        )
    write(output / "candidates.json", manifest)
    with (output / "private-score.log").open("x") as log:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts/scoring/score.py"),
                "--factory",
                str(config["factory"]),
                "--manifest",
                str(output / "candidates.json"),
                "--driver",
                args.driver,
                "--scratch",
                str(output / "scratch"),
                "--output",
                str(output / "private"),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    write(
        output / "private-score-receipt.json",
        {"exit_code": result.returncode, "at": now()},
    )
    collect(output)


def clean(protocol, output):
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location(
        "c2r_batch_cleanup", REPO / "scripts/experiment-batch.py"
    )
    batch = module_from_spec(spec)
    spec.loader.exec_module(batch)
    batch.clean(
        Path(protocol["root"]),
        [Path(protocol["workspace"]) / "target"],
        output / "cleanup.json",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=REPO / ".local/openhands.json")
    sub = parser.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("init")
    initialize.add_argument("--from-profile", type=Path, required=True)
    initialize.add_argument("--ask-key", action="store_true")
    install = sub.add_parser("setup")
    install.add_argument("--checkout-parent", type=Path, default=REPO.parent)
    sub.add_parser("doctor")
    smoke = sub.add_parser("preflight")
    smoke.add_argument("--driver", choices=catalog(), default="virtio-rng")
    for name in ("prepare", "run", "status"):
        action = sub.add_parser(name)
        action.add_argument("--driver", choices=catalog(), required=True)
        action.add_argument("--trial", type=trial_id, required=True)
        if name == "run":
            action.add_argument("--private-score", action="store_true")
    args = parser.parse_args()
    args.profile = args.profile.resolve()
    if args.command == "init":
        init(args)
    elif args.command == "setup":
        setup(args)
    else:
        config = load(args.profile)
        if args.command == "doctor":
            print(json.dumps(doctor(config), indent=2))
        elif args.command == "preflight":
            preflight(args, config)
        elif args.command == "prepare":
            prepare(args, config)
        elif args.command == "run":
            run(args, config)
        else:
            output = (
                selected_suite(config, args.driver)
                / "comparisons"
                / args.trial
                / args.driver
                / "openhands"
            )
            for name in ("run.json", "result.json", "model-home/worker-result.json"):
                if (output / name).exists():
                    print(
                        name,
                        json.dumps(read(output / name), ensure_ascii=False, indent=2),
                    )


if __name__ == "__main__":
    main()
