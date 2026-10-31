#!/usr/bin/env bash
# Installer for oracle-keepalive.  Installs only the files that sit next to it;
# never downloads anything.
#
#   sudo ./install.sh                 install (or upgrade) and start
#   sudo ./install.sh --dry-run       show what would happen, change nothing
#   sudo ./install.sh --uninstall     stop and remove files, keep the config
#   sudo ./install.sh --uninstall --purge
#                                     also remove the config and the state dir
#   sudo ./install.sh --replace-eat   remove MoeClub's eat.service first
#
set -euo pipefail

PROG="oracle-keepalive"
UNIT_NAME="${PROG}.service"
LIB_DIR="/usr/local/lib/${PROG}"
UNIT_FILE="/etc/systemd/system/${UNIT_NAME}"
CONF_FILE="/etc/oracle-keepalive.conf"
DROPIN_DIR="/etc/systemd/system/${UNIT_NAME}.d"
DROPIN_FILE="${DROPIN_DIR}/memory.conf"
DEFAULT_STATE_DIR="/var/lib/oracle-keepalive"
SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

DRY_RUN=0
UNINSTALL=0
PURGE=0
REPLACE_EAT=0
STATE_DIR_SKIPPED=0
REMOVED=()

usage() {
    cat <<EOF
Usage: ${0##*/} [options]

Options:
  --dry-run       Print every action, change nothing (no root required).
  --uninstall     Stop and disable the service, remove unit, drop-in and program.
  --purge         With --uninstall: also remove ${CONF_FILE} and the state dir.
  --replace-eat   Remove MoeClub's eat.service and /run/shm/MemoryEat.bin first.
  -h, --help      This help.

Notes:
  * Needs root except with --dry-run.
  * Installs from the files in ${SRC_DIR}; nothing is downloaded.
  * An existing ${CONF_FILE} is never overwritten.
EOF
}

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

for arg in "$@"; do
    case "${arg}" in
        --dry-run)     DRY_RUN=1 ;;
        --uninstall)   UNINSTALL=1 ;;
        --purge)       PURGE=1 ;;
        --replace-eat) REPLACE_EAT=1 ;;
        -h|--help)     usage; exit 0 ;;
        *)             usage >&2; die "unknown argument: ${arg}" ;;
    esac
done

if [ "${PURGE}" -eq 1 ] && [ "${UNINSTALL}" -eq 0 ]; then
    die "--purge only makes sense together with --uninstall"
fi

# ---------------------------------------------------------------- helpers

conf_get() {  # conf_get NAME DEFAULT  -> value of the last matching uncommented line
    local name="$1" def="$2" line=""
    if [ -f "${CONF_FILE}" ]; then
        line="$(grep -E "^[[:space:]]*${name}=" "${CONF_FILE}" | tail -n 1 || true)"
    fi
    if [ -z "${line}" ]; then
        printf '%s' "${def}"
        return 0
    fi
    line="${line#*=}"
    line="${line%\"}"; line="${line#\"}"
    line="${line%\'}"; line="${line#\'}"
    printf '%s' "${line}"
}

memtotal_kb() {
    awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0
}

