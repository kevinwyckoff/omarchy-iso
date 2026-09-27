"""Offline target keyboard configuration.

archinstall's set_keyboard_language boots the installed system in a
systemd-nspawn container just to run localectl. systemd-firstboot --root
produces the part of that output Omarchy actually consumes — KEYMAP for the
console plus the XKB* settings in vconsole.conf that omarchy's
detect-keyboard-layout.sh copies into Hyprland's kb_layout — without booting
anything. The Xorg 00-keyboard.conf that localectl also writes is not
generated: nothing on an Omarchy system reads it.

systemd derives those XKB settings from its kbd-model-map, and writes none for
a console keymap that has no row there, which leaves Hyprland on US. For each
such keymap it offers, Omarchy's keyboard list (setup-form.sh, vendored onto
the ISO from the runtime it installs) names the XKB layout itself, and that is
written in systemd's place.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .command import capture

# Where the Omarchy keyboard list is: vendored next to this package on the ISO
# by build-iso.sh, else in the runtime OMARCHY_PATH points at (a checkout, for
# bin/omarchy-iso-installer).
SETUP_FORM_CANDIDATES = (
    Path(__file__).resolve().parent.parent / "setup-form.sh",
    Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")) / "install/provisioning/setup-form.sh",
)

_XKB_SETTING = re.compile(r"XKB(LAYOUT|MODEL|VARIANT|OPTIONS)=[A-Za-z0-9_+:,-]*")


def picked_xkb_settings(keymap: str) -> list[str]:
    """The vconsole.conf XKB lines Omarchy's keyboard list pins for keymap.

    Empty for a keymap systemd maps itself, one the list doesn't offer, and
    when the list comes from a runtime older than omarchy_keyboard_xkb_settings:
    those installs keep exactly what systemd-firstboot writes.
    """
    form = next((path for path in SETUP_FORM_CANDIDATES if path.is_file()), None)
    if form is None:
        return []
    try:
        result = capture([
            "bash", "-c",
            'source "$1" >/dev/null 2>&1 || exit 0\n'
            "declare -F omarchy_keyboard_xkb_settings >/dev/null || exit 0\n"
            'omarchy_keyboard_xkb_settings "$2"',
            "setup-form", str(form), keymap,
        ])
    except OSError:
        return []
    lines = result.stdout.splitlines()
    if result.returncode != 0 or not all(_XKB_SETTING.fullmatch(line) for line in lines):
        return []
    return lines


def configure_keyboard(target: Path, language: str) -> bool:
    """Write the console keymap into a mounted target without booting it.

    Returns False for layouts localectl doesn't know, matching archinstall:
    warn and keep the default. Every layout the configurator offers is known;
    the guard is for the kb_layout an autoinstall drive can name freely.
    """
    if not language.strip():
        return True

    result = capture(["localectl", "--no-pager", "list-keymaps"])
    if result.returncode != 0:
        detail = result.stderr.strip() or "localectl returned an error"
        raise RuntimeError(f"Unable to list keyboard layouts: {detail}")
    if language.lower() not in {layout.lower() for layout in result.stdout.splitlines()}:
        return False

    # systemd-firstboot --force rewrites vconsole.conf wholesale, dropping the
    # FONT= line archinstall's set_vconsole wrote earlier.
    vconsole_path = target / "etc" / "vconsole.conf"
    font = None
    if vconsole_path.exists():
        font = next(
            (line for line in vconsole_path.read_text().splitlines() if line.startswith("FONT=")),
            None,
        )

    result = capture(["systemd-firstboot", f"--root={target}", f"--keymap={language}", "--force"])
    if result.returncode != 0:
        detail = result.stderr.strip() or "systemd-firstboot returned an error"
        raise RuntimeError(f"Unable to configure keyboard layout {language}: {detail}")

    # Only where systemd wrote no XKB layout, so a keymap it maps stays exactly
    # as it wrote it. The pinned lines go right after KEYMAP, where systemd
    # puts its own.
    lines = vconsole_path.read_text().splitlines()
    keymap_line = f"KEYMAP={language}"
    if keymap_line in lines and not any(line.startswith("XKBLAYOUT=") for line in lines):
        settings = picked_xkb_settings(language)
        if settings:
            at = lines.index(keymap_line) + 1
            vconsole_path.write_text("\n".join(lines[:at] + settings + lines[at:]) + "\n")

    if font:
        vconsole_path.write_text(vconsole_path.read_text() + font + "\n")

    return True
