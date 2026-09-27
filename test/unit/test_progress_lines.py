"""Unit tests for the progress lines arch_install_system writes to the log.

The install log is what gets attached to a support request and what the
failure screen tails, so each line has to describe this install: no
"encrypting" on a plain one, no "creating user" on a deferred-provisioning
one. archinstall is replaced by mocks and the phase's own helpers that touch
the target are stubbed; only the info() lines are recorded.
"""

import sys
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs/airootfs/usr/share/omarchy-iso"))

sys.modules.setdefault(
    "orchestrator.archinstall_adapter", types.ModuleType("orchestrator.archinstall_adapter")
)

from orchestrator import phases_impl  # noqa: E402

TARGET_HELPERS = (
    "_mount_offline_package_cache",
    "_unmount_offline_package_cache",
    "_mask_mkinitcpio_pacman_hooks",
    "_unmask_mkinitcpio_pacman_hooks",
    "_install_early_packages",
    "_configure_limine_boot",
)


class ArchInstallSystemProgressTest(unittest.TestCase):
    def install(self, encrypted, users):
        config = types.SimpleNamespace(
            kernels=["linux-omarchy"], locale_config=None, mirror_config=None, swap=None,
            auth_config=types.SimpleNamespace(users=users), app_config=None,
            timezone=None, ntp=False, hostname="omarchy", pacman_config=None,
        )
        ctx = types.SimpleNamespace(
            state={"arch_config_handler": types.SimpleNamespace(config=config), "mirror_handler": None},
            target=Path("/nonexistent"), omarchy_install={}, tailscale_authkey_path=None,
        )
        arch = mock.MagicMock()
        arch.is_pre_mount.return_value = False
        arch.is_encrypted.return_value = encrypted
        lines = []

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(phases_impl, "arch", arch))
            for helper in TARGET_HELPERS:
                stack.enter_context(mock.patch.object(phases_impl, helper))
            stack.enter_context(mock.patch.object(phases_impl, "configure_keyboard", return_value=True))
            stack.enter_context(mock.patch.object(phases_impl, "_runtime_package_list", return_value=["omarchy"]))
            stack.enter_context(mock.patch.object(phases_impl, "info", side_effect=lines.append))
            phases_impl.arch_install_system(ctx)

        self.installer = arch.open_installer.return_value.__enter__.return_value
        return lines

    def test_plain_install_is_not_logged_as_encrypting(self):
        lines = self.install(encrypted=False, users=["user"])
        self.assertIn("› partitioning + formatting", lines)
        self.assertFalse([line for line in lines if "encrypt" in line], lines)

    def test_encrypted_install_is_logged_as_encrypting(self):
        self.assertIn("› partitioning + formatting + encrypting", self.install(encrypted=True, users=["user"]))

    def test_user_creation_is_logged_when_a_user_is_created(self):
        self.assertIn("› creating user (with /etc/skel populated)", self.install(encrypted=False, users=["user"]))
        self.installer.create_users.assert_called_once_with(["user"])

    def test_deferred_provisioning_install_is_not_logged_as_creating_a_user(self):
        lines = self.install(encrypted=True, users=[])
        self.assertFalse([line for line in lines if "creating user" in line], lines)
        self.installer.create_users.assert_not_called()


if __name__ == "__main__":
    unittest.main()
