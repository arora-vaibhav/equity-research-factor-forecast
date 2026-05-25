"""Minimal .env reader + provider->API-key lookup (Phase A.3.1).

No new runtime dependency: parses simple `KEY=value` directly.
Supports comments (#), blank lines, and quoted values (single or double).
Missing files return empty mappings — sources degrade gracefully.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml


_DEFAULT_ENV_PATH = Path(".env")
_DEFAULT_KEYS_YAML_PATH = Path("config") / "api_keys.yaml"


def load_env(path: Path | str = _DEFAULT_ENV_PATH) -> dict[str, str]:
    """Read a .env file and return its mapping.

    Each line is `KEY=value`. Comments (lines starting with `#`) and
    blank lines are ignored. Values may be optionally wrapped in single
    or double quotes — quotes are stripped. Lines without `=` are skipped.

    Missing file returns {}.
    """
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not key:
            continue
        out[key] = value
    return out


def _load_provider_mapping(keys_yaml_path: Path | str = _DEFAULT_KEYS_YAML_PATH) -> dict[str, str]:
    p = Path(keys_yaml_path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {str(k): str(v) for k, v in data.items()}


def get_api_key(
    provider: str,
    env_path: Path | str = _DEFAULT_ENV_PATH,
    keys_yaml_path: Path | str = _DEFAULT_KEYS_YAML_PATH,
) -> Optional[str]:
    """Look up the API key for `provider` by resolving its env var.

    1. Read api_keys.yaml -> {provider_name: env_var_name}
    2. If provider not in mapping, return None
    3. Read .env -> {env_var: value}
    4. Return env value, or None if absent
    """
    mapping = _load_provider_mapping(keys_yaml_path)
    env_var_name = mapping.get(provider)
    if env_var_name is None:
        return None
    env = load_env(env_path)
    value = env.get(env_var_name)
    if value is None or value == "":
        return None
    return value
