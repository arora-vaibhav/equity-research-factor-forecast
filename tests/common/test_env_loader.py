"""Tests for src/common/env_loader.py (Phase A.3.1)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.common.env_loader import load_env, get_api_key


def test_load_env_reads_simple_key_value_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO=bar\nBAZ=qux\n")
    result = load_env(env_file)
    assert result == {"FOO": "bar", "BAZ": "qux"}


def test_load_env_ignores_comments_and_blank_lines(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# this is a comment\n"
        "\n"
        "REAL_KEY=value\n"
        "# another comment\n"
        "   \n"
        "OTHER=thing\n"
    )
    result = load_env(env_file)
    assert result == {"REAL_KEY": "value", "OTHER": "thing"}


def test_load_env_handles_quoted_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'KEY1="quoted value"\n'
        "KEY2='single quoted'\n"
        "KEY3=unquoted\n"
    )
    result = load_env(env_file)
    assert result == {
        "KEY1": "quoted value",
        "KEY2": "single quoted",
        "KEY3": "unquoted",
    }


def test_load_env_missing_file_returns_empty_dict(tmp_path: Path):
    result = load_env(tmp_path / "nonexistent.env")
    assert result == {}


def test_load_env_skips_malformed_lines(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GOOD=value\n"
        "no_equals_sign_here\n"
        "ALSO_GOOD=2\n"
    )
    result = load_env(env_file)
    assert result == {"GOOD": "value", "ALSO_GOOD": "2"}


def test_get_api_key_returns_value_when_present(tmp_path):
    (tmp_path / ".env").write_text("FMP_API_KEY=test-fmp-key\n")
    (tmp_path / "api_keys.yaml").write_text(
        "fmp: FMP_API_KEY\npolygon: POLYGON_API_KEY\n"
    )
    from src.common import env_loader as el
    key = el.get_api_key(
        "fmp",
        env_path=tmp_path / ".env",
        keys_yaml_path=tmp_path / "api_keys.yaml",
    )
    assert key == "test-fmp-key"


def test_get_api_key_returns_none_when_provider_unknown(tmp_path):
    (tmp_path / ".env").write_text("FMP_API_KEY=abc\n")
    (tmp_path / "api_keys.yaml").write_text("fmp: FMP_API_KEY\n")
    from src.common import env_loader as el
    assert el.get_api_key(
        "nonexistent_provider",
        env_path=tmp_path / ".env",
        keys_yaml_path=tmp_path / "api_keys.yaml",
    ) is None


def test_get_api_key_returns_none_when_env_var_not_set(tmp_path):
    (tmp_path / ".env").write_text("")
    (tmp_path / "api_keys.yaml").write_text("fmp: FMP_API_KEY\n")
    from src.common import env_loader as el
    assert el.get_api_key(
        "fmp",
        env_path=tmp_path / ".env",
        keys_yaml_path=tmp_path / "api_keys.yaml",
    ) is None
