#!/usr/bin/env python3.11
"""Run a frozen driver batch once, score externally and clean each completed driver's build cache."""

import argparse
import csv
import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / "src"),
    str(Path(__file__).resolve().parent / "native_experiment"),
]
from common import read, sha, write
from experiment import trial_id
from experiment_deadline import run_translation
from storage_budget import reserve
from team_profile import DEFAULT_PROFILE, REPO, catalog, read_profile, selected_suite

METHODS = {"pair": ("dpf", "codex"), "factory": ("dpf",), "codex": ("codex",)}


def now():
    return datetime.now(UTC).isoformat()


def clean(root, targets, output):
    # Only regenerable build intermediates, never final ISO or observations.
    paths = []
    for target in targets:
        target = target.resolve()
        if not target.is_relative_to(root) or target.is_symlink():
            raise ValueError("Cleanup target is outside this suite")
        build = target / "target"
        for relative in (
            "target/debug",
            "target/release",
            "target/x86_64-unknown-none",
            "target/native-tests/rootfs",
            "osdk/target/debug",
            "osdk/target/release",
        ):
            path = target / relative
            if path.exists():
                paths.append(str(path.relative_to(root)))
        paths.extend(
            str(p.relative_to(root))
            for p in (build / "osdk").glob("*")
            if p.name != "asterinas-osdk-bin.iso" and not p.is_symlink()
        )
    script = (
        "import json,os,shutil; from pathlib import Path; "
        "paths=json.loads(os.environ['C2R_CLEAN_PATHS']); "
        "[(shutil.rmtree(p) if p.is_dir() else p.unlink()) for p in "
        "[Path('/suite')/v for v in paths] if p.exists()]; print(json.dumps(paths))"
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "-v",
            str(root) + ":/suite",
            "-e",
            "C2R_CLEAN_PATHS=" + json.dumps(paths),
            read(root / "environment.json")["image_id"],
            "python3",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    write(
        output,
        {
            "at": now(),
            "exit_code": result.returncode,
            "removed": paths,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "final_iso_retained": True,
        },
    )
    if result.returncode:
        raise ValueError(
            "Cleanup failed; stop before accumulating another driver's cache"
        )


def seal(root, driver, trial, factory, destination, methods=("dpf", "codex")):
    from comparison import configure_factory, trial_path

    configure_factory(factory)
    from driver_port_factory.acquisition.repository import load_repository_acquisition
    from driver_port_factory.composition import open_project

    candidates, missing, targets = [], [], []
    scoring = catalog()[driver][1]["drivers"][driver].get("private_scoring")
    for method in methods:
        work = trial_path(root, driver, trial, method)
        if method == "codex":
            target = work / "target"
        else:
            project = open_project(work / "run", read_only=True, verify_artifacts=False)
            target = (
                work / "run" / load_repository_acquisition(project).target_worktree.path
            )
        targets.append(target)
        iso = target / "target/osdk/asterinas-osdk-bin.iso"
        if not iso.exists():
            missing.append(
                {"driver": driver, "method": method, "reason": "NO_FINAL_ISO"}
            )
            continue
        files = {}
        for directory, dirs, names in os.walk(target, followlinks=False):
            dirs[:] = [
                d
                for d in dirs
                if d not in {".git", "target", ".dpf-output", "__pycache__", "build"}
            ]
            for name in names:
                if name == ".git":
                    continue
                path = Path(directory) / name
                if path.is_file():
                    files[str(path.relative_to(target))] = sha(path)
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        candidates.append(
            {
                "driver": driver,
                "method": method,
                "target": str(target),
                "iso": str(iso),
                "iso_sha256": sha(iso),
                "suite": str(root),
                "source_files": files,
                "source_manifest_sha256": digest,
                "provenance": {"trial": trial},
                "private_scoring": scoring,
            }
        )
    write(
        destination,
        {
            "schema_version": 1,
            "created_at": now(),
            "evaluation_mode": "DEVELOPER_EVIDENCE",
            "private_oracle_files": {
                relative: sha(REPO / relative)
                for relative in [scoring["script"], *scoring.get("dependencies", [])]
            }
            if scoring
            else {},
            "test_author_candidate_exposure": "NON_INDEPENDENT operator context; frozen existing parameter variants",
            "candidate_freeze_relation": "sealed before this scoring run; tests existed before translation",
            "producer_feedback": "NONE; no repair or resume",
            "candidates": candidates,
            "missing": missing,
        },
    )
    return targets


def table(profile, args, state):
    rows = []
    for driver in args.drivers:
        root = selected_suite(profile, driver)
        protocol = root / "comparisons" / args.batch / driver / "protocol.json"
        for method in METHODS[args.method]:
            result = protocol.parent / method / "result.json"
            row = {
                "driver": driver,
                "method": method,
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "batch_state": state["drivers"].get(driver, {}).get("state", "QUEUED"),
            }
            if result.exists():
                r = read(result)
                row.update(
                    {k: v for k, v in r.items() if not isinstance(v, (dict, list))}
                )
                row.update(r.get("usage") or {})
            private = args.output / "private" / driver / method
            observations = [
                r for p in sorted(private.glob("summary*.json")) for r in read(p)
            ]
            for variant in ("A", "B"):
                group = [r for r in observations if r.get("variant") == variant]
                row["private_" + variant + "_passed"] = (
                    sum(r["status"] == "PASS" for r in group) if group else None
                )
                row["private_" + variant + "_observed"] = len(group) if group else None
            rows.append(row)
    fields = sorted({k for r in rows for k in r})
    with (args.output / "measurements.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    write(args.output / "measurements.json", rows)
    headers = [
        "驱动",
        "方法",
        "状态",
        "总时长（分钟）",
        "输入 token",
        "缓存读取 token",
        "缓存写入 token",
        "输出 token",
        "协议费率估计费用（" + state["pricing"].get("currency", "USD") + "）",
        "编译通过",
        "启动通过",
        "初始化通过",
        "公开测试通过",
        "公开测试函数通过",
        "私有 A 通过",
        "私有 B 通过",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]

    def value(v):
        return "" if v is None else str(v)

    def outcome(v):
        return "" if v is None else "1/1" if v is True else "0/1"

    for r in rows:
        private = [
            f"{r['private_' + v + '_passed']}/{r['private_' + v + '_observed']}"
            if r["private_" + v + "_observed"] is not None
            else ""
            for v in ("A", "B")
        ]
        data = [
            r["driver"],
            "Factory" if r["method"] == "dpf" else "Codex＋goal",
            r.get("state", r["batch_state"]),
            f"{r['total_seconds'] / 60:.2f}"
            if r.get("total_seconds") is not None
            else "",
            *[
                value(r.get(k))
                for k in [
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                ]
            ],
            value(r.get("cost_estimate", r.get("usd_estimate"))),
            outcome(r.get("build_passed")),
            outcome(r.get("boot_passed")),
            outcome(r.get("initialization_passed")),
            f"{r['cases_passed']}/{r['cases_expected']}" if "cases_passed" in r else "",
            f"{r['functions_passed']}/{r['functions_expected']}"
            if r.get("functions_expected") is not None
            else "",
            *private,
        ]
        lines.append("| " + " | ".join(data) + " |")
    (args.output / "comparison.md").write_text("\n".join(lines) + "\n")


def run_driver(task):
    profile, args, driver = task
    record = {"state": "RUNNING", "started_at": now()}
    try:
        with reserve(
            selected_suite(profile, driver),
            f"{args.batch}/{driver}/batch",
            len(METHODS[args.method]),
        ):
            argv = [
                sys.executable,
                str(REPO / "scripts/experiment.py"),
                "--profile",
                str(args.profile.resolve()),
                "run",
                "--driver",
                driver,
                "--trial",
                args.batch,
                "--method",
                args.method,
                "--model",
                args.model,
                "--reasoning-effort",
                args.reasoning_effort,
                "--pricing",
                str(args.pricing.resolve()),
            ]
            if args.parallel:
                argv.append("--parallel")
            max_seconds = getattr(args, "max_seconds", None)
            if max_seconds is not None:
                argv.extend(["--max-seconds", str(max_seconds)])
            with (args.output / (driver + ".log")).open("x") as log:
                result, timed_out = run_translation(
                    argv,
                    log,
                    max_seconds,
                    selected_suite(profile, driver),
                    driver,
                    args.batch,
                    METHODS[args.method],
                )
            record["translation_exit_code"] = result.returncode
            record["translation_timed_out"] = timed_out
            if result.returncode and not timed_out:
                raise ValueError(
                    "Run infrastructure error; evidence retained, no retry: " + driver
                )
            root = selected_suite(profile, driver)
            targets = seal(
                root,
                driver,
                args.batch,
                profile["factory"],
                args.output / (driver + "-candidates.json"),
                methods=METHODS[args.method],
            )
            if args.private_score:
                with (args.output / (driver + "-private.log")).open("x") as log:
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(REPO / "scripts/scoring/score.py"),
                            "--factory",
                            str(profile["factory"]),
                            "--manifest",
                            str(args.output / (driver + "-candidates.json")),
                            "--driver",
                            driver,
                            "--scratch",
                            str(args.output / "scratch"),
                            "--output",
                            str(args.output / "private"),
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                record["private_exit_code"] = result.returncode
            clean(root, targets, args.output / (driver + "-cleanup.json"))
        record.update(state="FINISHED", ended_at=now())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        record.update(state="ERROR", error=str(error), ended_at=now())
    return record


def run_tasks(tasks, jobs, worker, started, finished):
    """Bounded independent processes; only the parent publishes batch records."""
    if jobs == 1:
        for task in tasks:
            started(task)
            finished(task, worker(task))
        return
    with ProcessPoolExecutor(
        max_workers=jobs, mp_context=multiprocessing.get_context("fork")
    ) as pool:
        pending = {}
        remaining = iter(tasks)

        def submit_next():
            task = next(remaining, None)
            if task is not None:
                started(task)
                pending[pool.submit(worker, task)] = task

        for _ in range(jobs):
            submit_next()
        while pending:
            completed, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                task = pending.pop(future)
                finished(task, future.result())
                submit_next()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--batch", type=trial_id, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--method", choices=METHODS, default="pair")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument(
        "--max-seconds",
        type=float,
        help="wall-clock limit for preparation, translation and public acceptance per driver; private scoring and cleanup excluded",
    )
    parser.add_argument("--pricing", type=Path, required=True)
    parser.add_argument("--drivers", nargs="+", choices=catalog(), required=True)
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="run the two methods concurrently within each driver",
    )
    parser.add_argument(
        "--private-score",
        action="store_true",
        help="score frozen parameter variants, never feed back",
    )
    parser.add_argument(
        "--driver-jobs", type=int, default=1, help="concurrent drivers (default: 1)"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.driver_jobs < 1:
        parser.error("--driver-jobs must be positive")
    if args.max_seconds is not None and not 0 < args.max_seconds < float("inf"):
        parser.error("--max-seconds must be finite and positive")
    if len(set(args.drivers)) != len(args.drivers):
        parser.error("--drivers must not contain duplicates")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    profile = read_profile(args.profile)
    args.parallel = args.method == "pair" and (
        args.parallel or profile.get("parallel_methods", False)
    )
    state = {
        "created_at": now(),
        "batch": args.batch,
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "methods": list(METHODS[args.method]),
        "env_revision": subprocess.check_output(
            ["git", "-C", REPO, "rev-parse", "HEAD"], text=True
        ).strip(),
        "factory_revision": subprocess.check_output(
            ["git", "-C", profile["factory"], "rev-parse", "HEAD"], text=True
        ).strip(),
        "parallel_methods": args.parallel,
        "driver_jobs": args.driver_jobs,
        "limits": {"usd": None, "seconds": args.max_seconds},
        "pricing": read(args.pricing),
        "private_parameter_manifest_sha256": sha(
            REPO / "scoring/variants/generated-manifest.json"
        )
        if args.private_score
        else None,
        "state": "RUNNING",
        "drivers": {d: {"state": "QUEUED"} for d in args.drivers},
    }

    def publish():
        write(args.output / "batch.json", state)
        table(profile, args, state)

    def started(task):
        state["drivers"][task[2]] = {"state": "RUNNING", "started_at": now()}
        publish()

    def finished(task, record):
        state["drivers"][task[2]] = record
        publish()

    publish()
    try:
        run_tasks(
            [(profile, args, driver) for driver in args.drivers],
            args.driver_jobs,
            run_driver,
            started,
            finished,
        )
        errors = [
            driver + ": " + record["error"]
            for driver, record in state["drivers"].items()
            if record["state"] == "ERROR"
        ]
        state.update(state="ERROR" if errors else "FINISHED", ended_at=now())
        if errors:
            state["error"] = "; ".join(errors)
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as error:
        state.update(state="ERROR", error=str(error), ended_at=now())
    finally:
        write(args.output / "batch.json", state)
        table(profile, args, state)
    if state["state"] == "ERROR":
        parser.exit(1, state["error"] + "\n")


if __name__ == "__main__":
    main()
