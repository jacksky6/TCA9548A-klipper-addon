#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ADDON_PATHS=("tca9548a.py" "tca9548a_drivers")
FIRMWARE_DIR="${FIRMWARE_DIR:-${KLIPPER_DIR:-}}"
FIRMWARE_NAME=""
TARGET_DIR=""
UNINSTALL=0
ALLOW_LEGACY_I2C=0
I2C_RECOVERY_SUPPORTED=0

usage() {
    echo "Usage: $0 [--firmware-dir PATH] [--allow-legacy-i2c] [-u|--uninstall]"
    echo "Install or uninstall TCA9548A symbolic links in Klipper or Kalico extras."
    echo "--allow-legacy-i2c installs without an interactive confirmation when"
    echo "the target does not support recoverable I2C status responses."
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -u|--uninstall)
            UNINSTALL=1
            shift
            ;;
        --firmware-dir|--klipper-dir)
            if [[ $# -lt 2 ]]; then
                usage
                exit 2
            fi
            FIRMWARE_DIR="$2"
            shift 2
            ;;
        --allow-legacy-i2c)
            ALLOW_LEGACY_I2C=1
            shift
            ;;
        --help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

detect_firmware() {
    if [[ -z "${FIRMWARE_DIR}" ]]; then
        for candidate in "${HOME}/klipper" "${HOME}/kalico"; do
            if [[ -d "${candidate}/klippy/extras" || \
                  -d "${candidate}/kalico/extras" ]]; then
                FIRMWARE_DIR="${candidate}"
                break
            fi
        done
    fi

    if [[ -z "${FIRMWARE_DIR}" ]]; then
        echo "No Klipper or Kalico installation was detected." >&2
        echo "Use --firmware-dir PATH or set FIRMWARE_DIR." >&2
        exit 1
    fi

    if [[ -d "${FIRMWARE_DIR}/klippy/extras" ]]; then
        TARGET_DIR="${FIRMWARE_DIR}/klippy/extras"
    elif [[ -d "${FIRMWARE_DIR}/kalico/extras" ]]; then
        TARGET_DIR="${FIRMWARE_DIR}/kalico/extras"
    else
        echo "Extras directory not found under: ${FIRMWARE_DIR}" >&2
        echo "Use --firmware-dir PATH to select the Klipper or Kalico root." >&2
        exit 1
    fi

    remote_url="$(git -C "${FIRMWARE_DIR}" remote get-url origin 2>/dev/null || true)"
    if printf '%s' "${remote_url}" | grep -qi 'kalico'; then
        FIRMWARE_NAME="Kalico"
    elif [[ "${FIRMWARE_DIR##*/}" == "kalico" ]]; then
        FIRMWARE_NAME="Kalico"
    else
        FIRMWARE_NAME="Klipper"
    fi
}

detect_i2c_recovery_support() {
    local bus_file="${TARGET_DIR}/bus.py"
    I2C_RECOVERY_SUPPORTED=0

    if [[ ! -f "${bus_file}" ]]; then
        echo "I2C compatibility check could not find: ${bus_file}" >&2
        return
    fi

    # Modern Klipper/Kalico creates i2c_transfer_cmd and decodes the MCU's
    # i2c_bus_status enumeration from i2c_response. Both markers are needed:
    # an i2c_transfer string by itself is not sufficient for recovery.
    if grep -Fq 'i2c_transfer oid=%c write=%*s read_len=%u' "${bus_file}" \
        && grep -Fq 'i2c_response oid=%c i2c_bus_status=%c response=%*s' \
            "${bus_file}"; then
        I2C_RECOVERY_SUPPORTED=1
    fi
}

confirm_i2c_recovery_support() {
    detect_i2c_recovery_support
    if [[ "${I2C_RECOVERY_SUPPORTED}" -eq 1 ]]; then
        cat <<EOF
I2C recovery check: supported by this ${FIRMWARE_NAME} host source.

The AHT driver can keep Klipper/Kalico running when a modern MCU firmware
returns an I2C status such as NACK, START_NACK, or BUS_TIMEOUT. The actual
MCU must also be rebuilt and flashed with matching modern firmware. At runtime
the driver's i2c_status_supported status field confirms that final capability.
EOF
        return
    fi

    cat >&2 <<EOF
I2C recovery check: NOT supported by this ${FIRMWARE_NAME} host source.

This target uses the legacy i2c_read/i2c_write protocol. On that protocol the
MCU firmware handles I2C NACK, START_NACK, START_READ_NACK, and timeout errors
by entering shutdown before Python code can catch the error. The add-on remains
compatible with this target, but it cannot guarantee that a disconnected or
failed AHT sensor will not stop Klipper/Kalico.

To use recoverable AHT communication, update the ${FIRMWARE_NAME} host and
rebuild and flash the MCU firmware from the same modern source. Installing now
keeps the legacy behavior until both sides are updated.
EOF

    if [[ "${ALLOW_LEGACY_I2C}" -eq 1 ]]; then
        echo "Continuing because --allow-legacy-i2c was supplied." >&2
        return
    fi
    if [[ ! -t 0 ]]; then
        echo "Refusing non-interactive legacy installation." >&2
        echo "Re-run with --allow-legacy-i2c to acknowledge this limitation." >&2
        exit 1
    fi

    local answer
    read -r -p "Continue installation with legacy I2C behavior? [y/N] " answer
    case "${answer}" in
        [yY]|[yY][eE][sS])
            ;;
        *)
            echo "Installation cancelled." >&2
            exit 1
            ;;
    esac
}

