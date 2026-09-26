#!/bin/bash
#
# The wizard's answers become install.toml (wizard-toml.sh): the disk named by
# its most stable identifier, every string escaped as TOML, the password in a
# private file rather than the TOML, and the confirmed disk pinned by its
# fingerprint. Each file it writes must pass `chefs-kitchen validate`.

set -uo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
SHARE="$ROOT/configs/airootfs/usr/share/omarchy-iso"

command -v jq >/dev/null 2>&1 || { echo "SKIP: jq is not installed"; exit 0; }

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

export LC_ALL=C.UTF-8
export DISK_INSPECT_LSBLK_JSON="$ROOT/test/unit/fixtures/lsblk-mixed.json"
export DISK_INSPECT_BY_ID_DIR="$WORK/by-id"
export WIZARD_SECRETS_DIR="$WORK/secrets"
mkdir -p "$DISK_INSPECT_BY_ID_DIR"
ln -s /dev/sdc "$DISK_INSPECT_BY_ID_DIR/ata-SSD_16GB_SMALL0001"

source "$SHARE/disk-inspect.sh"
source "$SHARE/wizard-toml.sh"
disk_inventory_refresh

failures=0
check() {
  if [[ $2 == "$3" ]]; then
    printf '  ok   %s\n' "$1"
  else
    printf '  FAIL %s\n    expected: %s\n    actual:   %s\n' "$1" "$2" "$3"
    failures=$((failures + 1))
  fi
}
check_contains() {
  if [[ $3 == *"$2"* ]]; then printf '  ok   %s\n' "$1"; else printf '  FAIL %s: missing %s\n%s\n' "$1" "$2" "$3"; failures=$((failures + 1)); fi
}
check_absent() {
  if [[ $3 != *"$2"* ]]; then printf '  ok   %s\n' "$1"; else printf '  FAIL %s: unexpected %s\n' "$1" "$2"; failures=$((failures + 1)); fi
}

validate() {
  if python3 -c 'import tomllib' 2>/dev/null; then
    PYTHONPATH="$SHARE" python3 -m chefs_kitchen_config validate "$1" 2>&1 | tail -1
  else
    echo "valid (skipped: no Python 3.11+)"
  fi
}

answers() {
  keyboard="us" hostname="marvin" timezone="America/Toronto"
  username="kevin" password='hunter2 "quoted" \ back' full_name='Kevin "K" O'"'"'Brien' email_address="kevin@example.com"
  disk="/dev/nvme1n1" install_target="full_disk" encrypt_installation=true defer_provisioning=false
  disk_fingerprint="sha256:$(printf '%064d' 0)"
}

echo "==> naming the disk"
check "a unique serial" '{ serial = "S69ENX0T812345" }' "$(disk_selector_for /dev/nvme1n1)"
check "neither a serial nor a by-id name: the path" '{ path = "/dev/vda" }' "$(disk_selector_for /dev/vda)"
check "serial preferred over by-id" '{ serial = "SMALL0001" }' "$(disk_selector_for /dev/sdc)"

echo "==> a full-disk install"
answers
write_install_toml "$WORK/install.toml"
toml=$(cat "$WORK/install.toml")
check_contains "the disk is pinned" 'expect_fingerprint = "sha256:0000' "$toml"
check_contains "and may be erased, since the wizard confirmed it" 'on_existing_data = "wipe"' "$toml"
check_contains "names are escaped" 'full_name = "Kevin \"K\" O'"'"'Brien"' "$toml"
check_absent "the password is not in install.toml" "hunter2" "$toml"
check "the password is in a private file" "$password" "$(cat "$WIZARD_SECRETS_DIR/kevin.pass")"
check "which only root can read" "600" "$(stat -c %a "$WIZARD_SECRETS_DIR/kevin.pass")"
check "install.toml is private too" "600" "$(stat -c %a "$WORK/install.toml")"
check "it validates" "$WORK/install.toml: valid" "$(validate "$WORK/install.toml")"

echo "==> free space, unencrypted"
answers
install_target="free_space" encrypt_installation=false
write_install_toml "$WORK/free.toml"
toml=$(cat "$WORK/free.toml")
check_contains "free-space mode" 'mode = "free-space"' "$toml"
check_absent "nothing is erased, so no on_existing_data" "on_existing_data" "$toml"
check_contains "not encrypted" "enabled = false" "$toml"
check "it validates" "$WORK/free.toml: valid" "$(validate "$WORK/free.toml")"

echo "==> deferred provisioning"
answers
rm -rf "$WIZARD_SECRETS_DIR"
defer_provisioning=true username="" password="" full_name="" email_address=""
write_install_toml "$WORK/defer.toml"
toml=$(cat "$WORK/defer.toml")
check_absent "no user" "[[users]]" "$toml"
check_contains "defers provisioning" "defer = true" "$toml"
check "no password file" "no" "$([[ -e $WIZARD_SECRETS_DIR ]] && echo yes || echo no)"
check "it validates" "$WORK/defer.toml: valid" "$(validate "$WORK/defer.toml")"

echo
if (( failures )); then echo "$failures check(s) failed"; exit 1; fi
echo "all checks passed"
