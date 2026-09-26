"""Turn a disk selector from install.toml into exactly one block device.

Selectors name a disk by something that survives a reboot and a re-cabling:
its serial, its /dev/disk/by-id name, or its WWN. { path = "/dev/vda" } is
allowed too, for VMs, where kernel names are stable and serials often absent.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .schema import DiskSelector

LSBLK_COLUMNS = "NAME,PATH,TYPE,SIZE,FSTYPE,UUID,SERIAL,WWN,MODEL,VENDOR,TRAN,PTTYPE,MOUNTPOINTS"
BY_ID_DIR = Path(os.environ.get("CHEFS_KITCHEN_BY_ID_DIR", "/dev/disk/by-id"))


def inventory() -> list[dict]:
    """Every block device as lsblk -J reports it, flattened (disks and partitions)."""
    fixture = os.environ.get("CHEFS_KITCHEN_LSBLK_JSON")
    if fixture:
        data = json.loads(Path(fixture).read_text())
    else:
        output = subprocess.run(
            ["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS], capture_output=True, text=True, check=True
        ).stdout
        data = json.loads(output)

    devices: list[dict] = []

    def walk(nodes: list[dict]) -> None:
        for node in nodes:
            devices.append(node)
            walk(node.get("children") or [])

    walk(data.get("blockdevices") or [])
    return devices


@dataclass
class Resolution:
    disk: str | None
    error: str | None = None


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _wwn(value: str | None) -> str:
    value = _norm(value)
    return value[2:] if value.startswith("0x") else value


def resolve(selector: DiskSelector, devices: list[dict]) -> Resolution:
    disks = [d for d in devices if d.get("type") == "disk"]

    if selector.kind == "serial":
        matches = [d["path"] for d in disks if _norm(d.get("serial")) and _norm(d.get("serial")) == _norm(selector.value)]
    elif selector.kind == "wwn":
        matches = [d["path"] for d in disks if _wwn(d.get("wwn")) and _wwn(d.get("wwn")) == _wwn(selector.value)]
    elif selector.kind == "by_id":
        name = selector.value.removeprefix(f"{BY_ID_DIR}/")
        link = BY_ID_DIR / name
        if not link.is_symlink():
            return Resolution(None, f"no disk named {name} in {BY_ID_DIR}")
        target = os.path.realpath(link)
        found = next((d for d in devices if d.get("path") == target), None)
        if found and found.get("type") != "disk":
            return Resolution(None, f"{name} is a partition ({target}), not a whole disk")
        matches = [target] if found else []
    else:
        target = os.path.realpath(selector.value)
        found = next((d for d in devices if d.get("path") in (selector.value, target)), None)
        if found and found.get("type") != "disk":
            return Resolution(None, f"{selector.value} is a {found.get('type')}, not a whole disk")
        matches = [found["path"]] if found else []

    if not matches:
        return Resolution(None, f"no disk matches {selector}")
    if len(matches) > 1:
        return Resolution(None, f"{selector} matches {len(matches)} disks ({', '.join(matches)}); pick one by by_id or wwn")
    return Resolution(matches[0])


def device(devices: list[dict], path: str) -> dict:
    return next((d for d in devices if d.get("path") == path), {})
