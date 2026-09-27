#!/usr/bin/python

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "configs/airootfs/usr/share/omarchy-iso"))

from orchestrator import keyboard as KEYBOARD  # noqa: E402

# The layout list lives in the Omarchy runtime now, shared verbatim with the
# first-boot owner setup; build-iso.sh vendors it onto the ISO. Read it from
# wherever this checkout can see a runtime, and skip the coverage test rather
# than fail when none is around (a bare CI checkout of just this repo).
SETUP_FORM_CANDIDATES = (
    Path("/omarchy-source/install/provisioning/setup-form.sh"),
    ROOT.parent / "omarchy/install/provisioning/setup-form.sh",
    Path("/usr/share/omarchy/install/provisioning/setup-form.sh"),
)
SETUP_FORM = next((path for path in SETUP_FORM_CANDIDATES if path.is_file()), None)


def supported_keymaps():
    if SETUP_FORM is None:
        return None
    block = re.search(r"OMARCHY_KEYBOARD_LAYOUTS=\$'(.*?)'\n", SETUP_FORM.read_text(), re.DOTALL)
    assert block, f"no layout list in {SETUP_FORM}"
    # label|keymap, plus an XKB layout on rows systemd can't map
    return [line.split("|")[1] for line in block.group(1).splitlines()]


SUPPORTED_KEYMAPS = supported_keymaps()

# A runtime's keyboard list as the picker states it, for the tests that must
# not depend on which runtime checkout is around: pl is pinned to its XKB
# layout, colemak to a layout and variant, de-latin1 is left to systemd.
PINNING_FORM = r"""OMARCHY_KEYBOARD_LAYOUTS=$'English (US)|us
English (US, Colemak)|colemak|us:colemak
German|de-latin1
Polish|pl|pl'

omarchy_keyboard_xkb_settings() {
  awk -F'|' -v keymap="$1" '
    $2 == keymap && $3 != "" {
      split($3, xkb, ":")
      print "XKBLAYOUT=" xkb[1]
      print "XKBMODEL=pc105"
      if (xkb[2] != "") print "XKBVARIANT=" xkb[2]
      print "XKBOPTIONS=terminate:ctrl_alt_bksp"
      exit
    }' <<<"$OMARCHY_KEYBOARD_LAYOUTS"
}
"""

# The same list from a runtime that predates the XKB field.
OLDER_FORM = r"""OMARCHY_KEYBOARD_LAYOUTS=$'English (US)|us
English (US, Colemak)|colemak
German|de-latin1
Polish|pl'
"""

REAL_CAPTURE = KEYBOARD.capture


def localectl_knows(*keymaps):
    """Answer `localectl list-keymaps` with keymaps; run everything else for real.

    localectl needs a booted systemd to list keymaps, which a test container
    doesn't have. systemd-firstboot --root and the list's shell helper don't.
    """

    def capture(cmd, **kwargs):
        if cmd[:1] == ["localectl"]:
            return subprocess.CompletedProcess(cmd, 0, "".join(f"{k}\n" for k in keymaps), "")
        return REAL_CAPTURE(cmd, **kwargs)

    return mock.patch.object(KEYBOARD, "capture", capture)


def keyboard_list(path):
    return mock.patch.object(KEYBOARD, "SETUP_FORM_CANDIDATES", (Path(path),))


