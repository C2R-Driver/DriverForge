"""One reviewable baseline prompt, rendered for each task without planning its solution."""

from pathlib import Path
from string import Template


def render(
    task,
    workspace,
    qemu_source,
    config,
    shared_assets=None,
    *,
    include_behavior_scope=True,
    require_public_validation=False,
):
    from behavior_scope import describe, scoped_task

    if include_behavior_scope:
        task = scoped_task(task)
    workspace = Path(workspace)
    behavior_scope_section = ""
    if include_behavior_scope:
        behavior_scope_section = (
            "The authoritative functional boundary is the operator-owned\n"
            f"`{workspace / 'behavior-scope.json'}`, shared unchanged with Factory:\n\n"
            + describe(task["behavior_scope"])
            + "\n\n"
            "Implement the required behaviors and their necessary dependencies. Preserve\n"
            "their source error, concurrency, ownership and cleanup semantics even when public\n"
            "tests do not exercise them. Do not expand excluded functions into implementation\n"
            "tasks. Full Linux source is available for investigation; reading a definition does\n"
            "not make all of its callers or neighboring features part of this delivery.\n\n"
        )
    environment = task.get("environment", {})
    values = dict(config) | environment
    values.update(
        kernel_log_level=environment.get(
            "kernel_log_level", config.get("kernel_log_level", "error")
        ),
        driver=task["driver"],
        workspace=str(workspace),
        source=str(workspace / "source"),
        target=str(workspace / "target"),
        target_location=str(workspace / "target" / task["omit"]),
        source_entries="\n".join(
            f"- `{workspace / 'source' / p}`" for p in task["source_entries"]
        ),
        public_cases="\n".join(
            f"- `{p}`" for p in [*task["tests"], *task["benchmarks"]]
        ),
        scope=task["scope"],
        behavior_scope_section=behavior_scope_section,
        linux_revision=task.get("upstream_linux", config["source_revision"]),
        target_revision=task.get("upstream_asterinas", config["target_revision"]),
        image_tag=environment.get("image_tag", config["image"]),
        firmware=environment.get("firmware", "/root/ovmf/release/OVMF.fd"),
        qemu_source=str(qemu_source),
        qemu_revision=environment.get(
            "qemu_evidence_revision", config["qemu_revision"]
        ),
        qemu_model=task["qemu_model"],
        shared_assets=str(shared_assets or Path("/experiment-assets")),
        target_baseline=task.get(
            "target_baseline", "only this driver has been removed"
        ),
        public_interface="",
        public_validation_instruction=(
            "Before calling finish, build and boot the candidate in the fixed Docker/QEMU\n"
            "environment and run the complete provided public test set. Use the visible test\n"
            "sources, native kernel instructions and shared assets; you may write your own\n"
            "execution script. Repair observed failures and check the resulting candidate.\n"
            "Compilation, static review or reading test sources is not runtime validation.\n"
            "Do not defer these available checks to the operator. If a real execution blocker\n"
            "prevents progress, report the command, error and unfinished checks honestly.\n"
            "The operator's independent final acceptance below does not replace your self-tests."
            if require_public_validation
            else "Validate real device behavior as needed during development."
        ),
        runtime_profile_rule="Retain the upstream OVMF/device profile.",
    )
    if task.get("suite_kind") == "fixed-public-fixture":
        values["public_interface"] = (
            f"Fixed Rust test source: `{workspace / 'target' / task['fixture_file']}`. "
            f"Read `{workspace / 'target/.dpf-output/harness/public/INTERFACE.md'}` for "
            "the real driver/proc interface. The file also contains Factory-specific tool advice; "
            "no such tools are supplied to this baseline. Adapt the interface, not the assertions. "
            "Only the unchanged fixture may emit its test success markers. Raw public cases and "
            "QEMU profiles are in the public-tests directory of the shared assets. Read the "
            "complete public execution procedure in INTERFACE.md and each case's acceptance "
            "object when present; fixture markers and benchmark readiness alone do not prove "
            "that its external workload and assertions passed. You may execute these public "
            "procedures using standard terminal/Docker/QEMU commands. The operator "
            "runs the existing guest assertions and any declared external peer "
            "at final acceptance."
        )
        values["runtime_profile_rule"] = (
            "Use the fixed QEMU profiles in /experiment-assets/public-tests/index.json. "
            "Retain the declared machine, device and network settings for each case. "
            "Do not substitute devices or claim coverage beyond the frozen scope."
        )
    template = Path(__file__).with_name("baseline-prompt.md").read_text()
    return Template(template).substitute(values)
