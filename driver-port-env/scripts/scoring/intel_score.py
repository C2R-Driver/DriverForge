"""Private parameter variations on the sealed Intel candidate ISO; no repair feedback."""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path[:0] = ["/transport", "/env/scripts/native_experiment"]
from guest import Guest
from intel_runtime import argv


def main():
    driver = sys.argv[1]
    os.chdir("/root/asterinas")
    index = json.loads(Path("/dpf-shared/public-tests/index.json").read_text())
    spec = index[driver + "/" + driver + "-probe"]
    # Hidden stimuli remain operator-side; neither concrete group is in worker mounts.
    for group, length, duration, flows in [("A", 512, 7, 3), ("B", 1200, 11, 5)]:
        rows = []
        for case in [
            "iperf3/tcp_virtio_bw",
            "iperf3/udp_virtio_bw_rx",
            "iperf3/udp_virtio_bw_tx",
        ]:
            output = Path("/work/results") / group / Path(case).name
            output.mkdir(parents=True, exist_ok=True)
            row = {"case": case, "status": "FAIL", "variant": group, "driver": driver}
            try:
                with Guest(
                    argv(spec, "/candidate.iso", output, driver),
                    output,
                    180,
                    ready_text="# ",
                ) as guest:
                    guest.wait_serial("# ")
                    row["boot_passed"] = True
                    guest.assert_guest("ip link show eth0")
                    guest.send(
                        "/benchmark/common/bench_runner.sh " + case + " asterinas\n"
                    )
                    guest.wait_serial("The VM is ready for the benchmark.")
                    guest.observe(1)
                    command = [
                        "iperf3",
                        "-c",
                        "10.0.2.15",
                        "-f",
                        "m",
                        "-t",
                        str(duration),
                        "-P",
                        str(flows),
                    ]
                    if "udp" in case:
                        command += ["-u", "-b", "12M", "-l", str(length)]
                    if case.endswith("_tx"):
                        command += ["-R"]
                    with (output / "host.log").open("w") as stream:
                        host = subprocess.Popen(
                            command, stdout=stream, stderr=subprocess.STDOUT
                        )
                        import time

                        deadline = time.monotonic() + 55
                        try:
                            while host.poll() is None and time.monotonic() < deadline:
                                guest.pump()
                            if host.poll() is None:
                                raise RuntimeError("PRIVATE_IPERF_TIMEOUT")
                        finally:
                            if host.poll() is None:
                                host.terminate()
                            host.wait(timeout=5)
                    import re

                    rates = re.findall(
                        r"(\d+(?:\.\d+)?)\s+Mbits/sec.*\b(receiver|sender)\b",
                        (output / "host.log").read_text(),
                    )
                    if host.returncode or {
                        role for v, role in rates if float(v) > 0
                    } != {"sender", "receiver"}:
                        raise RuntimeError("PRIVATE_TRAFFIC_FAILED")
                    row.update(status="PASS", rates_mbits=rates)
            except (
                OSError,
                RuntimeError,
                ValueError,
                subprocess.SubprocessError,
            ) as error:
                row["error"] = str(error)
            rows.append(row)
        Path("/work", f"summary-{group}.json").write_text(
            json.dumps(
                rows,
                indent=2,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
