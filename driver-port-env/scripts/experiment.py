#!/usr/bin/env python3.11
"""Team entry point for environment setup and isolated driver experiments."""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "src"),
    str(Path(__file__).resolve().parent / "native_experiment"),
]

from storage_budget import reserve
from team_environment import (
    doctor,
    install,
    preflight,
    prepare_environment,
    verify_suite,
)
from team_profile import (
    DEFAULT_PROFILE,
    REPO,
    catalog,
    initialize,
    model_environment,
    read_profile,
    selected_suite,
)

METHODS = {"pair": ["dpf", "codex"], "factory": ["dpf"], "codex": ["codex"]}


def trial_id(value):
    import re

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise argparse.ArgumentTypeError(
            "Use a short trial ID containing letters, numbers, _ or -"
        )
    return value


def protocol_path(profile, args):
    return (
        selected_suite(profile, args.driver)
        / "comparisons"
        / args.trial
        / args.driver
        / "protocol.json"
    )


def prepare_run(profile, args):
    from comparison import prepare

    verify_suite(profile, args.driver)
    path = protocol_path(profile, args)
    prepare(
        argparse.Namespace(
            root=selected_suite(profile, args.driver),
            driver=args.driver,
            trial=args.trial,
            qemu=profile["repositories"]["qemu"],
            factory=profile["factory"],
            model=args.model or profile["model"],
            reasoning_effort=args.reasoning_effort or profile["reasoning_effort"],
            codex_bin=profile["codex_bin"],
            base_url=args.base_url or profile["base_url"],
            provider_name=profile.get("provider_name", "example-provider"),
            model_catalog_json=profile.get("model_catalog_json"),
            effective_reasoning_effort=profile.get("effective_reasoning_effort"),
            pricing=args.pricing.resolve() if args.pricing else profile["pricing"],
            methods=METHODS[args.method],
            max_cost_usd=args.max_cost_usd,
            max_seconds=args.max_seconds,
        )
    )
    return path


def launch_command(profile, path, method):
    return [
        str(profile["factory"] / ".venv/bin/python"),
        str(REPO / "scripts/compare-experiments.py"),
        "run",
        str(path),
        "--method",
        method,
    ]


def run(profile, args):
    if not doctor(profile, args.driver):
        raise ValueError("Environment checks failed; no model started")
    args.parallel = args.method == "pair" and (
        args.parallel or profile.get("parallel_methods", False)
    )
    with reserve(
        selected_suite(profile, args.driver),
        f"{args.trial}/{args.driver}/{args.method}",
        len(METHODS[args.method]),
    ):
        run_reserved(profile, args)


def run_reserved(profile, args):
    from common import read

    environment = model_environment(profile)
    path = protocol_path(profile, args)
    if path.exists():
        protocol = read(path)
        if protocol.get("methods", METHODS["pair"]) != METHODS[args.method]:
            raise ValueError(
                "Method selection differs from prepared protocol; use its original selection or a new trial"
            )
        # A prepared protocol owns the model configuration. Credentials remain external.
        overrides = {
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
            "base_url": args.base_url,
            "pricing": args.pricing,
            "usd": args.max_cost_usd,
            "seconds": args.max_seconds,
        }
        expected = {
            **protocol,
            **protocol["limits"],
            "base_url": protocol["provider"]["base_url"],
        }
        if args.pricing:
            overrides["pricing"] = read(args.pricing)
        if any(
            value is not None and value != expected[key]
            for key, value in overrides.items()
        ):
            raise ValueError("Cannot change a frozen protocol; prepare a new trial")
        if (
            Path(protocol["factory"]) != profile["factory"]
            or protocol["codex_bin"] != profile["codex_bin"]
        ):
            raise ValueError("Factory/Codex paths differ from prepared protocol")
    else:
        path = prepare_run(profile, args)
        protocol = read(path)
    for method in METHODS[args.method]:
        if (path.parent / method).exists():
            raise ValueError(
                "This trial has already started; use a new --trial, not an automatic retry"
            )
    preflight(protocol, path)
    print("Frozen model:", protocol["model"], protocol["reasoning_effort"], flush=True)
    print("Protocol:", path, flush=True)
    jobs = []
    failed = False
    try:
        for method in METHODS[args.method]:
            log = path.parent / (method + ".operator.log")
            with log.open("x") as stream:
                process = subprocess.Popen(
                    launch_command(profile, path, method),
                    env=environment,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                )
            jobs.append((method, process))
            print(f"{method}: PID {process.pid}; log {log}", flush=True)
            if not args.parallel:
                failed = process.wait() != 0 or failed
        if args.parallel:
            for _, process in jobs:
                failed = process.wait() != 0 or failed
    except KeyboardInterrupt:
        for _, process in jobs:
            if process.poll() is None:
                process.send_signal(2)
        for _, process in jobs:
            process.wait()
        raise
    status(profile, args)
    if failed:
        raise ValueError(
            "At least one run failed; records retained, no automatic retry"
        )


