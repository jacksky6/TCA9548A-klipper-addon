#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_FILE="${SCRIPT_DIR}/tca9548a.py"
FIRMWARE_DIR="${FIRMWARE_DIR:-${KLIPPER_DIR:-}}"
FIRMWARE_NAME=""
TARGET_DIR=""
UNINSTALL=0

usage() {
    echo "Usage: $0 [--firmware-dir PATH] [-u|--uninstall]"
    echo "Install or uninstall the tca9548a.py symbolic link in Klipper or Kalico extras."
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
    if [[ ! -f "${SOURCE_FILE}" ]]; then
        echo "Source file not found: ${SOURCE_FILE}" >&2
        exit 1
    fi

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
TARGET_FILE="${TARGET_DIR}/tca9548a.py"

if [[ "${UNINSTALL}" -eq 1 ]]; then
    if [[ -L "${TARGET_FILE}" ]]; then
        rm -- "${TARGET_FILE}"
        echo "Removed symbolic link: ${TARGET_FILE}"
    elif [[ -e "${TARGET_FILE}" ]]; then
        echo "Target exists and is not a symbolic link: ${TARGET_FILE}" >&2
        echo "Refusing to remove it." >&2
        exit 1
    else
        echo "No symbolic link is installed at: ${TARGET_FILE}"
    fi
    echo "Manually remove the [update_manager tca9548a] section from moonraker.conf if configured."
    echo "Restart Moonraker, then restart Klipper or Kalico from Fluidd or Mainsail."
    exit 0
fi

if [[ -L "${TARGET_FILE}" ]]; then
    rm -- "${TARGET_FILE}"
    echo "Replaced existing symbolic link: ${TARGET_FILE}"
elif [[ -e "${TARGET_FILE}" ]]; then
    echo "Target exists and is not a symbolic link: ${TARGET_FILE}" >&2
    echo "Refusing to overwrite it." >&2
    exit 1
fi

ln -s "${SOURCE_FILE}" "${TARGET_FILE}"
echo "Installed symbolic link: ${TARGET_FILE} -> ${SOURCE_FILE}"

echo "Restart Klipper or Kalico from Fluidd or Mainsail before using the add-on."
