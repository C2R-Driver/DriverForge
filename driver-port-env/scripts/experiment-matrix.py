#!/usr/bin/env python3.11
"""Sequential model/driver comparisons with physical-host admission and result publication."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from team_profile import read_profile


def publish(output, batches):
    rows = []
    for batch in batches:
        file = batch / "measurements.json"
        if file.exists():
            rows.extend(json.loads(file.read_text()))
    (output / "measurements.json").write_text(json.dumps(rows, indent=2) + "\n")
    text = [
        "| 模型 | 驱动 | 方法 | 状态 | 费用（USD） | 时长（分钟） | 输入 token | 缓存输入 token | 输出 token | 公开测试 | 私有 A | 私有 B |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for row in rows:
        seconds = row.get("total_seconds")
        vals = [
            row["model"],
            row["driver"],
            "Factory" if row["method"] == "dpf" else "Codex＋goal",
            row.get("state", row.get("batch_state", "")),
            str(row.get("cost_estimate", row.get("usd_estimate", ""))),
            f"{seconds / 60:.2f}" if seconds is not None else "",
            *(
                str(row.get(key, ""))
                for key in ["input_tokens", "cached_input_tokens", "output_tokens"]
            ),
            f"{row['cases_passed']}/{row['cases_expected']}"
            if "cases_passed" in row
            else "",
        ]
        for group in ["A", "B"]:
            count = row.get("private_" + group + "_observed")
            vals.append(
                f"{row['private_' + group + '_passed']}/{count}"
                if count is not None
                else ""
            )
        text.append("| " + " | ".join(vals) + " |")
    (output / "comparison.zh-CN.md").write_text("\n".join(text) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=Path, nargs="+", required=True)
    parser.add_argument("--drivers", nargs="+", required=True)
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host-storage", type=Path)
    parser.add_argument("--host-safety-gib", type=float, default=8)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    batches = []
    for driver in args.drivers:
        for number, path in enumerate(args.profiles, 1):
            profile = read_profile(path)
            batch = f"{args.matrix}-m{number}-{driver}"
            out = args.output / batch
            if args.host_storage:
                required = (16 + args.host_safety_gib) * 2**30
                free = shutil.disk_usage(args.host_storage).free
                if free < required:
                    raise ValueError(
                        f"Physical host admission denied: {free / 2**30:.1f} GiB free; need {required / 2**30:.1f} GiB. No new run started."
                    )
            command = [
                sys.executable,
                str(Path(__file__).with_name("experiment-batch.py")),
                "--profile",
                str(path.resolve()),
                "--batch",
                batch,
                "--model",
                profile["model"],
                "--reasoning-effort",
                profile["reasoning_effort"],
                "--method",
                "pair",
                "--pricing",
                str(profile["pricing"]),
                "--drivers",
                driver,
                "--driver-jobs",
                "1",
                "--private-score",
                "--output",
                str(out.resolve()),
            ]
            batches.append(out)
            print("START", batch, profile["model"], flush=True)
            with (args.output / (batch + ".operator.log")).open("x") as log:
                result = subprocess.run(
                    command, stdout=log, stderr=subprocess.STDOUT, check=False
                )
            publish(args.output, batches)
            state = out / "batch.json"
            if result.returncode or (
                state.exists()
                and any(
                    v.get("state") == "ERROR"
                    for v in json.loads(state.read_text())["drivers"].values()
                )
            ):
                raise RuntimeError(
                    "Infrastructure error; stop the matrix, retain evidence: " + batch
                )
            print("FINISHED", batch, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
