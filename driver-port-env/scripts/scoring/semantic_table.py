"""Render data only; first scoring pass, never averages repeated old candidates."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "results/scoring"
PACKAGE = ROOT / "four-semantics-20261013"
METHODS = (
    "factory-gpt6-sol",
    "codex-gpt6-sol",
    "openhands-gpt6-sol",
    "original-asterinas",
)
CASES = {
    "virtio-rng": [
        ("partial", "部分读取"),
        ("interrupt", "SIGKILL 等待终止"),
        ("selection", "设备选择恢复"),
        ("quality", "质量边界"),
    ],
    "virtio-blk": [
        ("grow", "容量增长"),
        ("shrink", "容量缩小"),
        ("readonly", "只读保护"),
        ("unwritten-status", "未写状态完成"),
        ("empty-completion", "零长度完成"),
    ],
    "virtio-net": [
        ("delivery", "有限速率交付"),
        ("after-load", "负载后恢复"),
        ("device-needs-reset", "设备错误后停止收发"),
    ],
    "nvme-pci": [
        ("largeio", "多页读写"),
        ("namespaces", "namespace 隔离"),
        ("timeout-stop", "命令超时停机"),
    ],
    "ne2k-pci": [
        ("ring-pressure", "接收环积压恢复"),
        ("tx-completion-loss", "发送完成丢失恢复"),
    ],
}


def main():
    rows = []
    measurements = []
    for driver, cases in CASES.items():
        directory = (
            ROOT / "rng-semantics-20261013/execution-v3"
            if driver == "virtio-rng"
            else PACKAGE / (driver + ("-v3" if driver == "virtio-net" else "-v1"))
        )
        values = {}
        directories = [directory]
        faults = {
            "virtio-blk": "block-fault-v4",
            "virtio-net": "network-fault-v1",
            "nvme-pci": "nvme-fault-v4",
            "ne2k-pci": "ne2k-tx-fault-v1",
        }
        if driver in faults:
            directories.append(PACKAGE / faults[driver])
        for directory in directories:
            for method in METHODS:
                source = directory / method / "summary.json"
                if source.exists():
                    for row in json.loads(source.read_text())["results"]:
                        if row.get("repetition", 1) != 1:
                            continue
                        if (method, row["case"]) in values:
                            raise ValueError("Duplicate authoritative case")
                        values[method, row["case"]] = row
                        measurements.append(
                            {
                                "driver": driver,
                                "method": method,
                                "source": str(source),
                                **row,
                            }
                        )
        for case, label in cases:
            cells = []
            for method in METHODS:
                value = values.get((method, case), {})
                text = value.get("status", "")
                if "delivered" in value:
                    text += f" ({value['delivered']}/{value['sent']})"
                cells.append(text)
            rows.append([driver, label, *cells])
    header = ["驱动", "测例", "Factory", "Codex＋goal", "OpenHands", "原星绽"]
    (PACKAGE / "comparison.zh-CN.md").write_text(
        "| "
        + " | ".join(header)
        + " |\n| "
        + " | ".join(["---"] * len(header))
        + " |\n"
        + "".join("| " + " | ".join(r) + " |\n" for r in rows)
    )
    (PACKAGE / "measurements.json").write_text(
        json.dumps(measurements, indent=2, ensure_ascii=False) + "\n"
    )


if __name__ == "__main__":
    main()
