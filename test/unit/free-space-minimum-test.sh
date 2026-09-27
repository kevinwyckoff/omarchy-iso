#!/bin/bash
#
# A free-space install needs 32GiB for everything Omarchy creates, its own 2GiB
# ESP included, so a gap of exactly 32GiB must be accepted and one MiB less
# refused. The regression this pins: parted reports a free region's end as the
# region's last byte, and rounding that down to a MiB boundary as if it were
# the first byte after the region lost the region's last MiB. A gap of exactly
# 32GiB, what shrinking Windows by 32768MB leaves, was refused.
#
# It also pins the layout inside the region. parted takes a partition's end as
# its last byte too, so an ESP ended on the first byte after its 2GiB came out
# a sector over, and root, aligned up past that sector, started a MiB late and
# came out a MiB short. The ESP must be exactly 2GiB, root must start on the
# byte after it, and root must fill the rest of the region.
#
# Runs the configurator's run_partition_decide against real parted on sparse
# image files, with the screens and prompts around the analysis stubbed, then
# creates the ESP and root it laid out, to show parted takes them.
#
# parted operates on image files directly, so this needs no root, no loop
# devices, and no real disk.

set -uo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
CONFIGURATOR="$ROOT/configs/airootfs/root/configurator"
LIB="$ROOT/configs/airootfs/usr/share/omarchy-iso/disk-partitioning.sh"

if ! command -v parted >/dev/null 2>&1; then
  echo "SKIP: parted is not installed"
  exit 0
fi

# shellcheck source=../configs/airootfs/usr/share/omarchy-iso/disk-partitioning.sh
source "$LIB"
# The function alone, the way test_kernel_selection.py takes detect_kernel.
eval "$(sed -n '/^run_partition_decide() {$/,/^}$/p' "$CONFIGURATOR")"
if ! declare -F run_partition_decide >/dev/null; then
  echo "FAIL: no run_partition_decide in $CONFIGURATOR"
  exit 1
fi

# Everything around the analysis. The confirm prompts answer "No", so the
# function returns once it has laid the partitions out, and anything else it
# calls fails the same way.
command_not_found_handle() { return 1; }
gum() { return 1; }
step() { :; }
say() { :; }
clear_logo() { :; }
detect_bitlocker() { :; }
detect_windows_esp() { :; }
detect_kernel() { echo linux; }
partprobe() { :; }
udevadm() { :; }
sleep() { :; }
not_enough_space() { refused_with=${1:-"(no size)"}; return 1; }
# lsblk can't read an image file; parted can. An empty PTTYPE is a blank disk.
lsblk() {
  case $* in
    *PTTYPE*) [[ $blank == true ]] || echo gpt ;;
    *SIZE*) stat -c %s "$disk" ;;
  esac
}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

MIB=$((1024 * 1024))
GIB=$((1024 * MIB))
failures=0

check() {
  local label="$1" expected="$2" actual="$3"
  if [[ $expected == "$actual" ]]; then
    printf '  ok   %s\n' "$label"
  else
    printf '  FAIL %s: expected %s, got %s\n' "$label" "$expected" "$actual"
    failures=$((failures + 1))
  fi
}

# A partition's start and end byte, as parted reports them.
part_bounds() {
  parted -ms "$disk" unit B print | awk -F: -v n="$1" '$1 == n { gsub(/B/, ""); print $2, $3 }'
}

# A partition's size in 512-byte sectors, as parted reports it.
part_sectors() {
  parted -ms "$disk" unit s print | awk -F: -v n="$1" '$1 == n { gsub(/s/, "", $4); print $4 }'
}

