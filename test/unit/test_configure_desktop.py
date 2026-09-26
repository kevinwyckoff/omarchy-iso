"""configure_desktop: install.toml's [desktop], applied after the user exists.

The theme is applied headless as the user, as install/user/theme.sh does on a
first install. The agent is only recorded: installing it needs a network and
the user's own tools, so Omarchy's first-login setup-agent hook installs it.
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


class ConfigureDesktopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = mock.Mock()
        self.ctx.target = Path(self.tmp.name)
        self.ctx.username = "kevin"
        self.ctx.defer_provisioning = False

    def run_phase(self, desktop):
        self.ctx.omarchy_install = {"desktop": desktop} if desktop is not None else {}
        with mock.patch.object(phases_impl, "_run_target_setup_command") as run:
            phases_impl.configure_desktop(self.ctx)
        return run

    def test_nothing_without_desktop_choices(self):
        self.run_phase(None).assert_not_called()

    def test_the_theme_is_applied_headless_as_the_user(self):
        run = self.run_phase({"theme": "Tokyo Night"})
        cmd = run.call_args.args[1]
        self.assertEqual(cmd[-2:], ["/usr/bin/omarchy-theme-set", "Tokyo Night"])
        self.assertIn("OMARCHY_THEME_HEADLESS=1", cmd)
        self.assertEqual(run.call_args.kwargs["user"], "kevin")

    def test_the_agent_is_recorded_for_first_login(self):
        run = self.run_phase({"agent": "claude"})
        cmd = run.call_args.args[1]
        self.assertIn("first-run-agent", cmd[2])
        self.assertEqual(cmd[-1], "claude")
        self.assertEqual(run.call_args.kwargs["user"], "kevin")

    def test_deferred_provisioning_skips_it(self):
        self.ctx.defer_provisioning = True
        self.run_phase({"theme": "nord"}).assert_not_called()


if __name__ == "__main__":
    unittest.main()
