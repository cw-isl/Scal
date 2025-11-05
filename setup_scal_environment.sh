#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "[ERROR] This setup script must be run as root." >&2
    exit 1
fi

REPO_URL="https://github.com/cw-isl/Scal.git"
BRANCH="${SCAL_BRANCH:-}"
APP_USER="scal"
APP_GROUP="scal"
APP_DIR="/opt/scal"
DATA_DIR="/var/lib/scal"
CONFIG_DIR="/etc/scal"
PYTHON_BIN="/usr/bin/python3"
SERVICE_NAME="scal.service"
SYSTEMD_PATH="/etc/systemd/system/${SERVICE_NAME}"

log() {
    echo "[scal-setup] $*"
}

ensure_packages() {
    log "Installing system dependencies"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y --no-install-recommends \
        git python3 python3-venv python3-pip ca-certificates
}

ensure_user() {
    if ! getent group "$APP_GROUP" >/dev/null 2>&1; then
        log "Creating system group $APP_GROUP"
        groupadd --system "$APP_GROUP"
    fi
    if ! id -u "$APP_USER" >/dev/null 2>&1; then
        log "Creating system user $APP_USER"
        useradd --system --gid "$APP_GROUP" --create-home --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
    fi
}

prepare_directories() {
    log "Preparing directories"
    mkdir -p "$(dirname "$APP_DIR")" "$DATA_DIR" "$CONFIG_DIR"
    chown -R "$APP_USER":"$APP_GROUP" "$DATA_DIR" "$CONFIG_DIR"
}

fetch_repository() {
    local branch
    branch=$(resolve_branch)
    if [[ -d "$APP_DIR/.git" ]]; then
        log "Updating existing repository"
        git -C "$APP_DIR" fetch origin "$branch"
        git -C "$APP_DIR" checkout "$branch"
        git -C "$APP_DIR" reset --hard "origin/$branch"
    else
        log "Cloning repository from $REPO_URL"
        git clone --branch "$branch" "$REPO_URL" "$APP_DIR"
    fi
}

resolve_branch() {
    if [[ -n "$BRANCH" ]]; then
        echo "$BRANCH"
        return
    fi
    local head
    head=$(git ls-remote --symref "$REPO_URL" HEAD 2>/dev/null | awk '/^ref:/ {print $2}' | sed 's@refs/heads/@@')
    if [[ -n "$head" ]]; then
        echo "$head"
    else
        echo "main"
    fi
}

setup_virtualenv() {
    log "Creating Python virtual environment"
    if [[ ! -d "$APP_DIR/.venv" ]]; then
        "$PYTHON_BIN" -m venv "$APP_DIR/.venv"
    fi
    source "$APP_DIR/.venv/bin/activate"
    pip install --upgrade pip setuptools wheel
    pip install -r "$APP_DIR/requirements.txt"
    deactivate
    chown -R "$APP_USER":"$APP_GROUP" "$APP_DIR/.venv"
}

configure_app() {
    log "Configuring application defaults"
    if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
        cp "$APP_DIR/config/config.sample.yaml" "$CONFIG_DIR/config.yaml"
        chown "$APP_USER":"$APP_GROUP" "$CONFIG_DIR/config.yaml"
        chmod 640 "$CONFIG_DIR/config.yaml"
    fi
}

install_service() {
    log "Installing systemd service"
    cat <<SERVICE >"$SYSTEMD_PATH"
[Unit]
Description=Scal Smart Frame Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
Environment=SCAL_DATA_DIR=$DATA_DIR
Environment=SCAL_CONFIG_FILE=$CONFIG_DIR/config.yaml
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/scal_main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE
    chmod 644 "$SYSTEMD_PATH"
    systemctl daemon-reload
    systemctl enable --now "$SERVICE_NAME"
}

main() {
    ensure_packages
    ensure_user
    prepare_directories
    fetch_repository
    chown -R "$APP_USER":"$APP_GROUP" "$APP_DIR"
    setup_virtualenv
    configure_app
    install_service
    log "Setup complete. Service status:"
    systemctl status "$SERVICE_NAME" --no-pager
}

main "$@"
