"""Calls into the configurator's Bash disk helpers.

The wizard and chefs-kitchen must agree on which disks are installable and on
what the wipe summary says, so both use the same code: disk-inspect.sh, sourced
in a Bash subprocess, rather than a second implementation in Python.
"""

from __future__ import annotations

import os
import re
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


def free_space(snippet: str, *args: str, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a free-space.sh function, with disk-partitioning.sh loaded first."""
    return subprocess.run(
        # $0 is disk-partitioning.sh and $1 free-space.sh; shift leaves the arguments.
        ["bash", "-c", f'source "$0"; source "$1"; shift; {snippet}',
         str(HELPER_DIR / "disk-partitioning.sh"), str(HELPER_DIR / "free-space.sh"), *args],
        input=stdin, capture_output=True, text=True, check=check,
        env={**os.environ, "LC_ALL": "C.UTF-8"},
    )


def free_space_region(disk: str) -> tuple[bool, int, int, int, int] | int:
    """(needs_mklabel, efi_start, efi_end, root_start, root_end), or the usable
    bytes found when no region is big enough."""
    result = free_space('free_space_region "$1"', disk, check=False)
    fields = result.stdout.split()
    if result.returncode != 0 or len(fields) != 5:
        return int(fields[0]) if fields and fields[0].isdigit() else 0
    return (fields[0] == "true", *map(int, fields[1:]))


def bitlocker_partitions(disk: str) -> list[str]:
    return disk_inspect(
        'disk_probe "$1"; for p in $(disk_partitions "$1"); do [[ ${disk_contents[$p]:-} == "BitLocker on" ]] && echo "$p"; done; true',
        disk,
    ).split()


def free_space_summary(disk: str, encrypt: bool, width: int, medium: str, region: tuple, swap_strategy: str) -> str:
    _, efi_start, efi_end, root_start, root_end = region
    return disk_inspect(
        'disk_probe_all; render_wipe_summary "$1" free_space "$2" "$3" "$4" "$5" "$6" "$7"',
        disk, "true" if encrypt else "false", str(width), str(efi_start), str(efi_end), str(root_start), str(root_end),
        install_medium=medium,
        env={"DISK_INSPECT_SWAP_STRATEGY": swap_strategy},
    )


OFFLINE_MIRROR = Path("/var/cache/omarchy/mirror/offline")


def iso_list(name: str) -> list[str] | None:
    """A list build-iso.sh wrote for the bundled runtime (themes, agents), or
    None when this isn't an ISO that has one."""
    path = HELPER_DIR / name
    return [line.strip() for line in path.read_text().splitlines() if line.strip()] if path.exists() else None


def resolve_offline(packages: list[str]) -> str | None:
    """None when every package and its dependencies are in the ISO's offline
    mirror, otherwise pacman's complaint. The live ISO's pacman.conf is the
    offline one; a throwaway database keeps this from touching the live system's."""
    result = subprocess.run(
        ["bash", "-c", 'db=$(mktemp -d); trap "rm -rf $db" EXIT; '
         'pacman --dbpath "$db" -Sy >/dev/null 2>&1 && pacman --dbpath "$db" -S --print --print-format %n "$@" >/dev/null',
         "-", *packages],
        capture_output=True, text=True,
    )
    return None if result.returncode == 0 else (result.stderr.strip() or "pacman could not resolve them")


def min_full_disk_bytes() -> int:
    return int(bash("disk-inspect.sh", 'echo "$DISK_INSPECT_MIN_FULL_DISK_B"').strip())
