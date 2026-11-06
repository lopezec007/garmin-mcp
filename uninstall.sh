#!/bin/bash
# Remove the garmin-mcp service installed by install.sh.
#
#   sudo ./uninstall.sh            remove the service, the program and the wrapper;
#                                  keep the config and the Garmin tokens
#   sudo ./uninstall.sh --purge    also remove the config, the tokens and the
#                                  garmin-mcp user and group

set -euo pipefail

readonly PREFIX=/opt/garmin-mcp
readonly CONF_DIR=/etc/garmin-mcp
readonly STATE_DIR=/var/lib/garmin-mcp
readonly UNIT=garmin-mcp.service
readonly UNIT_PATH=/etc/systemd/system/$UNIT
readonly DROPIN_DIR=/etc/systemd/system/$UNIT.d
readonly WRAPPER=/usr/local/bin/garmin-mcp
readonly WRAPPER_MARKER=garmin-mcp-wrapper
readonly RUN_USER=garmin-mcp

log() { printf '==> %s\n' "$*"; }
warn() { printf 'uninstall.sh: warning: %s\n' "$*" >&2; }
die() {
    printf 'uninstall.sh: error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: sudo ./uninstall.sh [--purge]

Remove the garmin-mcp systemd service, $PREFIX and $WRAPPER.

  --purge      also remove $CONF_DIR (config), $STATE_DIR (Garmin tokens),
               unit drop-ins and the $RUN_USER user and group
  -h, --help   show this help
EOF
}

purge=false
for arg in "$@"; do
    case "$arg" in
        --purge) purge=true ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done

[[ $EUID -eq 0 ]] || die "must be run as root: sudo ./uninstall.sh"

have_systemd=false
if [[ -d /run/systemd/system ]]; then
    have_systemd=true
fi

if $have_systemd && [[ -e $UNIT_PATH ]]; then
    log "Stopping and disabling $UNIT"
    systemctl disable --now "$UNIT" || warn "could not stop/disable $UNIT; continuing"
fi

log "Removing $UNIT_PATH"
rm -f -- "$UNIT_PATH"
if $have_systemd; then
    systemctl daemon-reload
    # Forget a failed state left over from the removed unit, if any.
    systemctl reset-failed "$UNIT" 2>/dev/null || true
fi

if [[ -e $WRAPPER || -L $WRAPPER ]]; then
    if grep -qs "$WRAPPER_MARKER" -- "$WRAPPER"; then
        log "Removing $WRAPPER"
        rm -f -- "$WRAPPER"
    else
        warn "$WRAPPER was not installed by install.sh; leaving it alone"
    fi
fi

log "Removing $PREFIX"
rm -rf -- "$PREFIX"

if $purge; then
    log "Removing $CONF_DIR, $STATE_DIR and unit drop-ins"
    rm -rf -- "$CONF_DIR" "$STATE_DIR" "$DROPIN_DIR"
    if $have_systemd; then
        systemctl daemon-reload
    fi
    if getent passwd "$RUN_USER" >/dev/null; then
        log "Removing user $RUN_USER"
        userdel "$RUN_USER" || warn "could not remove user $RUN_USER"
    fi
    # userdel normally removes the user's own group as well.
    if getent group "$RUN_USER" >/dev/null; then
        log "Removing group $RUN_USER"
        groupdel "$RUN_USER" || warn "could not remove group $RUN_USER"
    fi
    echo
    echo "garmin-mcp is removed, including its config and tokens."
else
    echo
    echo "garmin-mcp is removed. Kept (re-used if you install again):"
    if [[ -e $CONF_DIR ]]; then
        echo "  $CONF_DIR   config, including the bearer token and any stored password"
    fi
    if [[ -e $STATE_DIR ]]; then
        echo "  $STATE_DIR   Garmin tokens"
    fi
    if [[ -e $DROPIN_DIR ]]; then
        echo "  $DROPIN_DIR   your unit drop-ins"
    fi
    if getent passwd "$RUN_USER" >/dev/null; then
        echo "  the $RUN_USER user and group"
    fi
    echo "Remove these too with: sudo ./uninstall.sh --purge"
fi
