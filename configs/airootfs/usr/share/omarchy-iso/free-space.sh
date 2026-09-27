# Free-space installs: find the largest unallocated region on a disk, then
# partition, format and mount it for the orchestrator's pre-mounted
# (protected) mode. Shared by the configurator, which decides and confirms,
# and chefs-kitchen, which executes, so a wizard install and an install.toml
# with mode = "free-space" use the same region and the same layout.
#
# Sourced, not executed, after disk-partitioning.sh. Omarchy always creates
# its own ESP in the free space and never adopts an existing (Windows) one:
# Windows Update or repair can reclaim or reformat it, the usual 100-260MiB
# Windows ESP is far too small for Omarchy's Unified Kernel Images, and a
# shared ESP would force the install unencrypted.

FREE_SPACE_ALIGN_B=$((1024 * 1024))
FREE_SPACE_EFI_B=$((2 * 1024 * 1024 * 1024))
FREE_SPACE_MIN_INSTALL_B=$((32 * 1024 * 1024 * 1024))

# free_space_region <disk>
#
# Prints "<needs_mklabel> <efi_start> <efi_end> <root_start> <root_end>" in
# bytes for the largest free region, aligned to 1MiB; <root_end> is root's last
# byte, which is how parted takes a partition's end. When there is no region
# big enough for 32GiB, the ESP included, prints the usable bytes it did find
# and returns 1. An unlabeled disk has no partition table for parted to scan,
# so its whole surface is the region, less a MiB at each end for the GPT, and
# needs_mklabel is true.
free_space_region() {
  local disk="$1" pt_type size free needs_mklabel=false
  local free_start free_end free_size efi_start efi_end root_start root_end install_max

  pt_type=$(lsblk -dno PTTYPE "$disk" 2>/dev/null)
  if [[ -z $pt_type ]]; then
    size=$(lsblk -bdno SIZE "$disk" 2>/dev/null)
    if [[ -n $size ]] && (( size > 0 )); then
      free_start=$FREE_SPACE_ALIGN_B
      free_end=$(( (size - FREE_SPACE_ALIGN_B) / FREE_SPACE_ALIGN_B * FREE_SPACE_ALIGN_B - 1 ))
      free="$free_start $free_end $((free_end - free_start + 1))"
      needs_mklabel=true
    fi
  else
    # -s (script mode) keeps parted from prompting on stderr for warnings
    # like a stale GPT backup; without it the caller hangs invisibly.
    free=$(parted -ms "$disk" unit B print free </dev/null 2>/dev/null | awk -F: '
      $NF ~ /free/ {
        start = $2; end = $3; size = $4
        gsub(/B/, "", start); gsub(/B/, "", end); gsub(/B/, "", size)
        if (size + 0 > max_size + 0) { max_size = size; max_start = start; max_end = end }
      }
      END { if (max_size + 0 > 0) printf "%s %s %s\n", max_start, max_end, max_size }')
  fi

  if [[ -z $free ]]; then
    echo 0
    return 1
  fi
  read -r free_start free_end free_size <<<"$free"

  # A region's end, like parted reports it, is its last byte. Root ends on
  # the last byte before the region's last MiB boundary, so a gap of exactly
  # 32GiB holds 32GiB.
  efi_start=$(( (free_start + FREE_SPACE_ALIGN_B - 1) / FREE_SPACE_ALIGN_B * FREE_SPACE_ALIGN_B ))
  efi_end=$((efi_start + FREE_SPACE_EFI_B))
  root_start=$(( (efi_end + 1 + FREE_SPACE_ALIGN_B - 1) / FREE_SPACE_ALIGN_B * FREE_SPACE_ALIGN_B ))
  root_end=$(( (free_end + 1) / FREE_SPACE_ALIGN_B * FREE_SPACE_ALIGN_B - 1 ))

  if (( root_end <= root_start )); then
    echo "$free_size"
    return 1
  fi
  install_max=$((root_end + 1 - efi_start))
  if (( install_max < FREE_SPACE_MIN_INSTALL_B )); then
    echo "$install_max"
    return 1
  fi

  echo "$needs_mklabel $efi_start $efi_end $root_start $root_end"
}

# Undo a half-built layout when run by chefs-kitchen. The configurator
# defines its own, which also shows the error on screen.
if ! declare -F disk_abort_hook >/dev/null; then
  disk_abort_hook() {
    umount -R /mnt 2>/dev/null || true
    umount /mnt/btrfs-root 2>/dev/null || true
    rmdir /mnt/btrfs-root 2>/dev/null || true
    cryptsetup close omarchy_root 2>/dev/null || true
    rollback_created_parts "$FREE_SPACE_DISK"
    echo "Error: $1" >&2
    exit 1
  }
fi

# free_space_partition <disk> <encrypt> <needs_mklabel> <efi_start> <efi_end> <root_start> <root_end> <result_file>
#
# Create the ESP and root partitions in the region free_space_region found,
# LUKS2 on root when <encrypt> is true (the passphrase on stdin, never argv),
# btrfs with the @, @home, @log and @pkg subvolumes, all mounted under /mnt.
# Writes what the orchestrator needs to <result_file> as shell-quoted
# key=value lines: efi_dev, root_device, root_mapper, luks_uuid, esp_mount.
free_space_partition() {
  local disk="$1" encrypt="$2" needs_mklabel="$3"
  local efi_start="$4" efi_end="$5" root_start="$6" root_end="$7" result="$8"
  local passphrase="" efi_num root_num efi_dev root_dev root_mapper esp_mount luks_uuid="" subvol

  FREE_SPACE_DISK=$disk
  if [[ $encrypt == "true" ]]; then
    IFS= read -r passphrase || true
    [[ -n $passphrase ]] || disk_abort_hook "No LUKS passphrase on stdin"
    esp_mount=/boot
  else
    # Unencrypted, the kernel can load from the root, and the ESP mounts at
    # /efi, the Freedesktop default for a separate ESP.
    esp_mount=/efi
  fi

  echo "Creating partitions on $disk"
  if [[ $needs_mklabel == "true" ]]; then
    disk_step "initializing GPT on $disk" parted --script "$disk" mklabel gpt
    partprobe "$disk" 2>/dev/null || true
    sleep 1
  fi

  # create_partition reports the number parted actually used; nothing here
  # may assume it. It sets created_partition_number rather than printing so
  # the rollback bookkeeping survives (a $(...) would subshell it away).
  create_partition "$disk" "$efi_start" "$efi_end" fat32 OMARCHY_EFI ||
    disk_abort_hook "Could not create the EFI partition on $disk"
  efi_num="$created_partition_number"
  create_partition "$disk" "$root_start" "$root_end" btrfs OMARCHY_ROOT ||
    disk_abort_hook "Could not create the root partition on $disk"
  root_num="$created_partition_number"

  disk_step "flagging partition $efi_num as ESP" parted --script "$disk" set "$efi_num" esp on

  efi_dev=$(partition_path "$disk" "$efi_num")
  root_dev=$(partition_path "$disk" "$root_num")
  partprobe "$disk"
  sync
  sleep 2
  wait_for_device "$efi_dev" || disk_abort_hook "EFI partition $efi_dev never appeared"
  wait_for_device "$root_dev" || disk_abort_hook "Root partition $root_dev never appeared"

  # Both partitions are new, but the space they occupy may carry signatures
  # from whatever was deleted to free it.
  disk_step "clearing stale signatures on $efi_dev" wipefs -af "$efi_dev"
  disk_step "clearing stale signatures on $root_dev" wipefs -af "$root_dev"

  if [[ $encrypt == "true" ]]; then
    echo "Setting up LUKS2 on $root_dev"
    # Pipes rather than disk_step: the passphrase must not become an argv the
    # process table can show. Folding stderr into stdout still logs errors.
    printf "%s" "$passphrase" | cryptsetup luksFormat --type luks2 --batch-mode "$root_dev" - 2>&1 ||
      disk_abort_hook "Formatting LUKS2 on $root_dev failed"
    printf "%s" "$passphrase" | cryptsetup open "$root_dev" omarchy_root - 2>&1 ||
      disk_abort_hook "Opening the encrypted root on $root_dev failed"
    root_mapper=/dev/mapper/omarchy_root
    luks_uuid=$(blkid -s UUID -o value "$root_dev")
  else
    root_mapper=$root_dev
  fi

  echo "Creating Btrfs filesystem and subvolumes"
  disk_step "creating the Btrfs filesystem on $root_mapper" mkfs.btrfs -f -L OMARCHY "$root_mapper"
  wait_for_device "$root_mapper" || disk_abort_hook "Root device $root_mapper never appeared"

  mkdir -p /mnt/btrfs-root
  disk_step "mounting $root_mapper" mount "$root_mapper" /mnt/btrfs-root
  for subvol in @ @home @log @pkg; do
    disk_step "creating subvolume $subvol" btrfs subvolume create "/mnt/btrfs-root/$subvol"
  done
  umount /mnt/btrfs-root
  rmdir /mnt/btrfs-root

  disk_step "mounting the target root" mount -o noatime,compress=zstd,subvol=@ "$root_mapper" /mnt
  mkdir -p /mnt/home /mnt/var/log /mnt/var/cache/pacman/pkg "/mnt$esp_mount"
  disk_step "mounting /home" mount -o noatime,compress=zstd,subvol=@home "$root_mapper" /mnt/home
  disk_step "mounting /var/log" mount -o noatime,compress=zstd,subvol=@log "$root_mapper" /mnt/var/log
  disk_step "mounting the package cache" \
    mount -o noatime,compress=zstd,subvol=@pkg "$root_mapper" /mnt/var/cache/pacman/pkg
  disk_step "creating the ESP filesystem on $efi_dev" mkfs.fat -F32 -n OMARCHY_EFI "$efi_dev"
  disk_step "mounting the ESP" mount "$efi_dev" "/mnt$esp_mount"

  # The orchestrator's first act is to verify this handoff. Check it here so
  # a failure names the step that broke rather than surfacing later as
  # "protected mode: /mnt is not a mountpoint".
  mountpoint -q /mnt || disk_abort_hook "Target /mnt is not mounted; not starting the installer"
  mountpoint -q "/mnt$esp_mount" || disk_abort_hook "ESP is not mounted at /mnt$esp_mount"

  {
    printf 'efi_dev=%q\n' "$efi_dev"
    printf 'root_device=%q\n' "$root_dev"
    printf 'root_mapper=%q\n' "$root_mapper"
    printf 'luks_uuid=%q\n' "$luks_uuid"
    printf 'esp_mount=%q\n' "$esp_mount"
  } >"$result"
}
