"""Local non-secret configuration and model credentials for operator commands."""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from experiment_config import load_config

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO / ".local/team.json"


def read_profile(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text())
    if any(key in data for key in ("api_key", "key", "token")):
        raise ValueError(
            "Keep credentials in api_key_file or api_key_env, not the JSON profile"
        )
    validate_url(data["base_url"])
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", data["api_key_env"]):
        raise ValueError("Invalid api_key_env variable name")
    if data.get("codex_version") != "0.160.1":
        raise ValueError("This experiment runner is pinned to Codex 0.160.1")
    for key in (
        "factory",
        "workspace",
        "pricing",
        "api_key_file",
        "model_catalog_json",
    ):
        if data.get(key):
            data[key] = resolve(path, data[key])
    for group in ("repositories", "suites"):
        data[group] = {key: resolve(path, value) for key, value in data[group].items()}
    binary = data.get("codex_bin", "codex")
    data["codex_bin"] = str(resolve(path, binary)) if "/" in binary else binary
    return data


def validate_url(value):
    url = urlsplit(value)
    if (
        url.scheme not in {"https", "http"}
        or not url.netloc
        or url.username
        or url.password
    ):
        raise ValueError("base_url must be an HTTP(S) API URL without credentials")
    if url.query or url.fragment:
        raise ValueError("base_url must not contain a query or fragment")


def resolve(profile, value):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else profile.parent / path).resolve()


def initialize(path, ask_key=False):
    import getpass

    path = Path(path).resolve()
    if path.exists():
        raise ValueError("Profile exists; edit it directly rather than overwriting it")
    data = json.loads((REPO / "configs/experiments/team.example.json").read_text())
    origin = REPO / "configs/experiments/team.example.json"
    for key in ("factory", "workspace", "pricing", "api_key_file", "codex_bin"):
        data[key] = str(resolve(origin, data[key]))
    for group in ("repositories", "suites"):
        data[group] = {
            key: str(resolve(origin, value)) for key, value in data[group].items()
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)
    if ask_key:
        key = getpass.getpass("中转站 API key（隐藏输入，不写入 JSON/Git）：").strip()
        if not key or "\n" in key or "\r" in key:
            raise ValueError("API key must be a nonempty single line")
        key_file = Path(data["api_key_file"])
        key_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(key + "\n")
    print("Local profile:", path)


def model_environment(profile):
    key = os.environ.get(profile["api_key_env"], "").strip()
    if not key and profile.get("api_key_file"):
        key_file = profile["api_key_file"]
        if key_file.exists():
            if key_file.stat().st_mode & 0o077:
                raise ValueError("API key file must have mode 600")
            key = key_file.read_text().strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError(
            "Set api_key_env or the private api_key_file before running models"
        )
    # Existing isolated execution uses this environment variable on both methods.
    # It is an internal credential transport name, not a requirement to use example-provider.
    return {**os.environ, "example-provider_API_KEY": key}


def catalog():
    result = {}
    for group, name in (
        ("native", "native-drivers.json"),
        ("public", "public-drivers.json"),
        ("intel", "intel-drivers.json"),
    ):
        config = load_config(REPO / "configs/experiments" / name)
        result.update(
            {
                driver: (group, config)
                for driver in config["drivers"]
                if (REPO / "tasks" / driver).exists()
            }
        )
    return result


def selected_suite(profile, driver):
    return profile["suites"][catalog()[driver][0]]
