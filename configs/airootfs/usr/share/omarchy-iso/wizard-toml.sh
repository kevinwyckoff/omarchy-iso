# Turn the configurator's answers into install.toml, shared with
# test/unit/wizard-toml-test.sh.
#
# Every clicked install is also a described one: the wizard writes
# /root/install.toml and chefs-kitchen installs it, the same path an
# install.toml on a cidata drive takes. The installed system keeps the file,
# secrets stripped, at /etc/chefs-kitchen/install.toml.
#
# The wizard has already shown the wipe summary and had it confirmed, so the
# file says on_existing_data = "wipe" and pins expect_fingerprint to the disk
# as it was when the user confirmed: chefs-kitchen then installs without asking
# again, and refuses if the disk changed in between.
#
# Sourced, not executed. Needs disk-inspect.sh.

WIZARD_SECRETS_DIR="${WIZARD_SECRETS_DIR:-/run/chefs-kitchen/wizard}"

# A TOML basic string. jq's JSON string escapes are all valid TOML escapes.
toml_string() {
  printf '%s' "$1" | jq -Rs .
}

# The most stable way to name the disk in install.toml: its serial when no
# other disk shares it, else its /dev/disk/by-id name, else its path.
disk_selector_for() {
  local disk="$1" serial others by_id
  serial=$(disk_serial "$disk")
  if [[ -n $serial ]]; then
    others=$(_disk_jq --arg d "$disk" --arg s "$serial" '
      [.blockdevices[] | select(.type == "disk" and .path != $d)
       | select(((.serial // "") | gsub("^\\s+|\\s+$"; "")) == $s)] | length')
    if (( others == 0 )); then
      echo "{ serial = $(toml_string "$serial") }"
      return
    fi
  fi
  by_id=$(disk_by_id "$disk")
  if [[ -n $by_id ]]; then
    echo "{ by_id = $(toml_string "$by_id") }"
  else
    echo "{ path = $(toml_string "$disk") }"
  fi
}

# write_install_toml <path>
#
# Reads the configurator's answers from its globals: keyboard, hostname,
# timezone, username, password, full_name, email_address, disk,
# install_target (full_disk|free_space), encrypt_installation,
# defer_provisioning, and disk_fingerprint. Writes the user's password to a
# private file under WIZARD_SECRETS_DIR, never into install.toml.
write_install_toml() {
  local out="$1" password_file mode

  mode="wipe"
  [[ $install_target == "free_space" ]] && mode="free-space"

  {
    echo "# Written by the Omarchy installer from the answers given in the wizard."
    echo "schema = 1"
    echo
    echo "[system]"
    echo "hostname = $(toml_string "$hostname")"
    echo "timezone = $(toml_string "$timezone")"
    echo "keyboard = $(toml_string "$keyboard")"

    if ! $defer_provisioning; then
      mkdir -p -m 700 "$WIZARD_SECRETS_DIR"
      password_file="$WIZARD_SECRETS_DIR/$username.pass"
      (umask 077 && printf '%s\n' "$password" >"$password_file")
      echo
      echo "[[users]]"
      echo "name = $(toml_string "$username")"
      [[ -n $full_name ]] && echo "full_name = $(toml_string "$full_name")"
      [[ -n $email_address ]] && echo "email = $(toml_string "$email_address")"
      echo "password = { file = $(toml_string "$password_file") }"
    fi

    echo
    echo "[disk]"
    echo "target = $(disk_selector_for "$disk")"
    echo "mode = $(toml_string "$mode")"
    [[ $mode == "wipe" ]] && echo 'on_existing_data = "wipe"'
    echo "expect_fingerprint = $(toml_string "$disk_fingerprint")"
    echo
    echo "[encryption]"
    echo "enabled = $encrypt_installation"
    echo
    echo "[provisioning]"
    echo "defer = $defer_provisioning"
  } >"$out"
  chmod 600 "$out"
}
