"""Produce a separate acceptance score table; never overwrite public comparison results."""

import csv
import json
import re
from pathlib import Path

ENV = Path(__file__).resolve().parents[2]
ROOT = ENV / "results/scoring"
DRIVERS = {
    "evbug": ("evbug", 3),
    "virtio-rng": ("VirtIO RNG", 1),
    "virtio-blk": ("VirtIO Block", 3),
    "virtio-net": ("VirtIO Network", 3),
    "ne2k-pci": ("NE2000 PCI", 3),
    "nvme-pci": ("NVMe PCI", 1),
}
METHODS = {
    "dpf": "Factory",
    "codex": "Codex＋goal",
    "dpf-legacy": "Factory（旧开发候选）",
}


def read(path):
    return json.loads(path.read_text())


def cases(path):
    return [
        row for summary in sorted(path.glob("summary*.json")) for row in read(summary)
    ]


def function_score(observations):
    expected = [(row, name) for row in observations for name in row.get("expected", [])]
    if not expected:
        return "—"
    count = sum(
        name in row.get("observed", {})
        and row["observed"][name][0] > 0
        and row["observed"][name][1] == 0
        for row, name in expected
    )
    return f"{count}/{len(expected)}"


def main():
    manifest = read(ROOT / "candidates-20261006.json")
    rows = []
    all_cases = []
    for driver, (label, count) in DRIVERS.items():
        for method in ["dpf", "codex"]:
            matches = [
                c
                for c in manifest["candidates"]
                if c["driver"] == driver
                and (
                    c["method"] == method
                    or method == "dpf"
                    and c["method"] == "dpf-legacy"
                )
            ]
            candidate = matches[0] if matches else None
            actualmethod = candidate["method"] if candidate else method
            path = ROOT / "run" / driver / actualmethod
            observations = cases(path)
            status = (
                "NOT_RUN"
                if not candidate
                else (
                    "BLOCKED"
                    if (path / "operator-error.json").exists()
                    else "COMPLETE"
                    if len(observations) == count * 2
                    else "INCOMPLETE"
                )
            )
            scores = {
                v: f"{sum(r['status'] == 'PASS' for r in observations if r['variant'] == v)}/{count}"
                if status in {"COMPLETE", "INCOMPLETE"}
                else "—"
                for v in ["A", "B"]
            }
            total = (
                f"{sum(r['status'] == 'PASS' for r in observations)}/{count * 2}"
                if observations
                else "—"
            )
            original = (
                candidate.get("provenance", {}).get(
                    "original_public_cases",
                    "旧开发 3/3" if actualmethod == "dpf-legacy" else "—",
                )
                if candidate
                else "—"
            )
            row = {
                "driver": label,
                "driver_id": driver,
                "method": METHODS[actualmethod],
                "original_public_cases": original,
                "variant_A": scores["A"],
                "variant_B": scores["B"],
                "additional_cases": total,
                "functions": function_score(observations),
                "status": status,
                "candidate_iso_sha256": candidate["iso_sha256"] if candidate else None,
                "identity": "源码重建¹"
                if driver in {"evbug", "ne2k-pci"} and candidate
                else "原内核 ELF 相同"
                if candidate
                else "无候选",
                "results": str(path.relative_to(ENV)) if candidate else None,
            }
            rows.append(row)
            for observed in observations:
                evidence = (
                    path / "results" / observed["variant"] / Path(observed["case"]).stem
                )
                serialpath = evidence / "serial.log"
                serial = (
                    serialpath.read_text(errors="replace")
                    if serialpath.exists()
                    else ""
                )
                serial = re.sub(r"\x1b\[[0-9;]*m", "", serial)
                panic = re.search(r"Uncaught panic:\s*([^\r\n]+)", serial)
                failure = (
                    "KERNEL_PANIC: " + panic[1]
                    if panic
                    else "HOST_TIMEOUT"
                    if observed.get("host_timeout")
                    else "ASSERTION_FAILURE"
                    if observed["status"] == "FAIL"
                    else observed["status"]
                )
                all_cases.append(
                    {
                        "method": METHODS[actualmethod],
                        **observed,
                        "evidence_directory": str(evidence.relative_to(ENV)),
                        "outcome_classification": failure,
                    }
                )
    columns = [
        "驱动",
        "方法",
        "原公开场景",
        "变种 A",
        "变种 B",
        "追加总分",
        "测试函数",
        "执行状态",
        "内核身份",
    ]
    lines = [
        "# 参数变种追加评分",
        "",
        "这是追加验收评分，未进行驱动修复或模型调用。分母为原测试程序/场景；测试函数另列。`NOT_RUN` 不计失败。原公开成绩保持原样。",
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                str(row[key])
                for key in [
                    "driver",
                    "method",
                    "original_public_cases",
                    "variant_A",
                    "variant_B",
                    "additional_cases",
                    "functions",
                    "status",
                    "identity",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## 原实现校准",
        "",
        "| 驱动 | A | B | 测试函数 | 状态 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for driver in ["virtio-rng", "virtio-blk", "virtio-net", "nvme-pci"]:
        obs = cases(ROOT / "reference" / driver / "reference")
        n = DRIVERS[driver][1]
        lines.append(
            "| "
            + " | ".join(
                [
                    DRIVERS[driver][0],
                    *[
                        f"{sum(r['status'] == 'PASS' for r in obs if r['variant'] == v)}/{n}"
                        for v in ["A", "B"]
                    ],
                    function_score(obs),
                    "PASS"
                    if len(obs) == n * 2 and all(r["status"] == "PASS" for r in obs)
                    else "INCOMPLETE_OR_FAIL",
                ]
            )
            + " |"
        )
    failures = [r for r in all_cases if r["status"] != "PASS"]
    lines += [
        "",
        "## 未通过场景",
        "",
        "| 驱动 | 方法 | 参数组 | 场景 | 观察 | 原始证据 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in failures:
        relative = str(Path(r["evidence_directory"]).relative_to("results/scoring"))
        lines.append(
            "| "
            + " | ".join(
                [
                    DRIVERS[r["driver"]][0],
                    r["method"],
                    r["variant"],
                    r["case"],
                    r["outcome_classification"],
                    f"[日志]({relative}/serial.log)",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "Network panic 的落点为 TCP 协议栈的序列号减法；本次只记录候选运行的失败现象，未定位并证明具体驱动根因。单次参数实例也不足以估计失败概率。",
        "",
    ]
    lines += [
        "",
        "## 判读与证据",
        "",
        "- `DEVELOPER_EVIDENCE`：测试设计者接触过历史候选和公开结果。本次不是独立盲测，不宣称证明未过拟合。无私有反馈、无失败重试、无翻译返修。",
        "- RNG Factory 为含续跑的历史探索候选；NE2000 Factory 为旧开发候选，没有配对 Codex；NVMe 为自主配对候选。不得混合成同一配置下的干净首轮对照实验。",
        "- evbug/NE2000 的数据位于内核测试夹具，因此构建评分衍生 ISO。驱动及全部其他封存源码核对相同；历史生成依赖锁已随构建缓存清理，无法验证依赖解析与原 ISO 一致。已留存重建解析锁与编译日志（Factory evbug A 未独立采集完整锁），这些结果应作为源码重建评分单列。",
        "- RNG/Block/NVMe 打包仅替换 initramfs 测试二进制，内核 ELF/GRUB 哈希相同；Network 原 ISO 完全不变。每个场景重新启动 QEMU，候选每组一次。",
        "- 原实现校准通过只说明变种可执行；未开展缺陷注入校准、真实硬件或独立语义审查。Block 的 ext2 文件程序自身不能独立证明具体驱动生效，须连同设备存在检查及块设备程序看待。",
        "- 通过不代表性能等价。Network 保持原收发非零判据，原始速率另存，不新增吞吐门槛。",
        "- 挂载/GPT打包/离线依赖的工具纠正尝试保存在 `harness-errors/`；这些尝试未执行候选评分，不删去记录、不把它们算作候选失败。",
        "",
        "[评分协议](../../docs/experiments/PARAMETER_VARIANT_SCORING.zh-CN.md) · [候选封存](candidates-20261006.json) · [逐场景结果](parameter-variant-cases.json) · [结构化表格](parameter-variant-results.json)",
        "",
    ]
    (ROOT / "PARAMETER_VARIANT_RESULTS.md").write_text("\n".join(lines))
    (ROOT / "parameter-variant-results.json").write_text(
        json.dumps(
            {
                "evaluation_mode": "DEVELOPER_EVIDENCE",
                "candidate_freeze_relation": "mixed chronology: historical candidates predate test design; NVMe variants predate translation; all candidates sealed before scoring",
                "test_author_candidate_exposure": "prior candidates/public results observed",
                "producer_private_feedback": "NONE",
                "rows": rows,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    (ROOT / "parameter-variant-cases.json").write_text(
        json.dumps(all_cases, indent=2, ensure_ascii=False) + "\n"
    )
    with (ROOT / "parameter-variant-results.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(
        "Recorded",
        len(rows),
        "method/driver rows and",
        len(all_cases),
        "candidate scoring observations",
    )


if __name__ == "__main__":
    main()
