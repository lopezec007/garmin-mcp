#!/bin/bash
# Install or upgrade garmin-mcp as a systemd service (Debian 13; no containers).
#
#   sudo ./install.sh [--upgrade-deps]
#
# Safe to re-run: it rebuilds the package in /opt/garmin-mcp/venv, refreshes the
# unit and the admin wrapper and restarts the service. The config in
# /etc/garmin-mcp and the state in /var/lib/garmin-mcp (Garmin tokens, OAuth
# clients and owner password) are never overwritten.
# The package is installed as a copy, so this checkout is not needed afterwards
# (keep it for upgrades and for uninstall.sh).
#
# Layout:
#   /opt/garmin-mcp/venv                      virtualenv with the package
#   /etc/garmin-mcp/garmin-mcp.env            settings and secrets (root:garmin-mcp 0640)
#   /var/lib/garmin-mcp                       state: Garmin tokens, OAuth database (created by systemd)
#   /etc/systemd/system/garmin-mcp.service    the unit
#   /usr/local/bin/garmin-mcp                 admin wrapper: sudo garmin-mcp login|check|set-password|...
#
# Environment:
#   PYTHON   base interpreter for the virtualenv (default /usr/bin/python3, the
#            Debian one, so that a python3 in /usr/local/bin is never picked up)

set -euo pipefail
# Everything installed here must be readable by the service user, whatever
# root's own umask is.
umask 022

readonly PREFIX=/opt/garmin-mcp
readonly VENV=$PREFIX/venv
readonly CONF_DIR=/etc/garmin-mcp
readonly ENV_FILE=$CONF_DIR/garmin-mcp.env
readonly STATE_DIR=/var/lib/garmin-mcp
readonly UNIT=garmin-mcp.service
readonly UNIT_PATH=/etc/systemd/system/$UNIT
readonly WRAPPER=/usr/local/bin/garmin-mcp
readonly RUN_USER=garmin-mcp
readonly MIN_PYTHON=3.12
readonly START_TIMEOUT=30

PYTHON=${PYTHON:-/usr/bin/python3}
SRC_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly PYTHON SRC_DIR

log() { printf '==> %s\n' "$*"; }
warn() { printf 'install.sh: warning: %s\n' "$*" >&2; }
die() {
    printf 'install.sh: error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: sudo ./install.sh [--upgrade-deps]

Install or upgrade the garmin-mcp systemd service from this checkout.

  --upgrade-deps   also upgrade every dependency to the newest version that
                   pyproject.toml allows (default: only upgrade what must change)
  -h, --help       show this help
EOF
}

# --- arguments and preconditions -------------------------------------------------

upgrade_deps=false
for arg in "$@"; do
    case "$arg" in
        --upgrade-deps) upgrade_deps=true ;;
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

[[ $EUID -eq 0 ]] || die "must be run as root: sudo ./install.sh"
[[ -d /run/systemd/system ]] || die "systemd is not running as the init system here"
for file in pyproject.toml deploy/garmin-mcp.service deploy/garmin-mcp.env.example \
    deploy/garmin-mcp-wrapper.sh; do
    [[ -f $SRC_DIR/$file ]] || die "$SRC_DIR/$file is missing; run install.sh from a complete checkout"
done

trap 'printf "install.sh: failed at line %s; nothing after that step was done\n" "$LINENO" >&2' ERR

# --- 1. prerequisites ------------------------------------------------------------

pkg_installed() {
    [[ $(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null) == installed ]]
}

has_venv_support() {
    "$PYTHON" -c 'import ensurepip, venv' >/dev/null 2>&1
}