# The ESP is exactly 2GiB, root starts on the byte after it, and root is
# <root_bytes>, ending on a MiB boundary.
check_layout() {
  local esp="$1" root="$2" root_bytes="$3" esp_last root_first root_last
  read -r _ esp_last <<<"$(part_bounds "$esp")"
  read -r root_first root_last <<<"$(part_bounds "$root")"
  check "the ESP is exactly 2GiB" "$((2 * GIB / 512)) sectors" "$(part_sectors "$esp") sectors"
  check "root starts on the byte after the ESP" "$((esp_last + 1))" "$root_first"
  check "root ends on a MiB boundary" "0" "$(((root_last + 1) % MIB))"
  check "root is $((root_bytes / MIB))MiB" "$root_bytes" "$((root_last + 1 - root_first))"
}

decide() {
  refused_with=""
  EFI_START_B="" EFI_END_B="" ROOT_START_B="" ROOT_END_B=""
  run_partition_decide >/dev/null 2>&1
}

# Create what run_partition_decide laid out, as run_partition_execute does.
create_layout() {
  created_parts=()
  [[ $needs_mklabel == true ]] && parted --script "$disk" mklabel gpt
  create_partition "$disk" "$EFI_START_B" "$EFI_END_B" fat32 OMARCHY_EFI &&
    create_partition "$disk" "$ROOT_START_B" "$ROOT_END_B" btrfs OMARCHY_ROOT
}

# A Windows disk: ESP, MSR, C: shrunk by <gap> MiB, then WinRE after the gap.
# What is left at the end of the disk is smaller than the gap.
windows_disk() {
  local gap_mib="$1" c_end=40277
  disk="$WORK/windows.img" blank=false
  rm -f "$disk"
  truncate -s 100G "$disk"
  parted --script "$disk" -- mklabel gpt \
    mkpart ESP fat32 1MiB 261MiB \
    mkpart MSR 261MiB 277MiB \
    mkpart C ntfs 277MiB ${c_end}MiB \
    mkpart WinRE ntfs $((c_end + gap_mib))MiB $((c_end + gap_mib + 650))MiB
  gap_start=$((c_end * MIB))
  gap_end=$(((c_end + gap_mib) * MIB - 1))
}

echo "==> a gap of exactly 32GiB between Windows partitions"
windows_disk 32768
winre_before=$(part_bounds 4)
decide
check "accepted" "" "$refused_with"
check "the ESP starts where the gap does" "$gap_start" "$EFI_START_B"
check "root ends on the gap's last byte" "$gap_end" "$ROOT_END_B"
if create_layout; then
  check "the ESP is the partition parted added" "$EFI_START_B" "$(part_bounds 5 | cut -d' ' -f1)"
  check "root fills the gap to its last byte" "$ROOT_START_B $gap_end" "$(part_bounds 6)"
  check "WinRE is untouched" "$winre_before" "$(part_bounds 4)"
  check_layout 5 6 $((30 * GIB))
else
  check "parted creates the ESP and root" "created" "refused"
fi

echo "==> a gap of 32GiB less 1MiB"
windows_disk 32767
decide
check "refused, counting the whole gap" "$((32767 * MIB))" "$refused_with"

# A blank disk gets a new GPT, with 1MiB before the ESP and 1MiB after root
# for the backup table: 32GiB and 2MiB is the smallest that fits.
echo "==> a blank disk of 32GiB and 2MiB"
disk="$WORK/blank.img" blank=true
rm -f "$disk"
truncate -s $((32 * GIB + 2 * MIB)) "$disk"
decide
check "accepted" "" "$refused_with"
check "a new GPT" "true" "$needs_mklabel"
if create_layout; then
  check "root ends 1MiB before the end of the disk" "$((32 * GIB + MIB - 1))" "$(part_bounds 2 | cut -d' ' -f2)"
  check_layout 1 2 $((30 * GIB))
else
  check "parted creates the ESP and root" "created" "refused"
fi

echo "==> a blank disk of 32GiB and 1MiB"
rm -f "$disk"
truncate -s $((32 * GIB + MIB)) "$disk"
decide
check "refused" "$((32 * GIB - MIB))" "$refused_with"

if ((failures)); then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all free-space minimum checks passed"
