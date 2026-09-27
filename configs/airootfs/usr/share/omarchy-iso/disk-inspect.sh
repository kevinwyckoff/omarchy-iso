# Disk inspection for the configurator's disk picker and wipe summary, shared
# with test/unit/disk-inspect-test.sh.
#
# Everything here is read-only. The one side effect is a content probe that
# mounts a partition read-only (never replaying a journal) to see what is on
# it. Every probe is bounded by a timeout and falls back to an empty answer,
# which the screens show as "—": a strange disk can slow the summary down but
# can never block an install.
#
# Device data comes from `lsblk -J -b` rather than whitespace-split `lsblk -r`
# output, which shifted columns whenever a field like FSTYPE was empty.
#
# Sourced, not executed. Tests point DISK_INSPECT_LSBLK_JSON at a fixture and
# set DISK_INSPECT_PROBE=0 (or pre-fill disk_contents) to skip mounting.

DISK_INSPECT_ESP_B=$((2 * 1024 * 1024 * 1024))
DISK_INSPECT_MIN_ROOT_B=$((32 * 1024 * 1024 * 1024))
# Matches the free-space minimum: a 2GiB ESP plus 32GiB for Omarchy.
DISK_INSPECT_MIN_FULL_DISK_B=$((DISK_INSPECT_ESP_B + DISK_INSPECT_MIN_ROOT_B))

DISK_INSPECT_PROBE="${DISK_INSPECT_PROBE:-1}"
DISK_INSPECT_PROBE_TIMEOUT="${DISK_INSPECT_PROBE_TIMEOUT:-10}"
DISK_INSPECT_BY_ID_DIR="${DISK_INSPECT_BY_ID_DIR:-/dev/disk/by-id}"
DISK_INSPECT_MEMINFO="${DISK_INSPECT_MEMINFO:-/proc/meminfo}"

# The disk the live system booted from. The configurator sets it; the picker
# never offers it and the summary labels it.
DISK_INSPECT_INSTALL_MEDIUM="${DISK_INSPECT_INSTALL_MEDIUM:-}"

disk_inventory_json=""

# What each partition holds, keyed by partition path. Filled by disk_probe(),
# which must run in the calling shell: a $(...) would probe in a subshell and
# throw the answers away.
declare -gA disk_contents=()

disk_inventory_refresh() {
  if [[ -n ${DISK_INSPECT_LSBLK_JSON:-} ]]; then
    disk_inventory_json=$(<"$DISK_INSPECT_LSBLK_JSON")
  else
    disk_inventory_json=$(lsblk -J -b -o NAME,PATH,TYPE,SIZE,FSTYPE,FSVER,LABEL,PARTLABEL,PARTTYPENAME,PARTN,MODEL,VENDOR,SERIAL,TRAN,WWN,PTTYPE,RM,MOUNTPOINTS 2>/dev/null)
  fi
  [[ -n $disk_inventory_json ]] || disk_inventory_json='{"blockdevices":[]}'
  disk_contents=()
}

_disk_jq() {
  jq -r "$@" <<<"$disk_inventory_json"
}

# One field of one device (disk or partition), as text. Null is "", and a list
# (MOUNTPOINTS) is joined with commas after dropping lsblk's null entries.
disk_field() {
  _disk_jq --arg p "$1" --arg f "$2" '
    [.blockdevices[] | recurse(.children[]?) | select(.path == $p)][0][$f]
    | if . == null then ""
      elif type == "array" then map(select(. != null)) | join(",")
      else tostring end'
}

disk_partitions() {
  _disk_jq --arg d "$1" '
    .blockdevices[] | select(.path == $d) | .children[]? | select(.type == "part") | .path'
}

# Disks worth listing at all: real drives, not loop devices, zram or optical.
_disk_all_drives() {
  _disk_jq '
    .blockdevices[]
    | select(.type == "disk")
    | select(.name | test("^(sd|hd|vd|nvme|mmcblk|xv)"))
    | select(.name | test("^mmcblk[0-9]+(boot[0-9]+|rpmb)$") | not)
    | .path'
}

