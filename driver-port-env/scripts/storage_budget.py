"""Host-wide admission reservations, independent of model and evaluation settings."""

import argparse
import fcntl
import json
import os
import platform
import shutil
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

GIB = 2**30
TOKEN_ENV = "C2R_STORAGE_RESERVATION"


def state_root():
    return (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        / "c2r-experiments"
    )


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def identity():
    return {
        "pid": os.getpid(),
        "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "start": Path(f"/proc/{os.getpid()}/stat")
        .read_text()
        .rsplit(")", 1)[1]
        .split()[19],
    }


def alive(owner):
    try:
        if owner["boot"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
            return False
        fields = (
            Path(f"/proc/{owner['pid']}/stat").read_text().rsplit(")", 1)[1].split()
        )
        return fields[0] != "Z" and fields[19] == owner["start"]
    except FileNotFoundError:
        return False


@contextmanager
def locked():
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "reservations.json"
        state = json.loads(path.read_text()) if path.exists() else {"leases": {}}
        # A dead supervisor must not free an attached worker's reservation.
        for token, lease in list(state["leases"].items()):
            lease["owners"] = [p for p in lease["owners"] if alive(p)]
            if not lease["owners"]:
                del state["leases"][token]
        try:
            yield root, state
        finally:
            write(path, state)


def available(policy, usage, reserved):
    """Conservative: allocations already on disk are not subtracted from reservations."""
    return (
        min(usage.free, policy["max_used_bytes"] - usage.used)
        - reserved
        - policy["safety_bytes"]
    )


def policy_for(root, workspace):
    path = root / "policy.json"
    if path.exists():
        policy = json.loads(path.read_text())
    elif "microsoft" in platform.release().lower():
        raise ValueError(
            "WSL storage ceiling is not configured. Run scripts/storage_budget.py configure "
            "--max-used-gib <existing-VHDX-safe-ceiling>; virtual df capacity is unsafe."
        )
    else:
        policy = {
            "max_used_bytes": shutil.disk_usage(workspace).total,
            "experiment_bytes": 8 * GIB,
            "safety_bytes": 8 * GIB,
        }
    if any(
        type(policy.get(k)) is not int or policy[k] <= 0
        for k in ("max_used_bytes", "experiment_bytes", "safety_bytes")
    ):
        raise ValueError("Invalid global storage policy")
    return policy


@contextmanager
def reserve(workspace, label, instances=1):
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    owner = identity()
    inherited = os.environ.get(TOKEN_ENV)
    with locked() as (root, state):
        if inherited:
            lease = state["leases"].get(inherited)
            if lease is None or lease["device"] != workspace.stat().st_dev:
                raise ValueError(
                    "Inherited storage reservation is missing or on another filesystem"
                )
            if lease["instances"] < instances:
                raise ValueError("Inherited storage reservation is too small")
            token = inherited
            lease["owners"].append(owner)
        else:
            if type(instances) is not int or instances <= 0:
                raise ValueError("Storage instance count must be positive")
            policy = policy_for(root, workspace)
            device = workspace.stat().st_dev
            usage = shutil.disk_usage(workspace)
            reserved = sum(
                r["bytes"] for r in state["leases"].values() if r["device"] == device
            )
            required = instances * policy["experiment_bytes"]
            room = available(policy, usage, reserved)
            if required > room:
                raise ValueError(
                    f"Storage admission denied: need {required / GIB:.1f} GiB, "
                    f"unreserved room {room / GIB:.1f} GiB; used {usage.used / GIB:.1f}, "
                    f"global reserved {reserved / GIB:.1f}, ceiling "
                    f"{policy['max_used_bytes'] / GIB:.1f}, safety {policy['safety_bytes'] / GIB:.1f} GiB. "
                    "No experiment started; clean completed build products first."
                )
            token = uuid.uuid4().hex
            state["leases"][token] = {
                "label": label,
                "bytes": required,
                "instances": instances,
                "device": device,
                "workspace": str(workspace),
                "owners": [owner],
                "started_at": datetime.now(UTC).isoformat(),
            }
    os.environ[TOKEN_ENV] = token
    try:
        yield token
    finally:
        if inherited is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = inherited
        with locked() as (_, state):
            lease = state["leases"].get(token)
            if lease:
                lease["owners"].remove(owner)
                if not lease["owners"]:
                    del state["leases"][token]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    setup = sub.add_parser("configure")
    setup.add_argument("--max-used-gib", type=float, required=True)
    setup.add_argument("--experiment-gib", type=float, default=8)
    setup.add_argument("--safety-gib", type=float, default=8)
    status = sub.add_parser("status")
    status.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    with locked() as (root, state):
        if args.action == "configure":
            values = [args.max_used_gib, args.experiment_gib, args.safety_gib]
            if any(not 0 < v < float("inf") for v in values):
                parser.error("Storage sizes must be finite and positive")
            if state["leases"]:
                parser.error(
                    "Cannot change the storage policy while reservations are active"
                )
            policy = dict(
                zip(
                    ("max_used_bytes", "experiment_bytes", "safety_bytes"),
                    (int(v * GIB) for v in values),
                    strict=True,
                )
            )
            write(root / "policy.json", policy)
            print(json.dumps(policy, indent=2))
        else:
            workspace = args.workspace.resolve()
            policy = policy_for(root, workspace)
            usage = shutil.disk_usage(workspace)
            reserved = sum(
                r["bytes"]
                for r in state["leases"].values()
                if r["device"] == workspace.stat().st_dev
            )
            print(
                json.dumps(
                    {
                        "policy": policy,
                        "used_gib": usage.used / GIB,
                        "reserved_gib": reserved / GIB,
                        "admissible_gib": available(policy, usage, reserved) / GIB,
                        "leases": state["leases"],
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.exit(str(error))
