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
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import helpers, resolve
from .schema import InstallConfig, Issue



@dataclass
class DiskPlan:
    path: str
    fingerprint: str
    has_signatures: bool
    summary: str = ""
    # free-space mode: (needs_mklabel, efi_start, efi_end, root_start, root_end)
    region: tuple | None = None


@dataclass
class Plan:
    config: InstallConfig
    unattended: bool
    target: DiskPlan | None = None
    home: DiskPlan | None = None
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


# The directories localectl list-keymaps reads.
KEYMAP_DIRS = (Path("/usr/share/keymaps"), Path("/usr/share/kbd/keymaps"), Path("/usr/lib/kbd/keymaps"))


def console_keymaps() -> set[str] | None:
    """What localectl list-keymaps lists on this machine: <name>.map or
    <name>.map.gz anywhere under KEYMAP_DIRS. None if there are none."""
    keymaps = {
        path.name.removesuffix(".gz").removesuffix(".map")
        for root in KEYMAP_DIRS
        if root.is_dir()
        for path in root.rglob("*.map*")
        if path.name.endswith((".map", ".map.gz"))
    }
    return keymaps or None


def _check_keyboard(plan: Plan, config: InstallConfig) -> None:
    # The install's keyboard step looks the keymap up in localectl list-keymaps
    # on this same live system, and only logs one it doesn't find: the machine
    # comes up with a US console and a US desktop, with nobody at an unattended
    # install to see the line. Check the name here, in its case although that
    # step ignores case: loadkeys and systemd's XKB mapping look it up exactly,
    # so "DE-LATIN1" would end the same way.
    keymaps = console_keymaps()
    if keymaps is None:
        plan.issues.append(Issue("system.keyboard", "can't be checked here: this machine has no console keymaps", "warning"))
    elif config.keyboard not in keymaps:
        plan.issues.append(Issue(
            "system.keyboard",
            f'unknown keymap {config.keyboard!r} (use a name from localectl list-keymaps, like "de-latin1")',
        ))


def theme_slug(name: str) -> str:
    """How omarchy-theme-set names a theme's directory: "Tokyo Night" is tokyo-night."""
    return re.sub(r"<[^>]+>", "", name).lower().replace(" ", "-")


def _check_desktop(plan: Plan, config: InstallConfig) -> None:
    def warn(path: str, message: str) -> None:
        plan.issues.append(Issue(path, message, "warning"))

    if config.theme:
        themes = helpers.iso_list("themes")
        if themes is None:
            warn("desktop.theme", "can't be checked here: this isn't an ISO that lists its themes")
        elif theme_slug(config.theme) not in themes:
            plan.issues.append(Issue("desktop.theme", f"{config.theme!r} isn't a theme this ISO has: {', '.join(themes)}"))
    if config.agent:
        agents = helpers.iso_list("agents")
        if agents is None:
            warn("desktop.agent", "can't be checked here: this isn't an ISO that lists its agents")
        elif config.agent not in agents:
            plan.issues.append(Issue("desktop.agent", f"{config.agent!r} isn't an agent Omarchy knows: {', '.join(agents)}"))
        else:
            warn("desktop.agent", f"{config.agent} installs at first login, which needs a network connection")
    if config.extra_packages:
        if not helpers.OFFLINE_MIRROR.is_dir():
            warn("packages.extra", "can't be checked here: this isn't an ISO with an offline mirror")
        elif problem := helpers.resolve_offline(config.extra_packages):
            plan.issues.append(Issue("packages.extra", f"can't all be installed from the ISO's offline mirror: {problem}"))