# With the default interpreter, install the Debian packages that are missing.
# python3-venv is not part of a default Debian install; without it
# `python3 -m venv` cannot bootstrap pip.
if [[ $PYTHON == /usr/bin/python3 ]] && command -v dpkg-query >/dev/null &&
    command -v apt-get >/dev/null; then
    missing=()
    for pkg in python3 python3-venv; do
        pkg_installed "$pkg" || missing+=("$pkg")
    done
    if ((${#missing[@]} > 0)); then
        log "Installing missing packages: ${missing[*]}"
        export DEBIAN_FRONTEND=noninteractive
        apt-get update || warn "apt-get update failed; trying to install anyway"
        apt-get install -y --no-install-recommends "${missing[@]}"
    fi
fi

[[ -x $PYTHON ]] || die "$PYTHON not found (install python3, or set PYTHON=/path/to/python3)"
py_version=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! "$PYTHON" -c "import sys; sys.exit(sys.version_info[:2] < tuple(map(int, '$MIN_PYTHON'.split('.'))))"; then
    die "$PYTHON is Python $py_version, but garmin-mcp needs Python $MIN_PYTHON or newer. \
Debian 13 ships 3.13; on an older system set PYTHON=/path/to/a/newer/python3."
fi
has_venv_support ||
    die "$PYTHON cannot create virtualenvs (the venv/ensurepip modules are missing); install python3-venv"
log "Using Python $py_version ($PYTHON)"

# --- 2. service user -------------------------------------------------------------

if ! getent group "$RUN_USER" >/dev/null; then
    log "Creating system group $RUN_USER"
    groupadd --system "$RUN_USER"
fi
if ! getent passwd "$RUN_USER" >/dev/null; then
    log "Creating system user $RUN_USER"
    useradd --system --gid "$RUN_USER" --home-dir /nonexistent --no-create-home \
        --shell /usr/sbin/nologin --comment "Garmin MCP server" "$RUN_USER"
fi

# --- 3. virtualenv and package ---------------------------------------------------

venv_usable() {
    [[ -x $VENV/bin/python ]] &&
        [[ $("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) == "$py_version" ]] &&
        "$VENV/bin/python" -m pip --version >/dev/null 2>&1
}

install -d -m 0755 "$PREFIX"
if venv_usable; then
    log "Reusing virtualenv $VENV"
else
    # Missing, broken, or built for another Python version (for example after
    # a Debian release upgrade): start from an empty one.
    log "Creating virtualenv $VENV"
    "$PYTHON" -m venv --clear "$VENV"
fi

log "Installing garmin-mcp from $SRC_DIR"
pip_args=(install --disable-pip-version-check --no-input --upgrade)
if $upgrade_deps; then
    pip_args+=(--upgrade-strategy eager)
fi
"$VENV/bin/python" -m pip "${pip_args[@]}" "$SRC_DIR"
installed_version=$("$VENV/bin/garmin-mcp" --version)
log "Installed $installed_version"

# --- 4. configuration (kept on upgrade) ------------------------------------------

install -d -m 0750 -o root -g "$RUN_USER" "$CONF_DIR"
# Always refreshed, for reference when a new version adds settings.
install -m 0640 -o root -g "$RUN_USER" "$SRC_DIR/deploy/garmin-mcp.env.example" \
    "$CONF_DIR/garmin-mcp.env.example"
if [[ -e $ENV_FILE ]]; then
    log "Keeping existing $ENV_FILE"
else
    log "Creating $ENV_FILE with a freshly generated GARMIN_MCP_AUTH_TOKEN"
    install -m 0640 -o root -g "$RUN_USER" "$SRC_DIR/deploy/garmin-mcp.env.example" "$ENV_FILE"
    # Secure by default: the endpoint never comes up open on the LAN. The token
    # is hex, so it is safe in sed and needs no quoting in the env file.
    auth_token=$("$VENV/bin/python" -c 'import secrets; print(secrets.token_hex(32))')
    sed -i "s|^#GARMIN_MCP_AUTH_TOKEN=.*|GARMIN_MCP_AUTH_TOKEN=$auth_token|" "$ENV_FILE"
    grep -qx "GARMIN_MCP_AUTH_TOKEN=$auth_token" "$ENV_FILE" ||
        warn "could not write GARMIN_MCP_AUTH_TOKEN to $ENV_FILE; set it by hand"
    unset auth_token
fi

# --- 5. unit and admin wrapper ---------------------------------------------------

log "Installing $UNIT_PATH and $WRAPPER"
install -m 0644 -o root -g root "$SRC_DIR/deploy/garmin-mcp.service" "$UNIT_PATH"
install -d -m 0755 "$(dirname "$WRAPPER")"
install -m 0755 -o root -g root "$SRC_DIR/deploy/garmin-mcp-wrapper.sh" "$WRAPPER"

# --- 6. (re)start and verify -----------------------------------------------------

# Value of one environment variable of the running service, exactly as systemd
# parsed it from the env file (empty if unset).
service_env() {
    local pid=$1 name=$2
    { tr '\0' '\n' <"/proc/$pid/environ" | sed -n "s/^$name=//p" | head -n 1; } 2>/dev/null || true
}

http_ok() {
    "$VENV/bin/python" - "$1" <<'PY'
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=3) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

start_failed() {
    echo >&2
    systemctl --no-pager --full --lines=25 status "$UNIT" >&2 || true
    echo >&2
    printf 'install.sh: error: %s\n' "$1" >&2
    cat >&2 <<EOF

The files are installed, but the service is not healthy. Usual cause: a bad
value in $ENV_FILE. Look at the log, fix it, and restart:

    journalctl -u garmin-mcp -e
    sudo systemctl restart garmin-mcp
EOF
    exit 1
}

log "Starting $UNIT"
systemctl daemon-reload
systemctl enable "$UNIT"
systemctl restart "$UNIT" || start_failed "systemd could not start $UNIT."

# The unit restarts on failure, so "started" proves little: wait until the
# process stays up and answers the health check, and notice a crash loop.
main_pid=
health_url=
healthy=false
deadline=$((SECONDS + START_TIMEOUT))
while ((SECONDS < deadline)); do
    active_state=$(systemctl show --property=ActiveState --value "$UNIT")
    sub_state=$(systemctl show --property=SubState --value "$UNIT")
    case "$active_state/$sub_state" in
        active/running)
            main_pid=$(systemctl show --property=MainPID --value "$UNIT")
            if [[ $main_pid =~ ^[1-9][0-9]*$ && -r /proc/$main_pid/environ ]]; then
                host=$(service_env "$main_pid" GARMIN_MCP_HOST)
                port=$(service_env "$main_pid" GARMIN_MCP_PORT)
                case "$host" in
                    "" | 0.0.0.0) probe_host=127.0.0.1 ;;
                    ::) probe_host='[::1]' ;;
                    *:*) probe_host="[$host]" ;;
                    *) probe_host=$host ;;
                esac
                health_url="http://$probe_host:${port:-8765}/healthz"
                if http_ok "$health_url"; then
                    healthy=true
                    break
                fi
            fi
            ;;
        failed/* | inactive/* | */auto-restart)
            start_failed "$UNIT exited right after starting (state: $active_state/$sub_state)."
            ;;
    esac
    sleep 1
