# Usage

Requires x86_64 Linux, Python 3.11+, Git, strace, Docker with KVM access, and a configured Codex CLI.

```bash
git clone <repository-url> DriverForge
cd DriverForge/driver-port-factory-main
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
docker pull asterinas/dev:0.18.1-20260901
```

Provide Git checkouts of Linux, Asterinas, and QEMU. Set the paths below to your local repositories. Replace `LOCAL_INPUT_REVISION` and `LOCAL_INPUT_FINGERPRINT` in the task and environment configuration with locally selected versions before using the experiment runner.

```bash
./scripts/run-experiment.sh \
  --workspace /path/to/workspace/virtio-rng \
  --driver-name virtio-rng \
  --catalog configs/drivers/linux-virtio-rng.catalog.json \
  --platform-image asterinas/dev:0.18.1-20260901 \
  --platform-accelerator kvm \
  --local-source-repository /path/to/linux \
  --local-target-repository /path/to/asterinas \
  --local-qemu-repository /path/to/qemu
```

Check progress or resume the workspace:

```bash
./scripts/status.sh /path/to/workspace/virtio-rng
./scripts/resume-experiment.sh --workspace /path/to/workspace/virtio-rng
```

List environment and comparison commands:

```bash
python3.11 ../driver-port-env/scripts/experiment.py --help
```

Open the CSV files in `../data/` to inspect the experiment results.