class KeyboardConfigurationTest(unittest.TestCase):
    def target(self, directory: str) -> Path:
        target = Path(directory)
        (target / "etc").mkdir()
        (target / "etc/vconsole.conf").write_text(
            "KEYMAP=us\nFONT=default8x16\n"
        )
        return target

    def form(self, directory: str, text: str) -> Path:
        path = Path(directory) / "setup-form.sh"
        path.write_text(text)
        return path

    def configured(self, keymap: str, form=None) -> list[str]:
        """vconsole.conf lines after configure_keyboard, with the given list."""
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            with localectl_knows(keymap), keyboard_list(form or Path(directory) / "no-setup-form.sh"):
                self.assertTrue(KEYBOARD.configure_keyboard(target, keymap))
            return (target / "etc/vconsole.conf").read_text().splitlines()

    def firstboot_alone(self, keymap: str) -> list[str]:
        """What systemd-firstboot writes for keymap, plus the preserved font."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "etc").mkdir()
            subprocess.run(
                ["systemd-firstboot", f"--root={root}", f"--keymap={keymap}", "--force"],
                check=True,
                capture_output=True,
            )
            return (root / "etc/vconsole.conf").read_text().splitlines() + ["FONT=default8x16"]

    def test_writes_keymap_and_xkb_settings_and_preserves_font(self):
        # XKBLAYOUT is load-bearing: omarchy's detect-keyboard-layout.sh copies
        # it into Hyprland's kb_layout on the installed system.
        with tempfile.TemporaryDirectory() as directory:
            target = self.target(directory)
            self.assertTrue(KEYBOARD.configure_keyboard(target, "us"))
            lines = (target / "etc/vconsole.conf").read_text().splitlines()
            self.assertIn("KEYMAP=us", lines)
            self.assertIn("XKBLAYOUT=us", lines)
            self.assertEqual(lines.count("FONT=default8x16"), 1)

    def test_all_configurator_keymaps_are_known_to_localectl(self):
        if SUPPORTED_KEYMAPS is None:
            self.skipTest("no Omarchy runtime checkout to read the layout list from")
        for keymap in SUPPORTED_KEYMAPS:
            with self.subTest(keymap=keymap), tempfile.TemporaryDirectory() as directory:
                target = self.target(directory)
                self.assertTrue(KEYBOARD.configure_keyboard(target, keymap))
                lines = (target / "etc/vconsole.conf").read_text().splitlines()
                self.assertIn(f"KEYMAP={keymap}", lines)
                self.assertEqual(lines.count("FONT=default8x16"), 1)

    def test_all_configurator_keymaps_give_hyprland_a_layout(self):
        # Hyprland's input.lua takes kb_layout from XKBLAYOUT and falls back to
        # us, so a picked layout systemd can't map to XKB leaves the desktop on
        # US while the console and the LUKS prompt use the picked one.
        if SUPPORTED_KEYMAPS is None:
            self.skipTest("no Omarchy runtime checkout to read the layout list from")
        without_xkb = []
        for keymap in SUPPORTED_KEYMAPS:
            lines = self.configured(keymap, SETUP_FORM)
            if not any(line.startswith("XKBLAYOUT=") and line != "XKBLAYOUT=" for line in lines):
                without_xkb.append(keymap)
        self.assertEqual(without_xkb, [])

    def test_keymaps_systemd_maps_come_out_as_systemd_writes_them(self):
        keymaps = ["us", "de-latin1", "fr", "ru"]
        model_map = Path("/usr/share/systemd/kbd-model-map")
        if SUPPORTED_KEYMAPS is not None and model_map.is_file():
            rows = [line.split() for line in model_map.read_text().splitlines()]
            mapped = {row[0] for row in rows if row and not row[0].startswith("#")}
            keymaps += [keymap for keymap in SUPPORTED_KEYMAPS if keymap in mapped]
        for keymap in dict.fromkeys(keymaps):
            with self.subTest(keymap=keymap):
                self.assertEqual(self.configured(keymap, SETUP_FORM), self.firstboot_alone(keymap))

    def test_a_pinned_layout_follows_keymap_and_keeps_the_font(self):
        with tempfile.TemporaryDirectory() as forms:
            form = self.form(forms, PINNING_FORM)
            for keymap, settings in (
                ("pl", ["XKBLAYOUT=pl", "XKBMODEL=pc105", "XKBOPTIONS=terminate:ctrl_alt_bksp"]),
                (
                    "colemak",
                    ["XKBLAYOUT=us", "XKBMODEL=pc105", "XKBVARIANT=colemak", "XKBOPTIONS=terminate:ctrl_alt_bksp"],
                ),
            ):
                with self.subTest(keymap=keymap):
                    # systemd-firstboot alone writes KEYMAP and nothing else here.
                    written = self.firstboot_alone(keymap)
                    self.assertEqual(written[-2:], [f"KEYMAP={keymap}", "FONT=default8x16"])
                    self.assertEqual(self.configured(keymap, form), written[:-1] + settings + written[-1:])

    def test_the_list_never_overrides_systemd(self):
        # A pin for a keymap systemd does map (a list ahead of the host's
        # kbd-model-map, say) leaves systemd's own layout in place.
        with tempfile.TemporaryDirectory() as forms:
            form = self.form(forms, PINNING_FORM.replace("German|de-latin1", "German|de-latin1|ch"))
            self.assertEqual(self.configured("de-latin1", form), self.firstboot_alone("de-latin1"))

    def test_keymaps_outside_the_list_and_older_runtimes_stay_as_today(self):
        # install.toml can name any console keymap; one the picker doesn't
        # offer, a runtime whose list predates the XKB field, and an ISO with no
        # list at all all get exactly what systemd-firstboot writes.
        with tempfile.TemporaryDirectory() as forms:
            pinning = self.form(forms, PINNING_FORM)
            older = Path(forms) / "older.sh"
            older.write_text(OLDER_FORM)
            cases = (
                ("pl3", pinning),
                ("pl", older),
                ("pl", Path(forms) / "missing.sh"),
            )
            for keymap, form in cases:
                with self.subTest(keymap=keymap, form=form.name):
                    self.assertEqual(self.configured(keymap, form), self.firstboot_alone(keymap))

    def test_unknown_and_empty_keymaps_match_archinstall_behavior(self):
        for keymap, expected in (("definitely-not-a-keymap", False), ("", True)):
            with self.subTest(keymap=keymap), tempfile.TemporaryDirectory() as directory:
                target = self.target(directory)
                self.assertEqual(KEYBOARD.configure_keyboard(target, keymap), expected)
                self.assertEqual(
                    (target / "etc/vconsole.conf").read_text(),
                    "KEYMAP=us\nFONT=default8x16\n",
                )


if __name__ == "__main__":
    unittest.main()
