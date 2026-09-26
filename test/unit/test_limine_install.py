"""Limine setup leaves loader upgrades to limine-install where it can.

limine-mkinitcpio-hook's 80-limine-efi-deploy.hook redeploys, enrolls and
signs EFI/limine/limine_x64.efi after every limine upgrade, so the installer
writes its own 99-omarchy-limine.hook only for loaders limine-install does not
keep, such as the removable EFI/BOOT slot and BIOS installs.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/share/omarchy-iso"))
sys.modules.setdefault(
    "orchestrator.archinstall_adapter",
    types.ModuleType("orchestrator.archinstall_adapter"),
)
from orchestrator import phases_impl  # noqa: E402

HOOK = "etc/pacman.d/hooks/99-omarchy-limine.hook"


class LimineInstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name)
        limine = self.target / "usr/share/limine"
        limine.mkdir(parents=True)
        (limine / "BOOTX64.EFI").write_text("limine efi")
        (limine / "limine-bios.sys").write_text("limine bios")

        self.run_calls = []
        for patch in [
            mock.patch.object(phases_impl.subprocess, "run",
                              side_effect=lambda cmd, **kwargs: self.run_calls.append(cmd)),
            mock.patch.object(phases_impl, "_register_limine_efi_entry"),
            mock.patch.object(phases_impl.arch, "parent_device_path",
                              return_value=Path("/dev/vda"), create=True),
            mock.patch.object(phases_impl.arch, "unique_device_path",
                              return_value=None, create=True),
        ]:
            patch.start()
            self.addCleanup(patch.stop)

    def install(self, *, uefi, removable=False):
        partition = types.SimpleNamespace(
            mountpoint=Path("/boot"), safe_dev_path=Path("/dev/vda1"), partn=1,
        )
        installer = types.SimpleNamespace(
            _get_boot_partition=lambda: partition,
            _get_efi_partition=lambda: partition,
            _get_root=lambda: object(),
            _helper_flags={},
        )
        config = types.SimpleNamespace(
            bootloader_config=types.SimpleNamespace(removable=removable),
        )
        ctx = types.SimpleNamespace(target=self.target)
        with mock.patch.object(phases_impl.arch, "has_uefi", return_value=uefi, create=True):
            phases_impl._install_limine_omarchy(ctx, installer, config)

    def install_pre_mounted(self, **boot):
        ctx = types.SimpleNamespace(
            target=self.target,
            omarchy_install={"boot": boot, "storage": {"esp_device": "/dev/nvme0n1p1"}},
            is_protected=True,
        )
        with mock.patch.object(phases_impl, "_read_efibootmgr",
                               return_value={"entries": {}, "order": []}), \
             mock.patch.object(phases_impl, "_split_partition_device",
                               return_value=("/dev/nvme0n1", 1)):
            phases_impl._install_pre_mounted_limine(ctx)

    def test_uefi_install_leaves_the_loader_to_limine_install(self):
        self.install(uefi=True)

        self.assertEqual(
            (self.target / "boot/EFI/limine/limine_x64.efi").read_text(), "limine efi",
        )
        self.assertFalse((self.target / HOOK).exists())
        phases_impl._register_limine_efi_entry.assert_called_once_with(
            Path("/dev/vda"), 1, "\\EFI\\limine\\limine_x64.efi", pre_state=None,
        )

    def test_pre_mounted_install_leaves_the_loader_to_limine_install(self):
        self.install_pre_mounted()

        self.assertTrue((self.target / "boot/EFI/limine/limine_x64.efi").exists())
        self.assertFalse((self.target / HOOK).exists())

    def test_esp_path_without_leading_slash_is_the_same_loader(self):
        self.install_pre_mounted(esp_path="EFI/limine")

        self.assertTrue((self.target / "boot/EFI/limine/limine_x64.efi").exists())
        self.assertFalse((self.target / HOOK).exists())

    def test_loader_outside_efi_limine_keeps_its_loader_current(self):
        self.install_pre_mounted(esp_path="/EFI/omarchy", efi_binary="limine.efi")

        self.assertTrue((self.target / "boot/EFI/omarchy/limine.efi").exists())
        self.assertIn(
            'Exec = /bin/sh -c "/usr/bin/cp /usr/share/limine/BOOTX64.EFI '
            '/boot/EFI/omarchy/limine.efi"\n',
            (self.target / HOOK).read_text(),
        )

    def test_removable_uefi_install_keeps_its_loader_current(self):
        self.install(uefi=True, removable=True)

        self.assertTrue((self.target / "boot/EFI/BOOT/BOOTX64.EFI").exists())
        self.assertIn(
            'Exec = /bin/sh -c "/usr/bin/cp /usr/share/limine/BOOTX64.EFI '
            '/boot/EFI/BOOT/BOOTX64.EFI"\n',
            (self.target / HOOK).read_text(),
        )

    def test_bios_install_keeps_its_loader_current(self):
        self.install(uefi=False)

        self.assertEqual(
            self.run_calls,
            [["arch-chroot", str(self.target), "limine", "bios-install", "/dev/vda"]],
        )
        self.assertIn(
            'Exec = /bin/sh -c "/usr/bin/limine bios-install /dev/vda && '
            '/usr/bin/cp /usr/share/limine/limine-bios.sys /boot/limine/"\n',
            (self.target / HOOK).read_text(),
        )
        phases_impl._register_limine_efi_entry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
