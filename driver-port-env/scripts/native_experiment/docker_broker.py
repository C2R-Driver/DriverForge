"""Private infrastructure for standard Docker commands, scoped to one experiment worker."""

import base64
import json
import selectors
import shlex
import shutil
import socketserver
import subprocess
import threading
import time
import uuid
from pathlib import Path

from docker_policy import qemu_runtime_error, validated_run
from goal_client import terminate


class DockerBroker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(
        self,
        path,
        image,
        writable,
        readonly,
        *,
        allow_cli_version=False,
        enforce_frozen_tools=False,
        accelerator=None,
        runtime=None,
    ):
        self.image = image
        self.writable = [Path(path).resolve() for path in writable]
        self.readonly = [Path(path).resolve() for path in readonly]
        self.owner = "c2r.worker=" + uuid.uuid4().hex
        self.allow_cli_version = allow_cli_version
        self.docker = shutil.which("docker")
        self.tool_dirs = ()
        self.accelerator = accelerator
        self.runtime = runtime if enforce_frozen_tools else None
        if enforce_frozen_tools:
            tools = json.loads(
                subprocess.check_output(
                    [
                        self.docker,
                        "run",
                        "--rm",
                        "--pull=never",
                        "--network",
                        "none",
                        image,
                        "python3",
                        "-c",
                        (
                            "import json,shutil; print(json.dumps([shutil.which(n) "
                            "for n in ['cargo','qemu-system-x86_64']]))"
                        ),
                    ],
                    text=True,
                )
            )
            if not all(tools):
                raise ValueError(
                    "Frozen image is missing Cargo or QEMU; stop environment setup"
                )
            self.tool_dirs = tuple(sorted({str(Path(tool).parent) for tool in tools}))
        super().__init__(str(path), Handler)

    def known(self):
        rows = subprocess.check_output(
            [
                self.docker,
                "ps",
                "-a",
                "--filter",
                "label=" + self.owner,
                "--format",
                "{{.ID}} {{.Names}}",
                "--no-trunc",
            ],
            text=True,
        ).splitlines()
        return {value for row in rows for value in row.split()}

    def command(self, argv):
        if self.allow_cli_version and argv in (["--version"], ["-v"]):
            return [self.docker, *argv]
        if argv[0] == "run":
            validated_run(
                argv,
                self.image,
                self.writable,
                self.readonly,
                tool_dirs=self.tool_dirs,
                accelerator=self.accelerator,
            )
            return [self.docker, "run", "--label", self.owner, *argv[1:]]
        if argv[0] == "ps":
            return [self.docker, *argv, "--filter", "label=" + self.owner]
        if argv[0] == "events":
            return [self.docker, *argv, "--filter", "label=" + self.owner]
        if argv[0] in {"inspect", "top", "rm"}:
            names = [arg for arg in argv[1:] if not arg.startswith("-")]
            # top's final format is harmless; only the first container is addressed.
            identifiers = names[:1] if argv[0] == "top" else names
            if not identifiers or not set(identifiers) <= self.known():
                raise ValueError("Container is not owned by this experiment")
            return [self.docker, *argv]
        if argv[:2] == ["image", "inspect"] and argv[-1] == self.image:
            return [self.docker, *argv]
        if argv[0] in {"version", "info"}:
            return [self.docker, *argv]
        if argv[:2] == ["context", "inspect"]:
            return ["/bin/echo", "unix:///var/run/docker.sock"]
        raise ValueError(
            "Docker operation unavailable to workers: "
            + argv[0]
            + (
                "; this command is unsupported, not a Docker outage. "
                "Use docker version or docker run with the fixed image."
                if self.allow_cli_version
                else ""
            )
        )

    def runtime_violation(self):
        if not self.runtime:
            return None
        rows = subprocess.check_output(
            [self.docker, "ps", "-q", "--filter", "label=" + self.owner], text=True
        ).splitlines()
        for container in rows:
            result = subprocess.run(
                [self.docker, "top", container, "-eo", "pid,args"],
                capture_output=True,
                text=True,
                check=False,
            )
            for row in result.stdout.splitlines()[1:]:
                fields = row.split(None, 1)
                if len(fields) != 2 or not fields[0].isdigit():
                    continue
                argv = shlex.split(fields[1])
                if not argv or not Path(argv[0]).name.startswith("qemu-system-"):
                    continue
                error = qemu_runtime_error(argv, self.runtime)
                if error:
                    return container, error
        return None


class Handler(socketserver.StreamRequestHandler):
    def send(self, value):
        self.wfile.write(json.dumps(value).encode() + b"\n")
        self.wfile.flush()

    def input(self, process):
        try:
            for line in self.rfile:
                message = json.loads(line)
                if "stdin_eof" in message:
                    break
                process.stdin.write(base64.b64decode(message["stdin"]))
                process.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            process.stdin.close()

    def handle(self):
        process = None
        try:
            request = json.loads(self.rfile.readline())
            argv = self.server.command(request["argv"])
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            threading.Thread(target=self.input, args=(process,), daemon=True).start()
            self.output(process)
        except (OSError, ValueError, KeyError, IndexError) as error:
            try:
                self.send({"error": str(error)})
            except OSError:
                pass
        finally:
            if process:
                terminate(process)

    def output(self, process):
        last_check = 0
        with selectors.DefaultSelector() as poller:
            poller.register(process.stdout, selectors.EVENT_READ, 1)
            poller.register(process.stderr, selectors.EVENT_READ, 2)
            while poller.get_map():
                if self.server.runtime and time.monotonic() - last_check >= 2:
                    last_check = time.monotonic()
                    violation = self.server.runtime_violation()
                    if violation:
                        container, error = violation
                        self.send(
                            {
                                "fd": 2,
                                "data": base64.b64encode(
                                    (error + "\n").encode()
                                ).decode(),
                            }
                        )
                        subprocess.run(
                            [self.server.docker, "rm", "-f", container],
                            capture_output=True,
                            check=False,
                        )
                        terminate(process)
                        self.send({"exit_code": 125})
                        return
                for key, _ in poller.select(timeout=1):
                    chunk = key.fileobj.read1(65536)
                    if not chunk:
                        poller.unregister(key.fileobj)
                    else:
                        self.send(
                            {"fd": key.data, "data": base64.b64encode(chunk).decode()}
                        )
            self.send({"exit_code": process.wait()})