done
$healthy || start_failed "$UNIT is $active_state/$sub_state but ${health_url:-its health check} did not answer within ${START_TIMEOUT}s."

# --- 7. summary ------------------------------------------------------------------

host=$(service_env "$main_pid" GARMIN_MCP_HOST)
port=$(service_env "$main_pid" GARMIN_MCP_PORT)
path=$(service_env "$main_pid" GARMIN_MCP_PATH)
token_dir=$(service_env "$main_pid" GARMIN_TOKEN_DIR)
case "$host" in
    "" | 0.0.0.0 | ::)
        lan_host=$(hostname -I 2>/dev/null | awk '{print $1}') || true
        lan_host=${lan_host:-<server-ip>}
        ;;
    *:*) lan_host="[$host]" ;;
    *) lan_host=$host ;;
esac
path=${path:-/mcp}
[[ $path == /* ]] || path=/$path
mcp_url="http://$lan_host:${port:-8765}$path"
# OAuth sign-in is on unless switched off. A public URL, if set, is the
# address the server publishes for it (public access through a TLS proxy).
oauth_on=true
case "$(service_env "$main_pid" GARMIN_MCP_OAUTH | tr '[:upper:]' '[:lower:]')" in
    0 | false | no | off) oauth_on=false ;;
esac
public_url=$(service_env "$main_pid" GARMIN_MCP_PUBLIC_URL)
while [[ $public_url == */ ]]; do public_url=${public_url%/}; done
public_line=
if [[ -n $public_url ]]; then
    public_line="
  Public URL     $public_url$path  (OAuth sign-in; reached through your TLS proxy)"