def _check_disk(
    plan: Plan, key: str, selector, devices: list[dict], medium: str, minimum: int, minimum_note: str = ""
) -> str | None:
    """Resolve a selector to one installable disk, or record why not."""
    def refuse(message: str) -> None:
        plan.issues.append(Issue(key, message))

    found = resolve.resolve(selector, devices)
    if found.error:
        refuse(found.error)
        return None
    disk = found.disk
    if disk == medium:
        refuse(f"{disk} is the install medium this ISO booted from")
        return None
    if helpers.is_cidata(disk):
        refuse(f"{disk} is a cidata drive")
        return None
    if disk not in helpers.installable_disks(medium):
        refuse(f"{disk} isn't a disk Omarchy can be installed on")
        return None
    size = int(resolve.device(devices, disk).get("size") or 0)
    if size < minimum:
        refuse(f"{disk} is {size // 2**30} GiB; Omarchy needs at least {minimum // 2**30} GiB{minimum_note}")
    if selector.kind == "path" and not _is_virtual_machine():
        plan.issues.append(Issue(key, "{ path = ... } can name a different disk after a reboot or re-cabling; on real hardware prefer serial, by_id or wwn", "warning"))
    for line in helpers.busy_partitions(disk):
        part, _, where = line.partition("\t")
        refuse(f"{part} is in use ({where}); unmount it or turn its swap off first")
    return disk


def make_plan(config: InstallConfig, unattended: bool, width: int = 100) -> Plan:
    plan = Plan(config, unattended)

    def refuse(path: str, message: str) -> None:
        plan.issues.append(Issue(path, message))

    _check_desktop(plan, config)

    _check_keyboard(plan, config)

    if unattended and config.needs_passphrase_prompt():
        refuse("encryption.passphrase", "needs to be typed at install time, which --yes can't do")

    if config.target is None:
        return plan

    devices = resolve.inventory()
    medium = helpers.install_medium()
    minimum = helpers.min_full_disk_bytes()

    disk = _check_disk(plan, "disk.target", config.target, devices, medium, minimum, ", its own 2 GiB ESP included")
    home_disk = None
    if config.home_location == "disk" and config.home_disk:
        # /home needs no ESP, but anything smaller than the root minimum is no
        # home for a desktop's worth of files either.
        home_disk = _check_disk(plan, "disk.home.disk", config.home_disk, devices, medium, 8 * 2**30)
        if home_disk and home_disk == disk:
            refuse("disk.home.disk", f"names the same disk as disk.target ({disk})")
            home_disk = None
    if disk is None:
        return plan

    target = DiskPlan(disk, fingerprint(disk, devices), helpers.has_signatures(disk))
    plan.target = target

    if config.mode == "free-space":
        # Nothing is erased, so on_existing_data has nothing to guard; the
        # fingerprint still pins the install to the layout that was planned.
        for part in helpers.bitlocker_partitions(disk):
            refuse("disk.target", f"{part} is BitLocker-encrypted. Turn BitLocker off in Windows and let the drive "
                                  "finish decrypting (suspending it is not enough), then try again")
        region = helpers.free_space_region(disk)
        if isinstance(region, int):
            refuse("disk.mode", f"{disk} has {region // 2**30} GiB of usable free space in one piece; "
                                "Omarchy needs 32 GiB, its own 2 GiB ESP included")
        else:
            target.region = region
            target.summary = helpers.free_space_summary(
                disk, config.encryption_enabled, width, medium, region, config.swap_strategy
            )
        if config.expect_fingerprint and config.expect_fingerprint != target.fingerprint:
            refuse("disk.expect_fingerprint", f"{disk} no longer looks like it did: its fingerprint is now {target.fingerprint}")
        return plan

    target.summary = helpers.wipe_summary(
        disk, config.encryption_enabled, width, medium, config.swap_strategy, other_erased=home_disk or ""
    )

    if home_disk:
        plan.home = DiskPlan(home_disk, fingerprint(home_disk, devices), helpers.has_signatures(home_disk))
        plan.home.summary = helpers.wipe_summary(
            home_disk, config.encryption_enabled, width, medium, config.swap_strategy,
            other_erased=disk, mode="home_disk",
        )

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
    if unattended and plan.home and plan.home.has_signatures and config.on_existing_data == "abort":
        refuse(
            "disk.on_existing_data",
            f'{home_disk} (the /home disk) has data on it and on_existing_data = "abort". '
            'To erase it anyway, set on_existing_data = "wipe"',
        )
    return plan
