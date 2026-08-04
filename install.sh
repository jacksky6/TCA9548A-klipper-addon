#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_FILE="${SCRIPT_DIR}/tca9548a.py"
FIRMWARE_DIR="${FIRMWARE_DIR:-${KLIPPER_DIR:-}}"
FIRMWARE_NAME=""
TARGET_DIR=""

usage() {
    echo "Usage: $0 [--firmware-dir PATH]"
    echo "Install tca9548a.py as a symbolic link in Klipper or Kalico extras."
}

if [[ $# -gt 0 ]]; then
    if [[ $# -eq 2 && ( "$1" == "--firmware-dir" || "$1" == "--klipper-dir" ) ]]; then
        FIRMWARE_DIR="$2"
    elif [[ $# -eq 1 && "$1" == "--help" ]]; then
        usage
        exit 0
    else
        usage
        exit 2
    fi
fi

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

if [[ ! -f "${SOURCE_FILE}" ]]; then
    echo "Source file not found: ${SOURCE_FILE}" >&2
    exit 1
fi

BRANCH="$(git -C "${SCRIPT_DIR}" branch --show-current 2>/dev/null || true)"
if [[ -z "${BRANCH}" ]]; then
    BRANCH="detached at $(git -C "${SCRIPT_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
fi
echo "Repository branch: ${BRANCH}"

read -r -p "Update this repository with git pull --ff-only before installation? [y/N] " ANSWER
case "${ANSWER}" in
    [yY]|[yY][eE][sS])
        if ! git -C "${SCRIPT_DIR}" diff --quiet || \
           ! git -C "${SCRIPT_DIR}" diff --cached --quiet; then
            echo "Tracked local changes found; skipping repository update."
        elif ! git -C "${SCRIPT_DIR}" rev-parse --abbrev-ref \
                --symbolic-full-name '@{upstream}' >/dev/null 2>&1; then
            echo "No upstream branch is configured; skipping repository update."
        elif ! git -C "${SCRIPT_DIR}" pull --ff-only; then
            echo "Repository update failed; installation cancelled." >&2
            exit 1
        fi
        ;;
    *)
        echo "Repository update skipped."
        ;;
esac

detect_firmware
echo "Detected firmware: ${FIRMWARE_NAME} (${FIRMWARE_DIR})"
TARGET_FILE="${TARGET_DIR}/tca9548a.py"

if [[ -e "${TARGET_FILE}" || -L "${TARGET_FILE}" ]]; then
    if [[ -L "${TARGET_FILE}" && "$(readlink "${TARGET_FILE}")" == "${SOURCE_FILE}" ]]; then
        echo "Symbolic link is already installed: ${TARGET_FILE}"
        exit 0
    fi
    read -r -p "Target already exists: ${TARGET_FILE}. Replace it? [y/N] " ANSWER
    case "${ANSWER}" in
        [yY]|[yY][eE][sS])
            rm -- "${TARGET_FILE}"
            ;;
        *)
            echo "Installation cancelled."
            exit 1
            ;;
    esac
fi

ln -s "${SOURCE_FILE}" "${TARGET_FILE}"
echo "Installed symbolic link: ${TARGET_FILE} -> ${SOURCE_FILE}"
