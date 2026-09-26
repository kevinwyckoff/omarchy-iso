"""Compile an install.toml plan into the files the orchestrator reads today.

The orchestrator is unchanged: it reads user_configuration.json (archinstall's
format plus an omarchy_install block), user_credentials.json, and the loose
files next to them, exactly as the wizard and legacy cidata drives provide
them. Reading the plan directly can come later, once this shim has proved it
produces the same inputs.

The full-disk layout matches the configurator's byte for byte in the fields
that matter: a 2GiB ESP at 1MiB, the root partition filling the disk less
1MiB for the backup GPT, the same subvolumes, the same object IDs.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .schema import InstallConfig

MIB = 1024 * 1024
GIB = 1024 * MIB
ESP_OBJ_ID = "ea21d3f2-82bb-49cc-ab5d-6f81ae94e18d"
ROOT_OBJ_ID = "8c2c2b92-1070-455d-b76a-56263bab24aa"


@dataclass
class Secrets:
    """Secret values resolved for this install, never written to install.toml."""

    user_password: str | None = None
    user_password_hash: str | None = None
    luks_passphrase: str | None = None
    tailscale_authkey: str | None = None


def hash_password(password: str) -> str:
    result = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"], input=password, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def detect_kernel(pci_devices: Path = Path("/sys/bus/pci/devices")) -> str:
    # T2 Macs need their own kernel for keyboard and Wi-Fi, as in the configurator.
    for device in pci_devices.glob("*"):
        try:
            vendor = (device / "vendor").read_text().strip().lower()
            device_id = (device / "device").read_text().strip().lower()
        except OSError:
            continue
        if vendor == "0x106b" and device_id in {"0x1801", "0x1802"}:
            return "linux-t2"
    return "linux-omarchy"


def full_disk_configuration(
    config: InstallConfig,
    disk: str,
    disk_size: int,
    secrets: Secrets,
    kernel: str,
    runtime_package: str,
    settings_package: str,
    home_disk: str | None = None,
) -> dict:
    disk_size_in_mib = disk_size // MIB * MIB
    boot_start = MIB
    boot_size = 2 * GIB
    main_start = boot_start + boot_size
    main_size = disk_size_in_mib - main_start - MIB

    def size(value: int) -> dict:
        return {"sector_size": {"unit": "B", "value": 512}, "unit": "B", "value": value}

    disk_config: dict = {
        "config_type": "default_layout",
        "device_modifications": [
            {
                "device": disk,
                "partitions": [
                    {
                        "btrfs": [],
                        "dev_path": None,
                        "flags": ["boot", "esp"],
                        "fs_type": "fat32",
                        "mount_options": [],
                        "mountpoint": "/boot",
                        "obj_id": ESP_OBJ_ID,
                        "size": size(boot_size),
                        "start": size(boot_start),
                        "status": "create",
                        "type": "primary",
                    },
                    {
                        # With /home on its own disk, @home lives there instead:
                        # the orchestrator creates and mounts it before any user exists.
                        "btrfs": [
                            {"mountpoint": "/", "name": "@"},
                            *([] if home_disk else [{"mountpoint": "/home", "name": "@home"}]),
                            {"mountpoint": "/var/log", "name": "@log"},
                            {"mountpoint": "/var/cache/pacman/pkg", "name": "@pkg"},
                        ],
                        "dev_path": None,
                        "flags": [],
                        "fs_type": "btrfs",
                        "mount_options": ["compress=zstd"],
                        "mountpoint": None,
                        "obj_id": ROOT_OBJ_ID,
                        "size": size(main_size),
                        "start": size(main_start),
                        "status": "create",
                        "type": "primary",
                    },
                ],
                "wipe": True,
            }
        ],
    }

    if config.encryption_enabled:
        encryption: dict = {
            "encryption_type": "luks",
            "lvm_volumes": [],
            "iter_time": 2000,
            "partitions": [ROOT_OBJ_ID],
        }
        # Deferred-provisioning installs carry no passphrase: the orchestrator
        # generates a throwaway one and stages it for the first-boot re-key.
        if not config.defer_provisioning:
            encryption["encryption_password"] = secrets.luks_passphrase
        disk_config["disk_encryption"] = encryption

    return {
        "app_config": None,
        "archinstall-language": "English",
        "auth_config": {},
        "audio_config": {"audio": "pipewire"},
        "bootloader_config": {"bootloader": "Limine", "uki": False, "removable": False},
        "custom_commands": [],
        "omarchy_install": {
            "mode": "full_disk",
            "defer_provisioning": config.defer_provisioning,
            "target_mount": "/mnt",
            "boot": {
                "esp_mount": "/boot",
                "esp_path": "/EFI/limine",
                "efi_binary": "limine_x64.efi",
                "enable_fallback": True,
            },
            "storage": {"kernel": kernel},
            "swap": {"strategy": config.swap_strategy},
            **({"home": {"device": home_disk, "encrypt": config.encryption_enabled}} if home_disk else {}),
        },
        "disk_config": disk_config,
        "hostname": config.hostname,
        "kernels": [kernel],
        "network_config": {"type": "iso"},
        "ntp": True,
        "parallel_downloads": 8,
        "script": None,
        "services": [],
        "swap": True,
        "timezone": config.timezone,
        "locale_config": {"kb_layout": config.keyboard, "sys_enc": "UTF-8", "sys_lang": "en_US.UTF-8"},
        "mirror_config": {
            "custom_repositories": [],
            "custom_servers": [
                {"url": "https://mirror.omarchy.org/$repo/os/$arch"},
                {"url": "https://mirror.rackspace.com/archlinux/$repo/os/$arch"},
                {"url": "https://geo.mirror.pkgbuild.com/$repo/os/$arch"},
            ],
            "mirror_regions": {},
            "optional_repositories": [],
        },
        "packages": ["base-devel", "git", "omarchy-keyring", settings_package, runtime_package],
        "profile_config": {"gfx_driver": None, "greeter": None, "profile": {}},
        "version": "3.0.9",
    }


def credentials(config: InstallConfig, secrets: Secrets) -> dict:
    if config.defer_provisioning:
        return {"users": []}
    creds: dict = {}
    if config.encryption_enabled:
        creds["encryption_password"] = secrets.luks_passphrase
    creds["root_enc_password"] = secrets.user_password_hash
    creds["users"] = [
        {"enc_password": secrets.user_password_hash, "groups": [], "sudo": True, "username": config.user.name}
    ]
    return creds


def write_inputs(
    out: Path,
    config: InstallConfig,
    disk: str,
    disk_size: int,
    secrets: Secrets,
    kernel: str,
    runtime_package: str,
    settings_package: str,
    home_disk: str | None = None,
) -> None:
    """Write every orchestrator input into `out` (/root on the ISO), replacing
    whatever a previous attempt left there."""
    out.mkdir(parents=True, exist_ok=True)
    for stale in ("user_configuration.json", "user_credentials.json", "user_full_name.txt",
                  "user_email_address.txt", "user_encrypt_installation.txt", "authorized_keys",
                  "tailscale_authkey", "defer-provisioning", "install.stripped.toml"):
        (out / stale).unlink(missing_ok=True)

    def write(name: str, content: str, mode: int = 0o600) -> None:
        path = out / name
        path.touch(mode=mode)
        path.chmod(mode)
        path.write_text(content)

    configuration = full_disk_configuration(
        config, disk, disk_size, secrets, kernel, runtime_package, settings_package, home_disk
    )
    write("user_configuration.json", json.dumps(configuration, indent=4) + "\n")
    write("user_credentials.json", json.dumps(credentials(config, secrets), indent=4) + "\n")

    user = config.user
    write("user_full_name.txt", (user.full_name if user else "") + "\n", 0o644)
    write("user_email_address.txt", (user.email if user else "") + "\n", 0o644)
    if user and user.ssh_authorized_keys:
        write("authorized_keys", "\n".join(key.strip() for key in user.ssh_authorized_keys) + "\n", 0o644)
    if secrets.tailscale_authkey:
        write("tailscale_authkey", secrets.tailscale_authkey + "\n")
    write("install.stripped.toml", stripped_toml(config), 0o644)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def stripped_toml(config: InstallConfig) -> str:
    """The install as described, minus every secret, for /etc/chefs-kitchen/install.toml.

    Written from the parsed config rather than copied, so comments or odd
    formatting in the original can't carry a secret through."""
    lines = [
        "# How this machine was installed, written by chefs-kitchen.",
        "# Passwords, password hashes, passphrases and auth keys were removed.",
        "schema = 1",
        "",
        "[system]",
        f"hostname = {_toml_string(config.hostname)}",
        f"timezone = {_toml_string(config.timezone)}",
        f"keyboard = {_toml_string(config.keyboard)}",
    ]
    for user in config.users:
        lines += ["", "[[users]]", f"name = {_toml_string(user.name)}"]
        if user.full_name:
            lines.append(f"full_name = {_toml_string(user.full_name)}")
        if user.email:
            lines.append(f"email = {_toml_string(user.email)}")
        lines.append("# password removed")
        if user.ssh_authorized_keys:
            keys = ", ".join(_toml_string(key.strip()) for key in user.ssh_authorized_keys)
            lines.append(f"ssh_authorized_keys = [{keys}]")

    lines += ["", "[disk]", f"target = {{ {config.target.kind} = {_toml_string(config.target.value)} }}",
              f"mode = {_toml_string(config.mode)}", f"on_existing_data = {_toml_string(config.on_existing_data)}"]
    if config.expect_fingerprint:
        lines.append(f"expect_fingerprint = {_toml_string(config.expect_fingerprint)}")
    lines += ["", "[disk.home]", f"location = {_toml_string(config.home_location)}"]
    if config.home_disk:
        lines.append(f"disk = {{ {config.home_disk.kind} = {_toml_string(config.home_disk.value)} }}")

    lines += ["", "[swap]", f"strategy = {_toml_string(config.swap_strategy)}"]
    lines += ["", "[encryption]", f"enabled = {'true' if config.encryption_enabled else 'false'}"]
    if config.passphrase and config.passphrase.kind == "same_as_user":
        lines.append(f"passphrase = {{ same_as_user = {_toml_string(config.passphrase.value)} }}")
    elif config.passphrase:
        lines.append("# passphrase removed")

    if config.theme or config.agent:
        lines += ["", "[desktop]"]
        if config.theme:
            lines.append(f"theme = {_toml_string(config.theme)}")
        if config.agent:
            lines.append(f"agent = {_toml_string(config.agent)}")
    if config.extra_packages:
        lines += ["", "[packages]", f"extra = [{', '.join(_toml_string(p) for p in config.extra_packages)}]"]
    if config.tailscale_authkey:
        lines += ["", "[network]", "# tailscale_authkey removed"]
    lines += ["", "[provisioning]", f"defer = {'true' if config.defer_provisioning else 'false'}", ""]
    return "\n".join(lines)