if [[ "${UNINSTALL}" -eq 0 ]]; then
    for addon_path in "${ADDON_PATHS[@]}"; do
        source_path="${SCRIPT_DIR}/${addon_path}"
        if [[ ! -e "${source_path}" ]]; then
            echo "Add-on source not found: ${source_path}" >&2
            exit 1
        fi
    done

    BRANCH="$(git -C "${SCRIPT_DIR}" branch --show-current 2>/dev/null || true)"
    if [[ -z "${BRANCH}" ]]; then
        BRANCH="detached at $(git -C "${SCRIPT_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    fi
    echo "Repository branch: ${BRANCH}"

    if git -C "${SCRIPT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "Updating repository with git pull --ff-only..."
        if ! git -C "${SCRIPT_DIR}" pull --ff-only; then
            echo "Repository update failed; continuing with the local files." >&2
        fi
    else
        echo "Repository is not a Git checkout; continuing with the local files."
    fi
fi

detect_firmware
echo "Detected firmware: ${FIRMWARE_NAME} (${FIRMWARE_DIR})"

if [[ "${UNINSTALL}" -eq 0 ]]; then
    confirm_i2c_recovery_support
fi

validate_target_paths() {
    for addon_path in "${ADDON_PATHS[@]}"; do
        target_path="${TARGET_DIR}/${addon_path}"
        if [[ -e "${target_path}" && ! -L "${target_path}" ]]; then
            echo "Target exists and is not a symbolic link: ${target_path}" >&2
            echo "Refusing to overwrite or remove it." >&2
            exit 1
        fi
    done
}

if [[ "${UNINSTALL}" -eq 1 ]]; then
    validate_target_paths
    for addon_path in "${ADDON_PATHS[@]}"; do
        target_path="${TARGET_DIR}/${addon_path}"
        if [[ -L "${target_path}" ]]; then
            rm -- "${target_path}"
            echo "Removed symbolic link: ${target_path}"
        else
            echo "No symbolic link is installed at: ${target_path}"
        fi
    done
    echo "Manually remove the [update_manager tca9548a] section from moonraker.conf if configured."
    echo "Restart Moonraker, then restart Klipper or Kalico from Fluidd or Mainsail."
    exit 0
fi

validate_target_paths
for addon_path in "${ADDON_PATHS[@]}"; do
    source_path="${SCRIPT_DIR}/${addon_path}"
    target_path="${TARGET_DIR}/${addon_path}"
    if [[ -L "${target_path}" ]]; then
        rm -- "${target_path}"
        echo "Replaced existing symbolic link: ${target_path}"
    fi
    ln -s "${source_path}" "${target_path}"
    echo "Installed symbolic link: ${target_path} -> ${source_path}"
done

echo "Restart Klipper or Kalico from Fluidd or Mainsail before using the add-on."
