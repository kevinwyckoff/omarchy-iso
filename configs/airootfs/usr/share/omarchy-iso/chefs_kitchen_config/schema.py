"""install.toml, schema version 1: parsing and static validation.

Static means no hardware access: `chefs-kitchen validate` runs this anywhere
with Python 3.11+ (tomllib), so an install.toml can be checked in CI before it
goes on a drive. Everything that needs the machine (which disk a selector
names, what is on it) is plan.py's job.

Rules that matter more than they look:
- Unknown keys are errors. A typo must never be silently ignored, least of all
  in a file that decides which disk gets erased.
- Secrets are never plain strings. Passwords, passphrases and auth keys are
  { file = "..." } (relative to install.toml), { prompt = true } where that makes
  sense, or { insecure_plaintext = "..." }, which is accepted with a warning in
  every mode. password_hash is a crypt(3) hash, not a secret in this sense, but
  it is still left out of the copy kept on the installed system.
- Username and hostname follow the wizard's own rules (install/provisioning/
  setup-form.sh in the omarchy repo), so a described install accepts exactly
  what a clicked one does.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Same patterns as setup-form.sh.
USERNAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$")
RESERVED_USERNAMES = frozenset(
    "root bin daemon mail ftp http nobody dbus systemd-coredump systemd-network systemd-oom "
    "systemd-journal-remote systemd-resolve systemd-timesync tss uuidd alpm git avahi cups "
    "cups-browsed lp _talkd polkitd rtkit qemu brltty gluster rpc libvirt-qemu pcscd "
    "nvidia-persistenced sddm".split()
)
HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")

FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
# crypt(3) hashes all start with "$<id>$": $6$ (SHA-512), $y$ (yescrypt), ...
PASSWORD_HASH_PATTERN = re.compile(r"^\$[0-9a-z]+\$\S+$")
PACKAGE_NAME_PATTERN = re.compile(r"^[a-z0-9@_+][a-z0-9@._+-]*$")
SSH_KEY_TYPES = ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-", "sk-ssh-ed25519@", "sk-ecdsa-sha2-", "ssh-dss")

DISK_SELECTOR_KINDS = ("serial", "by_id", "wwn", "path")
DISK_MODES = ("wipe", "free-space")
ON_EXISTING_DATA = ("abort", "wipe")
HOME_LOCATIONS = ("same", "disk")
SWAP_STRATEGIES = ("zram+hibernate", "zram", "none")

DEFAULT_HOSTNAME = "omarchy"
DEFAULT_TIMEZONE = "UTC"
DEFAULT_KEYBOARD = "us"


class ConfigError(Exception):
    """install.toml could not be read or parsed at all."""


@dataclass
class Issue:
    path: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"{self.severity}: {self.path}: {self.message}"


@dataclass(frozen=True)
class Secret:
    """Where a secret comes from. The value itself is only read when needed."""

    kind: str  # "file", "prompt", "insecure_plaintext" or "same_as_user"
    value: str | None = None
    base_dir: Path | None = None

    @property
    def path(self) -> Path | None:
        if self.kind != "file" or self.value is None:
            return None
        path = Path(self.value)
        return path if path.is_absolute() or self.base_dir is None else self.base_dir / path

    def read(self) -> str:
        if self.kind == "file":
            return self.path.read_text().rstrip("\n")
        if self.kind == "insecure_plaintext":
            return self.value or ""
        raise ValueError(f"a {self.kind} secret has no stored value")


@dataclass(frozen=True)
class DiskSelector:
    kind: str
    value: str

    def __str__(self) -> str:
        return f'{{ {self.kind} = "{self.value}" }}'


@dataclass
class User:
    name: str
    full_name: str = ""
    email: str = ""
    password_hash: str | None = None
    password: Secret | None = None
    ssh_authorized_keys: list[str] = field(default_factory=list)


@dataclass
class InstallConfig:
    source: Path | None
    hostname: str = DEFAULT_HOSTNAME
    timezone: str = DEFAULT_TIMEZONE
    keyboard: str = DEFAULT_KEYBOARD
    users: list[User] = field(default_factory=list)
    target: DiskSelector | None = None
    mode: str = "wipe"
    on_existing_data: str = "abort"
    expect_fingerprint: str | None = None
    home_location: str = "same"
    home_disk: DiskSelector | None = None
    swap_strategy: str = "zram+hibernate"
    encryption_enabled: bool = True
    passphrase: Secret | None = None
    theme: str | None = None
    agent: str | None = None
    extra_packages: list[str] = field(default_factory=list)
    tailscale_authkey: Secret | None = None
    defer_provisioning: bool = False

    @property
    def user(self) -> User | None:
        return self.users[0] if self.users else None

    def needs_passphrase_prompt(self) -> bool:
        """True when only an interactive install can supply the LUKS passphrase."""
        if not self.encryption_enabled or self.defer_provisioning or self.passphrase is None:
            return False
        if self.passphrase.kind == "prompt":
            return True
        if self.passphrase.kind == "same_as_user":
            user = self.user
            return user is not None and user.password is None
        return False


def load(path: Path) -> tuple[InstallConfig, list[Issue]]:
    """Read and validate install.toml. Raises ConfigError when it isn't TOML."""
    try:
        data = tomllib.loads(path.read_text())
    except OSError as exc:
        raise ConfigError(f"can't read {path}: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse(data, source=path)


def parse(data: dict[str, Any], source: Path | None = None) -> tuple[InstallConfig, list[Issue]]:
    v = _Validator(source)
    config = v.config(data)
    return config, v.issues


class _Validator:
    def __init__(self, source: Path | None):
        self.source = source
        self.base_dir = source.parent if source else None
        self.issues: list[Issue] = []

    # ---------------------------------------------------------------- helpers

    def error(self, path: str, message: str) -> None:
        self.issues.append(Issue(path, message))

    def warn(self, path: str, message: str) -> None:
        self.issues.append(Issue(path, message, "warning"))

    def table(self, data: dict, key: str, path: str, allowed: set[str]) -> dict:
        value = data.get(key, {})
        if not isinstance(value, dict):
            self.error(path, "must be a table")
            return {}
        self.unknown_keys(value, path, allowed)
        return value

    def unknown_keys(self, table: dict, path: str, allowed: set[str]) -> None:
        for key in table:
            if key not in allowed:
                where = f"{path}.{key}" if path else key
                self.error(where, f"unknown key (expected one of: {', '.join(sorted(allowed))})")

    def string(self, table: dict, key: str, path: str, default: str | None = None, required: bool = False) -> str | None:
        if key not in table:
            if required:
                self.error(path, "is required")
            return default
        value = table[key]
        if not isinstance(value, str):
            self.error(path, "must be a string")
            return default
        return value

    def boolean(self, table: dict, key: str, path: str, default: bool) -> bool:
        if key not in table:
            return default
        value = table[key]
        if not isinstance(value, bool):
            self.error(path, "must be true or false")
            return default
        return value

    def choice(self, table: dict, key: str, path: str, choices: tuple[str, ...], default: str) -> str:
        value = self.string(table, key, path, default)
        if value not in choices:
            self.error(path, f'must be one of {", ".join(repr(c) for c in choices)}')
            return default
        return value

    def string_list(self, table: dict, key: str, path: str) -> list[str]:
        value = table.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            self.error(path, "must be a list of strings")
            return []
        return value

    def selector(self, value: Any, path: str) -> DiskSelector | None:
        if not isinstance(value, dict) or len(value) != 1:
            self.error(path, f"must be one of {{ serial = ... }}, {{ by_id = ... }}, {{ wwn = ... }} or {{ path = ... }}")
            return None
        (kind, raw), = value.items()
        if kind not in DISK_SELECTOR_KINDS:
            self.error(f"{path}.{kind}", f"unknown disk selector (expected one of: {', '.join(DISK_SELECTOR_KINDS)})")
            return None
        if not isinstance(raw, str) or not raw.strip():
            self.error(f"{path}.{kind}", "must be a non-empty string")
            return None
        if kind == "path" and not raw.startswith("/dev/"):
            self.error(f"{path}.path", "must be a /dev path")
            return None
        return DiskSelector(kind, raw.strip())

    def secret(self, value: Any, path: str, allowed: tuple[str, ...]) -> Secret | None:
        forms = " or ".join(
            {"file": '{ file = "..." }', "prompt": "{ prompt = true }",
             "insecure_plaintext": '{ insecure_plaintext = "..." }',
             "same_as_user": '{ same_as_user = "..." }'}[kind] for kind in allowed
        )
        if isinstance(value, str):
            self.error(path, f"a plaintext secret is not allowed here; use {forms}")
            return None
        if not isinstance(value, dict) or len(value) != 1:
            self.error(path, f"must be {forms}")
            return None
        (kind, raw), = value.items()
        if kind not in allowed:
            self.error(f"{path}.{kind}", f"not allowed here; use {forms}")
            return None

        if kind == "prompt":
            if raw is not True:
                self.error(f"{path}.prompt", "must be true")
                return None
            return Secret("prompt")
        if not isinstance(raw, str) or not raw:
            self.error(f"{path}.{kind}", "must be a non-empty string")
            return None
        if kind == "insecure_plaintext":
            self.warn(path, "stored in plain text in install.toml; prefer { file = \"...\" }")
        secret = Secret(kind, raw, self.base_dir)
        if kind == "file":
            file = secret.path
            if not file.is_file():
                self.error(f"{path}.file", f"{file} does not exist")
            elif not file.read_text().strip():
                self.error(f"{path}.file", f"{file} is empty")
        return secret

    # ------------------------------------------------------------ the schema

    def config(self, data: dict) -> InstallConfig:
        config = InstallConfig(source=self.source)
        self.unknown_keys(
            data, "", {"schema", "system", "users", "disk", "swap", "encryption", "desktop", "packages", "network", "provisioning"}
        )

        schema = data.get("schema")
        if schema is None:
            self.error("schema", f"is required (schema = {SCHEMA_VERSION})")
        elif schema != SCHEMA_VERSION or isinstance(schema, bool):
            self.error("schema", f"this installer reads schema {SCHEMA_VERSION}, not {schema!r}")

        self.system(data, config)
        self.provisioning(data, config)
        self.users(data, config)
        self.disk(data, config)
        self.swap(data, config)
        self.encryption(data, config)
        self.desktop(data, config)
        self.packages(data, config)
        self.network(data, config)
        return config

    def system(self, data: dict, config: InstallConfig) -> None:
        system = self.table(data, "system", "system", {"hostname", "timezone", "keyboard"})
        config.hostname = self.string(system, "hostname", "system.hostname", DEFAULT_HOSTNAME)
        if not HOSTNAME_PATTERN.match(config.hostname):
            self.error("system.hostname", "must be letters, digits and dashes, not starting or ending with a dash, at most 63 characters")

        config.timezone = self.string(system, "timezone", "system.timezone", DEFAULT_TIMEZONE)
        if not _known_timezone(config.timezone):
            self.error("system.timezone", f"unknown timezone {config.timezone!r} (use a name like \"America/Toronto\")")

        config.keyboard = self.string(system, "keyboard", "system.keyboard", DEFAULT_KEYBOARD)
        # Only the name's shape: whether the keymap exists is plan's check,
        # because kbd's keymaps differ between distributions (Fedora's has no
        # "colemak", which the wizard offers) and validate runs anywhere.
        if not re.match(r"^[A-Za-z0-9_.-]+$", config.keyboard):
            self.error("system.keyboard", "must be a console keymap name like \"us\" or \"de-latin1\"")

    def provisioning(self, data: dict, config: InstallConfig) -> None:
        provisioning = self.table(data, "provisioning", "provisioning", {"defer"})
        config.defer_provisioning = self.boolean(provisioning, "defer", "provisioning.defer", False)

    def users(self, data: dict, config: InstallConfig) -> None:
        users = data.get("users", [])
        if not isinstance(users, list) or not all(isinstance(u, dict) for u in users):
            self.error("users", "must be an array of tables ([[users]])")
            return

        if config.defer_provisioning:
            if users:
                self.error("users", "must be empty when provisioning.defer = true: the machine's owner creates their account at first boot")
            return
        if len(users) != 1:
            self.error("users", f"exactly one [[users]] entry is supported, found {len(users)}")
            if not users:
                return

        for index, raw in enumerate(users[:1]):
            path = f"users[{index}]"
            self.unknown_keys(raw, path, {"name", "full_name", "email", "password_hash", "password", "ssh_authorized_keys"})
            user = User(name=self.string(raw, "name", f"{path}.name", "", required=True) or "")
            if user.name and (not USERNAME_PATTERN.match(user.name) or user.name in RESERVED_USERNAMES):
                self.error(f"{path}.name", "must be a lowercase Linux username that isn't a system account (like \"kevin\")")
            user.full_name = self.string(raw, "full_name", f"{path}.full_name", "") or ""
            user.email = self.string(raw, "email", f"{path}.email", "") or ""

            if "password_hash" in raw:
                user.password_hash = self.string(raw, "password_hash", f"{path}.password_hash")
                if user.password_hash and not PASSWORD_HASH_PATTERN.match(user.password_hash):
                    self.error(f"{path}.password_hash", "is not a crypt(3) hash; make one with: openssl passwd -6")
            if "password" in raw:
                user.password = self.secret(raw["password"], f"{path}.password", ("file", "insecure_plaintext"))
            if "password_hash" in raw and "password" in raw:
                self.error(path, "set password_hash or password, not both")
            elif "password_hash" not in raw and "password" not in raw:
                self.error(path, 'needs password_hash (openssl passwd -6) or password = { file = "..." }')

            user.ssh_authorized_keys = self.string_list(raw, "ssh_authorized_keys", f"{path}.ssh_authorized_keys")
            for key_index, key in enumerate(user.ssh_authorized_keys):
                if not key.strip().startswith(SSH_KEY_TYPES):
                    self.error(f"{path}.ssh_authorized_keys[{key_index}]", "is not an SSH public key (like \"ssh-ed25519 AAAA... you@host\")")
            config.users.append(user)

    def disk(self, data: dict, config: InstallConfig) -> None:
        disk = self.table(data, "disk", "disk", {"target", "mode", "on_existing_data", "expect_fingerprint", "home"})
        if "target" not in disk:
            self.error("disk.target", "is required: which disk to install to")
        else:
            config.target = self.selector(disk["target"], "disk.target")

        config.mode = self.choice(disk, "mode", "disk.mode", DISK_MODES, "wipe")
        config.on_existing_data = self.choice(disk, "on_existing_data", "disk.on_existing_data", ON_EXISTING_DATA, "abort")

        fingerprint = self.string(disk, "expect_fingerprint", "disk.expect_fingerprint")
        if fingerprint is not None:
            if FINGERPRINT_PATTERN.match(fingerprint):
                config.expect_fingerprint = fingerprint
            else:
                self.error("disk.expect_fingerprint", "must be \"sha256:\" and 64 hex digits, as `chefs-kitchen plan` prints it")

        home = self.table(disk, "home", "disk.home", {"location", "disk"})
        config.home_location = self.choice(home, "location", "disk.home.location", HOME_LOCATIONS, "same")
        if "disk" in home:
            config.home_disk = self.selector(home["disk"], "disk.home.disk")
        if config.home_location == "disk" and "disk" not in home:
            self.error("disk.home.disk", 'is required when location = "disk"')
        if config.home_location == "same" and "disk" in home:
            self.error("disk.home.disk", 'only applies when location = "disk"')
        if config.home_disk and config.target and config.home_disk == config.target:
            self.error("disk.home.disk", "must be a different disk than disk.target")
        if config.mode == "free-space" and config.home_location == "disk":
            self.error("disk.home.location", 'a separate /home disk is only supported with mode = "wipe"')

    def swap(self, data: dict, config: InstallConfig) -> None:
        swap = self.table(data, "swap", "swap", {"strategy"})
        config.swap_strategy = self.choice(swap, "strategy", "swap.strategy", SWAP_STRATEGIES, "zram+hibernate")

    def encryption(self, data: dict, config: InstallConfig) -> None:
        encryption = self.table(data, "encryption", "encryption", {"enabled", "passphrase"})
        config.encryption_enabled = self.boolean(encryption, "enabled", "encryption.enabled", True)

        if "passphrase" in encryption:
            if not config.encryption_enabled:
                self.error("encryption.passphrase", "is set but encryption.enabled = false")
                return
            if config.defer_provisioning:
                self.error("encryption.passphrase", "can't be set with provisioning.defer = true: the installer generates a throwaway one that the owner replaces at first boot")
                return
            config.passphrase = self.secret(
                encryption["passphrase"], "encryption.passphrase", ("same_as_user", "prompt", "file", "insecure_plaintext")
            )
        elif config.encryption_enabled and not config.defer_provisioning and config.user:
            config.passphrase = Secret("same_as_user", config.user.name)

        passphrase = config.passphrase
        if passphrase and passphrase.kind == "same_as_user":
            if not config.user or passphrase.value != config.user.name:
                self.error("encryption.passphrase.same_as_user", f"no user named {passphrase.value!r} in [[users]]")
            elif config.needs_passphrase_prompt():
                self.warn(
                    "encryption.passphrase",
                    "uses the user's password, but only its hash is known: the install will ask for it, "
                    'so it can\'t run with --yes (give users.password = { file = "..." } for unattended installs)',
                )
        elif passphrase and passphrase.kind == "prompt":
            self.warn("encryption.passphrase", "prompts at install time, so this config can't install with --yes")

    def desktop(self, data: dict, config: InstallConfig) -> None:
        desktop = self.table(data, "desktop", "desktop", {"theme", "agent"})
        config.theme = self.string(desktop, "theme", "desktop.theme")
        config.agent = self.string(desktop, "agent", "desktop.agent")

    def packages(self, data: dict, config: InstallConfig) -> None:
        packages = self.table(data, "packages", "packages", {"extra"})
        config.extra_packages = self.string_list(packages, "extra", "packages.extra")
        for index, name in enumerate(config.extra_packages):
            if not PACKAGE_NAME_PATTERN.match(name):
                self.error(f"packages.extra[{index}]", f"{name!r} is not a package name")

    def network(self, data: dict, config: InstallConfig) -> None:
        network = self.table(data, "network", "network", {"tailscale_authkey"})
        if "tailscale_authkey" in network:
            config.tailscale_authkey = self.secret(
                network["tailscale_authkey"], "network.tailscale_authkey", ("file", "insecure_plaintext")
            )


def _known_timezone(name: str) -> bool:
    if name == "UTC":
        return True
    try:
        from zoneinfo import available_timezones
    except ImportError:  # pragma: no cover
        return True
    zones = available_timezones()
    # Without tzdata (a minimal container), don't reject what can't be checked.
    return not zones or name in zones
