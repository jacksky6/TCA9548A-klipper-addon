#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ADDON_PATHS=("tca9548a.py" "tca9548a_drivers")
FIRMWARE_DIR="${FIRMWARE_DIR:-${KLIPPER_DIR:-}}"
FIRMWARE_NAME=""
TARGET_DIR=""
UNINSTALL=0

usage() {
    echo "Usage: $0 [--firmware-dir PATH] [-u|--uninstall]"
    echo "Install or uninstall TCA9548A symbolic links in Klipper or Kalico extras."
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
