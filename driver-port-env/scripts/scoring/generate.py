"""Deterministic, data-only variants of already supplied public tests."""

import copy
import hashlib
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "scoring/variants/parameter-variants.json"
GENERATED = REPO / "scoring/generated"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def replace(text, before, after):
    if before not in text:
        raise ValueError("Pinned test constant missing: " + before)
    return text.replace(before, after)


def mutate_c(driver, source, p):
    if driver == "virtio-rng":
        names = {
            "HWRNG_CONCURRENT_READERS": "readers",
            "HWRNG_CONCURRENT_READ_SIZE": "concurrent_bytes",
            "HWRNG_PRIME_READ_SIZE": "prime_bytes",
            "HWRNG_SHORT_READ_SIZE": "short_bytes",
            "HWRNG_LARGE_READ_SIZE": "large_bytes",
            "HWRNG_SMOKE_READ_SIZE": "smoke_bytes",
        }
        for name, key in names.items():
            source, count = re.subn(
                r"(?m)^#define " + name + r" .+$",
                "#define " + name + " " + str(p[key]),
                source,
            )
            if count != 1:
                raise ValueError(name)
        source = replace(
            source,
            "sizes[] = { 1, 3, 7 }",
            "sizes[] = { " + ", ".join(map(str, p["small_reads"])) + " }",
        )
        source = replace(
            source,
            "uint8_t buf[7] = { 0 };",
            "uint8_t buf[" + str(max(p["small_reads"])) + "] = { 0 };",
        )
    elif driver == "nvme-pci":
        source = replace(
            source,
            "#define BUFFER_SIZE 65536",
            "#define BUFFER_SIZE " + str(p["buffer_bytes"]),
        )
        source = replace(
            source,
            "srand((unsigned int)time(NULL));",
            "srand(" + str(p["random_seed"]) + ");",
        )
        source = replace(source, "/nvme/nvme_test", p["file"])
    elif driver == "virtio-blk":
        for a, z in [
            ("short_read[64]", f"short_read[{p['short_bytes']}]"),
            ("bytes[13]", f"bytes[{p['small_bytes']}]"),
            ("SECTOR_SIZE - 5", f"SECTOR_SIZE - {p['boundary_backoff']}"),
            ("{ 'X', 'Y', 'Z' }", "{ " + ", ".join(repr(x) for x in p["patch"]) + " }"),
            ('"hello world"', json.dumps(p["message"])),
            ("wbuf[100]", f"wbuf[{p['partial_bytes']}]"),
            ("rbuf[100]", f"rbuf[{p['partial_bytes']}]"),
            ("'A'", repr(p["pattern_fill"])),
            ("wbuf[8192]", f"wbuf[{p['cross_bytes']}]"),
            ("rbuf[8192]", f"rbuf[{p['cross_bytes']}]"),
            ("(i & 0xff)", f"((i * {p['pattern_stride']} + 11) & 0xff)"),
            ('"abcdefg"', json.dumps(p["short_write_data"])),
        ]:
            source = source.replace(a, z)
    return source


def mutate_fixture(driver, source, p):
    if driver == "evbug":
        source = replace(source, "KeyCode::A", "KeyCode::" + p["key"])
        source = replace(
            source, "RelCode::X, -7", "RelCode::X, " + str(p["relative_x"])
        )
        source = replace(source, "RelCode::Y, 5", "RelCode::Y, " + str(p["relative_y"]))
        source = replace(source, "i32::MIN", str(p["minimum"]))
        source = replace(source, "i32::MAX", str(p["maximum"]))
        source = re.sub(
            r'"(dpf-(?:events|early|late|boundaries))"',
            lambda m: '"' + m[1] + p["name_suffix"] + '"',
            source,
        )
    else:
        for name, key in [("MAC", "mac"), ("PEER", "peer_mac")]:
            value = ", ".join("0x" + x for x in p[key].split(":"))
            source, count = re.subn(
                r"const " + name + r": \[u8; 6\] = \[[^\n]+\];",
                f"const {name}: [u8; 6] = [{value}];",
                source,
            )
            if count != 1:
                raise ValueError(name)
        source = replace(
            source, "wrapping_mul(17)", f"wrapping_mul({p['pattern_stride']})"
        )
        source = replace(source, "0xa5", hex(p["response_xor"]))
        source = replace(source, "0..96", f"0..{p['traffic_count']}")
        source = replace(source, "[60, 61, 127, 256, 513, 1514]", str(p["lengths"]))
    return source


