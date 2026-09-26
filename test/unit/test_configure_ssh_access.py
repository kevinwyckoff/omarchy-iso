"""Unit tests for the orchestrator's configure_ssh_access phase.

The phase touches the target only through the filesystem and arch-chroot, so
the tests run it against a temp directory with subprocess.run recorded and the
ufw side effect (writing user.rules) simulated by the fake, which also fails
the way the real one does in the chroot when ufw.conf says ENABLED=yes.
"""

import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs/airootfs/usr/share/omarchy-iso"))

# phases_impl imports the archinstall adapter at module scope, which pulls in
# the archinstall library that only exists on the live ISO. configure_ssh_access
# never touches it, so stub the adapter out before the import.
sys.modules["orchestrator.archinstall_adapter"] = types.ModuleType("orchestrator.archinstall_adapter")

from orchestrator import phases_impl  # noqa: E402

UFW_RULE = "-A ufw-user-input -p tcp --dport 22 -j ACCEPT\n"
# What firewall.sh leaves behind: ufw set to start on first boot.
UFW_CONF = "# /etc/ufw/ufw.conf\nENABLED=yes\nLOGLEVEL=low\n"


class ConfigureSshAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name)
        self.calls = []

        info_patch = mock.patch.object(phases_impl, "info")
        self.info = info_patch.start()
        self.addCleanup(info_patch.stop)

        run_patch = mock.patch.object(phases_impl.subprocess, "run", side_effect=self.fake_run)
        run_patch.start()
        self.addCleanup(run_patch.stop)

        self.ufw_conf = self.target / "etc" / "ufw" / "ufw.conf"
        self.ufw_conf.parent.mkdir(parents=True)
        self.ufw_conf.write_text(UFW_CONF)

        self.ufw_writes_rule = True
        self.ufw_result = (0, "Rules updated\nRules updated (v6)\n", "")
        self.ufw_raises = None
        self.ufw_runs = []

    def fake_run(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[2] == "ufw":
            self.ufw_runs.append((kwargs, self.ufw_conf.read_text()))
            if self.ufw_raises:
                raise self.ufw_raises
            if self.ufw_writes_rule:
                (self.target / "etc" / "ufw" / "user.rules").write_text(UFW_RULE)
            # An enabled ufw pushes the rule into the running firewall, which
            # from the chroot is the live installer's and has no ufw chains.
            if "ENABLED=yes" in self.ufw_conf.read_text():
                return CompletedProcess(cmd, 1, stdout="", stderr="ERROR: problem running\n")
            returncode, stdout, stderr = self.ufw_result
            return CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
        return CompletedProcess(cmd, 0)

    def ctx(self, authorized_keys=None):
        authorized_keys_path = None
        if authorized_keys is not None:
            authorized_keys_path = self.target / "authorized_keys"
            authorized_keys_path.write_text(authorized_keys)
        return types.SimpleNamespace(target=self.target, username="jeff", authorized_keys_path=authorized_keys_path, defer_provisioning=False)

    def configure(self, **kwargs):
        phases_impl.configure_ssh_access(self.ctx(**kwargs))

    def authorized_keys(self):
        return self.target / "home" / "jeff" / ".ssh" / "authorized_keys"

    def chrooted(self, program):
        return [cmd for cmd in self.calls if cmd[:2] == ["arch-chroot", str(self.target)] and cmd[2] == program]

    def test_no_authorized_keys_is_a_no_op(self):
        self.configure()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.target / "home" / "jeff" / ".ssh").exists())

    def test_installs_keys_one_per_line(self):
        self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\nssh-rsa BBBB jeff@work\n")
        self.assertEqual(self.authorized_keys().read_text(), "ssh-ed25519 AAAA jeff@host\nssh-rsa BBBB jeff@work\n")

    def test_drops_blank_lines_and_comments(self):
        self.configure(authorized_keys="# work laptop\n\n  ssh-ed25519 AAAA jeff@host  \n")
        self.assertEqual(self.authorized_keys().read_text(), "ssh-ed25519 AAAA jeff@host\n")

    def test_keys_with_options_pass_through(self):
        key = 'command="/usr/bin/true",no-pty ssh-ed25519 AAAA jeff@host'
        self.configure(authorized_keys=f"{key}\n")
        self.assertEqual(self.authorized_keys().read_text(), f"{key}\n")

    def test_ssh_dir_is_private(self):
        self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")
        mode = stat.S_IMODE((self.target / "home" / "jeff" / ".ssh").stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_authorized_keys_is_private(self):
        self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")
        self.assertEqual(stat.S_IMODE(self.authorized_keys().stat().st_mode), 0o600)

    def test_chowns_ssh_dir_to_the_user(self):
        self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")
        self.assertEqual(self.chrooted("chown"), [
            ["arch-chroot", str(self.target), "chown", "-R", "jeff:jeff", "/home/jeff/.ssh"],
        ])

    def test_enables_sshd(self):
        self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")
        self.assertEqual(self.chrooted("systemctl"), [
            ["arch-chroot", str(self.target), "systemctl", "enable", "sshd.service"],
        ])

    def test_allows_ssh_through_ufw(self):
        self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")
        self.assertEqual(self.chrooted("ufw"), [
            ["arch-chroot", str(self.target), "ufw", "allow", "ssh"],
        ])

    def test_ufw_runs_disabled_and_is_enabled_again_after(self):
        self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")
        [(_, conf_seen)] = self.ufw_runs
        self.assertIn("ENABLED=no", conf_seen)
        self.assertNotIn("ENABLED=yes", conf_seen)
        self.assertEqual(self.ufw_conf.read_text(), UFW_CONF)

    def test_ufw_is_enabled_again_when_the_ufw_call_is_interrupted(self):
        # Ctrl+C on the install console, landing before ufw returns anything.
        self.ufw_raises = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")
        [(_, conf_seen)] = self.ufw_runs
        self.assertIn("ENABLED=no", conf_seen)
        self.assertEqual(self.ufw_conf.read_text(), UFW_CONF)

    def test_ufw_output_stays_off_the_console_on_success(self):
        self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")
        [(kwargs, _)] = self.ufw_runs
        # Not inherited: ufw's stdout and stderr would otherwise land on the
        # installer's console and in its log.
        self.assertTrue(kwargs.get("capture_output"))
        logged = "".join(str(call) for call in self.info.call_args_list)
        self.assertNotIn("Rules updated", logged)

    def test_fails_when_ufw_is_not_on_the_target(self):
        # ufw.conf ships in the ufw package, so a target without it has no ufw.
        self.ufw_conf.unlink()
        with self.assertRaisesRegex(RuntimeError, "ufw is not installed on the target"):
            self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")
        self.assertEqual(self.chrooted("ufw"), [])
        self.assertFalse(self.ufw_conf.exists())

    def test_fails_with_ufw_output_when_ufw_does_not_record_the_rule(self):
        # ufw checks user.rules is writable before it writes anything.
        self.ufw_writes_rule = False
        self.ufw_result = (1, "", "ERROR: '/etc/ufw/user.rules' is not writable\n")
        with self.assertRaisesRegex(RuntimeError, "allow rule for port 22 .*user.rules' is not writable"):
            self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")
        self.assertEqual(self.ufw_conf.read_text(), UFW_CONF)

    def test_fails_with_ufw_output_when_ufw_fails_after_recording_the_rule(self):
        # user.rules alone is half the rule: user6.rules is written after it.
        self.ufw_result = (1, "Rules updated\n", "ERROR: '/etc/ufw/user6.rules' is not writable\n")
        with self.assertRaisesRegex(RuntimeError, "(?s)allow rule for port 22 .*user6.rules' is not writable"):
            self.configure(authorized_keys="ssh-ed25519 AAAA jeff@host\n")

    def test_empty_file_fails_the_phase(self):
        with self.assertRaisesRegex(RuntimeError, "contains no SSH keys"):
            self.configure(authorized_keys="")

    def test_blank_only_file_fails_the_phase(self):
        with self.assertRaisesRegex(RuntimeError, "contains no SSH keys"):
            self.configure(authorized_keys="\n   \n")

    def test_comment_only_file_fails_the_phase(self):
        with self.assertRaisesRegex(RuntimeError, "contains no SSH keys"):
            self.configure(authorized_keys="# no keys here\n")


class OptionalPathTest(unittest.TestCase):
    def test_unset_and_missing_paths_are_none(self):
        from orchestrator.context import _optional_path

        self.assertIsNone(_optional_path(None))
        self.assertIsNone(_optional_path(""))
        self.assertIsNone(_optional_path("/does/not/exist/authorized_keys"))

    def test_existing_path_is_returned(self):
        from orchestrator.context import _optional_path

        with tempfile.NamedTemporaryFile() as f:
            self.assertEqual(_optional_path(f.name), Path(f.name))


if __name__ == "__main__":
    unittest.main()
