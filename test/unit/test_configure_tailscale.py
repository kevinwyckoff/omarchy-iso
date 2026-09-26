"""Unit tests for the orchestrator's configure_tailscale phase.

Same harness as the SSH access tests: the phase touches the target only
through the filesystem and arch-chroot, so it runs against a temp directory
with subprocess.run recorded and the ufw side effect (writing user.rules)
simulated by the fake, which also fails the way the real one does in the
chroot when ufw.conf says ENABLED=yes.
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
# the archinstall library that only exists on the live ISO. configure_tailscale
# never touches it, so stub the adapter out before the import.
sys.modules["orchestrator.archinstall_adapter"] = types.ModuleType("orchestrator.archinstall_adapter")

from orchestrator import phases_impl  # noqa: E402

UFW_RULE = "-A ufw-user-input -i tailscale0 -j ACCEPT\n"
# What firewall.sh leaves behind: ufw set to start on first boot.
UFW_CONF = "# /etc/ufw/ufw.conf\nENABLED=yes\nLOGLEVEL=low\n"


class ConfigureTailscaleTest(unittest.TestCase):
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
        self.ufw_runs = []

    def fake_run(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[2] == "ufw":
            self.ufw_runs.append((kwargs, self.ufw_conf.read_text()))
            if self.ufw_writes_rule:
                (self.target / "etc" / "ufw" / "user.rules").write_text(UFW_RULE)
            # An enabled ufw pushes the rule into the running firewall, which
            # from the chroot is the live installer's and has no ufw chains.
            if "ENABLED=yes" in self.ufw_conf.read_text():
                return CompletedProcess(cmd, 1, stdout="", stderr="ERROR: problem running\n")
            returncode, stdout, stderr = self.ufw_result
            return CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
        return CompletedProcess(cmd, 0)

    def ctx(self, authkey=None, tailscale_installed=True):
        authkey_path = None
        if authkey is not None:
            authkey_path = self.target / "tailscale_authkey"
            authkey_path.write_text(authkey)
        if tailscale_installed:
            binary = self.target / "usr" / "bin" / "tailscale"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.touch()
        return types.SimpleNamespace(target=self.target, tailscale_authkey_path=authkey_path)

    def configure(self, **kwargs):
        phases_impl.configure_tailscale(self.ctx(**kwargs))

    def staged_key(self):
        return self.target / "etc" / "tailscale" / "authkey"

    def unit(self):
        return self.target / "etc" / "systemd" / "system" / "omarchy-tailscale-join.service"

    def chrooted(self, program):
        return [cmd for cmd in self.calls if cmd[:2] == ["arch-chroot", str(self.target)] and cmd[2] == program]

    def test_no_authkey_is_a_no_op(self):
        self.configure()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.target / "etc" / "tailscale").exists())

    def test_stages_the_key(self):
        self.configure(authkey="tskey-auth-kFAKEKEY\n")
        self.assertEqual(self.staged_key().read_text(), "tskey-auth-kFAKEKEY\n")

    def test_drops_blank_lines_and_comments(self):
        self.configure(authkey="# reusable, tagged\n\n  tskey-auth-kFAKEKEY  \n")
        self.assertEqual(self.staged_key().read_text(), "tskey-auth-kFAKEKEY\n")

    def test_key_dir_and_file_are_root_private(self):
        self.configure(authkey="tskey-auth-kFAKEKEY\n")
        self.assertEqual(stat.S_IMODE((self.target / "etc" / "tailscale").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.staged_key().stat().st_mode), 0o600)

    def test_installs_the_join_unit(self):
        self.configure(authkey="tskey-auth-kFAKEKEY\n")
        text = self.unit().read_text()
        self.assertIn("ConditionPathExists=/etc/tailscale/authkey", text)
        # The key must not outlive a successful join, the unit must not run
        # again after one, and cleanup must be sequenced after the join
        # succeeds -- inside the script, not in ExecStartPost.
        self.assertIn(
            "ExecStart=/usr/bin/sh -c 'until tailscale up --auth-key file:/etc/tailscale/authkey;"
            " do sleep 15; done; rm -f /etc/tailscale/authkey;"
            " systemctl disable omarchy-tailscale-join.service'",
            text,
        )

    def test_join_unit_does_not_hold_up_boot(self):
        # A Type=oneshot wanted by multi-user.target holds the whole boot
        # (SDDM included) hostage until the join finishes or times out --
        # target units implicitly gain After= for their Wants=.
        self.configure(authkey="tskey-auth-kFAKEKEY\n")
        text = self.unit().read_text()
        self.assertIn("Type=simple", text)
        self.assertNotIn("Type=oneshot", text)
        self.assertNotIn("TimeoutStartSec", text)

    def test_unit_avoids_systemd_variable_expansion(self):
        self.configure(authkey="tskey-auth-kFAKEKEY\n")
        self.assertNotIn("$", self.unit().read_text())

    def test_enables_tailscaled_and_the_join(self):
        self.configure(authkey="tskey-auth-kFAKEKEY\n")
        self.assertEqual(self.chrooted("systemctl"), [
            ["arch-chroot", str(self.target), "systemctl", "enable",
             "tailscaled.service", "omarchy-tailscale-join.service"],
        ])

    def test_allows_tailnet_traffic_through_ufw(self):
        self.configure(authkey="tskey-auth-kFAKEKEY\n")
        self.assertEqual(self.chrooted("ufw"), [
            ["arch-chroot", str(self.target), "ufw", "allow", "in", "on", "tailscale0"],
        ])

    def test_ufw_runs_disabled_and_is_enabled_again_after(self):
        self.configure(authkey="tskey-auth-kFAKEKEY\n")
        [(_, conf_seen)] = self.ufw_runs
        self.assertIn("ENABLED=no", conf_seen)
        self.assertNotIn("ENABLED=yes", conf_seen)
        self.assertEqual(self.ufw_conf.read_text(), UFW_CONF)

    def test_ufw_output_stays_off_the_console_on_success(self):
        self.configure(authkey="tskey-auth-kFAKEKEY\n")
        [(kwargs, _)] = self.ufw_runs
        self.assertTrue(kwargs.get("capture_output"))
        logged = "".join(str(call) for call in self.info.call_args_list)
        self.assertNotIn("Rules updated", logged)

    def test_fails_when_ufw_is_not_on_the_target(self):
        # ufw.conf ships in the ufw package, so a target without it has no ufw.
        self.ufw_conf.unlink()
        with self.assertRaisesRegex(RuntimeError, "ufw is not installed on the target"):
            self.configure(authkey="tskey-auth-kFAKEKEY\n")
        self.assertEqual(self.chrooted("ufw"), [])
        self.assertFalse(self.ufw_conf.exists())

    def test_fails_with_ufw_output_when_ufw_does_not_record_the_rule(self):
        # ufw checks user.rules is writable before it writes anything.
        self.ufw_writes_rule = False
        self.ufw_result = (1, "", "ERROR: '/etc/ufw/user.rules' is not writable\n")
        with self.assertRaisesRegex(RuntimeError, "allow rule for tailscale0 .*user.rules' is not writable"):
            self.configure(authkey="tskey-auth-kFAKEKEY\n")
        self.assertEqual(self.ufw_conf.read_text(), UFW_CONF)

    def test_fails_when_tailscale_is_not_on_the_target(self):
        with self.assertRaisesRegex(RuntimeError, "not installed on the target"):
            self.configure(authkey="tskey-auth-kFAKEKEY\n", tailscale_installed=False)

    def test_empty_file_fails_the_phase(self):
        with self.assertRaisesRegex(RuntimeError, "contains no auth key"):
            self.configure(authkey="")

    def test_comment_only_file_fails_the_phase(self):
        with self.assertRaisesRegex(RuntimeError, "contains no auth key"):
            self.configure(authkey="# no key here\n")

    def test_multiple_keys_fail_the_phase(self):
        with self.assertRaisesRegex(RuntimeError, "expected exactly one"):
            self.configure(authkey="tskey-auth-kONE\ntskey-auth-kTWO\n")


if __name__ == "__main__":
    unittest.main()
