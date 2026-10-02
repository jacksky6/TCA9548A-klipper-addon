#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ADDON_PATHS=("tca9548a.py" "tca9548a_drivers")
FIRMWARE_DIR="${FIRMWARE_DIR:-${KLIPPER_DIR:-}}"
FIRMWARE_NAME=""
FIRMWARE_VERSION=""
TARGET_DIR=""
UNINSTALL=0
ALLOW_LEGACY_I2C=0
SKIP_UPDATE=0
I2C_RECOVERY_SUPPORTED=0
I2C_RECOVERY_MIN_VERSION="v0.13.0-525-g8965958"
UPDATE_CHECK_TIMEOUT_SECONDS=10

COLOR_ENABLED=0
COLOR_RESET=""
COLOR_TITLE=""
COLOR_LABEL=""
COLOR_OK=""
COLOR_WARN=""
COLOR_ERROR=""

if [[ -t 1 && "${TERM:-dumb}" != "dumb" ]]; then
    COLOR_ENABLED=1
    COLOR_RESET=$'\033[0m'
    COLOR_TITLE=$'\033[1;36m'
    COLOR_LABEL=$'\033[1;37m'
    COLOR_OK=$'\033[1;32m'
    COLOR_WARN=$'\033[1;33m'
    COLOR_ERROR=$'\033[1;31m'
fi

print_colored() {
    local color="$1"
    shift
    printf '%b%s%b\n' "${color}" "$*" "${COLOR_RESET}"
}

print_banner() {
    print_colored "${COLOR_TITLE}" "+------------------------------------------------------------+"
    print_colored "${COLOR_TITLE}" "|                 TCA9548A Add-on Installer                 |"
    print_colored "${COLOR_TITLE}" "+------------------------------------------------------------+"
}

print_section() {
    print_colored "${COLOR_TITLE}" ""
    print_colored "${COLOR_TITLE}" "[ $1 ]"
}

print_field() {
    local label="$1"
    local value="$2"
    printf '%b%-24s%b %s\n' "${COLOR_LABEL}" "${label}:" \
        "${COLOR_RESET}" "${value}"
}

print_status() {
    local color="$1"
    local label="$2"
    local value="$3"
    printf '%b%-24s%b %b%s%b\n' "${COLOR_LABEL}" "${label}:" \
        "${COLOR_RESET}" "${color}" "${value}" "${COLOR_RESET}"
}

print_recovery_notice() {
    print_section "I2C Recovery"
    print_field "Applies to" "Klipper host and I2C MCU firmware"
    print_field "Minimum version" ">= ${I2C_RECOVERY_MIN_VERSION}"
    print_field "Important" "git pull does not update MCU firmware"
}

usage() {
    echo "Usage: $0 [--firmware-dir PATH] [--allow-legacy-i2c] [-s|--skip-update] [-u|--uninstall]"
    echo "Install or uninstall TCA9548A symbolic links in Klipper or Kalico extras."
    echo "-s, --skip-update skips the remote update check and uses local files."
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
        -s|--skip-update)
            SKIP_UPDATE=1
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

    FIRMWARE_VERSION="$(git -C "${FIRMWARE_DIR}" describe --tags --always \
        --dirty 2>/dev/null || true)"
    if [[ -z "${FIRMWARE_VERSION}" ]]; then
        FIRMWARE_VERSION="unknown (not a Git checkout)"
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

