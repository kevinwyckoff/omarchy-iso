"""Unit tests for chefs-kitchen: install.toml's schema, the compiler that turns
it into the orchestrator's existing inputs, disk selectors, the disk
fingerprint, and the plan's refusals.

Spec rows covered here without a VM: J (unknown key or plaintext secret fails
validate, naming the key), F (on_existing_data = "abort" against a disk with
data refuses) and G (a stale expect_fingerprint refuses). Rows E, F and G are
also run end to end on an ISO.
"""

import io
import json
import os
import re
import sys
import tempfile
import textwrap
import tomllib
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "configs/airootfs/usr/share/omarchy-iso"))

from chefs_kitchen_config import __main__ as cli  # noqa: E402
from chefs_kitchen_config import compile_archinstall, plan, resolve  # noqa: E402
from chefs_kitchen_config.schema import DiskSelector, Secret, parse  # noqa: E402

HASH = "$6$salt$" + "a" * 86


def minimal(**overrides) -> dict:
    config = {
        "schema": 1,
        "system": {"hostname": "marvin", "timezone": "America/Toronto", "keyboard": "us"},
        "users": [{"name": "kevin", "password_hash": HASH}],
        "disk": {"target": {"serial": "S69ENX0T812345"}},
    }
    config.update(overrides)
    return config


def errors(issues):
    return [str(i) for i in issues if i.severity == "error"]


def warnings(issues):
    return [str(i) for i in issues if i.severity == "warning"]


def kbd_tree(directory: Path) -> Path:
    """Keymaps as kbd lays them out: per-layout directories, some of them links."""
    root = directory / "keymaps"
    (root / "i386/qwerty").mkdir(parents=True)
    (root / "i386/qwertz").mkdir(parents=True)
    (root / "i386/qwerty/us.map.gz").write_bytes(b"")
    (root / "i386/qwerty/defkeymap.map").write_text("")
    (root / "i386/qwertz/de-latin1.map.gz").write_bytes(b"")
    (root / "i386/qwertz/de.map.gz").symlink_to("de-latin1.map.gz")
    return root


# The wizard's keyboard picker lives in the Omarchy runtime's setup-form.sh;
# read it from wherever this checkout can see one, as test_keyboard.py does.
SETUP_FORM_CANDIDATES = (
    Path("/omarchy-source/install/provisioning/setup-form.sh"),
    Path(__file__).resolve().parents[3] / "omarchy/install/provisioning/setup-form.sh",
    Path("/usr/share/omarchy/install/provisioning/setup-form.sh"),
)


def wizard_keymaps() -> list[str] | None:
    for candidate in SETUP_FORM_CANDIDATES:
        if candidate.is_file():
            block = re.search(r"OMARCHY_KEYBOARD_LAYOUTS=\$'(.*?)'\n", candidate.read_text(), re.DOTALL)
            assert block, f"no layout list in {candidate}"
            # The wizard takes a row's second '|' field as the keymap (the
            # form's awk prints $2), whatever fields follow it.
            return [line.split("|")[1] for line in block.group(1).splitlines()]
    return None


