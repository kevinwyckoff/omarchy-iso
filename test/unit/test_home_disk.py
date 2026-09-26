"""/home on its own disk (install.toml [disk.home] location = "disk").

Spec row H: an encrypted install still asks for one passphrase at boot, so the
/home disk's LUKS volume opens from /etc/crypttab with a random keyfile kept
on the encrypted root, never with a passphrase of its own.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs/airootfs/usr/share/omarchy-iso"))

sys.modules.setdefault(
    "orchestrator.archinstall_adapter", types.ModuleType("orchestrator.archinstall_adapter")
)

from orchestrator import phases_impl  # noqa: E402


class HomeDiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ctx = mock.Mock()
        self.ctx.target = root / "mnt"
        self.ctx.state_dir = root / "state"
        self.ctx.state_dir.mkdir()
        self.ctx.state = {}

    def prepare(self, encrypt, device="/dev/nvme1n1"):
        self.ctx.omarchy_install = {"home": {"device": device, "encrypt": encrypt}}
        with mock.patch.object(phases_impl.subprocess, "run") as run, \
             mock.patch.object(phases_impl, "capture_identifier", return_value="1111-2222") as uuid, \
             mock.patch.object(phases_impl.Path, "is_block_device", return_value=True):
            phases_impl._prepare_home_disk(self.ctx)
        return [call.args[0] for call in run.call_args_list], uuid

    def test_nothing_happens_without_a_home_disk(self):
        self.ctx.omarchy_install = {}
        with mock.patch.object(phases_impl.subprocess, "run") as run:
            phases_impl._prepare_home_disk(self.ctx)
        run.assert_not_called()

    def test_encrypted_home_uses_a_keyfile_not_a_passphrase(self):
        commands, _ = self.prepare(encrypt=True)
        partition = "/dev/nvme1n1p1"
        key = str(self.ctx.state_dir / "omarchy_home.key")
        self.assertIn(["wipefs", "-af", "/dev/nvme1n1"], commands)
        self.assertIn(["parted", "-s", "/dev/nvme1n1", "--", "mklabel", "gpt", "mkpart", "omarchy_home", "btrfs", "1MiB", "-1MiB"], commands)
        self.assertIn(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "--key-file", key, partition], commands)
        self.assertIn(["cryptsetup", "open", "--key-file", key, partition, "omarchy_home"], commands)
        self.assertIn(["mkfs.btrfs", "-f", "-L", "OMARCHY_HOME", "/dev/mapper/omarchy_home"], commands)
        self.assertEqual(commands[-1], ["mount", "-o", "noatime,compress=zstd,subvol=@home",
                                        "/dev/mapper/omarchy_home", str(self.ctx.target / "home")])
        self.assertEqual(len((self.ctx.state_dir / "omarchy_home.key").read_bytes()), 64)
        self.assertEqual(self.ctx.state["home_luks_uuid"], "1111-2222")

    def test_unencrypted_home_is_plain_btrfs(self):
        commands, uuid = self.prepare(encrypt=False, device="/dev/sdb")
        self.assertFalse(any(c[0] == "cryptsetup" for c in commands))
        self.assertIn(["mkfs.btrfs", "-f", "-L", "OMARCHY_HOME", "/dev/sdb1"], commands)
        uuid.assert_not_called()

    def test_crypttab_and_key_land_on_the_encrypted_root(self):
        self.prepare(encrypt=True)
        (self.ctx.target / "etc").mkdir(parents=True)
        (self.ctx.target / "etc/crypttab").write_text("# existing\n")
        phases_impl._write_home_crypttab(self.ctx)

        key = self.ctx.target / "etc/cryptsetup-keys.d/omarchy_home.key"
        self.assertEqual(key.stat().st_mode & 0o777, 0o400)
        self.assertEqual(key.read_bytes(), (self.ctx.state_dir / "omarchy_home.key").read_bytes())
        crypttab = (self.ctx.target / "etc/crypttab").read_text()
        self.assertEqual(crypttab, "# existing\nomarchy_home  UUID=1111-2222  none  luks,discard\n")

        phases_impl._write_home_crypttab(self.ctx)
        self.assertEqual((self.ctx.target / "etc/crypttab").read_text(), crypttab, "written once")

    def test_validate_boot_checks_fstab_crypttab_and_key(self):
        self.prepare(encrypt=True)
        etc = self.ctx.target / "etc"
        etc.mkdir(parents=True)
        (etc / "fstab").write_text("UUID=abcd  /home  btrfs  rw,noatime,compress=zstd,subvol=/@home 0 0\n")
        with self.assertRaisesRegex(RuntimeError, "crypttab"):
            phases_impl._validate_home_disk(self.ctx)
        phases_impl._write_home_crypttab(self.ctx)
        phases_impl._validate_home_disk(self.ctx)

        (etc / "fstab").write_text("UUID=abcd  /  btrfs  subvol=/@ 0 0\n")
        with self.assertRaisesRegex(RuntimeError, "fstab has no btrfs /home"):
            phases_impl._validate_home_disk(self.ctx)


if __name__ == "__main__":
    unittest.main()
