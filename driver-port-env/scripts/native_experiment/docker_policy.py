"""Docker access policy for a single worker: only its task and public asset mounts."""

import re
import shlex
from pathlib import Path

FLAGS = {"--rm", "--pull=never", "-i", "--interactive"}
OPTIONS = {
    "--name",
    "--hostname",
    "--device",
    "--network",
    "-v",
    "--volume",
    "-w",
    "--workdir",
    "-e",
    "--env",
    "--cap-add",
    "--entrypoint",
}
CACHES = {"dpf-native-cargo-registry", "dpf-native-cargo-git"}


def permitted_mount(value, writable, readonly):
    parts = value.split(":")
    if len(parts) not in (2, 3) or not parts[1].startswith("/"):
        return False
    origin = parts[0]
    if origin in CACHES:
        return True
    if not origin.startswith("/"):
        return False
    path = Path(origin).resolve()
    if any(path.is_relative_to(root) for root in writable):
        return True
    return (
        len(parts) == 3
        and parts[2] == "ro"
        and any(path == root or path.is_relative_to(root) for root in readonly)
    )


def validated_run(argv, image, writable, readonly, *, tool_dirs=(), accelerator=None):
    """Return a constrained docker run; never allow daemon administration or host mounts."""
    index, name = 1, None
    while index < len(argv) and argv[index].startswith("-"):
        option = argv[index]
        index += 1
        if option in FLAGS:
            continue
        if option not in OPTIONS or index >= len(argv):
            raise ValueError("Docker option unavailable to workers: " + option)
        value = argv[index]
        index += 1
        if tool_dirs and option in {"-e", "--env"} and value.split("=", 1)[0] == "PATH":
            path = value.partition("=")[2].split(":")
            missing = sorted(set(tool_dirs) - set(path))
            if missing:
                raise ValueError(
                    "Docker PATH hides image-provided tools in "
                    + ", ".join(missing)
                    + ". The tools are installed; remove the docker PATH override and "
                    "add the SDK inside the container shell: "
                    "export PATH=/experiment-assets/sdk:$PATH. Retry the command."
                )
        if option in {"-v", "--volume"} and not permitted_mount(
            value, writable, readonly
        ):
            raise ValueError("Docker mount outside the current task/public assets")
        if option == "--device" and value not in {"/dev/kvm", "/dev/net/tun"}:
            raise ValueError("Only frozen guest devices are available")
        if option == "--network" and value not in {"none", "bridge"}:
            raise ValueError("Host/container network namespaces are unavailable")
        if option == "--cap-add" and value != "NET_ADMIN":
            raise ValueError("Only NET_ADMIN for the frozen TAP route is available")
        if option == "--name":
            name = value
    if index >= len(argv) or argv[index] != image:
        raise ValueError(
            "Only the frozen Docker image ID is available. "
            f"Use exactly '{image}' as the image argument in docker run; "
            "keep the 'sha256:' prefix. Image tags, repository@sha256 references "
            "and hashes without the prefix are not accepted. "
            "Keep your other options, mounts and container command unchanged."
        )
    if tool_dirs:
        frozen_command(argv[index + 1 :], accelerator)
    return name


def frozen_command(command, accelerator):
    """Reject observed tool replacement/accelerator overrides, not general shell activity."""
    words = command
    if command and Path(command[0]).name in {"bash", "sh"} and len(command) >= 3:
        try:
            words = shlex.split(command[-1])
        except ValueError:
            return  # Let the container shell report its own syntax error.
    for i, word in enumerate(words):
        if word in {"apt", "apt-get"}:
            tail = words[i + 1 :]
            end = next(
                (j for j, v in enumerate(tail) if v in {"&&", ";", "||", "|"}),
                len(tail),
            )
            operation = tail[:end]
            if "install" in operation and any(
                v.startswith("qemu-system") or v in {"qemu", "qemu-kvm"}
                for v in operation
            ):
                raise ValueError(
                    "QEMU is already installed in the frozen image; installing another "
                    "version changes the experiment environment. Check command -v "
                    "qemu-system-x86_64 inside the image with its original PATH."
                )
        if accelerator == "kvm":
            options = (
                shlex.split(word.partition("=")[2])
                if word.startswith("--qemu-args=")
                else [word]
            )
            if (
                word == "-accel"
                and i + 1 < len(words)
                and words[i + 1].startswith("tcg")
                or "-accel" in options
                and options[options.index("-accel") + 1 :][:1] == ["tcg"]
                or any("accel=tcg" in v for v in options)
            ):
                raise ValueError(
                    "This experiment uses KVM, not TCG. Pass --device /dev/kvm to "
                    "Docker and select -accel kvm in QEMU."
                )


def qemu_runtime_error(argv, runtime):
    """Check a launched QEMU process; upstream scripts may hide its effective options."""

    def option(name):
        return (
            argv[argv.index(name) + 1]
            if name in argv and argv.index(name) + 1 < len(argv)
            else None
        )

    issues = []
    accel = option("-accel")
    machine = option("-machine") or ""
    kvm = (
        accel is not None
        and accel.split(",")[0] == "kvm"
        or "-enable-kvm" in argv
        or "--enable-kvm" in argv
        or "accel=kvm" in machine
    )
    if runtime.get("accelerator") == "kvm" and not kvm:
        issues.append("QEMU did not select KVM; add -accel kvm")
    cpus = option("-smp") or "1"
    cpus = cpus.split(",")[0].removeprefix("cpus=")
    if str(runtime.get("cpus", cpus)) != cpus:
        issues.append(f"QEMU uses {cpus} CPUs; use -smp {runtime['cpus']}")
    memory = option("-m") or "128M"

    def bytesize(value):
        match = re.fullmatch(
            r"(?:size=)?(\d+(?:\.\d+)?)([KMG]?)",
            str(value).split(",")[0],
            re.IGNORECASE,
        )
        if not match:
            return None
        number, unit = match.groups()
        return float(number) * 1024 ** {"K": 1, "": 2, "M": 2, "G": 3}[unit.upper()]

    if runtime.get("memory") and bytesize(memory) != bytesize(runtime["memory"]):
        issues.append(f"QEMU uses memory {memory}; use -m {runtime['memory']}")
    if issues:
        return (
            "Fixed experiment environment error: "
            + "; ".join(issues)
            + ". Correct the launch command and retry. Docker and QEMU are available."
        )
    return None