class SchemaTest(unittest.TestCase):
    def test_a_minimal_config_is_valid_and_fills_the_defaults(self):
        config, issues = parse(minimal())
        self.assertEqual(errors(issues), [])
        self.assertEqual(config.mode, "wipe")
        self.assertEqual(config.on_existing_data, "abort")
        self.assertEqual(config.swap_strategy, "zram+hibernate")
        self.assertTrue(config.encryption_enabled)
        self.assertEqual(config.passphrase, Secret("same_as_user", "kevin"))
        self.assertEqual(config.target, DiskSelector("serial", "S69ENX0T812345"))

    def test_unknown_keys_are_errors_that_name_the_key(self):
        _, issues = parse(minimal(swapp={"strategy": "zram"}))
        self.assertIn("error: swapp: unknown key", errors(issues)[0])

        data = minimal()
        data["disk"]["on_existing"] = "wipe"
        _, issues = parse(data)
        self.assertTrue(any(e.startswith("error: disk.on_existing: unknown key") for e in errors(issues)))

    def test_the_schema_version_is_required_and_checked(self):
        data = minimal()
        del data["schema"]
        self.assertIn("error: schema: is required (schema = 1)", errors(parse(data)[1]))
        self.assertTrue(any("reads schema 1, not 2" in e for e in errors(parse(minimal(schema=2))[1])))

    def test_plaintext_secrets_are_rejected(self):
        data = minimal()
        data["users"][0] = {"name": "kevin", "password": "hunter2"}
        self.assertTrue(any(e.startswith("error: users[0].password: a plaintext secret is not allowed") for e in errors(parse(data)[1])))

        data = minimal(encryption={"passphrase": "hunter2"})
        self.assertTrue(any(e.startswith("error: encryption.passphrase: a plaintext secret") for e in errors(parse(data)[1])))

        data = minimal(network={"tailscale_authkey": "tskey-auth-xyz"})
        self.assertTrue(any(e.startswith("error: network.tailscale_authkey: a plaintext secret") for e in errors(parse(data)[1])))

    def test_insecure_plaintext_is_accepted_with_a_warning(self):
        config, issues = parse(minimal(encryption={"passphrase": {"insecure_plaintext": "hunter2"}}))
        self.assertEqual(errors(issues), [])
        self.assertTrue(any("stored in plain text" in w for w in warnings(issues)))
        self.assertEqual(config.passphrase.read(), "hunter2")

    def test_a_password_hash_must_be_a_crypt_hash(self):
        data = minimal()
        data["users"][0]["password_hash"] = "hunter2"
        self.assertTrue(any("is not a crypt(3) hash" in e for e in errors(parse(data)[1])))

    def test_a_user_needs_exactly_one_of_password_and_hash(self):
        data = minimal()
        del data["users"][0]["password_hash"]
        self.assertTrue(any("needs password_hash" in e for e in errors(parse(data)[1])))

    def test_password_files_are_read_relative_to_install_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "kevin.pass").write_text("hunter2\n")
            data = minimal()
            data["users"][0] = {"name": "kevin", "password": {"file": "kevin.pass"}}
            config, issues = parse(data, source=Path(tmp) / "install.toml")
            self.assertEqual(errors(issues), [])
            self.assertEqual(config.user.password.read(), "hunter2")
            self.assertFalse(config.needs_passphrase_prompt())

            data["users"][0]["password"] = {"file": "missing.pass"}
            self.assertTrue(any("missing.pass does not exist" in e for e in errors(parse(data, source=Path(tmp) / "install.toml")[1])))

    def test_hash_only_with_encryption_needs_a_prompt(self):
        config, issues = parse(minimal())
        self.assertTrue(config.needs_passphrase_prompt())
        self.assertTrue(any("can't run with --yes" in w for w in warnings(issues)))

    def test_same_as_user_must_name_the_user(self):
        _, issues = parse(minimal(encryption={"passphrase": {"same_as_user": "someone"}}))
        self.assertTrue(any("no user named 'someone'" in e for e in errors(issues)))

    def test_a_passphrase_without_encryption_is_an_error(self):
        _, issues = parse(minimal(encryption={"enabled": False, "passphrase": {"prompt": True}}))
        self.assertTrue(any("encryption.enabled = false" in e for e in errors(issues)))

    def test_deferred_provisioning_has_no_users_and_no_passphrase(self):
        data = minimal(provisioning={"defer": True})
        self.assertTrue(any("must be empty when provisioning.defer" in e for e in errors(parse(data)[1])))

        del data["users"]
        config, issues = parse(data)
        self.assertEqual(errors(issues), [])
        self.assertIsNone(config.passphrase)
        self.assertFalse(config.needs_passphrase_prompt())

    def test_wizard_rules_for_names(self):
        data = minimal()
        data["users"][0]["name"] = "root"
        self.assertTrue(any("users[0].name" in e for e in errors(parse(data)[1])))
        data["users"][0]["name"] = "Kevin"
        self.assertTrue(any("users[0].name" in e for e in errors(parse(data)[1])))
        _, issues = parse(minimal(system={"hostname": "-marvin"}))
        self.assertTrue(any("system.hostname" in e for e in errors(issues)))

    def test_unknown_timezone(self):
        _, issues = parse(minimal(system={"timezone": "Mars/Olympus_Mons"}))
        self.assertTrue(any("unknown timezone" in e for e in errors(issues)))

    def test_a_keymap_name_is_only_checked_for_its_shape(self):
        # Whether it exists is plan's check (PlanTest): kbd's keymaps differ
        # between distributions, and validate runs anywhere.
        for keyboard in ("colemak", "german"):
            with self.subTest(keyboard=keyboard):
                self.assertEqual(errors(parse(minimal(system={"keyboard": keyboard}))[1]), [])
        issues = errors(parse(minimal(system={"keyboard": "de latin1"}))[1])
        self.assertTrue(any("system.keyboard: must be a console keymap name" in e for e in issues), issues)

    def test_disk_selectors(self):
        for target in ({"serial": "x"}, {"by_id": "nvme-x"}, {"wwn": "eui.1"}, {"path": "/dev/vda"}):
            self.assertEqual(errors(parse(minimal(disk={"target": target}))[1]), [], target)
        self.assertTrue(errors(parse(minimal(disk={"target": {"model": "x"}}))[1]))
        self.assertTrue(errors(parse(minimal(disk={"target": {"serial": "x", "wwn": "y"}}))[1]))
        self.assertTrue(errors(parse(minimal(disk={"target": {"path": "vda"}}))[1]))

    def test_home_disk_rules(self):
        home = {"target": {"serial": "A"}, "home": {"location": "disk"}}
        self.assertTrue(any("disk.home.disk: is required" in e for e in errors(parse(minimal(disk=home))[1])))
        home["home"]["disk"] = {"serial": "A"}
        self.assertTrue(any("different disk" in e for e in errors(parse(minimal(disk=home))[1])))
        home["home"]["disk"] = {"serial": "B"}
        self.assertEqual(errors(parse(minimal(disk=home))[1]), [])

    def test_fingerprint_format(self):
        disk = {"target": {"serial": "A"}, "expect_fingerprint": "abc"}
        self.assertTrue(any("disk.expect_fingerprint" in e for e in errors(parse(minimal(disk=disk))[1])))

    def test_ssh_keys_and_package_names(self):
        data = minimal(packages={"extra": ["firefox", "Not A Package"]})
        data["users"][0]["ssh_authorized_keys"] = ["ssh-ed25519 AAAA me@host", "hello"]
        messages = errors(parse(data)[1])
        self.assertTrue(any("ssh_authorized_keys[1]" in e for e in messages))
        self.assertTrue(any("packages.extra[1]" in e for e in messages))


