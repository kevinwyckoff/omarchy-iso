"""Unit tests for where the orchestrator's "is this install encrypted?" comes from.

The answer drives SDDM autologin (encrypted means the LUKS prompt is the auth
boundary) and validate_boot's cryptdevice= assertion, so it must follow what
actually gets installed. It used to come only from user_encrypt_installation.txt,
which had to be kept in sync with the JSON by hand: a mismatch gave an
unencrypted install passwordless autologin.
"""

import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs/airootfs/usr/share/omarchy-iso"))

sys.modules.setdefault(
    "orchestrator.archinstall_adapter", types.ModuleType("orchestrator.archinstall_adapter")
)

from orchestrator import phases_impl  # noqa: E402
from orchestrator.context import InstallContext, config_encryption  # noqa: E402

LUKS = {"encryption_type": "luks", "partitions": ["root"], "lvm_volumes": []}


class ConfigEncryptionTest(unittest.TestCase):
    def test_full_disk_follows_the_disk_encryption_block(self):
        encrypted = {"disk_config": {"config_type": "default_layout", "disk_encryption": LUKS}}
        plain = {"disk_config": {"config_type": "default_layout"}}
        disabled = {"disk_config": {"config_type": "default_layout", "disk_encryption": {"encryption_type": "no_encryption"}}}

        self.assertIs(config_encryption(encrypted, {"mode": "full_disk"}), True)
        self.assertIs(config_encryption(plain, {"mode": "full_disk"}), False)
        self.assertIs(config_encryption(disabled, {"mode": "full_disk"}), False)

    def test_protected_follows_the_luks_uuid(self):
        config = {"disk_config": {"config_type": "pre_mounted_config", "mountpoint": "/mnt"}}

        self.assertIs(config_encryption(config, {"mode": "protected", "storage": {"luks_uuid": "abcd"}}), True)
        self.assertIs(config_encryption(config, {"mode": "protected", "storage": {"luks_uuid": None}}), False)

    def test_a_hand_written_pre_mounted_config_does_not_say(self):
        config = {"disk_config": {"config_type": "pre_mounted_config", "mountpoint": "/mnt"}}

        self.assertIsNone(config_encryption(config, {"storage": {}}))
        self.assertIsNone(config_encryption(config, {"mode": "protected"}))


class ContextEncryptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "user_credentials.json").write_text(json.dumps({"users": [{"username": "kevin"}]}))

    def ctx(self, config, flag=None):
        (self.dir / "user_configuration.json").write_text(json.dumps(config))
        env = {
            "OMARCHY_INSTALL_CONFIG": str(self.dir / "user_configuration.json"),
            "OMARCHY_INSTALL_CREDS": str(self.dir / "user_credentials.json"),
            "OMARCHY_INSTALL_STATE_DIR": str(self.dir / "state"),
            "OMARCHY_INSTALL_ENCRYPT_FILE": str(self.dir / "user_encrypt_installation.txt"),
        }
        flag_file = self.dir / "user_encrypt_installation.txt"
        flag_file.unlink(missing_ok=True)
        if flag is not None:
            flag_file.write_text(flag + "\n")

        log = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(log):
            os.environ.pop("OMARCHY_INSTALL_DEFER_PROVISIONING_FILE", None)
            ctx = InstallContext.from_env()
        return ctx, log.getvalue()

    def full_disk(self, encrypted):
        disk_config = {"config_type": "default_layout"}
        if encrypted:
            disk_config["disk_encryption"] = dict(LUKS)
        return {"disk_config": disk_config, "omarchy_install": {"mode": "full_disk", "target_mount": "/mnt"}}

    def test_the_wizard_needs_no_flag_file(self):
        self.assertIs(self.ctx(self.full_disk(True))[0].encrypt, True)
        self.assertIs(self.ctx(self.full_disk(False))[0].encrypt, False)

    def test_a_stale_true_flag_cannot_grant_autologin_to_an_unencrypted_install(self):
        ctx, log = self.ctx(self.full_disk(False), flag="true")
        self.assertIs(ctx.encrypt, False)
        self.assertIn("following the configuration", log)

    def test_a_stale_false_flag_cannot_skip_the_cryptdevice_check(self):
        ctx, log = self.ctx(self.full_disk(True), flag="false")
        self.assertIs(ctx.encrypt, True)
        self.assertIn("following the configuration", log)

    def test_an_agreeing_flag_is_silent(self):
        ctx, log = self.ctx(self.full_disk(True), flag="true")
        self.assertIs(ctx.encrypt, True)
        self.assertNotIn("warning", log)

    def test_the_legacy_flag_still_decides_when_the_configuration_cannot(self):
        config = {"disk_config": {"config_type": "pre_mounted_config", "mountpoint": "/mnt"}}
        self.assertIs(self.ctx(config, flag="true")[0].encrypt, True)
        self.assertIs(self.ctx(config, flag="false")[0].encrypt, False)
        self.assertIs(self.ctx(config)[0].encrypt, False)


class ValidateBootCryptdeviceTest(unittest.TestCase):
    conf = Path("/mnt/boot/limine.conf")

    def test_encrypted_needs_cryptdevice(self):
        phases_impl._assert_cryptdevice_matches(True, self.conf, "cmdline: cryptdevice=UUID=x:omarchy_root root=/dev/mapper/omarchy_root")
        with self.assertRaisesRegex(RuntimeError, "Encrypted install but .* has no cryptdevice="):
            phases_impl._assert_cryptdevice_matches(True, self.conf, "cmdline: root=UUID=x")

    def test_unencrypted_must_not_have_cryptdevice(self):
        phases_impl._assert_cryptdevice_matches(False, self.conf, "cmdline: root=UUID=x")
        with self.assertRaisesRegex(RuntimeError, "Unencrypted install but .* has cryptdevice="):
            phases_impl._assert_cryptdevice_matches(False, self.conf, "cmdline: cryptdevice=UUID=x:omarchy_root")


class ConfigureLoginTest(unittest.TestCase):
    """Spec row D: not encrypted means an SDDM login screen, never autologin."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name)

    def login(self, encrypt):
        ctx = mock.Mock(spec=InstallContext)
        ctx.target = self.target
        ctx.encrypt = encrypt
        ctx.defer_provisioning = False
        ctx.username = "kevin"
        with mock.patch.object(phases_impl.subprocess, "run"):
            phases_impl.configure_login(ctx)
        return self.target / "etc/sddm.conf.d/autologin.conf"

    def test_encrypted_logs_in_after_the_luks_prompt(self):
        self.assertIn("User=kevin", self.login(True).read_text())

    def test_unencrypted_gets_a_login_screen(self):
        self.assertFalse(self.login(False).exists())


if __name__ == "__main__":
    unittest.main()
