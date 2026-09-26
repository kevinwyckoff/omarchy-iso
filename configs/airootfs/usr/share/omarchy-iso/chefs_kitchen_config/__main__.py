"""chefs-kitchen: validate, plan and install from install.toml.

  chefs-kitchen validate install.toml             schema and semantic checks, no hardware access
  chefs-kitchen plan --config install.toml [--yes]
                                                  resolve disks and print the wipe summary; touches nothing
  chefs-kitchen install --config install.toml     shows the wipe summary and asks before erasing
  chefs-kitchen install --config install.toml --yes
                                                  unattended, guarded by disk.on_existing_data
Exit status: 0 success, 1 the config or plan refused the install, 2 usage error.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import compile_archinstall, plan as planning
from .schema import ConfigError, InstallConfig, Issue, load

PACKAGE_TARGETS = Path("/usr/share/omarchy-iso/package-targets")
LAUNCHER = "/usr/local/bin/omarchy-iso-run"


# Everything a person needs to read goes to stdout, line by line: at boot the
# ISO tees stdout into the install log, and a refusal's reason must land in
# the log and on screen after the summary it refers to, not before it.
def _print_issues(issues: list[Issue]) -> None:
    for issue in issues:
        print(f"  {issue}")


def _load(path: str) -> tuple[InstallConfig, list[Issue]] | None:
    try:
        return load(Path(path))
    except ConfigError as exc:
        print(f"chefs-kitchen: {exc}")
        return None


def cmd_validate(args: argparse.Namespace) -> int:
    loaded = _load(args.file)
    if loaded is None:
        return 1
    _, issues = loaded
    _print_issues(issues)
    if any(issue.severity == "error" for issue in issues):
        print(f"{args.file}: invalid")
        return 1
    print(f"{args.file}: valid")
    return 0


def _make_plan(args: argparse.Namespace, unattended: bool) -> planning.Plan | None:
    loaded = _load(args.config)
    if loaded is None:
        return None
    config, issues = loaded
    if any(issue.severity == "error" for issue in issues):
        _print_issues(issues)
        print(f"{args.config}: invalid, run chefs-kitchen validate for details")
        return None

    width = min(shutil.get_terminal_size((100, 24)).columns - 1, 100)
    plan = planning.make_plan(config, unattended=unattended, width=width)
    plan.issues[:0] = issues
    if plan.target:
        print(plan.target.summary, end="")
        print()
        print(f'Fingerprint of {plan.target.path}: expect_fingerprint = "{plan.target.fingerprint}"')
    if plan.issues:
        print()
        _print_issues(plan.issues)
    return plan


def cmd_plan(args: argparse.Namespace) -> int:
    plan = _make_plan(args, unattended=args.yes)
    if plan is None or not plan.ok:
        print("\nThis install would not go ahead.")
        return 1
    print("\nThis install can go ahead.")
    return 0


def _gum(*args: str) -> subprocess.CompletedProcess:
    # gum draws on the terminal itself; only its answer comes back on stdout.
    return subprocess.run(["gum", *args], stdout=subprocess.PIPE, text=True)


def _confirm(plan: planning.Plan) -> bool:
    disk = plan.target.path
    name = os.path.basename(disk)
    if not plan.target.has_signatures:
        return _gum("confirm", "--affirmative", "Yes, install", "--negative", "No",
                    f"{disk} is blank. Install Omarchy on it?").returncode == 0
    while True:
        answer = _gum("input", "--placeholder", "", "--prompt", "> ",
                      "--header", f"Type {name} to erase this disk, or press Esc to cancel")
        if answer.returncode != 0:
            return False
        if answer.stdout.strip() == name:
            return True
        print(f"That doesn't match {name}.")


def _prompt_secret(header: str) -> str | None:
    while True:
        first = _gum("input", "--password", "--header", header)
        if first.returncode != 0:
            return None
        second = _gum("input", "--password", "--header", "Type it again to confirm")
        if second.returncode != 0:
            return None
        if first.stdout.rstrip("\n") == second.stdout.rstrip("\n") and first.stdout.strip():
            return first.stdout.rstrip("\n")
        print("Those didn't match, or were empty. Try again.")


def _resolve_secrets(config: InstallConfig) -> compile_archinstall.Secrets | None:
    secrets = compile_archinstall.Secrets()
    user = config.user
    if user:
        if user.password:
            secrets.user_password = user.password.read()
            secrets.user_password_hash = compile_archinstall.hash_password(secrets.user_password)
        else:
            secrets.user_password_hash = user.password_hash

    passphrase = config.passphrase
    if config.encryption_enabled and not config.defer_provisioning and passphrase:
        if passphrase.kind == "same_as_user" and secrets.user_password:
            secrets.luks_passphrase = secrets.user_password
        elif passphrase.kind in ("same_as_user", "prompt"):
            what = f"{user.name}'s password" if passphrase.kind == "same_as_user" else "a disk encryption passphrase"
            secrets.luks_passphrase = _prompt_secret(f"Type {what}. You'll type it at every boot to unlock the disk.")
            if secrets.luks_passphrase is None:
                return None
        else:
            secrets.luks_passphrase = passphrase.read()

    if config.tailscale_authkey:
        secrets.tailscale_authkey = config.tailscale_authkey.read()
    return secrets


def _package_targets() -> tuple[str, str]:
    targets = {}
    if PACKAGE_TARGETS.exists():
        for line in PACKAGE_TARGETS.read_text().splitlines():
            key, _, value = line.partition("=")
            targets[key.strip()] = value.strip()
    runtime = os.environ.get("OMARCHY_RUNTIME_PACKAGE") or targets.get("OMARCHY_RUNTIME_PACKAGE", "omarchy")
    settings = os.environ.get("OMARCHY_SETTINGS_PACKAGE") or targets.get("OMARCHY_SETTINGS_PACKAGE", "omarchy-settings")
    return runtime, settings


def cmd_install(args: argparse.Namespace) -> int:
    plan = _make_plan(args, unattended=args.yes)
    if plan is None or not plan.ok:
        print("\nNot installing.")
        return 1

    if not args.yes and not _confirm(plan):
        print("Not installing.")
        return 1

    secrets = _resolve_secrets(plan.config)
    if secrets is None:
        print("Not installing.")
        return 1

    size = int(planning.resolve.device(planning.resolve.inventory(), plan.target.path).get("size") or 0)
    runtime, settings = _package_targets()
    out = Path(args.out)
    compile_archinstall.write_inputs(
        out, plan.config, plan.target.path, size, secrets,
        compile_archinstall.detect_kernel(), runtime, settings,
    )
    print(f"Wrote the installer's inputs to {out}.")

    if args.no_launch:
        return 0
    if args.yes:
        os.environ["OMARCHY_UI_INTERACTIVE"] = "no"
    os.execv(LAUNCHER, [LAUNCHER])


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # a real stream, not a test's StringIO
        sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(prog="chefs-kitchen", description="Describe an Omarchy install in install.toml.")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate", help="check install.toml without touching any hardware")
    validate.add_argument("file")
    validate.set_defaults(func=cmd_validate)

    plan = commands.add_parser("plan", help="resolve install.toml on this machine and show what it would erase")
    plan.add_argument("--config", required=True)
    plan.add_argument("--yes", action="store_true", help="apply the unattended rules (on_existing_data, prompts)")
    plan.set_defaults(func=cmd_plan)

    install = commands.add_parser("install", help="install Omarchy as install.toml describes")
    install.add_argument("--config", required=True)
    install.add_argument("--yes", action="store_true", help="don't ask; guarded by disk.on_existing_data")
    install.add_argument("--out", default="/root", help=argparse.SUPPRESS)
    install.add_argument("--no-launch", action="store_true",
                         help="write the installer's inputs to /root but leave starting the install to the caller")
    install.set_defaults(func=cmd_install)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
