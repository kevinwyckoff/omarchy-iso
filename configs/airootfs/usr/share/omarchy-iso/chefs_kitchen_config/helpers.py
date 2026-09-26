"""Calls into the configurator's Bash disk helpers.

The wizard and chefs-kitchen must agree on which disks are installable and on
what the wipe summary says, so both use the same code: disk-inspect.sh, sourced
in a Bash subprocess, rather than a second implementation in Python.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

HELPER_DIR = Path(os.environ.get("CHEFS_KITCHEN_HELPER_DIR", Path(__file__).resolve().parent.parent))


def bash(script: str, snippet: str, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    """Source HELPER_DIR/<script>, run <snippet> with args as $1.., return stdout."""
    result = subprocess.run(
        ["bash", "-c", f'source "$0"; shift; set -- "$@"; {snippet}', str(HELPER_DIR / script), "-", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C.UTF-8", **(env or {})},
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"{script}: {snippet.split()[0]} failed: {result.stderr.strip() or result.returncode}")
    return result.stdout


def disk_inspect(snippet: str, *args: str, install_medium: str = "", check: bool = True, env: dict | None = None) -> str:
    return bash(
        "disk-inspect.sh",
        f"disk_inventory_refresh; {snippet}",
        *args,
        env={"DISK_INSPECT_INSTALL_MEDIUM": install_medium, **(env or {})},
        check=check,
    )


def install_medium() -> str:
    return bash("disk-inspect.sh", "disk_install_medium").strip()


def installable_disks(medium: str) -> list[str]:
    return disk_inspect("installable_disks", install_medium=medium).split()


def is_cidata(disk: str) -> bool:
    return disk_inspect('disk_is_cidata "$1" && echo yes || true', disk).strip() == "yes"


def has_signatures(disk: str) -> bool:
    return disk_inspect('disk_has_signatures "$1" && echo yes || true', disk).strip() == "yes"


def busy_partitions(disk: str) -> list[str]:
    return [line for line in disk_inspect('disk_busy_partitions "$1"', disk).splitlines() if line]


def wipe_summary(
    disk: str, encrypt: bool, width: int, medium: str, swap_strategy: str = "zram+hibernate",
    other_erased: str = "", mode: str = "full_disk",
) -> str:
    return disk_inspect(
        'disk_probe_all; render_wipe_summary "$1" "$4" "$2" "$3"',
        disk, "true" if encrypt else "false", str(width), mode,
        install_medium=medium,
        env={"DISK_INSPECT_SWAP_STRATEGY": swap_strategy, "DISK_INSPECT_OTHER_ERASED": other_erased},
    )


def min_full_disk_bytes() -> int:
    return int(bash("disk-inspect.sh", 'echo "$DISK_INSPECT_MIN_FULL_DISK_B"').strip())
