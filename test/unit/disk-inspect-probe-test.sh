#!/bin/bash
#
# The wipe summary's content probes against real filesystems on a loop
# device. What this pins: each probe names what is on a partition (Windows,
# a Linux distribution, the boot loaders on an ESP, LUKS, BitLocker), leaves a
# partition that is already mounted alone, and never writes a byte: the image
# hashes the same before and after.
#
# Needs root, loop devices and the mkfs tools, so it skips anywhere else. Run
# it with sudo, or in a privileged Arch container with /dev shared:
#
#   docker run --rm --privileged -v /dev:/dev -v "$PWD:/src:ro" archlinux/archlinux \
#     bash -c 'pacman -Sy --noconfirm jq dosfstools ntfsprogs btrfs-progs cryptsetup parted &&
#              bash /src/test/unit/disk-inspect-probe-test.sh'

set -uo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
LIB="$ROOT/configs/airootfs/usr/share/omarchy-iso/disk-inspect.sh"

skip() {
  echo "SKIP: $1"
  exit 0
}

(( EUID == 0 )) || skip "needs root for loop devices and read-only mounts"
for tool in jq losetup parted mkfs.vfat mkfs.ext4 mkfs.btrfs mkntfs cryptsetup blkid sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || skip "$tool is not installed"
done

export LC_ALL=C.UTF-8

WORK=$(mktemp -d)
IMG="$WORK/disk.img"
MNT="$WORK/mnt"
LOOP=""
mkdir -p "$MNT"