disk_is_cidata() {
  [[ $(_disk_jq --arg d "$1" '
    [.blockdevices[] | select(.path == $d) | recurse(.children[]?)
     | (.label // "") | ascii_downcase] | any(. == "cidata")') == "true" ]]
}

# Disks the installer may offer. Never the install medium, never a cidata
# drive (the wizard only runs when its config was missing or invalid, so it is
# exactly the drive a user would not expect to see), and never an eMMC's boot
# or RPMB areas, which lsblk reports as disks of their own.
installable_disks() {
  local d
  while IFS= read -r d; do
    [[ -n $d ]] || continue
    [[ $d == "$DISK_INSPECT_INSTALL_MEDIUM" ]] && continue
    disk_is_cidata "$d" && continue
    echo "$d"
  done < <(_disk_all_drives)
}

disk_size_bytes() {
  local size
  size=$(disk_field "$1" size)
  echo "${size:-0}"
}

disk_is_too_small() {
  (( $(disk_size_bytes "$1") < DISK_INSPECT_MIN_FULL_DISK_B ))
}

# 1024-based, with one decimal from GiB up: "465.8 GiB", "2 GiB", "260 MiB".
human_size() {
  awk -v b="${1:-0}" 'BEGIN {
    split("B KiB MiB GiB TiB PiB", u, " ")
    i = 1
    while (b >= 1024 && i < 6) { b /= 1024; i++ }
    s = (i <= 3) ? sprintf("%.0f", b) : sprintf("%.1f", b)
    sub(/\.0$/, "", s)
    printf "%s %s\n", s, u[i]
  }'
}

# Width and padding in characters, not bytes: the summary uses "—", "→" and
# "…", which printf's %-Ns would count as three columns each.
_disk_pad() {
  local LC_ALL=C.UTF-8
  local text="$1" width="$2"
  printf '%s%*s' "$text" $(( width > ${#text} ? width - ${#text} : 0 )) ''
}

_disk_truncate() {
  local LC_ALL=C.UTF-8
  local text="$1" width="$2"
  if (( width <= 1 )); then
    echo ""
  elif (( ${#text} > width )); then
    echo "${text:0:width-1}…"
  else
    echo "$text"
  fi
}

# "Samsung SSD 980 PRO 500GB". Vendors that tell a person nothing are
# dropped: a bare PCI ID (virtio's 0x1af4) and the "ATA" that libata reports
# for every SATA drive.
disk_model() {
  local disk="$1" model vendor
  model=$(disk_field "$disk" model | sed 's/[[:space:]]*$//')
  vendor=$(disk_field "$disk" vendor | sed 's/[[:space:]]*$//')
  [[ $vendor =~ ^0x[0-9a-fA-F]+$ || $vendor == "ATA" ]] && vendor=""

  if [[ -n $vendor && -n $model && $model != *"$vendor"* ]]; then
    echo "$vendor $model"
  elif [[ -n $model ]]; then
    echo "$model"
  elif [[ -n $vendor ]]; then
    echo "$vendor"
  elif [[ $disk == /dev/vd* ]]; then
    echo "VirtIO disk"
  elif [[ $disk == /dev/mmcblk* ]]; then
    echo "SD/eMMC storage"
  else
    echo "Disk"
  fi
}

disk_transport() {
  local disk="$1" tran
  tran=$(disk_field "$disk" tran)
  case $tran in
    nvme) echo "NVMe" ;;
    sata | ata) echo "SATA" ;;
    usb) echo "USB" ;;
    sas) echo "SAS" ;;
    mmc) echo "SD/eMMC" ;;
    "") [[ $disk == /dev/vd* ]] && echo "virtio" ;;
    *) echo "$tran" ;;
  esac
}

disk_serial() {
  disk_field "$1" serial | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

# A stable name for the whole disk from /dev/disk/by-id, preferring the
# model-and-serial form over wwn- and eui. ones, which mean nothing to a person.
disk_by_id() {
  local disk="$1" link fallback=""
  [[ -d $DISK_INSPECT_BY_ID_DIR ]] || return 0
  for link in "$DISK_INSPECT_BY_ID_DIR"/*; do
    [[ -L $link && $link != *-part[0-9]* ]] || continue
    [[ $(readlink -f "$link") == "$disk" ]] || continue
    case ${link##*/} in
      wwn-* | nvme-eui.* | nvme-nvme.*) fallback="${fallback:-${link##*/}}" ;;
      *) echo "${link##*/}"; return 0 ;;
    esac
  done
  [[ -n $fallback ]] && echo "$fallback"
  return 0
}

partition_number() {
  local part="$1" n
  n=$(disk_field "$part" partn)
  [[ -n $n ]] || n="${part##*[!0-9]}"
  echo "$n"
}

# Partitions (or the disk itself) that are mounted or in use as swap, one per
# line as "<path><TAB><mountpoints>". The wipe screen will not continue while
# any are listed.
disk_busy_partitions() {
  _disk_jq --arg d "$1" '
    .blockdevices[] | select(.path == $d) | recurse(.children[]?)
    | (.mountpoints // [] | map(select(. != null))) as $m
    | select($m | length > 0)
    | "\(.path)\t\($m | join(", "))"'
}

# Any signature at all (a partition table, a filesystem, RAID or LVM metadata)
# means there may be data to lose, so the wipe needs a typed confirmation.
disk_has_signatures() {
  [[ -n $(wipefs --noheadings "$1" 2>/dev/null) ]]
}

# BitLocker writes "-FVE-FS-" at offset 3 of the partition.
_disk_has_bitlocker_signature() {
  [[ $(dd if="$1" bs=1 skip=3 count=8 status=none 2>/dev/null | tr -d '\0') == "-FVE-FS-" ]]
}

_disk_os_name() {
  local root="$1" f
  for f in "$root"/etc/os-release "$root"/usr/lib/os-release "$root"/@/etc/os-release "$root"/@/usr/lib/os-release; do
    if [[ -r $f ]]; then
      (. "$f" 2>/dev/null && echo "${PRETTY_NAME:-${NAME:-}}")
      return 0
    fi
  done
  return 1
}

_disk_used_space() {
  local used
  used=$(df -B1 --output=used "$1" 2>/dev/null | tail -n 1 | tr -d ' ')
  [[ $used =~ ^[0-9]+$ ]] && echo "~$(human_size "$used") used"
}

_disk_esp_loaders() {
  local efi="$1/EFI" loaders=()
  [[ -d $efi ]] || efi="$1/efi"
  [[ -d $efi ]] || return 0
  [[ -d $efi/Microsoft ]] && loaders+=("Windows Boot Manager")
  [[ -d $efi/limine ]] && loaders+=("Limine")
  [[ -d $efi/systemd ]] && loaders+=("systemd-boot")
  [[ -d $efi/refind ]] && loaders+=("rEFInd")
  compgen -G "$efi/*/grubx64.efi" >/dev/null && loaders+=("GRUB")
  (( ${#loaders[@]} )) && (IFS=","; echo "${loaders[*]}" | sed 's/,/, /g')
}

# Mount read-only and look. ext4's noload, xfs's norecovery and btrfs's
# nologreplay keep a dirty journal from being replayed onto a disk the user
# has not agreed to touch yet.
_disk_probe_mounted() {
  local part="$1" fstype="$2" ptname="$3" mp opts os used loaders type mounted=false parts=() types=(auto)
  case $fstype in
    ext2 | ext3 | ext4) opts="ro,noload" ;;
    xfs) opts="ro,norecovery" ;;
    btrfs) opts="ro,rescue=nologreplay" ;;
    *) opts="ro" ;;
  esac
  # blkid calls it "ntfs", but the drivers are ntfs3 (kernel) and ntfs-3g.
  [[ $fstype == ntfs ]] && types=(ntfs3 ntfs-3g)

  mp=$(mktemp -d /tmp/disk-inspect.XXXXXX) || return 0
  for type in "${types[@]}"; do
    if timeout -k 2 "$DISK_INSPECT_PROBE_TIMEOUT" mount -t "$type" -o "$opts,nodev,nosuid,noexec" "$part" "$mp" 2>/dev/null; then
      mounted=true
      break
    fi
  done
  if ! $mounted; then
    rmdir "$mp" 2>/dev/null
    return 0
  fi

  case $fstype in
    vfat)
      loaders=$(_disk_esp_loaders "$mp")
      if [[ -n $loaders || $ptname == "EFI System" ]]; then
        parts+=("EFI boot files")
        [[ -n $loaders ]] && parts+=("$loaders")
      else
        parts+=("data")
        used=$(_disk_used_space "$mp") && parts+=("$used")
      fi
      ;;
    ntfs | ntfs3)
      if [[ -d $mp/Windows/System32 ]]; then parts+=("Windows"); else parts+=("data"); fi
      used=$(_disk_used_space "$mp") && parts+=("$used")
      ;;
    *)
      if os=$(_disk_os_name "$mp") && [[ -n $os ]]; then parts+=("$os"); else parts+=("data"); fi
      used=$(_disk_used_space "$mp") && parts+=("$used")
      ;;
  esac

  timeout -k 2 "$DISK_INSPECT_PROBE_TIMEOUT" umount "$mp" 2>/dev/null || umount -l "$mp" 2>/dev/null
  rmdir "$mp" 2>/dev/null

  (IFS="|"; echo "${parts[*]}" | sed 's/|/ · /g')
}