def status(profile, args):
    path = protocol_path(profile, args)
    if not path.exists():
        raise ValueError("No protocol at " + str(path))
    protocol = json.loads(path.read_text())
    print("Protocol:", path)
    for method in protocol.get("methods", METHODS["pair"]):
        directory = path.parent / method
        record = directory / "run.json"
        result = directory / "result.json"
        print(
            method + ":",
            record.read_text() if record.exists() else "PREPARED; not started",
        )
        if result.exists():
            print("Result:", result)
        print("Log:", path.parent / (method + ".operator.log"))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    commands = result.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="create an ignored local profile")
    init.add_argument(
        "--ask-key",
        action="store_true",
        help="save hidden input in a private local file",
    )
    setup = commands.add_parser(
        "install", help="install Python/CLI dependencies and official Docker image"
    )
    setup.add_argument(
        "--system",
        action="store_true",
        help="also install apt packages (Debian/Ubuntu)",
    )
    check = commands.add_parser(
        "doctor", help="read-only host and prepared-suite checks"
    )
    check.add_argument("--driver", choices=catalog(), default="virtio-rng")
    check.add_argument("--host-only", action="store_true")
    env = commands.add_parser(
        "prepare-env",
        help="fetch pinned source and prepare/reuse one driver environment; no model",
    )
    env.add_argument("--driver", required=True, choices=catalog())
    for action in ("prepare", "run", "status"):
        command = commands.add_parser(action)
        command.add_argument("--driver", choices=catalog(), required=True)
        command.add_argument(
            "--trial",
            type=trial_id,
            required=action == "status",
            default=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
        )
        if action == "status":
            continue
        command.add_argument("--method", choices=METHODS, default="pair")
        command.add_argument("--model")
        command.add_argument(
            "--reasoning-effort", choices=("low", "medium", "high", "xhigh")
        )
        command.add_argument("--base-url")
        command.add_argument("--pricing", type=Path)
        command.add_argument("--max-cost-usd", type=float)
        command.add_argument("--max-seconds", type=float)
        if action == "run":
            command.add_argument(
                "--parallel",
                action="store_true",
                help="run both methods concurrently; default sequential",
            )
    dump = commands.add_parser(
        "export", help="export existing measurements; no model or test"
    )
    dump.add_argument(
        "--driver",
        choices=catalog(),
        required=True,
        help="select the suite containing this driver",
    )
    dump.add_argument("--output", type=Path, required=True)
    return result


def main():
    cli = parser()
    args = cli.parse_args()
    try:
        if args.action == "init":
            initialize(args.profile, args.ask_key)
            return
        profile = read_profile(args.profile)
        if getattr(args, "base_url", None):
            # Validate overrides with the same URL rules as the profile.
            from team_profile import validate_url

            validate_url(args.base_url)
        for key in ("max_cost_usd", "max_seconds"):
            value = getattr(args, key, None)
            if value is not None and value <= 0:
                raise ValueError(
                    "Explicit budgets must be positive; omit for unlimited"
                )
        if args.action == "install":
            install(profile, args.system)
        elif args.action == "doctor":
            if not doctor(profile, args.driver, not args.host_only):
                raise ValueError("Some environment checks failed")
        elif args.action == "prepare-env":
            prepare_environment(profile, args.driver)
        elif args.action == "prepare":
            prepare_run(profile, args)
        elif args.action == "run":
            run(profile, args)
        elif args.action == "status":
            status(profile, args)
        elif args.action == "export":
            from comparison import export

            args.output = args.output.resolve()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            export(
                argparse.Namespace(
                    root=selected_suite(profile, args.driver), output=args.output
                )
            )
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        cli.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
