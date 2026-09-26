# Omarchy ISO

The Omarchy ISO is the only supported way to install Omarchy. It ships the Omarchy Configurator, installs Arch Linux, installs the Omarchy packages from the bundled mirror, runs target system setup in the chroot, creates the user, and runs `omarchy-setup-user` for that user.

## Downloading the latest ISO

See the ISO link on [omarchy.org](https://omarchy.org).

Every published ISO has a `.sha256` beside it at the same URL. Download both into the same directory and check the ISO before writing it to a USB stick:

```bash
sha256sum -c omarchy-3.0.iso.sha256
```

Corruption anywhere in the ISO is worth catching before the write, and corruption in the bundled package mirror is worth catching most: the mirror lives inside the ISO and the installer reads it straight off the medium, so those bytes surface minutes into the install as a pacman "invalid or corrupted package" error rather than as anything that names the download. Corruption elsewhere is louder and earlier — it stops the medium booting or mounting. There is a `.sig` beside the ISO too for anyone who wants to verify it against the Omarchy signing key.

## Creating the ISO

Run `./bin/omarchy-iso-make`; output goes into `./release`. By default the ISO uses the Omarchy packages and tracks the `quattro` branch, from the stable mirror. Pass `--edge` to use `omarchy-dev` and `omarchy-settings-dev` from the edge mirror.

For local development, build the ISO from sibling checkouts:

```bash
./bin/omarchy-iso-make --local-source ../omarchy-installer ../omarchy-pkgs
```

Despite the local folder name, the first argument is the Omarchy source checkout (runtime commands, configs, setup scripts, themes, shell, migrations). The installer itself lives in this ISO repo.

Use `--dev` or `--rc` to build against those package channels. Both `--dev` and `--edge` select the dev packages from the edge mirror.

## Autoinstall

The shipped ISO installs itself with no keyboard when it finds its configuration on a second drive. Attach a drive labeled `cidata` alongside the ISO and the installer copies the config off it and skips the configurator; with no such drive, nothing changes and the wizard runs as usual. No rebuild, no extra boot entry.

`cidata` is the cloud-init `NoCloud` label, so Proxmox, libvirt, and Packer already know how to attach one.

### install.toml

A drive carrying `install.toml` describes the whole install in one file, and takes precedence over the legacy files below. `chefs-kitchen` checks it, resolves it against the machine, and installs it:

```bash
chefs-kitchen validate --config install.toml         # schema and semantic checks, no hardware access
chefs-kitchen plan --config install.toml --yes       # resolve the disk and print what would be erased; touches nothing
chefs-kitchen install --config install.toml          # shows the wipe summary and asks before erasing
chefs-kitchen install --config install.toml --yes    # unattended: what the ISO runs for a cidata drive
```

```toml
schema = 1

[system]
hostname = "marvin"
timezone = "America/Toronto"
keyboard = "us"

[[users]]
name     = "kevin"
password = { file = "kevin.pass" }             # or password_hash = "$6$…" (openssl passwd -6)
ssh_authorized_keys = []

[disk]
target = { serial = "S69ENX0T812345" }         # or { by_id = "…" }, { wwn = "…" }, { path = "/dev/vda" }
on_existing_data = "abort"                     # "wipe" to erase a disk that has anything on it
# expect_fingerprint = "sha256:…"              # wipe only if the disk still looks like this

[encryption]
enabled    = true
passphrase = { same_as_user = "kevin" }        # or { file = "luks.pass" }, { prompt = true }
```

- **Secrets are never plain strings.** Passwords, passphrases and auth keys are `{ file = "…" }` (relative to `install.toml`), `{ prompt = true }` for interactive installs, or `{ insecure_plaintext = "…" }`, which warns every time. Unknown keys are errors, so a typo can't be silently ignored.
- **The disk is named by something stable.** Its serial, `/dev/disk/by-id` name or WWN. `{ path = … }` is fine in a VM.
- **Unattended installs never erase data by default.** With `--yes`, a disk with any signature on it stops the install unless `on_existing_data = "wipe"`. `chefs-kitchen plan` prints the disk's fingerprint for `expect_fingerprint`, which pins the wipe to exactly that layout.
- **An encrypted unattended install needs the passphrase as a file.** Either `passphrase = { file = … }`, or the user's password as a file with the default `same_as_user`.
- **The install is recorded.** `/etc/chefs-kitchen/install.toml` on the installed system holds the config it was installed from, with every secret removed.
- **Desktop and packages.** `[desktop] theme` (any theme the ISO bundles, like `"Tokyo Night"`) is applied during the install. `agent` (a name `omarchy default agent` accepts) installs at first login, since that needs a network. `[packages] extra` installs from the ISO's offline mirror, and `plan` refuses anything that isn't in it.
- **The wizard writes one too.** Its answers become `/root/install.toml`, and `chefs-kitchen` installs it, so every clicked install is also a described one. It pins `expect_fingerprint` to the disk as the user confirmed it.
- **From a USB drive.** Type `L` and Return on the installer's first screen to load `install.toml` from a USB drive instead of answering the wizard. The same wipe summary and typed confirmation follow.

### Configuration files

These are the configurator's own output files, so the way to get a starting set is to run one interactive install and copy what it wrote into `/root`.

| File | Required | Purpose |
|------|----------|---------|
| `user_configuration.json` | Yes | archinstall config: disk, hostname, timezone, keyboard |
| `user_credentials.json` | Yes | Username and password hash |
| `user_full_name.txt` | No | Git full name |
| `user_email_address.txt` | No | Git email |
| `user_encrypt_installation.txt` | No | Legacy, no longer needed: encryption is read from `user_configuration.json` (see below) |
| `authorized_keys` | No | SSH public keys in sshd's own format, one per line |
| `tailscale_authkey` | No | Tailscale auth key; the machine joins your tailnet on first boot |

Both required files must be present or the installer falls back to the configurator. Generate the password hash for `user_credentials.json` with `openssl passwd -6 "yourpassword"`.

Encryption is configured by the `disk_encryption` block inside `user_configuration.json` — which carries the passphrase in plaintext, so treat a drive built from an encrypted install accordingly. The installer takes whether the install is encrypted from that block (or, for a pre-mounted config, from `omarchy_install.storage.luks_uuid`), and that drives SDDM autologin and the final boot validation. Older drives that also carry `user_encrypt_installation.txt` still work: the file only decides for a pre-mounted config with no `luks_uuid`, and if it disagrees with the configuration the install log says so and the configuration wins.

`authorized_keys` is the same file sshd reads — copy your own or write one key per line:

```
ssh-ed25519 AAAA... you@host
```

When `authorized_keys` is present, autoinstall installs it as the user's `~/.ssh/authorized_keys`, enables `sshd`, and adds a `ufw allow ssh` rule — a stock Omarchy install ships openssh with the service disabled and its firewall opens neither port 22 nor anything else beyond LocalSend. Networking needs nothing extra; NetworkManager is already enabled with DHCP. Password SSH authentication is left at the distro default. An `authorized_keys` with no usable keys fails the install rather than producing a machine nobody can reach.

When `tailscale_authkey` is present (one key, blank lines and `#` comments ignored), the install adds the `tailscale` package from the ISO's bundled mirror — nothing is fetched from the network at install or boot — and stages the join for first boot: the key lands at `/etc/tailscale/authkey` (root-only), `tailscaled` is enabled, ufw allows traffic in on `tailscale0`, and a background unit runs `tailscale up` once the network is actually up, retrying until it succeeds without holding up the boot. After a successful join the key is deleted and the unit disables itself; until then both survive reboots, so a machine installed offline joins whenever it first gets connectivity. The node appears on the tailnet under the configured hostname. Use a reusable, pre-authorized (tagged) key so one drive image serves many machines — or an ephemeral key for disposable VMs.

### Building the drive

```bash
mkdir cidata
cp user_configuration.json user_credentials.json authorized_keys cidata/
genisoimage -output cidata.iso -volid cidata -joliet -rock cidata/
```

### Proxmox example

```bash
qm create 101 --name my-omarchy \
  --bios ovmf --machine q35 --cpu host --cores 4 --memory 8192 \
  --ostype l26 --scsihw virtio-scsi-single \
  --efidisk0 local-lvm:0,efitype=4m,pre-enrolled-keys=0 \
  --scsi0 local-lvm:40,discard=on,iothread=1 \
  --net0 virtio,bridge=vmbr0 --vga virtio --serial0 socket \
  --ide2 local:iso/omarchy.iso,media=cdrom \
  --ide3 local:iso/cidata.iso,media=cdrom \
  --boot order='scsi0;ide2'

qm start 101
```

Boot order is disk first: the empty disk falls through to the ISO on the first boot, and the installed system boots from disk afterwards. The machine reboots into Omarchy on its own when the install finishes.

Encrypted autoinstalls are not fully unattended — the LUKS passphrase prompt still needs someone at the first boot.

## Testing the ISO

Run `./bin/omarchy-iso-boot [release/omarchy.iso]`.

Run `./test/all` for the fast, VM-free tests under `test/unit/`, which cover cidata autoinstall loading and the orchestrator's phases without needing a built ISO.

To exercise installation alongside existing Windows-style partitions, run
`./bin/omarchy-iso-test-windows-disk [release/omarchy.iso]`. It creates a
synthetic disk in `/tmp` with an existing ESP and data partition plus ample
unallocated space, then offers to start an interactive installation on it. The
fixture exercises Windows partition preservation but does not contain Windows.

## Acceptance testing the ISO

Run `./bin/omarchy-iso-test [release/omarchy.iso]` to install the ISO into a headless VM by driving the real interactive install flow — the harness reads each screen via QMP screendumps + OCR and answers with virtual keystrokes, so the configurator wizard, install dashboard, reboot prompt, and SDDM login are all exercised exactly as a user would. It then boots the installed system, sends real VM keyboard shortcuts for the primary shell and window-management actions, and runs the in-guest acceptance suite (`test/acceptance` in the omarchy repo). The suite checks session and service health, the complete core-package manifest, user defaults, representative applications, menus, panels, live weather, launchers, visual selectors, notifications, clipboard, and other interactive shell behavior.

Visual checkpoints are saved as `success-<step>.png` or `failure-<step>.png` alongside the serial and install logs in `test-runs/<iso>/runs/<timestamp>/`. Independent test files and applications continue after a failure so one broken surface does not hide the rest of the report. The harness then stops the VM and opens the ordered screenshots in `imv` for quick visual review.

The harness syncs the acceptance suite from `$OMARCHY_PATH` when it is available. The install phase produces a reusable base image, so iterating against another checkout is fast:

```bash
./bin/omarchy-iso-test release/omarchy.iso --install-only        # once per ISO
./bin/omarchy-iso-test release/omarchy.iso --reuse-base \
  --sync-omarchy ../omarchy                                      # fast loop against local tests
```

Pass `--encrypt` to drive the encrypted install flow (including typing the LUKS passphrase at boot) instead of the unencrypted one. Pass `--no-preview` to collect the same visual artifacts without opening them in `imv` when the run finishes.

## Integration testing the ISO

Scenarios under `test/integration.d/` boot a real ISO install in QEMU and assert on what the running system actually does. The runner installs the ISO once — unattended, from a generated cidata drive — and saves the result as a reusable base image; every scenario then boots a throwaway overlay of that base with its own copy of the firmware vars, so neither disk nor NVRAM state leaks between runs. Shared machinery (VM lifecycle, QMP screendump + OCR console driving, virtual keystrokes, guest SSH, the cidata build) lives in `test/integration.d/base-test.sh`, so a new scenario is one file.

```bash
./test/integration release/omarchy.iso                   # install once, run all scenarios
./test/integration release/omarchy.iso --reuse-base      # fast loop against the saved base
./test/integration release/omarchy.iso factory-reset     # a single named scenario
```

The first scenario is `factory-reset`: it proves `omarchy-system-factory-reset` hands a machine on without destroying a shared ESP. The installed ESP gets a Windows entry with payload plus a second Linux cloned under a foreign machine-id with its own boot directory and UKIs; a real factory reset is then driven through a guest pty, and the harness asserts the foreign entries survive both the staged reset and first-boot provisioning, that the old Omarchy identity is fully retired, and that the machine reaches first-boot setup unattended.

Artifacts — screenshots, the fixtured/staged/final `limine.conf`, the reset typescript, and the factory-reset log — land under `test-runs/<iso>-integration/runs/<timestamp>-<scenario>/`, and `--no-preview` skips the `imv` review just like the acceptance harness.

## Signing the ISO

Run `./bin/omarchy-iso-sign [release/omarchy.iso]`. The signing key is retrieved from the shared Omarchy vault with the 1Password CLI.

## Uploading the ISO

Run `./bin/omarchy-iso-upload [release/omarchy.iso]`. This requires rclone configuration (`rclone config`). The `.sig` and `.sha256` sidecars go up with the ISO when they exist beside it.

## Full release of the ISO

Run `./bin/omarchy-iso-release VERSION` to create, test, sign, and upload the ISO in one flow. Add `--rc` to release an RC build instead.