# lsblk already carries the LUKS version (FSVER) and a LUKS2 label, so this
# never has to open the header.
_disk_describe_luks() {
  local version label
  version=$(disk_field "$1" fsver)
  label=$(disk_field "$1" label)
  echo "LUKS${version} encrypted${label:+ · $label}"
}

# What one partition holds, in a few words. Never unlocks, never writes.
_disk_probe_partition() {
  local part="$1" fstype ptname mounts
  fstype=$(disk_field "$part" fstype)
  ptname=$(disk_field "$part" parttypename)
  mounts=$(disk_field "$part" mountpoints)

  # Anything in use is reported as such and left alone.
  if [[ $mounts == *"[SWAP]"* ]]; then
    echo "swap in use"
    return
  elif [[ -n $mounts ]]; then
    echo "mounted at ${mounts//,/, }"
    return
  fi

  case $ptname in
    "Microsoft reserved") echo "Microsoft reserved"; return ;;
    "Windows recovery environment") echo "Windows recovery"; return ;;
    "BIOS boot") echo "BIOS boot"; return ;;
  esac

  case $fstype in
    BitLocker) echo "BitLocker on"; return ;;
    crypto_LUKS) _disk_describe_luks "$part"; return ;;
    swap) echo "swap"; return ;;
    LVM2_member) echo "LVM volume"; return ;;
    linux_raid_member) echo "RAID member"; return ;;
  esac

  (( DISK_INSPECT_PROBE )) || return 0

  if _disk_has_bitlocker_signature "$part"; then
    echo "BitLocker on"
    return
  fi

  case $fstype in
    vfat | ntfs | ntfs3 | exfat | ext2 | ext3 | ext4 | btrfs | xfs)
      _disk_probe_mounted "$part" "$fstype" "$ptname"
      ;;
  esac
}

