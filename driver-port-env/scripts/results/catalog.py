#!/usr/bin/env python3
"""Index historical measurement snapshots without selecting paper repetitions."""

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path

FIELDS = (
    "state",
    "stop_reason",
    "started_at",
    "ended_at",
    "total_seconds",
    "usd_estimate",
    "cost_status",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "requests_with_usage",
    "initialization_passed",
    "build_passed",
    "boot_passed",
    "cases_passed",
    "cases_expected",
    "suite_passed",
    "private_A_passed",
    "private_A_observed",
    "private_B_passed",
    "private_B_observed",
    "artifact_sha256",
    "source_revision",
    "target_revision",
    "image_id",
)


def collect(root):
    paths = sorted((root / "results/batches").rglob("measurements.json"))
    legacy = root / "results/paper-runs.json"
    if legacy.exists():
        paths.append(legacy)
    snapshots = []
    for path in paths:
        raw = json.loads(path.read_text())
        rows = raw if isinstance(raw, list) else raw["rows"]
        for number, row in enumerate(rows):
            if not all(row.get(key) for key in ("model", "driver", "method")):
                raise ValueError(f"Missing run identity: {path}, row {number}")
            method = "factory" if row["method"] == "dpf" else row["method"]
            source = path.relative_to(root).as_posix()
            trial = row.get("trial")
            identity = [
                row["model"],
                row.get("reasoning_effort") or "unknown",
                row["driver"],
                method,
                trial or f"unidentified:{path.parent.relative_to(root).as_posix()}",
            ]
            run_id = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
            values = {key: row.get(key) for key in FIELDS}
            if values["usd_estimate"] is None:
                values["usd_estimate"] = row.get("cost_estimate")
            if values["state"] is None:
                values["state"] = row.get("batch_state")
            usage = row.get("usage") or {}
            for key in FIELDS:
                if values[key] is None and key in usage:
                    values[key] = usage[key]
            snapshots.append(
                {
                    "run_id": run_id,
                    "model": identity[0],
                    "reasoning_effort": identity[1],
                    "driver": identity[2],
                    "method": method,
                    "trial": trial,
                    "identity_inferred": trial is None,
                    "source": source,
                    "row_index": number,
                    **values,
                }
            )
    return snapshots


def group_runs(snapshots):
    groups = {}
    for snapshot in snapshots:
        groups.setdefault(snapshot["run_id"], []).append(snapshot)
    runs = []
    for run_id, members in sorted(groups.items()):
        first = members[0]
        run = {
            key: first[key]
            for key in (
                "run_id",
                "model",
                "reasoning_effort",
                "driver",
                "method",
                "trial",
                "identity_inferred",
            )
        }
        run["snapshots"] = [
            {key: member[key] for key in ("source", "row_index")} for member in members
        ]
        run["conflicting_fields"] = []
        for field in FIELDS:
            values = {
                json.dumps(member[field], sort_keys=True)
                for member in members
                if member[field] is not None
            }
            run[field] = json.loads(next(iter(values))) if len(values) == 1 else None
            if len(values) > 1:
                run["conflicting_fields"].append(field)
        runs.append(run)
    return runs


def write_catalog(root, output):
    snapshots = collect(root)
    runs = group_runs(snapshots)
    output.mkdir(parents=True, exist_ok=True)
    payload = {"runs": runs, "snapshot_count": len(snapshots), "run_count": len(runs)}
    (output / "runs.json").write_text(json.dumps(payload, indent=2) + "\n")
    columns = [
        "run_id",
        "model",
        "reasoning_effort",
        "driver",
        "method",
        "trial",
        "identity_inferred",
        "source",
        "row_index",
        *FIELDS,
    ]
    with (output / "snapshots.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(snapshots)
    hierarchy = {}
    for run in runs:
        key = (run["model"], run["reasoning_effort"], run["driver"], run["method"])
        hierarchy.setdefault(key, []).append(run)
    for parts, members in hierarchy.items():
        # Metadata values are labels, never arbitrary filesystem destinations.
        if any(not re.fullmatch(r"[a-zA-Z0-9_.-]+", part) for part in parts):
            raise ValueError(f"Invalid catalog label: {parts}")
        directory = output.joinpath(*parts)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "runs.json").write_text(json.dumps(members, indent=2) + "\n")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--output", type=Path, default=Path("results/catalog"))
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output if args.output.is_absolute() else root / args.output
    payload = write_catalog(root, output)
    print(
        f"Indexed {payload['run_count']} runs / {payload['snapshot_count']} snapshots"
    )


if __name__ == "__main__":
    main()