# KEEPALIVE_STATE_DIR comes from a file the user is expected to edit, and
# --purge deletes it recursively.  Two levels of checking, deliberately:
#   state_dir_path_ok : absolute, not empty, no ".." component
#                       (enough to read from it, or to hand it to the program)
#   safe_state_dir    : the above, plus at least three components and a top
#                       level that can plausibly be ours (var/lib, var/cache,
#                       opt/*).  Only this one unlocks rm -rf.
state_dir_path_ok() {
    local path="$1"
    [ -n "${path}" ] || return 1
    case "${path}" in
        /*) ;;
        *) return 1 ;;
    esac
    case "/${path}/" in
        */../*) return 1 ;;
    esac
    return 0
}

state_dir_components() {  # one component per line; empty and "." are dropped
    local parts=() part
    IFS='/' read -r -a parts <<< "$1"
    for part in "${parts[@]}"; do
        case "${part}" in
            ''|.) continue ;;
        esac
        printf '%s\n' "${part}"
    done
}

safe_state_dir() {
    local path="$1" count first second
    local comps=()
    if ! state_dir_path_ok "${path}"; then
        printf 'error: KEEPALIVE_STATE_DIR=%s is not an absolute path without a ".." component; refusing to delete it\n' \
            "${path}" >&2
        return 1
    fi
    mapfile -t comps < <(state_dir_components "${path}")
    count="${#comps[@]}"
    if [ "${count}" -lt 3 ]; then
        printf 'error: KEEPALIVE_STATE_DIR=%s has only %d component(s), need at least 3; refusing to delete it\n' \
            "${path}" "${count}" >&2
        return 1
    fi
    first="${comps[0]}"
    second="${comps[1]}"
    case "${first}/${second}" in
        var/lib|var/cache) return 0 ;;
    esac
    if [ "${first}" = "opt" ]; then
        return 0
    fi
    printf 'error: KEEPALIVE_STATE_DIR=%s is not under /var/lib, /var/cache or /opt/*; refusing to delete it\n' \
        "${path}" >&2
    return 1
}

# Every other removal target must be one of the constants at the top of this
# script; nothing else is ever passed to rm -rf.
is_fixed_target() {
    local candidate="$1" known
    for known in "${LIB_DIR}" "${UNIT_FILE}" "${CONF_FILE}" "${DROPIN_DIR}" "${DROPIN_FILE}"; do
        [ "${candidate}" = "${known}" ] && return 0
    done
    return 1
}

remove_fixed() {
    local target="$1"
    if ! is_fixed_target "${target}"; then
        printf 'error: refusing to remove %s: not one of the fixed install paths\n' "${target}" >&2
        return 1
    fi
    run rm -rf "${target}"
}

eat_unit_files() {
    local d found=()
    for d in /etc/systemd/system /run/systemd/system /lib/systemd/system /usr/lib/systemd/system; do
        [ -f "${d}/eat.service" ] && found+=("${d}/eat.service")
    done
    printf '%s\n' "${found[@]:-}" | sed '/^$/d'
}

check_prereqs() {
    step "checking prerequisites"
    if [ "${DRY_RUN}" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
        die "must run as root (or use --dry-run)"
    fi
    command -v "${PYTHON}" >/dev/null 2>&1 || die "${PYTHON} not found"
    if ! "${PYTHON}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
        die "${PYTHON} is older than 3.8 ($("${PYTHON}" -V 2>&1)); this program needs 3.8+"
    fi
    say "python: $("${PYTHON}" -V 2>&1) at $(command -v "${PYTHON}")"
    command -v systemctl >/dev/null 2>&1 || die "systemd (systemctl) not found; this is a systemd-only installer"
    say "systemd: $(systemctl --version 2>/dev/null | head -n 1 || echo 'version unknown')"
    for f in "${SRC_DIR}/oracle-keepalive.py" "${SRC_DIR}/oracle-keepalive.service" \
             "${SRC_DIR}/oracle-keepalive.conf.example"; do
        [ -f "${f}" ] || die "missing file: ${f}"
    done
    say "source: ${SRC_DIR}"
}

remove_eat() {
    local units unit
    units="$(eat_unit_files)"
    if [ -z "${units}" ]; then
        say "eat.service is not installed; nothing to replace"
        return 0
    fi
    step "removing MoeClub eat.service (--replace-eat)"
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] systemctl disable --now eat.service\n'
    else
        systemctl disable --now eat.service 2>/dev/null || true
    fi
    while IFS= read -r unit; do
        [ -n "${unit}" ] || continue
        case "${unit}" in
            /etc/systemd/system/eat.service|/run/systemd/system/eat.service|\
            /lib/systemd/system/eat.service|/usr/lib/systemd/system/eat.service) ;;
            *) printf 'error: refusing to remove unexpected path %s\n' "${unit}" >&2
               continue ;;
        esac
        run rm -f "${unit}"
        REMOVED+=("${unit}")
    done <<< "${units}"
    run rm -f /run/shm/MemoryEat.bin
    if [ "${DRY_RUN}" -eq 0 ]; then systemctl daemon-reload; else printf '[dry-run] systemctl daemon-reload\n'; fi
}

# status/plan read the environment, exactly like the unit does. When the
# installer runs them, feed it whatever /etc/oracle-keepalive.conf currently
# says so the output reflects the real configuration and not the defaults.
run_status() {
    local name val
    local env_args=()
    for name in KEEPALIVE_MODE KEEPALIVE_CPU_DUTY KEEPALIVE_CPU_BURST_SECONDS \
                KEEPALIVE_CPU_WORKERS KEEPALIVE_MEM_TARGET KEEPALIVE_MEM_MAX_HOLD \
                KEEPALIVE_MEM_FLOOR KEEPALIVE_MEM_STEP_MB KEEPALIVE_TICK_SECONDS \
                KEEPALIVE_SAMPLE_SECONDS KEEPALIVE_STATE_DIR; do
        val="$(conf_get "${name}" "")"
        if [ -n "${val}" ] && [ "${name}" = "KEEPALIVE_STATE_DIR" ] && ! state_dir_path_ok "${val}"; then
            printf 'warning: %s=%s in %s is not an absolute path without ".."; not passing it on\n' \
                "${name}" "${val}" "${CONF_FILE}" >&2
            continue
        fi
        [ -n "${val}" ] && env_args+=("${name}=${val}")
    done
    if [ "${#env_args[@]}" -gt 0 ]; then
        env "${env_args[@]}" "${PYTHON}" "${LIB_DIR}/oracle-keepalive.py" status || true
    else
        "${PYTHON}" "${LIB_DIR}/oracle-keepalive.py" status || true
    fi
}

mem_mode_enabled() {
    # Decides whether the effective mode includes the memory hold, mirroring
    # resolve_mode() in the Python program.
    local mode machine total_kb
    mode="$(conf_get KEEPALIVE_MODE auto | tr '[:upper:]' '[:lower:]')"
    machine="$(uname -m)"
    total_kb="$(memtotal_kb)"
    case "${mode}" in
        auto)
            case "${machine}" in
                aarch64|arm64) mode="cpu+mem" ;;
                *)             mode="cpu" ;;
            esac
            ;;
        off) return 1 ;;
        cpu|mem|cpu+mem) ;;
        *) mode="cpu" ;;
    esac
    case "${mode}" in
        *mem*) ;;
        *) return 1 ;;
    esac
    if [ "${total_kb}" -gt 0 ] && [ "${total_kb}" -lt 2097152 ]; then
        return 1
    fi
    return 0
}

do_install() {
    check_prereqs

    local units
    units="$(eat_unit_files)"
    if [ -n "${units}" ]; then
        if [ "${REPLACE_EAT}" -eq 1 ]; then
            remove_eat
        else
            printf 'error: MoeClub eat.service is already installed:\n' >&2
            printf '  %s\n' "${units}" >&2
            cat >&2 <<EOF

Both units would fight over the same resources: eat.service writes zeros into a
tmpfs file every 15 s (holding 20-24% of RAM, above Oracle's 20% line), while
${UNIT_NAME} uses anonymous mmap and SCHED_IDLE bursts.

Choose one:
  * sudo ${0##*/} --replace-eat   remove eat.service and install this instead
  * or remove eat.service yourself first:
      systemctl disable --now eat.service
      rm /etc/systemd/system/eat.service && systemctl daemon-reload