# Probe every partition on a disk into disk_contents. Call directly, never in
# a $(...), so the answers stay in this shell.
disk_probe() {
  local part
  while IFS= read -r part; do
    [[ -n $part ]] || continue
    [[ -n ${disk_contents[$part]+x} ]] && continue
    disk_contents[$part]=$(_disk_probe_partition "$part")
  done < <(disk_partitions "$1")
}

disk_probe_all() {
  local d
  while IFS= read -r d; do
    [[ -n $d ]] && disk_probe "$d"
  done < <(_disk_all_drives)
}

# Contents that say something about a disk as a whole: an OS, BitLocker, LUKS.
# Loader lists, reserved and recovery partitions, and plain data don't.
_disk_notable_content() {
  local first="${1%% · *}"
  case $first in
    "" | data | swap | "swap in use" | "EFI boot files" | "Microsoft reserved" | "Windows recovery" | "BIOS boot" | "LVM volume" | "RAID member" | "mounted at"*) ;;
    *) echo "$first" ;;
  esac
}

# "Windows · 4 partitions", "Arch Linux, LUKS2 encrypted · 3 partitions",
# "empty". What tells two identical drives apart in the picker.
disk_contents_summary() {
  local disk="$1" part note n=0 notes=() fstype pttype
  local -A seen=()
  while IFS= read -r part; do
    [[ -n $part ]] || continue
    n=$((n + 1))
    note=$(_disk_notable_content "${disk_contents[$part]:-}")
    if [[ -n $note && -z ${seen[$note]+x} ]]; then
      seen[$note]=1
      notes+=("$note")
    fi
  done < <(disk_partitions "$disk")

  if (( n == 0 )); then
    fstype=$(disk_field "$disk" fstype)
    pttype=$(disk_field "$disk" pttype)
    if [[ -n $fstype ]]; then
      echo "$fstype, no partition table"
    elif [[ -n $pttype ]]; then
      echo "empty partition table"
    else
      echo "empty"
    fi
    return
  fi

  local count="$n partitions"
  (( n == 1 )) && count="1 partition"
  if (( ${#notes[@]} )); then
    (IFS=","; echo "${notes[*]} · $count" | sed 's/,/, /g')
  else
    echo "$count"
  fi
}

# Drop the partition count when there is something more telling to say:
# "Windows · 4 partitions" becomes "Windows". Lines with room to spare
# (the wipe summary's own table) keep the full form.
_disk_contents_brief() {
  local summary
  summary=$(disk_contents_summary "$1")
  [[ $summary == *" · "*" partition"* ]] && summary="${summary% · *}"
  echo "$summary"
}

# One picker line. The device path comes first because the configurator reads
# the selection back from the first field. The rest runs from most to least
# telling, so a narrow console cuts the model name, which is the one thing two
# identical drives share: size, what is on the drive, serial, model.
disk_picker_line() {
  local disk="$1" serial line
  serial=$(disk_serial "$disk")
  line="$disk  $(human_size "$(disk_size_bytes "$disk")")  "
  disk_is_too_small "$disk" && line+="too small · "
  line+="$(_disk_contents_brief "$disk")"
  [[ -n $serial ]] && line+=" · S/N $serial"
  line+=" · $(disk_model "$disk")"
  echo "$line"
}

_disk_ram_bytes() {
  awk '/^MemTotal:/ { printf "%.0f\n", $2 * 1024; exit }' "$DISK_INSPECT_MEMINFO" 2>/dev/null
}

# One row of a partition table: "#  SIZE  FILESYSTEM  LABEL  WHAT'S ON IT",
# the last column cut to fit $width.
_disk_table_row() {
  local width="$6"
  printf '   %s %s %s %s %s\n' \
    "$(_disk_pad "$1" 2)" "$(_disk_pad "$2" 10)" "$(_disk_pad "$(_disk_truncate "$3" 12)" 12)" \
    "$(_disk_pad "$(_disk_truncate "$4" 10)" 10)" "$(_disk_truncate "$5" $((width - 41)))"
}

# One row of the "What gets created" block.
_disk_created_row() {
  printf '   %s %s %s %s\n' "$(_disk_pad "$1" 2)" "$(_disk_pad "$2" 10)" "$(_disk_pad "$3" 15)" "$4"
}

# The "Not touched" block: every other drive, so the user sees the ones they
# did not pick, with enough to recognise each (model, serial, and what is on
# it, or why the installer is keeping clear of it).
_disk_render_untouched() {
  local target="$1" width="$2" d serial line lines=()
  while IFS= read -r d; do
    [[ -n $d && $d != "$target" ]] || continue
    disk_probe "$d"
    line="$(disk_model "$d")"
    if [[ $d == "$DISK_INSPECT_INSTALL_MEDIUM" ]]; then
      line+=" · $(human_size "$(disk_size_bytes "$d")") · install medium"
    elif disk_is_cidata "$d"; then
      line+=" · $(human_size "$(disk_size_bytes "$d")") · cidata drive"
    else
      serial=$(disk_serial "$d")
      [[ -n $serial ]] && line+=" · $serial"
      line+=" · $(_disk_contents_brief "$d")"
    fi
    lines+=("   $(_disk_pad "$d" 13) $(_disk_truncate "$line" $((width - 17)))")
  done < <(_disk_all_drives)

  if (( ${#lines[@]} )); then
    echo
    echo "Not touched:"
    printf '%s\n' "${lines[@]}"
  fi
}

# The last-chance summary, as plain text lines no wider than $width. The
# configurator adds the padding, colours, busy-partition warning and prompt.
#
#   render_wipe_summary <disk> full_disk <encrypt> <width>
#   render_wipe_summary <disk> free_space <encrypt> <width> <esp_start> <esp_end> <root_start> <root_end>
#
# Probe the drives first (disk_probe_all) when rendering in a pipeline, or the
# probes run again in the subshell and their answers are thrown away.
render_wipe_summary() {
  local disk="$1" mode="$2" encrypt="$3" width="${4:-80}"
  local title serial by_id tran part size fs label esp_b root_b ram root_desc esp_mount esp_num root_num table=()

  disk_probe "$disk"

  if [[ $mode == "full_disk" ]]; then
    title="THIS WILL ERASE A DISK"
  else
    title="OMARCHY WILL USE FREE SPACE ON THIS DISK"
  fi
  echo "$title"
  printf '%*s\n' "$(( width < 72 ? width - 2 : 70 ))" '' | sed 's/ /─/g'

  tran=$(disk_transport "$disk")
  _disk_truncate "Target   $(disk_model "$disk") · $(human_size "$(disk_size_bytes "$disk")")${tran:+ · $tran}" "$width"
  serial=$(disk_serial "$disk")
  [[ -n $serial ]] && _disk_truncate "         serial $serial" "$width"
  by_id=$(disk_by_id "$disk")
  if [[ -z $by_id ]]; then
    echo "         $disk"
  elif (( ${#disk} + ${#by_id} + 20 <= width )); then
    echo "         $disk  (by-id: $by_id)"
  else
    echo "         $disk"
    _disk_truncate "         by-id: $by_id" "$width"
  fi
  echo

  while IFS= read -r part; do
    [[ -n $part ]] || continue
    size=$(human_size "$(disk_field "$part" size)")
    fs=$(disk_field "$part" fstype)
    label=$(disk_field "$part" label)
    table+=("$(_disk_table_row "$(partition_number "$part")" "$size" "${fs:-—}" "${label:-—}" "${disk_contents[$part]:-—}" "$width")")
  done < <(disk_partitions "$disk")

  if [[ $mode == "full_disk" ]]; then
    echo "What dies:"
    if (( ${#table[@]} )); then
      _disk_table_row "#" "SIZE" "FILESYSTEM" "LABEL" "WHAT'S ON IT" "$width"
      printf '%s\n' "${table[@]}"
    elif [[ $(disk_contents_summary "$disk") == "empty" ]]; then
      echo "   Nothing. The disk is blank."
    else
      echo "   $(disk_contents_summary "$disk")"
    fi
  else
    echo "Nothing is erased."
    echo "Free space used: $(human_size $(( $8 - $5 )))"
    if (( ${#table[@]} )); then
      echo
      echo "Kept on this disk:"
      _disk_table_row "#" "SIZE" "FILESYSTEM" "LABEL" "WHAT'S ON IT" "$width"
      printf '%s\n' "${table[@]}"
    fi
  fi

  _disk_render_untouched "$disk" "$width"

  if [[ $mode == "full_disk" ]]; then
    esp_b=$DISK_INSPECT_ESP_B
    # Same arithmetic as the configurator's layout: 1MiB before the ESP and
    # 1MiB kept free at the end for the backup GPT.
    root_b=$(( $(disk_size_bytes "$disk") / 1048576 * 1048576 - DISK_INSPECT_ESP_B - 2 * 1048576 ))
  else
    esp_b=$(( $6 - $5 ))
    root_b=$(( $8 - $7 ))
  fi

  # The free-space install mounts an unencrypted target's ESP at /efi.
  esp_mount="/boot"
  if [[ $encrypt == "true" ]]; then
    root_desc="LUKS2 → btrfs"
  else
    root_desc="btrfs"
    [[ $mode == "free_space" ]] && esp_mount="/efi"
  fi

  # parted puts a free-space install's partitions in the lowest free GPT
  # slots, which need not be 1 and 2: those usually belong to partitions
  # listed as kept above. Leave the numbers blank rather than predict them;
  # the code that creates the partitions doesn't predict them either, but
  # reads back what parted assigned.
  esp_num=1 root_num=2
  [[ $mode == "free_space" ]] && esp_num="" root_num=""

  echo
  echo "What gets created:"
  _disk_created_row "$esp_num" "$(human_size "$esp_b")" "vfat" "$esp_mount"
  _disk_created_row "$root_num" "$(human_size "$root_b")" "$root_desc" "/  (@, @home, @log, @pkg)"
  ram=$(_disk_ram_bytes)
  if [[ -n $ram ]]; then
    _disk_truncate "      with zram and a $(human_size "$ram") hibernation swapfile" "$width"
  fi
}
