"""One operator-owned functional boundary for both comparison methods."""

import copy
import hashlib
import json

from common import HERE, read

REGISTRY = HERE.parents[1] / "configs/experiments/behavior-scopes.json"


def scoped_task(task, driver=None):
    value = copy.deepcopy(task)
    name = value.get("driver", driver)
    if name is None:
        raise ValueError("A behavior scope requires a driver identity")
    value["driver"] = name
    if "behavior_scope" not in value:
        registry = read(REGISTRY)
        if name not in registry["drivers"]:
            raise ValueError(
                "Configure a shared behavior scope before translation: " + name
            )
        value["behavior_scope"] = registry["drivers"][name]
    return value


def digest(scope):
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def describe(scope):
    return "\n".join(
        [
            "Delivery mode: " + scope["mode"],
            "Integration: " + scope["integration"],
            "",
            "Required behaviors:",
            *("- " + item for item in scope["required"]),
            "",
            "Excluded behaviors:",
            *("- " + item for item in scope["excluded"]),
        ]
    )