cleanup() {
  umount "$MNT" 2>/dev/null
  [[ -n $LOOP ]] && losetup -d "$LOOP" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

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

check_match() {
  local label="$1" pattern="$2" actual="$3"
  if [[ $actual =~ $pattern ]]; then
    printf '  ok   %s\n' "$label"
  else
    printf '  FAIL %s\n    pattern: %s\n    actual:  %s\n' "$label" "$pattern" "$actual"
    failures=$((failures + 1))
  fi
}

# Eight partitions, one of each kind the probes tell apart.
truncate -s 1600M "$IMG"
parted --script "$IMG" mklabel gpt \
  mkpart esp fat32 1MiB 101MiB set 1 esp on \
  mkpart windows ntfs 101MiB 301MiB \
  mkpart ubuntu ext4 301MiB 501MiB \
  mkpart arch btrfs 501MiB 801MiB \
  mkpart vault 801MiB 1001MiB \
  mkpart bitlocker 1001MiB 1101MiB \
  mkpart busy ext4 1101MiB 1301MiB \
  mkpart data fat32 1301MiB 1501MiB

LOOP=$(losetup -fP --show "$IMG") || skip "could not attach a loop device"
for n in 1 2 3 4 5 6 7 8; do
  for _ in $(seq 1 20); do
    [[ -b ${LOOP}p$n ]] && break
    sleep 0.2
  done
  [[ -b ${LOOP}p$n ]] || skip "partition ${LOOP}p$n never appeared"
done

populate() {
  local part="$1" fstype="$2"
  shift 2
  mount -t "$fstype" "$part" "$MNT" || return 1
  "$@"
  umount "$MNT"
}

mkfs.vfat -F32 -n SYSTEM "${LOOP}p1" >/dev/null
populate "${LOOP}p1" vfat bash -c "mkdir -p '$MNT/EFI/Microsoft/Boot' '$MNT/EFI/limine' '$MNT/EFI/arch' && touch '$MNT/EFI/arch/grubx64.efi'"

mkntfs -Q -F -L Windows "${LOOP}p2" >/dev/null 2>&1
ntfs_ok=true
populate "${LOOP}p2" ntfs3 bash -c "mkdir -p '$MNT/Windows/System32'" 2>/dev/null ||
  populate "${LOOP}p2" ntfs-3g bash -c "mkdir -p '$MNT/Windows/System32'" 2>/dev/null ||
  ntfs_ok=false

mkfs.ext4 -q -L ubuntu "${LOOP}p3"
populate "${LOOP}p3" ext4 bash -c "mkdir -p '$MNT/etc' && printf 'NAME=Ubuntu\nPRETTY_NAME=\"Ubuntu 24.04.3 LTS\"\n' >'$MNT/etc/os-release'"

mkfs.btrfs -q -L arch "${LOOP}p4"
populate "${LOOP}p4" btrfs bash -c "btrfs -q subvolume create '$MNT/@' && mkdir -p '$MNT/@/etc' && printf 'NAME=\"Arch Linux\"\nPRETTY_NAME=\"Arch Linux\"\n' >'$MNT/@/etc/os-release'"

printf 'kitchen' | cryptsetup luksFormat --type luks2 --batch-mode --pbkdf pbkdf2 --pbkdf-force-iterations 1000 \
  --label vault "${LOOP}p5" - >/dev/null 2>&1

# BitLocker's signature, and nothing else: the probe must go by the bytes.
printf -- '-FVE-FS-' | dd of="${LOOP}p6" bs=1 seek=3 conv=notrunc status=none

mkfs.ext4 -q -L busy "${LOOP}p7"
mkfs.vfat -F32 -n DATA "${LOOP}p8" >/dev/null
populate "${LOOP}p8" vfat bash -c "echo hello >'$MNT/notes.txt'"

sync
blockdev --flushbufs "$LOOP" 2>/dev/null

# The inventory the probes read, built from blkid so the test needs no udev.
# p7 is mounted before probing and reported as such, as lsblk would.
mount -o ro "${LOOP}p7" "$MNT"
inventory="$WORK/lsblk.json"
{
  printf '{"blockdevices":[{"name":"%s","path":"%s","type":"loop","size":%s,"children":[' \
    "${LOOP##*/}" "$LOOP" "$(blockdev --getsize64 "$LOOP")"
  for n in 1 2 3 4 5 6 7 8; do
    part="${LOOP}p$n"
    mounts='[null]'
    [[ $n == 7 ]] && mounts="[\"$MNT\"]"
    ptname=""
    [[ $n == 1 ]] && ptname="EFI System"
    jq -cn --arg path "$part" --arg fstype "$(blkid -p -o value -s TYPE "$part")" \
      --arg fsver "$(blkid -p -o value -s VERSION "$part")" --arg label "$(blkid -p -o value -s LABEL "$part")" \
      --arg ptname "$ptname" --argjson n "$n" --argjson size "$(blockdev --getsize64 "$part")" --argjson mounts "$mounts" \
      'def nullable: if . == "" then null else . end;
       {name: ($path | ltrimstr("/dev/")), path: $path, type: "part", size: $size, partn: $n,
        fstype: ($fstype | nullable), fsver: ($fsver | nullable),
        label: ($label | nullable), parttypename: ($ptname | nullable), mountpoints: $mounts}'
    (( n < 8 )) && printf ','
  done
  printf ']}]}\n'
} >"$inventory"
export DISK_INSPECT_LSBLK_JSON="$inventory"

# shellcheck source=../../configs/airootfs/usr/share/omarchy-iso/disk-inspect.sh
source "$LIB"
disk_inventory_refresh

# Hash every partition except the mounted one (its superblock records the
# mount), before and after probing.
hash_disk() {
  local n
  for n in 1 2 3 4 5 6 8; do
    sha256sum "${LOOP}p$n" | cut -d' ' -f1
  done
}
before=$(hash_disk)

disk_probe "$LOOP"

echo "==> what each partition holds"
check "ESP lists every loader it carries" "EFI boot files · Windows Boot Manager, Limine, GRUB" "${disk_contents[${LOOP}p1]}"
if $ntfs_ok; then
  check_match "NTFS with a Windows directory is Windows" '^Windows · ~[0-9.]+ [KMG]iB used$' "${disk_contents[${LOOP}p2]}"
else
  echo "  skip NTFS: no ntfs3 or ntfs-3g mount available here"
fi
check_match "ext4 names its distribution" '^Ubuntu 24\.04\.3 LTS · ~[0-9.]+ [KMG]iB used$' "${disk_contents[${LOOP}p3]}"
check_match "btrfs finds os-release inside @" '^Arch Linux · ~[0-9.]+ [KMG]iB used$' "${disk_contents[${LOOP}p4]}"
check "LUKS2 reports its version and label without opening" "LUKS2 encrypted · vault" "${disk_contents[${LOOP}p5]}"
check "BitLocker is found by its signature" "BitLocker on" "${disk_contents[${LOOP}p6]}"
check "a mounted partition is reported, not remounted" "mounted at $MNT" "${disk_contents[${LOOP}p7]}"
check_match "plain FAT is data" '^data · ~[0-9.]+ [KMG]iB used$' "${disk_contents[${LOOP}p8]}"
systems="Ubuntu 24.04.3 LTS, Arch Linux, LUKS2 encrypted, BitLocker on · 8 partitions"
$ntfs_ok && systems="Windows, $systems"
check "the disk summary names the systems on it" "$systems" "$(disk_contents_summary "$LOOP")"

echo "==> read-only"
check "probing wrote nothing to any partition" "$before" "$(hash_disk)"
check "probe mount points are cleaned up" "" "$(find /tmp -maxdepth 1 -name 'disk-inspect.*' 2>/dev/null)"
check "the mounted partition is still mounted where it was" "$MNT" "$(findmnt -rno TARGET "${LOOP}p7")"

echo
if (( failures > 0 )); then
  echo "$failures check(s) failed"
  exit 1
fi
echo "all checks passed"