def fixture_cases(driver, cases, p):
    rows = copy.deepcopy(cases)
    for row in rows:
        case = row["case"]
        if driver == "evbug":
            for step in case["steps"]:
                if "assert_serial_matches" not in step:
                    continue
                for observed in step["assert_serial_matches"]["expected"]:
                    for i, v in enumerate(observed):
                        if v.startswith("dpf-"):
                            observed[i] = v + p["name_suffix"]
                    if len(observed) == 4:
                        if observed[1] == "1":
                            observed[2] = str(p["key_code"])
                        if observed[3] == "-7":
                            observed[3] = str(p["relative_x"])
                        if observed[3] == "5":
                            observed[3] = str(p["relative_y"])
                        if observed[3] == "-2147483648":
                            observed[3] = str(p["minimum"])
                        if observed[3] == "2147483647":
                            observed[3] = str(p["maximum"])
        else:
            case["devices"] = [
                x.replace("52:54:00:12:34:56", p["mac"]) for x in case["devices"]
            ]
            if row["id"] == "ne2k-traffic":
                expected = [[n, p["lengths"][n % 6]] for n in range(p["traffic_count"])]
                case["network_peer"]["expected_exchanges"] = expected + [[2000, 60]]
                for step in case["steps"]:
                    a = step.get("assert_serial_matches")
                    if a and "EXCHANGE" in a["pattern"]:
                        a["expected"] = [[str(x), str(y)] for x, y in expected]
    return rows


def generate():
    spec = json.loads(SPEC.read_text())
    manifest = {
        "parameters_sha256": sha(SPEC),
        "upstream_files": {},
        "generated_files": {},
    }
    native = {
        "virtio-rng": ["device/hwrng.c"],
        "virtio-blk": [
            "io/file_io/block_device.c",
            "fs/ext2/file_io.c",
            "fs/ext2/short_rw.c",
        ],
        "nvme-pci": ["device/nvme.c"],
    }
    for variant, parameters in spec["profiles"].items():
        for driver, paths in native.items():
            original = REPO / "tasks" / driver / "target/test/initramfs/src/regression"
            dest = GENERATED / driver / variant
            for rel in [
                *paths,
                "common/test.h",
                *(["common/block_device.h"] if driver == "virtio-blk" else []),
            ]:
                src = original / rel
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    mutate_c(driver, src.read_text(), parameters[driver])
                    if rel in paths
                    else src.read_text()
                )
                manifest["upstream_files"][str(src.relative_to(REPO))] = sha(src)
        for driver, file in [
            ("evbug", "evbug_public.rs"),
            ("ne2k-pci", "ne2k_public.rs"),
        ]:
            src = REPO / "src/driver_port_factory/data/public-tests" / driver / file
            if not src.exists():
                raise ValueError("Missing original fixture " + str(src))
            dest = GENERATED / driver / variant
            dest.mkdir(parents=True, exist_ok=True)
            (dest / file).write_text(
                mutate_fixture(driver, src.read_text(), parameters[driver])
            )
            cases = json.loads((src.parent / "cases.json").read_text())
            (dest / "cases.json").write_text(
                json.dumps(fixture_cases(driver, cases, parameters[driver]), indent=2)
                + "\n"
            )
            manifest["upstream_files"][str(src.relative_to(REPO))] = sha(src)
    for p in GENERATED.rglob("*"):
        if p.is_file():
            manifest["generated_files"][str(p.relative_to(REPO))] = sha(p)
    (REPO / "scoring/variants/generated-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n"
    )
    print(
        "Generated",
        len(manifest["generated_files"]),
        "test/fixture files; no candidate or kernel changed.",
    )


if __name__ == "__main__":
    generate()
