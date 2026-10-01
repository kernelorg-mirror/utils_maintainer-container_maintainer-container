# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 The Linux Foundation and contributors
"""Tests for defaults.env, sourced by bash the way the scripts source it."""

import os
import subprocess
from pathlib import Path
from typing import Dict

DEFAULTS = Path(__file__).resolve().parent.parent / 'defaults.env'


def sourced(name: str, env: Dict[str, str]) -> str:
    """The value of `name' after sourcing defaults.env, or `<unset>'."""
    script = f'set -a; . "$1"; printf %s "${{{name}-<unset>}}"'
    proc = subprocess.run(
        ['bash', '-c', script, 'bash', str(DEFAULTS)], env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout


def clean_env() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != 'ARCHIVE_UPSTREAM'}


def test_archive_upstream_defaults_to_lore() -> None:
    assert sourced('ARCHIVE_UPSTREAM', clean_env()) == 'https://lore.kernel.org/all/'


def test_an_empty_archive_upstream_stays_empty() -> None:
    """`-e ARCHIVE_UPSTREAM=' is how the headers are turned off. With `:='
    instead of `=' in defaults.env, it would turn back into lore."""
    assert sourced('ARCHIVE_UPSTREAM', {**clean_env(), 'ARCHIVE_UPSTREAM': ''}) == ''


def test_archive_upstream_can_be_overridden() -> None:
    url = 'https://mirror.example.org/all/'
    assert sourced('ARCHIVE_UPSTREAM', {**clean_env(), 'ARCHIVE_UPSTREAM': url}) == url
