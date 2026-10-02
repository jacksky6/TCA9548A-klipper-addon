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
REQUESTED_BRANCH=""
SELECT_BRANCH=0
BRANCH_SWITCH_DESCRIPTION=""
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
    print_section "I2C Recovery Feature"
    print_field "Applies to" "Klipper host and I2C MCU firmware"
    print_field "Minimum version" ">= ${I2C_RECOVERY_MIN_VERSION}"
    print_field "Important" "git pull does not update MCU firmware"
}

usage() {
    echo "Usage: $0 [--firmware-dir PATH] [--allow-legacy-i2c] [-b|--branch BRANCH] [-s|--skip-update] [-u|--uninstall]"
    echo "Install or uninstall TCA9548A symbolic links in Klipper or Kalico extras."
    echo "-b, --branch [BRANCH] selects or switches to a branch before installation."
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
        -b|--branch)
            if [[ $# -gt 1 && "${2}" != -* ]]; then
                REQUESTED_BRANCH="$2"
                shift 2
            else
                SELECT_BRANCH=1
                shift
            fi
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

validate_addon_sources() {
    local addon_path source_path
    for addon_path in "${ADDON_PATHS[@]}"; do
        source_path="${SCRIPT_DIR}/${addon_path}"
        if [[ ! -e "${source_path}" ]]; then
            echo "Add-on source not found: ${source_path}" >&2
            exit 1
        fi
    done
}

switch_repository_branch() {
    local previous_branch="${BRANCH}"
    local fetch_status

    if ! git -C "${SCRIPT_DIR}" check-ref-format --branch \
        "${REQUESTED_BRANCH}" >/dev/null 2>&1; then
        echo "Invalid branch name: ${REQUESTED_BRANCH}" >&2
        exit 2
    fi
    if [[ "${REQUESTED_BRANCH}" == "${BRANCH}" ]]; then
        BRANCH_SWITCH_DESCRIPTION="${REQUESTED_BRANCH} (already selected)"
        return
    fi

    if ! git -C "${SCRIPT_DIR}" show-ref --verify --quiet \
        "refs/heads/${REQUESTED_BRANCH}"; then
        if ! git -C "${SCRIPT_DIR}" show-ref --verify --quiet \
            "refs/remotes/origin/${REQUESTED_BRANCH}"; then
            if [[ "${SKIP_UPDATE}" -eq 1 ]]; then
                echo "Branch '${REQUESTED_BRANCH}' is not available locally." >&2
                echo "Re-run without -s to fetch it from origin." >&2
                exit 1
            fi
            if ! git -C "${SCRIPT_DIR}" remote get-url origin >/dev/null 2>&1; then
                echo "Branch '${REQUESTED_BRANCH}' is not available locally and no origin remote exists." >&2
                exit 1
            fi
            print_field "Branch fetch" "${REQUESTED_BRANCH} (10s timeout)"
            if ! command -v timeout >/dev/null 2>&1; then
                echo "Cannot fetch branch: the timeout command is unavailable." >&2
                exit 1
            fi
            GIT_TERMINAL_PROMPT=0 timeout --foreground \
                "${UPDATE_CHECK_TIMEOUT_SECONDS}s" \
                git -C "${SCRIPT_DIR}" fetch --quiet --no-tags origin \
                "refs/heads/${REQUESTED_BRANCH}:refs/remotes/origin/${REQUESTED_BRANCH}"
            fetch_status=$?
            if [[ "${fetch_status}" -ne 0 ]]; then
                if [[ "${fetch_status}" -eq 124 ]]; then
                    echo "Branch fetch timed out after ${UPDATE_CHECK_TIMEOUT_SECONDS}s." >&2
                else
                    echo "Unable to fetch branch '${REQUESTED_BRANCH}' from origin." >&2
                fi
                exit 1
            fi
            if ! git -C "${SCRIPT_DIR}" show-ref --verify --quiet \
                "refs/remotes/origin/${REQUESTED_BRANCH}"; then
                echo "Branch '${REQUESTED_BRANCH}' was not found on origin." >&2
                exit 1
            fi
        fi
        if ! git -C "${SCRIPT_DIR}" checkout -b "${REQUESTED_BRANCH}" \
            --track "origin/${REQUESTED_BRANCH}" >/dev/null 2>&1; then
            echo "Unable to create local branch '${REQUESTED_BRANCH}'." >&2
            exit 1
        fi
    elif ! git -C "${SCRIPT_DIR}" checkout "${REQUESTED_BRANCH}" \
        >/dev/null 2>&1; then
        echo "Unable to switch to branch '${REQUESTED_BRANCH}'." >&2
        echo "Commit, stash, or remove conflicting local changes first." >&2
        exit 1
    fi

    BRANCH="$(git -C "${SCRIPT_DIR}" branch --show-current 2>/dev/null || true)"
    if [[ -z "${BRANCH}" ]]; then
        echo "Branch switch did not result in an active branch." >&2
        exit 1
    fi
    BRANCH_SWITCH_DESCRIPTION="${previous_branch} -> ${BRANCH}"
}

select_repository_branch() {
    local branch_list_output fetch_status selection index
    local -a branches

    if [[ ! -t 0 || ! -t 1 ]]; then
        echo "Branch selection requires an interactive terminal." >&2
        echo "Use -b BRANCH to select a branch non-interactively." >&2
        exit 1
    fi

    print_section "Branch Selection"
    if [[ "${SKIP_UPDATE}" -eq 0 ]] \
        && git -C "${SCRIPT_DIR}" remote get-url origin >/dev/null 2>&1; then
        print_field "Branch list" "origin (10s timeout)"
        if command -v timeout >/dev/null 2>&1; then
            GIT_TERMINAL_PROMPT=0 timeout --foreground \
                "${UPDATE_CHECK_TIMEOUT_SECONDS}s" \
                git -C "${SCRIPT_DIR}" fetch --quiet --no-tags origin \
                '+refs/heads/*:refs/remotes/origin/*'
            fetch_status=$?
            if [[ "${fetch_status}" -eq 124 ]]; then
                print_status "${COLOR_WARN}" "Branch list" "timed out; using local cache"
            elif [[ "${fetch_status}" -ne 0 ]]; then
                print_status "${COLOR_WARN}" "Branch list" "failed; using local cache"
            fi
        else
            print_status "${COLOR_WARN}" "Branch list" "unavailable; using local cache"
        fi
    elif [[ "${SKIP_UPDATE}" -eq 1 ]]; then
        print_field "Branch list" "local cache (-s)"
    else
        print_field "Branch list" "local branches (no origin remote)"
    fi

    branch_list_output="$(git -C "${SCRIPT_DIR}" for-each-ref \
        --format='%(refname:short)' refs/heads refs/remotes/origin \
        | sed -e 's|^origin/||' -e '/^HEAD$/d' | sort -u)"
    mapfile -t branches <<< "${branch_list_output}"
    if [[ "${#branches[@]}" -eq 0 || -z "${branches[0]}" ]]; then
        echo "No local or origin branches are available." >&2
        exit 1
    fi

    echo ""
    for index in "${!branches[@]}"; do
        if [[ "${branches[${index}]}" == "${BRANCH}" ]]; then
            printf '  %d) %s (current)\n' "$((index + 1))" "${branches[${index}]}"
        else
            printf '  %d) %s\n' "$((index + 1))" "${branches[${index}]}"
        fi
    done
    echo ""
    if ! read -r -p "Select branch [1-${#branches[@]}] (Enter to keep ${BRANCH}): " selection; then
        selection=""
    fi
    if [[ -z "${selection}" ]]; then
        print_field "Branch switch" "kept ${BRANCH}"
        return
    fi
    if [[ ! "${selection}" =~ ^[0-9]+$ \
        || "${selection}" -lt 1 || "${selection}" -gt "${#branches[@]}" ]]; then
        echo "Invalid branch selection." >&2
        exit 2
    fi
    REQUESTED_BRANCH="${branches[$((selection - 1))]}"
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
    print_section "I2C Recovery Feature Support"
    print_field "Firmware" "${FIRMWARE_NAME}"
    print_field "Current version" "${FIRMWARE_VERSION}"
}

confirm_i2c_recovery_support() {
    detect_i2c_recovery_support
    display_firmware_summary
    if [[ "${I2C_RECOVERY_SUPPORTED}" -eq 1 ]]; then
        print_status "${COLOR_OK}" "Feature support" "SUPPORTED"
        print_field "Host protocol" "modern I2C status responses"
        return
    fi

    print_status "${COLOR_WARN}" "Feature support" "LEGACY - RECOVERY UNAVAILABLE" >&2
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
    BRANCH="$(git -C "${SCRIPT_DIR}" branch --show-current 2>/dev/null || true)"
    if [[ -z "${BRANCH}" ]]; then
        BRANCH="detached at $(git -C "${SCRIPT_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    fi
    print_banner
    print_recovery_notice
    print_section "Add-on Repository"
    if git -C "${SCRIPT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        && [[ "${SELECT_BRANCH}" -eq 1 ]]; then
        select_repository_branch
    elif [[ "${SELECT_BRANCH}" -eq 1 ]]; then
        echo "Cannot select branches: add-on directory is not a Git checkout." >&2
        exit 1
    fi
    if git -C "${SCRIPT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        && [[ -n "${REQUESTED_BRANCH}" ]]; then
        switch_repository_branch
        print_status "${COLOR_OK}" "Branch switch" "${BRANCH_SWITCH_DESCRIPTION}"
    elif [[ -n "${REQUESTED_BRANCH}" ]]; then
        echo "Cannot switch branches: add-on directory is not a Git checkout." >&2
        exit 1
    fi
    print_field "Branch" "${BRANCH}"

    if git -C "${SCRIPT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        check_repository_update
    else
        print_field "Update check" "skipped (not a Git checkout)"
    fi
    validate_addon_sources
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