EOF
            exit 1
        fi
    fi

    step "installing files"
    run install -d -m 0755 "${LIB_DIR}"
    run install -m 0755 -o root -g root "${SRC_DIR}/oracle-keepalive.py" "${LIB_DIR}/oracle-keepalive.py"
    say "  ${LIB_DIR}/oracle-keepalive.py"
    run install -m 0644 -o root -g root "${SRC_DIR}/oracle-keepalive.service" "${UNIT_FILE}"
    say "  ${UNIT_FILE}"

    if [ -f "${CONF_FILE}" ]; then
        say "  keeping existing ${CONF_FILE}"
    else
        run install -m 0644 -o root -g root "${SRC_DIR}/oracle-keepalive.conf.example" "${CONF_FILE}"
        say "  ${CONF_FILE} (from the example)"
    fi

    step "memory cap drop-in"
    if mem_mode_enabled; then
        local total_kb mem_max_mb
        total_kb="$(memtotal_kb)"
        [ "${total_kb}" -gt 0 ] || die "cannot read MemTotal from /proc/meminfo"
        mem_max_mb=$(( total_kb / 1024 * 45 / 100 ))
        [ "${mem_max_mb}" -ge 64 ] || mem_max_mb=64
        run install -d -m 0755 "${DROPIN_DIR}"
        if [ "${DRY_RUN}" -eq 1 ]; then
            printf '[dry-run] write %s with MemoryMax=%sM\n' "${DROPIN_FILE}" "${mem_max_mb}"
        else
            local tmp
            tmp="$(mktemp)"
            printf '[Service]\n# Added by oracle-keepalive install.sh: 45%% of MemTotal (%s).\nMemoryMax=%sM\n' \
                "$(printf '%.1f GiB' "$(awk -v k="${total_kb}" 'BEGIN {print k/1048576}')")" \
                "${mem_max_mb}" > "${tmp}"
            install -m 0644 -o root -g root "${tmp}" "${DROPIN_FILE}"
            rm -f "${tmp}"
            say "  ${DROPIN_FILE}: MemoryMax=${mem_max_mb}M (45% of ${total_kb} kB)"
        fi
    else
        say "  memory hold not active on this host; no MemoryMax drop-in needed"
        if [ -f "${DROPIN_FILE}" ]; then
            run rm -f "${DROPIN_FILE}"
            say "  removed stale ${DROPIN_FILE}"
        fi
        if [ -d "${DROPIN_DIR}" ] && [ "${DRY_RUN}" -eq 0 ]; then
            rmdir "${DROPIN_DIR}" 2>/dev/null || true
        fi
    fi

    step "enabling and starting"
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] systemctl daemon-reload\n'
        printf '[dry-run] systemctl enable --now %s\n' "${UNIT_NAME}"
        printf '[dry-run] %s %s status  (environment taken from %s)\n' \
            "${PYTHON}" "${LIB_DIR}/oracle-keepalive.py" "${CONF_FILE}"
        say ""
        say "dry run complete; nothing was changed"
        return 0
    fi
    systemctl daemon-reload
    systemctl enable --now "${UNIT_NAME}"

    local i active=0
    for i in $(seq 1 15); do
        if systemctl is-active --quiet "${UNIT_NAME}"; then
            active=1
            break
        fi
        sleep 1
    done
    if [ "${active}" -ne 1 ]; then
        die "${UNIT_NAME} did not become active within 15 s; check: journalctl -u ${UNIT_NAME} -n 50"
    fi
    say "  ${UNIT_NAME} is active"

    step "status"
    run_status

    cat <<EOF

