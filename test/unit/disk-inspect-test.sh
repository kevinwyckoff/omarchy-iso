#!/bin/bash
#
# The disk picker and the last-chance wipe summary, from lsblk JSON fixtures.
# What this pins: identical drives never render identically, the picker never
# offers the install medium, a cidata drive, or an eMMC's boot/RPMB areas, and
# the summary says what dies, what survives and what gets created, inside the
# width it was given.
#
# No mounting and no real disks: DISK_INSPECT_PROBE=0 turns the content probes
# off and the probe answers are filled in by hand where a test needs them.
# test/unit/disk-inspect-probe-test.sh covers the probes against real
# filesystems.

set -uo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
LIB="$ROOT/configs/airootfs/usr/share/omarchy-iso/disk-inspect.sh"

if ! command -v jq >/dev/null 2>&1; then
  echo "SKIP: jq is not installed"
  exit 0
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

export DISK_INSPECT_LSBLK_JSON="$ROOT/test/unit/fixtures/lsblk-mixed.json"
export DISK_INSPECT_PROBE=0
export DISK_INSPECT_INSTALL_MEDIUM=/dev/sda
export DISK_INSPECT_BY_ID_DIR="$WORK/by-id"
export DISK_INSPECT_MEMINFO="$WORK/meminfo"
# Widths are counted in characters; the summary uses "—", "→" and "…".
export LC_ALL=C.UTF-8

mkdir -p "$DISK_INSPECT_BY_ID_DIR"
ln -s /dev/nvme1n1 "$DISK_INSPECT_BY_ID_DIR/nvme-eui.002538b111b1c2d4"
ln -s /dev/nvme1n1 "$DISK_INSPECT_BY_ID_DIR/nvme-Samsung_SSD_980_PRO_500GB_S69ENX0T812345"
ln -s /dev/nvme1n1p3 "$DISK_INSPECT_BY_ID_DIR/nvme-Samsung_SSD_980_PRO_500GB_S69ENX0T812345-part3"
ln -s /dev/nvme0n1 "$DISK_INSPECT_BY_ID_DIR/nvme-eui.002538b111b1c2d3"
echo "MemTotal:       16318092 kB" >"$DISK_INSPECT_MEMINFO"

# shellcheck source=../../configs/airootfs/usr/share/omarchy-iso/disk-inspect.sh
source "$LIB"

failures=0

check() {
  local label="$1" expected="$2" actual="$3"
  if [[ $expected == "$actual" ]]; then
    printf '  ok   %s\n' "$label"
  else
    printf '  FAIL %s\n    expected: %s\n    actual:   %s\n' "$label" "$expected" "$actual"
    failures=$((failures + 1))
  fi
}

check_contains() {
  local label="$1" needle="$2" haystack="$3"
  if [[ $haystack == *"$needle"* ]]; then
    printf '  ok   %s\n' "$label"
  else
    printf '  FAIL %s\n    missing: %s\n    in:\n%s\n' "$label" "$needle" "$haystack"
    failures=$((failures + 1))
  fi
}

check_absent() {
  local label="$1" needle="$2" haystack="$3"
  if [[ $haystack != *"$needle"* ]]; then
    printf '  ok   %s\n' "$label"
  else
    printf '  FAIL %s\n    unexpected: %s\n' "$label" "$needle"
    failures=$((failures + 1))
  fi
}