fi

cat <<EOF

$installed_version is installed and running.

  Health check   $health_url  (OK)
  MCP endpoint   $mcp_url  (streamable HTTP)$public_line
  Config         $ENV_FILE
  Tokens         ${token_dir:-$STATE_DIR/tokens}
  Logs           journalctl -u garmin-mcp -f

Next steps:
EOF

step=1
if [[ -n $(service_env "$main_pid" GARMIN_MCP_AUTH_TOKEN) ]]; then
    token_note="     The token is in the config: sudo grep AUTH_TOKEN $ENV_FILE"
elif $oauth_on; then
    # OAuth guards the endpoint; running without the static token is a choice.
    token_note=
else
    cat <<EOF
  $step. GARMIN_MCP_AUTH_TOKEN is not set: anyone who can reach the port can read
     this Garmin account. Set it (generate one with: openssl rand -hex 32) in
     $ENV_FILE, then: sudo systemctl restart garmin-mcp
EOF
    step=$((step + 1))
    token_note="     once GARMIN_MCP_AUTH_TOKEN is set."
fi
if [[ -s ${token_dir:-$STATE_DIR/tokens}/garmin_tokens.json ]]; then
    cat <<EOF
  $step. Garmin tokens are already saved. Verify them with: sudo garmin-mcp check
EOF
elif [[ -n $(service_env "$main_pid" GARMIN_EMAIL) && -n $(service_env "$main_pid" GARMIN_PASSWORD) ]]; then
    cat <<EOF
  $step. Garmin credentials are set in the env file. Verify them with:
     sudo garmin-mcp check     (if Garmin asks for MFA, run: sudo garmin-mcp login)
EOF
else
    cat <<EOF
  $step. Log in to Garmin once (prompts for email, password and MFA code):
     sudo garmin-mcp login
     Or set GARMIN_EMAIL and GARMIN_PASSWORD in $ENV_FILE
     (no MFA only) and restart the service.
EOF
fi
step=$((step + 1))
if [[ -n $public_url ]]; then
    cat <<EOF
  $step. Point MCP clients at $public_url$path
     They sign in through OAuth: approving one in the browser needs the owner
     password. If you have not set it yet: sudo garmin-mcp set-password
EOF
    if [[ -n $token_note ]]; then
        cat <<EOF
     Clients that send "Authorization: Bearer <token>" instead still work, also
     on $mcp_url
$token_note
EOF
    fi
elif [[ -n $token_note ]]; then
    cat <<EOF
  $step. Point MCP clients at $mcp_url
     with the header "Authorization: Bearer <token>".
$token_note
EOF
    if $oauth_on; then
        cat <<EOF
     Clients can also sign in with OAuth instead (set the owner password
     first: sudo garmin-mcp set-password), but most only do that over HTTPS.
EOF
    fi
else
    cat <<EOF
  $step. Point MCP clients at $mcp_url
     They sign in through OAuth: approving one in the browser needs the owner
     password. If you have not set it yet: sudo garmin-mcp set-password
     Most clients only do OAuth over HTTPS; for plain HTTP on the LAN, set
     GARMIN_MCP_AUTH_TOKEN in $ENV_FILE and use the header instead.
EOF
fi
