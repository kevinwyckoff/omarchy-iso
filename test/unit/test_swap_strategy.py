"""configure_hibernation honours install.toml's swap.strategy.

Spec row I: swap.strategy = "zram" means no swapfile and no resume=, which
is what skipping omarchy-hibernation-setup leaves. "none" also turns the zram
device off with an /etc drop-in that outranks omarchy-settings' own config.
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


class ConfigureHibernationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name)
        setup = self.target / "usr/bin/omarchy-hibernation-setup"
        setup.parent.mkdir(parents=True)
        setup.write_text("#!/bin/bash\n")

    def configure(self, swap=None):
        ctx = mock.Mock()
        ctx.target = self.target
        ctx.omarchy_install = {} if swap is None else {"swap": {"strategy": swap}}
        with mock.patch.object(phases_impl.subprocess, "run") as run:
            phases_impl.configure_hibernation(ctx)
        return run

    def dropin(self):
        return self.target / "etc/systemd/zram-generator.conf.d/99-chefs-kitchen-swap.conf"

    def test_the_default_sets_up_hibernation(self):
        run = self.configure()
        self.assertIn("/usr/bin/omarchy-hibernation-setup", run.call_args.args[0])
        self.assertFalse(self.dropin().exists())

    def test_zram_skips_the_swapfile(self):
        self.configure("zram").assert_not_called()
        self.assertFalse(self.dropin().exists())

    def test_none_also_turns_zram_off(self):
        self.configure("none").assert_not_called()
        self.assertIn("zram-size = 0", self.dropin().read_text())

    def test_the_log_says_what_each_strategy_leaves(self):
        for swap, line in (("zram", "› swap.strategy = zram: zram swap, no hibernation swapfile"),
                           ("none", "› swap.strategy = none: zram turned off, no hibernation swapfile")):
            with self.subTest(swap=swap), mock.patch.object(phases_impl, "info") as info:
                self.configure(swap)
                info.assert_called_once_with(line)


if __name__ == "__main__":
    unittest.main()