check_width() {
  local label="$1" width="$2" text="$3" line longest=0
  while IFS= read -r line; do
    (( ${#line} > longest )) && longest=${#line}
  done <<<"$text"
  if (( longest <= width )); then
    printf '  ok   %s\n' "$label"
  else
    printf '  FAIL %s: longest line is %s columns, limit %s\n' "$label" "$longest" "$width"
    failures=$((failures + 1))
  fi
}

disk_inventory_refresh

# What the content probes would have found on the Windows drive.
disk_contents[/dev/nvme1n1p1]="EFI boot files · Windows Boot Manager"
disk_contents[/dev/nvme1n1p3]="Windows · ~212 GiB used"

echo "==> human sizes"
check "whole GiB drops the decimal" "2 GiB" "$(human_size 2147483648)"
check "fractional GiB keeps one decimal" "465.8 GiB" "$(human_size 500107862016)"
check "MiB are whole" "260 MiB" "$(human_size 272629760)"
check "TiB" "1.8 TiB" "$(human_size 2000398934016)"

echo "==> which disks the picker offers"
check "not the install medium, cidata, eMMC boot/RPMB areas or zram" \
  "/dev/sdc /dev/mmcblk0 /dev/vda /dev/nvme0n1 /dev/nvme1n1" "$(installable_disks | xargs)"
disk_is_cidata /dev/sdb && cidata=yes || cidata=no
check "a CIDATA partition label marks the whole drive" "yes" "$cidata"

echo "==> picker lines"
disk_probe /dev/nvme0n1
disk_probe /dev/nvme1n1
line0=$(disk_picker_line /dev/nvme0n1)
line1=$(disk_picker_line /dev/nvme1n1)
check "Windows drive" \
  "/dev/nvme1n1  465.8 GiB  Windows · S/N S69ENX0T812345 · Samsung SSD 980 PRO 500GB" "$line1"
check "encrypted drive" \
  "/dev/nvme0n1  465.8 GiB  LUKS2 encrypted · S/N S69ENX0T899999 · Samsung SSD 980 PRO 500GB" "$line0"
check "identical models still render differently once the device path is set aside" \
  "different" "$([[ ${line0#* } != "${line1#* }" ]] && echo different || echo same)"
check "a virtio disk's PCI vendor ID is not shown as its name" \
  "/dev/vda  40 GiB  empty · VirtIO disk" "$(disk_picker_line /dev/vda)"
check "a drive below ESP + 32GiB is flagged, and one with nothing notable keeps its count" \
  "/dev/sdc  16 GiB  too small · 1 partition · S/N SMALL0001 · SSD 16GB" "$(disk_picker_line /dev/sdc)"
check_width "everything but the model fits an 80-column console" 78 \
  "$(for d in $(installable_disks); do line=$(disk_picker_line "$d"); echo "${line% · *}"; done)"
check "SD/eMMC with no model gets a readable name and keeps its serial" \
  "/dev/mmcblk0  58.2 GiB  empty · S/N 0x1b2c3d4e · SD/eMMC storage" "$(disk_picker_line /dev/mmcblk0)"
check "libata's generic ATA vendor is dropped" "SSD 16GB" "$(disk_model /dev/sdc)"

echo "==> busy partitions"
check "active swap is reported" $'/dev/sdc1\t[SWAP]' "$(disk_busy_partitions /dev/sdc)"
check "an idle drive has none" "" "$(disk_busy_partitions /dev/nvme1n1)"

echo "==> identity"
check "by-id prefers the model/serial name over eui." \
  "nvme-Samsung_SSD_980_PRO_500GB_S69ENX0T812345" "$(disk_by_id /dev/nvme1n1)"
check "by-id falls back to eui. when that is all there is" \
  "nvme-eui.002538b111b1c2d3" "$(disk_by_id /dev/nvme0n1)"

echo "==> full-disk wipe summary"
summary=$(render_wipe_summary /dev/nvme1n1 full_disk true 80)
check_contains "headline" "THIS WILL ERASE A DISK" "$summary"
check_contains "target model, size and transport" "Target   Samsung SSD 980 PRO 500GB · 465.8 GiB · NVMe" "$summary"
check_contains "target serial" "serial S69ENX0T812345" "$summary"
check_contains "target by-id" "/dev/nvme1n1  (by-id: nvme-Samsung_SSD_980_PRO_500GB_S69ENX0T812345)" "$summary"
check_contains "ESP row" "   1  260 MiB    vfat         SYSTEM     EFI boot files · Windows Boot Manager" "$summary"
check_contains "reserved partition row" "   2  16 MiB     —            —          Microsoft reserved" "$summary"
check_contains "Windows row" "   3  464.9 GiB  ntfs         Windows    Windows · ~212 GiB used" "$summary"
check_contains "recovery row" "   4  650 MiB    ntfs         WinRE      Windows recovery" "$summary"
check_contains "the other identical drive is listed as untouched" \
  "/dev/nvme0n1  Samsung SSD 980 PRO 500GB · S69ENX0T899999 · LUKS2 encrypted"$'\n' "$summary"
check_contains "the install medium is listed and labelled" "/dev/sda      SanDisk Ultra · 28.6 GiB · install medium" "$summary"
check_contains "the cidata drive is listed and labelled" "/dev/sdb      Generic Flash Drive · 7.5 GiB · cidata drive" "$summary"
check_absent "zram is not a drive" "/dev/zram0" "$summary"
check_absent "eMMC boot areas are not drives" "mmcblk0boot0" "$summary"
check_contains "encrypted root" "LUKS2 → btrfs   /  (@, @home, @log, @pkg)" "$summary"
check_contains "ESP is created at /boot" "   1  2 GiB      vfat            /boot" "$summary"
check_contains "root gets the rest, less the ESP and GPT slack" "   2  463.8 GiB" "$summary"
check_contains "swap matches RAM" "with zram and a 15.6 GiB hibernation swapfile" "$summary"
check_width "fits 80 columns" 80 "$summary"
check_width "fits 64 columns" 64 "$(render_wipe_summary /dev/nvme1n1 full_disk true 64)"

plain=$(render_wipe_summary /dev/nvme1n1 full_disk false 80)
check_absent "unencrypted root has no LUKS" "LUKS2 →" "$plain"
check_contains "unencrypted root is btrfs" "btrfs           /  (@, @home, @log, @pkg)" "$plain"

blank=$(render_wipe_summary /dev/vda full_disk true 80)
check_contains "a blank drive says so" $'What dies:\n   Nothing. The disk is blank.' "$blank"

echo "==> free-space summary"
gib=$((1024 * 1024 * 1024))
free=$(render_wipe_summary /dev/nvme1n1 free_space false 80 $((400 * gib)) $((402 * gib)) $((402 * gib + 1048576)) $((460 * gib)))
check_contains "headline" "OMARCHY WILL USE FREE SPACE ON THIS DISK" "$free"
check_contains "nothing is erased" "Nothing is erased." "$free"
check_absent "no 'What dies' in free-space mode" "What dies" "$free"
check_contains "existing partitions are listed as kept" "Kept on this disk:" "$free"
check_contains "unencrypted free-space ESP mounts at /efi" $'\n      2 GiB      vfat            /efi' "$free"
check_contains "root fills the free region" $'\n      58 GiB' "$free"
check_absent "the new ESP doesn't take a kept partition's number" "   1  2 GiB" "$free"
check_absent "the new root doesn't take a kept partition's number" "   2  58 GiB" "$free"

echo
if (( failures > 0 )); then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all checks passed"