class ValidateCommandTest(unittest.TestCase):
    """Spec row J: `chefs-kitchen validate` fails and names the key."""

    def run_validate(self, text, argv=("{path}",)):
        """validate the text as install.toml, with {path} in argv naming it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "install.toml"
            path.write_text(textwrap.dedent(text))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    status = cli.main(["validate", *(arg.format(path=path) for arg in argv)])
                except SystemExit as exc:  # a usage error, from argparse
                    status = exc.code
            return status, out.getvalue() + err.getvalue()

    BASE = f"""
        schema = 1
        [[users]]
        name = "kevin"
        password_hash = "{HASH}"
        [disk]
        target = {{ serial = "S69ENX0T812345" }}
    """

    def test_a_valid_file(self):
        status, output = self.run_validate(self.BASE)
        self.assertEqual(status, 0, output)
        self.assertIn("valid", output)

    def test_an_unknown_key(self):
        status, output = self.run_validate(self.BASE + '\n[swap]\nstrategyy = "zram"\n')
        self.assertEqual(status, 1)
        self.assertIn("swap.strategyy: unknown key", output)

    def test_a_plaintext_secret(self):
        status, output = self.run_validate(self.BASE + '\n[encryption]\npassphrase = "hunter2"\n')
        self.assertEqual(status, 1)
        self.assertIn("encryption.passphrase: a plaintext secret is not allowed", output)

    def test_a_keymap_is_not_looked_up(self):
        # Fedora's kbd has no "colemak", which the wizard offers; a made-up
        # name is plan's to refuse, on the machine that installs.
        for keyboard in ("colemak", "german"):
            with self.subTest(keyboard=keyboard):
                status, output = self.run_validate(self.BASE + f'\n[system]\nkeyboard = "{keyboard}"\n')
                self.assertEqual(status, 0, output)

    def test_not_toml(self):
        status, output = self.run_validate("schema = = 1")
        self.assertEqual(status, 1)
        self.assertIn("is not valid TOML", output)

    def test_config_names_the_file_as_it_does_for_plan_and_install(self):
        status, output = self.run_validate(self.BASE, ["--config", "{path}"])
        self.assertEqual(status, 0, output)
        self.assertTrue(output.endswith("install.toml: valid\n"), output)

        status, output = self.run_validate(self.BASE + '\n[swap]\nstrategyy = "zram"\n', ["--config", "{path}"])
        self.assertEqual(status, 1)
        self.assertIn("swap.strategyy: unknown key", output)

    def test_the_same_file_both_ways_is_one_file(self):
        status, output = self.run_validate(self.BASE, ["{path}", "--config", "{path.parent}/./{path.name}"])
        self.assertEqual(status, 0, output)

    def test_two_different_files_is_a_usage_error(self):
        status, output = self.run_validate(self.BASE, ["{path}", "--config", "{path.parent}/other.toml"])
        self.assertEqual(status, 2)
        self.assertIn("two files given", output)
        self.assertNotRegex(output, r"(?m): (in)?valid$")  # neither file was checked

    def test_no_file_is_a_usage_error(self):
        status, output = self.run_validate(self.BASE, [])
        self.assertEqual(status, 2)
        self.assertIn("give install.toml, as an argument or with --config", output)


class CompileTest(unittest.TestCase):
    def setUp(self):
        self.config, issues = parse(minimal())
        self.assertEqual(errors(issues), [])
        self.secrets = compile_archinstall.Secrets(
            user_password="hunter2", user_password_hash=HASH, luks_passphrase="hunter2"
        )

    def compile(self, config=None, size=40 * 2**30):
        return compile_archinstall.full_disk_configuration(
            config or self.config, "/dev/vda", size, self.secrets, "linux-omarchy", "omarchy-dev", "omarchy-settings-dev"
        )

    def test_the_layout_matches_the_configurator(self):
        configuration = self.compile()
        parts = configuration["disk_config"]["device_modifications"][0]["partitions"]
        mib, gib = 2**20, 2**30
        self.assertEqual((parts[0]["start"]["value"], parts[0]["size"]["value"]), (mib, 2 * gib))
        self.assertEqual(parts[1]["start"]["value"], 2 * gib + mib)
        self.assertEqual(parts[1]["size"]["value"], 40 * gib - (2 * gib + mib) - mib)
        self.assertEqual([s["name"] for s in parts[1]["btrfs"]], ["@", "@home", "@log", "@pkg"])
        self.assertEqual(configuration["omarchy_install"]["mode"], "full_disk")
        self.assertEqual(configuration["hostname"], "marvin")
        self.assertEqual(configuration["locale_config"]["kb_layout"], "us")
        self.assertEqual(configuration["packages"][-2:], ["omarchy-settings-dev", "omarchy-dev"])

    def test_encryption_follows_the_config(self):
        encrypted = self.compile()["disk_config"]["disk_encryption"]
        self.assertEqual(encrypted["encryption_password"], "hunter2")
        self.assertEqual(encrypted["partitions"], [compile_archinstall.ROOT_OBJ_ID])

        plain, _ = parse(minimal(encryption={"enabled": False}))
        self.assertNotIn("disk_encryption", self.compile(plain)["disk_config"])

    def test_deferred_provisioning_carries_no_passphrase_or_users(self):
        data = minimal(provisioning={"defer": True})
        del data["users"]
        config, _ = parse(data)
        configuration = self.compile(config)
        self.assertTrue(configuration["omarchy_install"]["defer_provisioning"])
        self.assertNotIn("encryption_password", configuration["disk_config"]["disk_encryption"])
        self.assertEqual(compile_archinstall.credentials(config, self.secrets), {"users": []})

    def test_a_home_disk_takes_home_off_the_root_disk(self):
        data = minimal(disk={"target": {"serial": "A"}, "home": {"location": "disk", "disk": {"serial": "B"}}})
        config, issues = parse(data)
        self.assertEqual(errors(issues), [])
        configuration = compile_archinstall.full_disk_configuration(
            config, "/dev/vda", 40 * 2**30, self.secrets, "linux-omarchy", "omarchy-dev", "omarchy-settings-dev", "/dev/vdb"
        )
        root = configuration["disk_config"]["device_modifications"][0]["partitions"][1]
        self.assertEqual([s["name"] for s in root["btrfs"]], ["@", "@log", "@pkg"])
        self.assertEqual(configuration["omarchy_install"]["home"], {"device": "/dev/vdb", "encrypt": True})
        self.assertNotIn("home", self.compile()["omarchy_install"])

    def test_a_free_space_install_is_pre_mounted(self):
        mounted = compile_archinstall.PreMounted("/dev/sda5", "/dev/sda6", "/dev/mapper/omarchy_root", "1234-abcd", "/boot")
        configuration = compile_archinstall.pre_mounted_configuration(
            self.config, mounted, "linux-omarchy", "omarchy-dev", "omarchy-settings-dev"
        )
        self.assertEqual(configuration["disk_config"], {"config_type": "pre_mounted_config", "mountpoint": "/mnt"})
        install = configuration["omarchy_install"]
        self.assertEqual(install["mode"], "protected")
        self.assertEqual(install["boot"]["esp_mount"], "/boot")
        self.assertFalse(install["boot"]["enable_fallback"])
        self.assertEqual(install["storage"], {"esp_device": "/dev/sda5", "root_device": "/dev/sda6",
                                              "root_mapper": "/dev/mapper/omarchy_root", "luks_uuid": "1234-abcd",
                                              "kernel": "linux-omarchy"})
        self.assertEqual(configuration["hostname"], "marvin")

    def test_free_space_results_are_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result"
            result.write_text("efi_dev=/dev/sda5\nroot_device=/dev/sda6\nroot_mapper=/dev/sda6\nluks_uuid=''\nesp_mount=/efi\n")
            mounted = compile_archinstall.PreMounted.from_result_file(result)
            self.assertEqual((mounted.efi_dev, mounted.luks_uuid, mounted.esp_mount), ("/dev/sda5", None, "/efi"))

    def test_the_swap_strategy_reaches_the_orchestrator(self):
        self.assertEqual(self.compile()["omarchy_install"]["swap"], {"strategy": "zram+hibernate"})
        config, _ = parse(minimal(swap={"strategy": "none"}))
        self.assertEqual(self.compile(config)["omarchy_install"]["swap"], {"strategy": "none"})

    def test_credentials(self):
        creds = compile_archinstall.credentials(self.config, self.secrets)
        self.assertEqual(creds["encryption_password"], "hunter2")
        self.assertEqual(creds["root_enc_password"], HASH)
        self.assertEqual(creds["users"], [{"enc_password": HASH, "groups": [], "sudo": True, "username": "kevin"}])

    def test_the_recorded_copy_has_no_secrets(self):
        data = minimal(network={"tailscale_authkey": {"insecure_plaintext": "tskey-auth-secret"}},
                       encryption={"passphrase": {"insecure_plaintext": "luks-secret"}})
        data["users"][0]["ssh_authorized_keys"] = ["ssh-ed25519 AAAA me@host"]
        config, issues = parse(data)
        self.assertEqual(errors(issues), [])
        text = compile_archinstall.stripped_toml(config)
        for secret in (HASH, "tskey-auth-secret", "luks-secret"):
            self.assertNotIn(secret, text)
        recorded = tomllib.loads(text)
        self.assertEqual(recorded["users"][0]["name"], "kevin")
        self.assertEqual(recorded["disk"]["target"], {"serial": "S69ENX0T812345"})
        self.assertEqual(recorded["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 AAAA me@host"])

    def test_write_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "user_encrypt_installation.txt").write_text("true\n")  # stale
            compile_archinstall.write_inputs(out, self.config, "/dev/vda", 40 * 2**30, self.secrets,
                                             "linux-omarchy", "omarchy-dev", "omarchy-settings-dev")
            self.assertFalse((out / "user_encrypt_installation.txt").exists())
            self.assertEqual(json.loads((out / "user_configuration.json").read_text())["hostname"], "marvin")
            self.assertEqual((out / "user_credentials.json").stat().st_mode & 0o777, 0o600)
            self.assertTrue((out / "install.stripped.toml").exists())
            self.assertFalse((out / "authorized_keys").exists())


LSBLK = {
    "blockdevices": [
        {"name": "vda", "path": "/dev/vda", "type": "disk", "size": 42949672960, "serial": None, "wwn": None,
         "children": [{"name": "vda1", "path": "/dev/vda1", "type": "part", "size": 1, "uuid": "AAAA-BBBB"}]},
        {"name": "sda", "path": "/dev/sda", "type": "disk", "size": 42949672960, "serial": "S69ENX0T812345",
         "wwn": "0x5002538e40000001"},
        {"name": "sdb", "path": "/dev/sdb", "type": "disk", "size": 42949672960, "serial": "DUP", "wwn": None},
        {"name": "sdc", "path": "/dev/sdc", "type": "disk", "size": 42949672960, "serial": "DUP", "wwn": None},
    ]
}


class HelpersTest(unittest.TestCase):
    """The Bash bridge itself, against stub scripts: the arguments must reach
    the function intact."""

    def test_free_space_passes_every_argument(self):
        from chefs_kitchen_config import helpers
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "disk-partitioning.sh").write_text("partitioning_loaded=yes\n")
            Path(tmp, "free-space.sh").write_text('echo_args() { echo "$partitioning_loaded $#: $*"; }\n')
            with mock.patch.object(helpers, "HELPER_DIR", Path(tmp)):
                result = helpers.free_space('echo_args "$@"', "/dev/sda", "true", "x y")
        self.assertEqual(result.stdout, "yes 3: /dev/sda true x y\n")

    def test_disk_inspect_passes_every_argument(self):
        from chefs_kitchen_config import helpers
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "disk-inspect.sh").write_text('disk_inventory_refresh() { :; }\necho_args() { echo "$#: $*"; }\n')
            with mock.patch.object(helpers, "HELPER_DIR", Path(tmp)):
                self.assertEqual(helpers.disk_inspect('echo_args "$@"', "/dev/sda", "full_disk"), "2: /dev/sda full_disk\n")


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.devices = []

        def walk(nodes):
            for node in nodes:
                self.devices.append(node)
                walk(node.get("children") or [])

        walk(LSBLK["blockdevices"])

    def test_serial_and_wwn(self):
        self.assertEqual(resolve.resolve(DiskSelector("serial", "s69enx0t812345"), self.devices).disk, "/dev/sda")
        self.assertEqual(resolve.resolve(DiskSelector("wwn", "5002538E40000001"), self.devices).disk, "/dev/sda")

    def test_ambiguous_and_missing(self):
        self.assertIn("matches 2 disks", resolve.resolve(DiskSelector("serial", "DUP"), self.devices).error)
        self.assertIn("no disk matches", resolve.resolve(DiskSelector("serial", "NOPE"), self.devices).error)

    def test_path_must_be_a_whole_disk(self):
        self.assertEqual(resolve.resolve(DiskSelector("path", "/dev/vda"), self.devices).disk, "/dev/vda")
        self.assertIn("not a whole disk", resolve.resolve(DiskSelector("path", "/dev/vda1"), self.devices).error)

    def test_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "virtio-kitchen").symlink_to("/dev/vda")
            (Path(tmp) / "virtio-kitchen-part1").symlink_to("/dev/vda1")
            with mock.patch.object(resolve, "BY_ID_DIR", Path(tmp)):
                self.assertEqual(resolve.resolve(DiskSelector("by_id", "virtio-kitchen"), self.devices).disk, "/dev/vda")
                self.assertIn("is a partition", resolve.resolve(DiskSelector("by_id", "virtio-kitchen-part1"), self.devices).error)
                self.assertIn("no disk named", resolve.resolve(DiskSelector("by_id", "nope"), self.devices).error)


class FingerprintTest(unittest.TestCase):
    def fingerprint(self, table):
        with tempfile.TemporaryDirectory() as tmp:
            if table is not None:
                (Path(tmp) / "vda.json").write_text(json.dumps({"partitiontable": table}))
            with mock.patch.dict(os.environ, {"CHEFS_KITCHEN_SFDISK_JSON_DIR": tmp}):
                return plan.fingerprint("/dev/vda", ResolveTest.devices_for())

    def test_it_changes_when_the_layout_changes(self):
        table = {"label": "gpt", "partitions": [{"node": "/dev/vda1", "start": 2048, "size": 532480,
                                                  "type": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"}]}
        first = self.fingerprint(table)
        self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first, self.fingerprint(table))
        table["partitions"][0]["size"] = 1064960
        self.assertNotEqual(first, self.fingerprint(table))
        self.assertNotEqual(first, self.fingerprint(None))


ResolveTest.devices_for = staticmethod(lambda: [
    {"path": "/dev/vda", "type": "disk", "size": 42949672960},
    {"path": "/dev/vda1", "type": "part", "uuid": "AAAA-BBBB"},
])


class PlanTest(unittest.TestCase):
    """make_plan's refusals, with the Bash helpers stubbed."""

    def make(self, data, unattended=True, signatures=True, fingerprint="sha256:" + "0" * 64, medium="/dev/sdz"):
        config, issues = parse(data)
        self.assertEqual(errors(issues), [])
        devices = [{"path": "/dev/sda", "type": "disk", "size": 42949672960, "serial": "S69ENX0T812345"},
                   {"path": "/dev/sdb", "type": "disk", "size": 42949672960, "serial": "HOME"}]
        signatures = signatures if callable(signatures) else (lambda disk, value=signatures: value)
        with mock.patch.object(plan.resolve, "inventory", return_value=devices), \
             mock.patch.object(plan.helpers, "install_medium", return_value=medium), \
             mock.patch.object(plan.helpers, "is_cidata", return_value=False), \
             mock.patch.object(plan.helpers, "installable_disks", return_value=["/dev/sda", "/dev/sdb"]), \
             mock.patch.object(plan.helpers, "min_full_disk_bytes", return_value=32 * 2**30), \
             mock.patch.object(plan.helpers, "has_signatures", side_effect=signatures), \
             mock.patch.object(plan.helpers, "wipe_summary", side_effect=lambda disk, *a, **k: f"summary of {disk} {k.get('mode', 'full_disk')}\n"), \
             mock.patch.object(plan.helpers, "busy_partitions", return_value=[]), \
             mock.patch.object(plan, "fingerprint", return_value=fingerprint), \
             mock.patch.object(plan, "_is_virtual_machine", return_value=True):
            return plan.make_plan(config, unattended=unattended)

    def unattended_config(self, **disk):
        data = minimal(encryption={"passphrase": {"insecure_plaintext": "hunter2"}})
        data["disk"].update(disk)
        return data

    def test_a_clean_plan(self):
        result = self.make(self.unattended_config(), signatures=False)
        self.assertTrue(result.ok, [str(i) for i in result.issues])
        self.assertEqual(result.target.path, "/dev/sda")

    def test_row_f_abort_refuses_a_disk_with_data(self):
        result = self.make(self.unattended_config())
        self.assertFalse(result.ok)
        self.assertIn('on_existing_data = "abort"', str(result.errors[0]))
        self.assertIn('expect_fingerprint = "sha256:', str(result.errors[0]))

    def test_interactive_installs_ask_instead(self):
        self.assertTrue(self.make(self.unattended_config(), unattended=False).ok)

    def test_a_home_disk_gets_its_own_summary_and_guard(self):
        data = self.unattended_config(on_existing_data="wipe")
        data["disk"]["home"] = {"location": "disk", "disk": {"serial": "HOME"}}
        result = self.make(data)
        self.assertTrue(result.ok, [str(i) for i in result.issues])
        self.assertEqual(result.home.path, "/dev/sdb")
        self.assertEqual(result.home.summary, "summary of /dev/sdb home_disk\n")

        data["disk"]["on_existing_data"] = "abort"
        result = self.make(data, signatures=lambda disk: disk == "/dev/sdb")
        self.assertIn("(the /home disk) has data on it", str(result.errors[0]))

        data["disk"]["home"]["disk"] = {"serial": "S69ENX0T812345"}
        parsed, issues = parse(data)
        self.assertTrue(any("different disk" in e for e in errors(issues)))

    def free_space(self, region=(False, 400 * 2**30, 402 * 2**30, 402 * 2**30 + 2**20, 460 * 2**30), bitlocker=()):
        data = self.unattended_config()
        data["disk"]["mode"] = "free-space"
        with mock.patch.object(plan.helpers, "free_space_region", return_value=region), \
             mock.patch.object(plan.helpers, "bitlocker_partitions", return_value=list(bitlocker)), \
             mock.patch.object(plan.helpers, "free_space_summary", return_value="OMARCHY WILL USE FREE SPACE\n"):
            return self.make(data)

    def test_free_space_ignores_on_existing_data(self):
        result = self.free_space()
        self.assertTrue(result.ok, [str(i) for i in result.issues])
        self.assertEqual(result.target.region[1], 400 * 2**30)
        self.assertEqual(result.target.summary, "OMARCHY WILL USE FREE SPACE\n")

    def test_free_space_needs_room(self):
        self.assertIn("Omarchy needs 32 GiB, its own 2 GiB ESP included", str(self.free_space(region=20 * 2**30).errors[0]))

    def test_free_space_refuses_bitlocker(self):
        self.assertIn("BitLocker", str(self.free_space(bitlocker=["/dev/sda3"]).errors[0]))

    def test_every_swap_strategy_can_install(self):
        for strategy in ("zram+hibernate", "zram", "none"):
            data = self.unattended_config(on_existing_data="wipe")
            data["swap"] = {"strategy": strategy}
            self.assertTrue(self.make(data).ok, strategy)

    def test_wipe_goes_ahead(self):
        self.assertTrue(self.make(self.unattended_config(on_existing_data="wipe")).ok)

    def test_row_g_a_stale_fingerprint_refuses(self):
        result = self.make(self.unattended_config(on_existing_data="wipe", expect_fingerprint="sha256:" + "1" * 64))
        self.assertFalse(result.ok)
        self.assertIn("no longer looks like it did", str(result.errors[0]))
        self.assertTrue(self.make(self.unattended_config(on_existing_data="wipe", expect_fingerprint="sha256:" + "0" * 64)).ok)

    def test_a_prompt_can_not_run_unattended(self):
        result = self.make(minimal(), signatures=False)
        self.assertIn("encryption.passphrase", str(result.errors[0]))

    def test_the_install_medium_is_refused(self):
        result = self.make(self.unattended_config(), medium="/dev/sda")
        self.assertIn("install medium", str(result.errors[0]))

    def keyboard_config(self, keyboard):
        data = self.unattended_config(on_existing_data="wipe")
        data["system"]["keyboard"] = keyboard
        return data

    def with_keymaps(self, keyboard, *keymap_dirs):
        with mock.patch.object(plan, "KEYMAP_DIRS", keymap_dirs):
            return self.make(self.keyboard_config(keyboard))

    def test_an_unknown_keymap_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = kbd_tree(Path(directory))
            for keyboard in ("us", "defkeymap", "de-latin1", "de"):
                with self.subTest(keyboard=keyboard):
                    result = self.with_keymaps(keyboard, Path(directory) / "missing", root)
                    self.assertTrue(result.ok, [str(i) for i in result.issues])
            # loadkeys would find neither a made-up name nor one in the wrong case.
            for keyboard in ("german", "DE-LATIN1", "us.map"):
                with self.subTest(keyboard=keyboard):
                    result = self.with_keymaps(keyboard, Path(directory) / "missing", root)
                    self.assertIn(f"system.keyboard: unknown keymap {keyboard!r}", str(result.errors[0]))

    def test_without_keymaps_the_keyboard_is_only_warned_about(self):
        # As with a theme on a machine that isn't the ISO: plan can't check it,
        # so it says so rather than refuse. The ISO always has kbd's keymaps.
        with tempfile.TemporaryDirectory() as directory:
            # Debian's console-data keymaps are .kmap.gz, which localectl
            # (systemd 262, the ISO's) doesn't list.
            (Path(directory) / "i386").mkdir()
            (Path(directory) / "i386/us.kmap.gz").write_bytes(b"")
            for dirs in ((Path(directory) / "missing",), (Path(directory),)):
                with self.subTest(dirs=dirs):
                    result = self.with_keymaps("german", *dirs)
                    self.assertTrue(result.ok, [str(i) for i in result.issues])
                    self.assertTrue(any("system.keyboard: can't be checked here" in w for w in warnings(result.issues)))

    def test_every_keymap_the_wizard_offers_is_known_to_the_iso(self):
        offered = wizard_keymaps()
        if offered is None:
            self.skipTest("no Omarchy runtime checkout to read the wizard's keymaps from")
        if not Path("/etc/arch-release").exists() or plan.console_keymaps() is None:
            self.skipTest("needs Arch's kbd keymaps, the ones the ISO has")
        for keyboard in offered:
            with self.subTest(keyboard=keyboard):
                result = self.make(self.keyboard_config(keyboard))
                self.assertTrue(result.ok, [str(i) for i in result.issues])
        # The same check, against the same keymaps, does refuse.
        self.assertIn("unknown keymap 'german'", str(self.make(self.keyboard_config("german")).errors[0]))

    def on_machine(self, size=42949672960):
        """make()'s machine as a context, for tests that run more than
        make_plan: one disk of <size>, and the real full-disk minimum."""
        stack = ExitStack()
        devices = [{"path": "/dev/sda", "type": "disk", "size": size, "serial": "S69ENX0T812345"}]
        for target, name, value in (
            (plan.resolve, "inventory", devices),
            (plan.helpers, "install_medium", "/dev/sdz"),
            (plan.helpers, "is_cidata", False),
            (plan.helpers, "installable_disks", ["/dev/sda"]),
            (plan.helpers, "has_signatures", True),
            (plan.helpers, "wipe_summary", "THIS WILL ERASE A DISK\n"),
            (plan.helpers, "busy_partitions", []),
            (plan, "_is_virtual_machine", True),
        ):
            stack.enter_context(mock.patch.object(target, name, return_value=value))
        return stack

    def test_install_refuses_an_unknown_keymap_before_writing_anything(self):
        # install plans first. Only a clean plan gets the installer's inputs
        # written and the install launched, and nothing is erased before that.
        for keyboard, expected in (("german", 1), ("de-latin1", 0)):
            with self.subTest(keyboard=keyboard), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "install.toml"
                path.write_text(textwrap.dedent(f"""
                    schema = 1
                    [system]
                    keyboard = "{keyboard}"
                    [[users]]
                    name = "kevin"
                    password_hash = "{HASH}"
                    [disk]
                    target = {{ serial = "S69ENX0T812345" }}
                    on_existing_data = "wipe"
                    [encryption]
                    passphrase = {{ insecure_plaintext = "hunter2" }}
                """))
                out = io.StringIO()
                with self.on_machine() as stack:
                    stack.enter_context(mock.patch.object(cli.compile_archinstall, "detect_kernel", return_value="linux"))
                    stack.enter_context(mock.patch.object(plan, "KEYMAP_DIRS", (kbd_tree(Path(tmp)),)))
                    write_inputs = stack.enter_context(mock.patch.object(cli.compile_archinstall, "write_inputs"))
                    stack.enter_context(redirect_stdout(out))
                    status = cli.main(["install", "--config", str(path), "--yes", "--no-launch", "--out", tmp])
                self.assertEqual(status, expected, out.getvalue())
                self.assertEqual(write_inputs.called, expected == 0)
                if expected:
                    self.assertIn("system.keyboard: unknown keymap 'german'", out.getvalue())
                    self.assertIn("Not installing.", out.getvalue())

    def test_the_full_disk_minimum_is_the_free_space_one(self):
        # 32 GiB for everything Omarchy creates, the ESP included, which is
        # what a free-space install needs and what the wizard's picker asks.
        config, _ = parse(self.unattended_config(on_existing_data="wipe"))
        with self.on_machine(size=32 * 2**30):
            result = plan.make_plan(config, unattended=True)
        self.assertTrue(result.ok, [str(i) for i in result.issues])
        with self.on_machine(size=32 * 2**30 - 2**20):
            result = plan.make_plan(config, unattended=True)
        self.assertIn("disk.target: /dev/sda is 31 GiB; Omarchy needs at least 32 GiB, its own 2 GiB ESP included",
                      str(result.errors[0]))

    def test_knobs_this_iso_can_not_install_yet(self):
        for extra in ({"desktop": {"theme": "nord"}}, {"packages": {"extra": ["firefox"]}}):
            data = self.unattended_config(on_existing_data="wipe")
            data.update(extra)
            self.assertIn("not supported by this ISO yet", str(self.make(data).errors[0]))


class ConfirmTest(unittest.TestCase):
    """Interactive installs confirm each disk before anything is written to it."""

    def test_a_blank_disk_is_offered_for_use_not_erasure(self):
        blank = plan.DiskPlan("/dev/sdb", "sha256:" + "0" * 64, has_signatures=False)
        with mock.patch.object(cli, "_gum", return_value=mock.Mock(returncode=0)) as gum:
            self.assertTrue(cli._confirm_disk(blank))
        self.assertEqual(gum.call_args.args, ("confirm", "--affirmative", "Yes, use it", "--negative", "No",
                                              "/dev/sdb is blank. Use it?"))


if __name__ == "__main__":
    unittest.main()
