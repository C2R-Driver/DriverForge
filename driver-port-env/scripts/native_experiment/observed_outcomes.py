"""Read final archived observations for boot, device presence and assertion counts."""

import json
from pathlib import Path

from common import sha


def final_observations(result):
    path = result.get("receipt")
    if not path or not Path(path).is_file():
        return {}
    report = json.loads(Path(path).read_text())
    if "tests" in report:
        rows = report["tests"]
        boot = report.get("boot_passed")
    else:
        rows = []
        boot = None
        for case in report.get("run", {}).get("cases", []):
            receipt = json.loads(Path(case["receipt"]).read_text())
            for log in receipt.get("result", {}).get("logs", []):
                # Archived runtime observations, not command or cleanup receipts.
                if not log["path"].endswith("/result.json"):
                    continue
                archive = Path(log["archive_path"])
                if sha(archive) != log["sha256"]:
                    raise ValueError("Archived final observation changed")
                row = json.loads(archive.read_text())
                if "boot_passed" in row and "case" in row:
                    rows.append(row)
        if rows:
            boot = any(row.get("boot_passed") is True for row in rows)
    presence = [
        r["device_present"] for r in rows if isinstance(r.get("device_present"), bool)
    ]
    initialized = (
        any(presence)
        if presence
        else True
        if any(r.get("passed") is True for r in rows)
        else None
    )
    functions = [(r, name) for r in rows for name in r.get("expected", [])]
    return {
        "boot_passed": boot,
        "initialization_passed": initialized,
        "initialization_basis": "device_present observation"
        if presence
        else "successful driver-focused public behavior"
        if initialized
        else "unknown",
        "functions_expected": len(functions) or None,
        "functions_passed": sum(
            name in row.get("observed", {})
            and row["observed"][name][0] > 0
            and row["observed"][name][1] == 0
            for row, name in functions
        )
        if functions
        else None,
    }
