#!/bin/bash
# Admin wrapper for the garmin-mcp service, installed as /usr/local/bin/garmin-mcp
# by install.sh (garmin-mcp-wrapper: uninstall.sh looks for this marker).
#
#   sudo garmin-mcp login    interactive Garmin login (handles MFA), saves tokens
#   sudo garmin-mcp check    verify that the saved tokens or credentials work
#
# Both run the installed program the way the service runs it: as the garmin-mcp
# user, with /etc/garmin-mcp/garmin-mcp.env loaded by systemd, and with the same
# state directory, so tokens end up in /var/lib/garmin-mcp/tokens with the right
# owner. They run as a short-lived transient systemd unit attached to this
# terminal.

set -euo pipefail

readonly SERVICE=garmin-mcp.service
readonly RUN_USER=garmin-mcp
readonly ENV_FILE=/etc/garmin-mcp/garmin-mcp.env
readonly STATE_NAME=garmin-mcp
readonly BIN=/opt/garmin-mcp/venv/bin/garmin-mcp

usage() {
    cat <<EOF
Usage: sudo garmin-mcp <command>

  login    log in to Garmin interactively (prompts for email, password and MFA
           code as needed), save tokens, then restart the service
  check    verify that the saved tokens or credentials work

The server itself is managed by systemd:
  systemctl status garmin-mcp        sudo systemctl restart garmin-mcp
  journalctl -u garmin-mcp -f        config: $ENV_FILE
EOF
}

die() {
    echo "garmin-mcp: $*" >&2
    exit 1
}

command=${1:-}
case "$command" in
    login | check) ;;
    serve)
        die "the server runs as a systemd service; use: sudo systemctl restart garmin-mcp"
        ;;
    --version)
        exec "$BIN" --version
        ;;
    -h | --help | help | "")
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
[[ $# -eq 1 ]] || die "'$command' takes no arguments"

[[ $EUID -eq 0 ]] || die "must be run as root: sudo garmin-mcp $command"
[[ -x $BIN ]] || die "$BIN not found; run install.sh first"
[[ -f $ENV_FILE ]] || die "$ENV_FILE not found; run install.sh first"
if [[ $command == login && ! (-t 0 && -t 1 && -t 2) ]]; then
    die "login is interactive and needs a terminal"
fi

# --pty --pipe: a pseudo terminal when run from a terminal (needed for the
# prompts), plain inherited stdio otherwise (so `check` works in scripts).
# systemd-run exits with the exit status of the command.
status=0
systemd-run --quiet --wait --collect --pty --pipe \
    --description="garmin-mcp $command (admin)" \
    --property=User="$RUN_USER" \
    --property=Group="$RUN_USER" \
    --property=EnvironmentFile="$ENV_FILE" \
    --property=StateDirectory="$STATE_NAME" \
    --property=StateDirectoryMode=0700 \
    --property=UMask=0077 \
    --property=NoNewPrivileges=yes \
    --property=CapabilityBoundingSet= \
    --property=ProtectSystem=strict \
    --property=ProtectHome=yes \
    --property=PrivateTmp=yes \
    "$BIN" "$command" || status=$?

if [[ $command == login && $status -eq 0 ]]; then
    # The running service still holds its old session and, after a failed
    # login, a retry cooldown. Restart it so it loads the new tokens now.
    if systemctl is-active --quiet "$SERVICE"; then
        if systemctl restart "$SERVICE"; then
            echo "Restarted $SERVICE to pick up the new tokens."
        else
            echo "garmin-mcp: could not restart $SERVICE; run: sudo systemctl restart garmin-mcp" >&2
            status=1
        fi
    else
        echo "$SERVICE is not running; start it with: sudo systemctl start garmin-mcp"
    fi
fi

exit "$status"