Installed. Useful commands:
  systemctl status ${UNIT_NAME}
  python3 ${LIB_DIR}/oracle-keepalive.py status
  python3 ${LIB_DIR}/oracle-keepalive.py plan
  journalctl -u ${UNIT_NAME} -f
  sudo ${0##*/} --uninstall
EOF
}

do_uninstall() {
    if [ "${DRY_RUN}" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
        die "must run as root (or use --dry-run)"
    fi
    step "stopping and disabling ${UNIT_NAME}"
    if command -v systemctl >/dev/null 2>&1; then
        if [ "${DRY_RUN}" -eq 1 ]; then
            printf '[dry-run] systemctl disable --now %s\n' "${UNIT_NAME}"
        else
            systemctl disable --now "${UNIT_NAME}" 2>/dev/null || say "  (was not enabled/running)"
        fi
    else
        say "  systemctl not found; skipping"
    fi

    step "removing files"
    local path
    for path in "${UNIT_FILE}" "${DROPIN_FILE}" "${DROPIN_DIR}" "${LIB_DIR}"; do
        if [ -e "${path}" ]; then
            remove_fixed "${path}"
            REMOVED+=("${path}")
            say "  removed ${path}"
        fi
    done

    if [ "${PURGE}" -eq 1 ]; then
        if [ -f "${CONF_FILE}" ]; then
            remove_fixed "${CONF_FILE}"
            REMOVED+=("${CONF_FILE}")
            say "  removed ${CONF_FILE}"
        fi
        local state_dir
        state_dir="$(conf_get KEEPALIVE_STATE_DIR "${DEFAULT_STATE_DIR}")"
        if safe_state_dir "${state_dir}"; then
            if [ -d "${state_dir}" ]; then
                run rm -rf "${state_dir}"
                REMOVED+=("${state_dir}")
                say "  removed ${state_dir}"
            else
                say "  state dir ${state_dir} was not present"
            fi
        else
            STATE_DIR_SKIPPED=1
            say "  skipped the state dir; fix KEEPALIVE_STATE_DIR and re-run --purge"
        fi
    elif [ -f "${CONF_FILE}" ]; then
        say "  kept ${CONF_FILE} (use --uninstall --purge to remove it and the state dir)"
    else
        say "  ${CONF_FILE} was not present"
    fi

    if command -v systemctl >/dev/null 2>&1; then
        if [ "${DRY_RUN}" -eq 1 ]; then
            printf '[dry-run] systemctl daemon-reload\n'
        else
            systemctl daemon-reload || true
        fi
    fi

    step "done"
    if [ "${#REMOVED[@]}" -eq 0 ]; then
        say "nothing was installed; nothing to remove"
    else
        say "removed ${#REMOVED[@]} path(s)"
    fi
    [ "${DRY_RUN}" -eq 1 ] && say "(dry run: nothing was actually changed)"
    if [ "${STATE_DIR_SKIPPED}" -eq 1 ]; then
        printf 'error: uninstall finished, but the state dir was left in place because KEEPALIVE_STATE_DIR is unsafe\n' >&2
        return 1
    fi
    return 0
}

if [ "${UNINSTALL}" -eq 1 ]; then
    do_uninstall
else
    do_install
fi
