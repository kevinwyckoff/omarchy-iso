"""`chefs-kitchen plan`: resolve install.toml against this machine.

A plan says which disk the config names, what is on it (the same wipe summary
the wizard shows), its fingerprint, and whether the install may go ahead.
Nothing here writes to a disk.

The fingerprint is the unattended equivalent of "yes, *that* drive, with
*that* data on it": a sha256 over the partition-table type and, for each
partition, its start, size, type GUID and filesystem UUID. `plan` prints it so
it can be pasted into disk.expect_fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field

from . import helpers, resolve
from .schema import InstallConfig, Issue

# Knobs the schema accepts that this ISO can't install yet. Each later change
# that adds support removes its entry here.
_NOT_YET = "is not supported by this ISO yet"


@dataclass
class DiskPlan:
    path: str
    fingerprint: str
    has_signatures: bool
    summary: str = ""


@dataclass
class Plan:
    config: InstallConfig
    unattended: bool
    target: DiskPlan | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


def fingerprint(disk: str, devices: list[dict]) -> str:
    table = _partition_table(disk)
    lines = [f"table {table.get('label', 'none')}"]
    for part in sorted(table.get("partitions", []), key=lambda p: int(p.get("start", 0))):
        fs_uuid = resolve.device(devices, part.get("node", "")).get("uuid") or "-"
        lines.append(f"part {part.get('start')} {part.get('size')} {str(part.get('type', '')).lower()} {fs_uuid}")
    digest = hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()
    return f"sha256:{digest}"


def _partition_table(disk: str) -> dict:
    fixture = os.environ.get("CHEFS_KITCHEN_SFDISK_JSON_DIR")
    if fixture:
        path = os.path.join(fixture, os.path.basename(disk) + ".json")
        return json.load(open(path))["partitiontable"] if os.path.exists(path) else {}
    result = subprocess.run(["sfdisk", "--json", disk], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return {}  # no partition table
    return json.loads(result.stdout).get("partitiontable", {})


def _is_virtual_machine() -> bool:
    result = subprocess.run(["systemd-detect-virt", "--vm", "--quiet"], capture_output=True)
    return result.returncode == 0


def make_plan(config: InstallConfig, unattended: bool, width: int = 100) -> Plan:
    plan = Plan(config, unattended)

    def refuse(path: str, message: str) -> None:
        plan.issues.append(Issue(path, message))

    if config.mode == "free-space":
        refuse("disk.mode", f'"free-space" {_NOT_YET}')
    if config.home_location == "disk":
        refuse("disk.home.location", f'"disk" {_NOT_YET}')
    if config.swap_strategy != "zram+hibernate":
        refuse("swap.strategy", f"{config.swap_strategy!r} {_NOT_YET}")
    if config.theme:
        refuse("desktop.theme", _NOT_YET)
    if config.agent:
        refuse("desktop.agent", _NOT_YET)
    if config.extra_packages:
        refuse("packages.extra", _NOT_YET)

    if unattended and config.needs_passphrase_prompt():
        refuse("encryption.passphrase", "needs to be typed at install time, which --yes can't do")

    if config.target is None:
        return plan

    devices = resolve.inventory()
    found = resolve.resolve(config.target, devices)
    if found.error:
        refuse("disk.target", found.error)
        return plan
    disk = found.disk

    medium = helpers.install_medium()
    if disk == medium:
        refuse("disk.target", f"{disk} is the install medium this ISO booted from")
        return plan
    if helpers.is_cidata(disk):
        refuse("disk.target", f"{disk} is a cidata drive")
        return plan
    if disk not in helpers.installable_disks(medium):
        refuse("disk.target", f"{disk} isn't a disk Omarchy can be installed on")
        return plan

    size = int(resolve.device(devices, disk).get("size") or 0)
    minimum = helpers.min_full_disk_bytes()
    if size < minimum:
        refuse("disk.target", f"{disk} is {size // 2**30} GiB; Omarchy needs at least {minimum // 2**30} GiB")

    if config.target.kind == "path" and not _is_virtual_machine():
        plan.issues.append(Issue("disk.target", f"{{ path = ... }} can name a different disk after a reboot or re-cabling; on real hardware prefer serial, by_id or wwn", "warning"))

    target = DiskPlan(disk, fingerprint(disk, devices), helpers.has_signatures(disk))
    target.summary = helpers.wipe_summary(disk, config.encryption_enabled, width, medium)
    plan.target = target

    if config.expect_fingerprint and config.expect_fingerprint != target.fingerprint:
        refuse(
            "disk.expect_fingerprint",
            f"{disk} no longer looks like it did: its fingerprint is now {target.fingerprint}",
        )
    elif unattended and target.has_signatures and config.on_existing_data == "abort":
        refuse(
            "disk.on_existing_data",
            f'{disk} has data on it and on_existing_data = "abort". To erase it anyway, set '
            f'on_existing_data = "wipe", and expect_fingerprint = "{target.fingerprint}" '
            "so only this exact layout gets erased",
        )

    for line in helpers.busy_partitions(disk):
        part, _, where = line.partition("\t")
        refuse("disk.target", f"{part} is in use ({where}); unmount it or turn its swap off first")

    return plan