check_repository_update() {
    local upstream remote_name remote_branch fetch_output fetch_status
    local revision_counts ahead behind answer pull_output

    if [[ "${SKIP_UPDATE}" -eq 1 ]]; then
        print_field "Update check" "skipped (-s)"
        return
    fi

    if [[ -z "${BRANCH}" || "${BRANCH}" == detached* ]]; then
        print_status "${COLOR_WARN}" "Update check" "skipped (detached HEAD)"
        return
    fi

    upstream="$(git -C "${SCRIPT_DIR}" rev-parse --abbrev-ref \
        --symbolic-full-name '@{upstream}' 2>/dev/null || true)"
    if [[ -z "${upstream}" || "${upstream}" != */* ]]; then
        print_status "${COLOR_WARN}" "Update check" "skipped (no upstream branch)"
        return
    fi
    remote_name="${upstream%%/*}"
    remote_branch="${upstream#*/}"

    print_field "Update check" "${upstream} (10s timeout)"
    if ! command -v timeout >/dev/null 2>&1; then
        print_status "${COLOR_WARN}" "Update" "check unavailable; using local files"
        return
    fi
    fetch_output="$(GIT_TERMINAL_PROMPT=0 timeout --foreground \
        "${UPDATE_CHECK_TIMEOUT_SECONDS}s" \
        git -C "${SCRIPT_DIR}" fetch --quiet --no-tags "${remote_name}" \
        "${remote_branch}" 2>&1)"
    fetch_status=$?
    if [[ "${fetch_status}" -ne 0 ]]; then
        if [[ "${fetch_status}" -eq 124 ]]; then
            print_status "${COLOR_WARN}" "Update" "check timed out; using local files"
        else
            print_status "${COLOR_WARN}" "Update" "check failed; using local files"
        fi
        return
    fi

    revision_counts="$(git -C "${SCRIPT_DIR}" rev-list --left-right \
        --count HEAD...FETCH_HEAD 2>/dev/null || true)"
    read -r ahead behind <<< "${revision_counts}"
    if [[ ! "${ahead}" =~ ^[0-9]+$ || ! "${behind}" =~ ^[0-9]+$ ]]; then
        print_status "${COLOR_WARN}" "Update" "check failed; using local files"
        return
    fi
    if [[ "${behind}" -eq 0 ]]; then
        print_status "${COLOR_OK}" "Update" "current"
        return
    fi

    print_status "${COLOR_WARN}" "Update" "available (${behind} commits)"
    if [[ ! -t 0 || ! -t 1 ]]; then
        print_status "${COLOR_WARN}" "Update" "skipped (non-interactive)"
        return
    fi
    if ! read -r -p "Update now? [Y/n] " answer; then
        answer="n"
    fi
    case "${answer}" in
        ""|[yY]|[yY][eE][sS])
            print_field "Update" "in progress"
            pull_output="$(GIT_TERMINAL_PROMPT=0 git -C "${SCRIPT_DIR}" \
                pull --ff-only 2>&1)"
            if [[ "$?" -eq 0 ]]; then
                print_status "${COLOR_OK}" "Update" "completed"
            else
                print_status "${COLOR_ERROR}" "Update" "failed; using local files"
            fi
            ;;
        *)
            print_field "Update" "skipped by user"
            ;;
    esac
}

display_firmware_summary() {
    print_section "Klipper Detection"
    print_field "Firmware" "${FIRMWARE_NAME}"
    print_field "Current version" "${FIRMWARE_VERSION}"
}

confirm_i2c_recovery_support() {
    detect_i2c_recovery_support
    display_firmware_summary
    if [[ "${I2C_RECOVERY_SUPPORTED}" -eq 1 ]]; then
        print_status "${COLOR_OK}" "Klipper check" "SUPPORTED"
        print_field "Host protocol" "modern I2C status responses"
        return
    fi

    print_status "${COLOR_WARN}" "Klipper check" "LEGACY - RECOVERY UNAVAILABLE" >&2
    print_field "Risk" "I2C failure can shut down ${FIRMWARE_NAME}" >&2
    print_field "Required action" "update this Klipper host" >&2

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
    print_banner
    print_recovery_notice
    print_section "Add-on Repository"
    print_field "Branch" "${BRANCH}"

    if git -C "${SCRIPT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        check_repository_update
    else
        print_field "Update check" "skipped (not a Git checkout)"
    fi
fi

detect_firmware

if [[ "${UNINSTALL}" -eq 0 ]]; then
    confirm_i2c_recovery_support
else
    print_banner
    print_recovery_notice
    display_firmware_summary
fi

validate_uninstall_paths() {
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
    validate_uninstall_paths
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

print_section "Installation"
for addon_path in "${ADDON_PATHS[@]}"; do
    source_path="${SCRIPT_DIR}/${addon_path}"
    target_path="${TARGET_DIR}/${addon_path}"
    if [[ -L "${target_path}" ]]; then
        rm -- "${target_path}"
    elif [[ -d "${target_path}" ]]; then
        rm -rf -- "${target_path}"
    elif [[ -e "${target_path}" ]]; then
        rm -- "${target_path}"
    fi
    ln -s "${source_path}" "${target_path}"
    print_status "${COLOR_OK}" "${addon_path}" "linked"
done

echo ""
echo "Restart ${FIRMWARE_NAME} from Fluidd or Mainsail to load the add-on."
