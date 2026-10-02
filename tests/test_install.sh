#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
TEMPORARY_DIRECTORY="$(mktemp -d)"
trap 'rm -rf -- "${TEMPORARY_DIRECTORY}"' EXIT

mkdir -p "${TEMPORARY_DIRECTORY}/bin"
cat > "${TEMPORARY_DIRECTORY}/bin/git" <<'EOF'
#!/usr/bin/env bash
case " $* " in
    *" branch --show-current "*) echo dev ;;
    *" rev-parse --is-inside-work-tree "*) echo true ;;
    *" rev-parse --abbrev-ref --symbolic-full-name @{upstream} "*) echo origin/dev ;;
    *" fetch "*) ;;
    *" rev-list --left-right --count HEAD...FETCH_HEAD "*)
        if [[ "${MOCK_UPDATE_AVAILABLE:-0}" == 1 ]]; then
            echo "0 2"
        else
            echo "0 0"
        fi
        ;;
    *" pull --ff-only "*) echo "Fast-forward" ;;
    *"/modern"*" describe "*) echo v0.13.0-772-gtest ;;
    *"/legacy"*" describe "*) echo v0.13.0-464-gtest ;;
esac
exit 0
EOF
chmod +x "${TEMPORARY_DIRECTORY}/bin/git"

create_firmware() {
    local name="$1"
    local modern="$2"
    local extras="${TEMPORARY_DIRECTORY}/${name}/klippy/extras"
    mkdir -p "${extras}"
    if [[ "${modern}" == 1 ]]; then
        cat > "${extras}/bus.py" <<'EOF'
i2c_transfer oid=%c write=%*s read_len=%u
i2c_response oid=%c i2c_bus_status=%c response=%*s
EOF
    else
        printf '%s\n' 'i2c_read oid=%c reg=%*s read_len=%u' > "${extras}/bus.py"
    fi
}

assert_installed() {
    local extras="$1/klippy/extras"
    [[ -L "${extras}/tca9548a.py" ]]
    [[ -L "${extras}/tca9548a_drivers" ]]
}

create_firmware modern 1
printf '%s\n' '# Legacy standalone add-on' \
    > "${TEMPORARY_DIRECTORY}/modern/klippy/extras/tca9548a.py"
mkdir "${TEMPORARY_DIRECTORY}/modern/klippy/extras/tca9548a_drivers"
printf '%s\n' '# Legacy driver package' \
    > "${TEMPORARY_DIRECTORY}/modern/klippy/extras/tca9548a_drivers/_legacy_marker"
PATH="${TEMPORARY_DIRECTORY}/bin:${PATH}" \
    bash "${REPOSITORY}/install.sh" \
    --firmware-dir "${TEMPORARY_DIRECTORY}/modern" \
    > "${TEMPORARY_DIRECTORY}/modern.log"
assert_installed "${TEMPORARY_DIRECTORY}/modern"
[[ ! -e "${TEMPORARY_DIRECTORY}/modern/klippy/extras/tca9548a_drivers/_legacy_marker" ]]
grep -Eq '^Klipper check:[[:space:]]+SUPPORTED$' \
    "${TEMPORARY_DIRECTORY}/modern.log"
grep -Eq '^tca9548a\.py:[[:space:]]+linked$' \
    "${TEMPORARY_DIRECTORY}/modern.log"
grep -Eq '^tca9548a_drivers:[[:space:]]+linked$' \
    "${TEMPORARY_DIRECTORY}/modern.log"
grep -Fq 'Current version:         v0.13.0-772-gtest' \
    "${TEMPORARY_DIRECTORY}/modern.log"
grep -Fq 'Minimum version:         >= v0.13.0-525-g8965958' \
    "${TEMPORARY_DIRECTORY}/modern.log"
grep -Eq '^Update:[[:space:]]+current$' "${TEMPORARY_DIRECTORY}/modern.log"

MOCK_UPDATE_AVAILABLE=1 PATH="${TEMPORARY_DIRECTORY}/bin:${PATH}" \
    bash "${REPOSITORY}/install.sh" \
    --firmware-dir "${TEMPORARY_DIRECTORY}/modern" \
    > "${TEMPORARY_DIRECTORY}/update-available.log" 2>&1
grep -Eq '^Update:[[:space:]]+available \(2 commits\)$' \
    "${TEMPORARY_DIRECTORY}/update-available.log"
grep -Eq '^Update:[[:space:]]+skipped \(non-interactive\)$' \
    "${TEMPORARY_DIRECTORY}/update-available.log"

PATH="${TEMPORARY_DIRECTORY}/bin:${PATH}" \
    bash "${REPOSITORY}/install.sh" \
    --firmware-dir "${TEMPORARY_DIRECTORY}/modern" --skip-update \
    > "${TEMPORARY_DIRECTORY}/skip-update.log"
grep -Fq 'Update check:           skipped (-s)' \
    "${TEMPORARY_DIRECTORY}/skip-update.log"
create_firmware legacy 0
if PATH="${TEMPORARY_DIRECTORY}/bin:${PATH}" \
    bash "${REPOSITORY}/install.sh" \
    --firmware-dir "${TEMPORARY_DIRECTORY}/legacy" \
    > "${TEMPORARY_DIRECTORY}/legacy.log" 2>&1; then
    echo "Legacy non-interactive installation unexpectedly succeeded" >&2
    exit 1
fi
grep -Fq 'Refusing non-interactive legacy installation' \
    "${TEMPORARY_DIRECTORY}/legacy.log"
grep -Fq 'Current version:         v0.13.0-464-gtest' \
    "${TEMPORARY_DIRECTORY}/legacy.log"
grep -Fq 'Minimum version:         >= v0.13.0-525-g8965958' \
    "${TEMPORARY_DIRECTORY}/legacy.log"

PATH="${TEMPORARY_DIRECTORY}/bin:${PATH}" \
    bash "${REPOSITORY}/install.sh" \
    --firmware-dir "${TEMPORARY_DIRECTORY}/legacy" --allow-legacy-i2c \
    > "${TEMPORARY_DIRECTORY}/legacy-allowed.log" 2>&1
assert_installed "${TEMPORARY_DIRECTORY}/legacy"
grep -Fq 'Continuing because --allow-legacy-i2c was supplied.' \
    "${TEMPORARY_DIRECTORY}/legacy-allowed.log"
