#!/usr/bin/env bash
# One-time VPS setup for the NSE momentum dashboard, Ubuntu 24.04 LTS.
# Run as root: bash install.sh
#
# What this does NOT do (separate, manual steps -- see the README section
# "Deploying to a VPS" and this script's own final printout):
#   - tailscale up (needs an interactive browser auth link)
#   - tailscale cert (needs HTTPS Certificates enabled on your tailnet first)
#   - migrating cache/state.db from your existing machine (real positions/
#     credentials -- see deploy/vps/MIGRATE.md... actually just follow the
#     printout at the end of this script)
#   - installing the systemd units (also printed at the end)

set -euo pipefail

APP_USER="nseapp"
APP_DIR="/opt/nse-momentum-dashboard"
REPO_URL="https://github.com/kkirankumar1511/nse-momentum-dashboard.git"
BRANCH="experiments/beta-scoring-bonus"

echo "== Setting timezone to Asia/Kolkata (IST) =="
# systemd timers below use IST times (16:00, 09:16, Mon 08:00) -- matching
# the system timezone avoids needing per-timer TimeZone= directives.
timedatectl set-timezone Asia/Kolkata

echo "== Updating system packages =="
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git sqlite3 age curl

echo "== Installing Tailscale =="
curl -fsSL https://tailscale.com/install.sh | sh

echo "== Creating dedicated app user (not root) =="
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash "$APP_USER"
fi

echo "== Cloning repository =="
# ALL git operations run as $APP_USER, never root -- running git as root
# against a directory owned by a different user trips Git's "dubious
# ownership" safety check (a real CVE-2022-24765 mitigation) on every
# re-run after the first, since the chown below hands the directory to
# $APP_USER. Pre-create+chown the (possibly not-yet-existing) directory
# first so a non-root user can clone into it at all -- /opt itself isn't
# writable by a non-root user.
mkdir -p "$APP_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR"
git config --global --add safe.directory "$APP_DIR"  # belt-and-suspenders for any manual root git use later
if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" pull
else
    sudo -u "$APP_USER" git clone -b "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "== Setting up Python virtual environment =="
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

echo ""
echo "================================================================"
echo "Base setup done. Remaining manual steps:"
echo "================================================================"
echo "1. Join Tailscale (interactive, needs a browser):"
echo "     tailscale up"
echo ""
echo "2. Once HTTPS Certificates is enabled for your tailnet (same as your"
echo "   Windows machine), get a cert for THIS VPS's Tailscale name:"
echo "     tailscale cert <this-vps-name>.<your-tailnet>.ts.net"
echo "     mkdir -p $APP_DIR/.streamlit/certs"
echo "     mv <this-vps-name>.<your-tailnet>.ts.net.{crt,key} $APP_DIR/.streamlit/certs/"
echo "     cp $APP_DIR/.streamlit/config.toml.example $APP_DIR/.streamlit/config.toml"
echo "     # then edit config.toml's sslCertFile/sslKeyFile paths to match"
echo "     chown -R $APP_USER:$APP_USER $APP_DIR/.streamlit"
echo ""
echo "3. Migrate your REAL state.db from the Windows machine -- run this ON"
echo "   THE WINDOWS MACHINE (not here), once both are on the same tailnet:"
echo "     scp cache\\state.db root@<this-vps-tailscale-ip>:$APP_DIR/cache/state.db"
echo "   (create $APP_DIR/cache/ here first: mkdir -p $APP_DIR/cache && chown $APP_USER:$APP_USER $APP_DIR/cache)"
echo ""
echo "4. Install the systemd units:"
echo "     cp $APP_DIR/deploy/vps/systemd/*.service $APP_DIR/deploy/vps/systemd/*.timer /etc/systemd/system/"
echo "     systemctl daemon-reload"
echo "     systemctl enable --now nse-dashboard.service"
echo "     systemctl enable --now nse-rebalance.timer nse-gap-check.timer nse-fundamentals.timer"
echo ""
echo "5. Update your Kite Connect app's Redirect URL and IP allowlist to"
echo "   point at this VPS instead of your home Tailscale address."
echo "================================================================"
