#!/usr/bin/env bash
# =============================================================================
# Weka Manila CI - One-Shot VM Setup
#
# Run once on a fresh Ubuntu 24.04 VM to install all dependencies,
# create the stack user, deploy DevStack, configure nginx for log
# hosting, and install systemd services.
#
# Usage: sudo bash setup.sh
#
# Prerequisites:
#   - Ubuntu 24.04
#   - SSH key for Gerrit already present at /home/ubuntu/.ssh/id_ed25519
#   - Environment variables set (or edit the defaults below):
#       WEKA_API_SERVER, WEKA_API_PORT, WEKA_USERNAME, WEKA_PASSWORD, WEKA_ORGANIZATION
# =============================================================================

set -euo pipefail

# ── Configuration (override via environment) ──────────────────────────────────

export WEKA_API_SERVER="${WEKA_API_SERVER:?Set WEKA_API_SERVER environment variable}"
export WEKA_API_PORT="${WEKA_API_PORT:-14000}"
export WEKA_USERNAME="${WEKA_USERNAME:-admin}"
export WEKA_PASSWORD="${WEKA_PASSWORD:?Set WEKA_PASSWORD environment variable}"
export WEKA_ORGANIZATION="${WEKA_ORGANIZATION:-Root}"
export CI_VM_IP="${CI_VM_IP:-$(hostname -I | awk '{print $1}')}"
export CI_HOST_IP="$(hostname -I | awk '{print $1}')"
export CI_LOG_URL="http://${CI_VM_IP}:8088"
GERRIT_USER="${GERRIT_USER:-Assaf}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"; }

log "=== Weka Manila CI Setup Starting ==="

# ── 1. System packages ───────────────────────────────────────────────────────

log "Installing system packages"
apt-get update
apt-get install -y \
    git python3 python3-pip python3-venv \
    nginx \
    openssh-client \
    bridge-utils \
    iptables \
    net-tools \
    curl \
    jq \
    gettext-base \
    gzip \
    nfs-common

# ── 2. Create the stack user (DevStack requirement) ──────────────────────────

log "Creating stack user"
if ! id -u stack &>/dev/null; then
    useradd -m -s /bin/bash stack
    echo "stack ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/stack
    chmod 0440 /etc/sudoers.d/stack
fi

# ── 3. Set up CI working directories ─────────────────────────────────────────

log "Setting up directories"
mkdir -p /opt/weka-ci
mkdir -p /var/lib/weka-ci
mkdir -p /var/www/ci-logs
mkdir -p /opt/stack
chown -R stack:stack /opt/weka-ci /var/lib/weka-ci /var/www/ci-logs /opt/stack

# ── 4. Copy CI scripts ───────────────────────────────────────────────────────

log "Installing CI scripts"
cp "${SCRIPT_DIR}/gerrit-listener.py" /opt/weka-ci/
cp "${SCRIPT_DIR}/ci-runner.sh" /opt/weka-ci/
cp "${SCRIPT_DIR}/post-results.sh" /opt/weka-ci/
cp "${SCRIPT_DIR}/collect-logs.sh" /opt/weka-ci/
cp "${SCRIPT_DIR}/full-redeploy.sh" /opt/weka-ci/
cp "${SCRIPT_DIR}/local.conf.template" /opt/weka-ci/
cp "${SCRIPT_DIR}/tempest-include.txt" /opt/weka-ci/
chmod +x /opt/weka-ci/*.sh /opt/weka-ci/*.py
chown -R stack:stack /opt/weka-ci

# ── 4a. Persist CI environment variables ─────────────────────────────────────

log "Persisting CI environment"
cat > /opt/weka-ci/ci-env <<EOF
CI_VM_IP=${CI_VM_IP}
CI_HOST_IP=${CI_HOST_IP}
WEKA_API_SERVER=${WEKA_API_SERVER}
WEKA_API_PORT=${WEKA_API_PORT}
WEKA_USERNAME=${WEKA_USERNAME}
WEKA_PASSWORD=${WEKA_PASSWORD}
WEKA_ORGANIZATION=${WEKA_ORGANIZATION}
GERRIT_USER=${GERRIT_USER}
EOF
chmod 600 /opt/weka-ci/ci-env
chown stack:stack /opt/weka-ci/ci-env

# ── 5. SSH key for Gerrit ────────────────────────────────────────────────────

log "Configuring SSH for stack user"
mkdir -p /home/stack/.ssh
cp /home/ubuntu/.ssh/id_* /home/stack/.ssh/ 2>/dev/null || true
chown -R stack:stack /home/stack/.ssh
chmod 700 /home/stack/.ssh
chmod 600 /home/stack/.ssh/id_* 2>/dev/null || true

# Add Gerrit host key
sudo -u stack ssh-keyscan -p 29418 review.opendev.org \
    >> /home/stack/.ssh/known_hosts 2>/dev/null || true

# ── 6. Configure nginx ───────────────────────────────────────────────────────

log "Configuring nginx"
cat > /etc/nginx/sites-available/weka-ci-logs <<'NGINX'
server {
    listen 8088;
    server_name _;
    root /var/www/ci-logs;
    location / {
        autoindex on;
        autoindex_exact_size off;
        autoindex_localtime on;
        types {
            text/html  html;
            text/plain log txt conf ini;
            application/gzip gz;
        }
        default_type text/plain;
        gzip_static on;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/weka-ci-logs /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
systemctl restart nginx
systemctl enable nginx

# ── 7. Install systemd services ──────────────────────────────────────────────

log "Installing systemd services"

# Listener service
cp "${SCRIPT_DIR}/weka-manila-ci.service" /etc/systemd/system/
cp "${SCRIPT_DIR}/weka-manila-ci-redeploy.service" /etc/systemd/system/
cp "${SCRIPT_DIR}/weka-manila-ci-redeploy.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable weka-manila-ci
systemctl enable weka-manila-ci-redeploy.timer

# ── 8. Log rotation cron ─────────────────────────────────────────────────────

log "Setting up log rotation"
cat > /etc/cron.d/weka-ci-logs <<'CRON'
# Clean CI logs older than 45 days (Manila requires 30 day minimum retention)
0 3 * * * root find /var/www/ci-logs -type d -mindepth 1 -maxdepth 1 -mtime +45 -exec rm -rf {} \;
CRON

# ── 9. Deploy DevStack ───────────────────────────────────────────────────────

log "Deploying DevStack (this will take 20-40 minutes)"

sudo -u stack \
    CI_HOST_IP="${CI_HOST_IP}" \
    CI_VM_IP="${CI_VM_IP}" \
    WEKA_API_SERVER="${WEKA_API_SERVER}" \
    WEKA_API_PORT="${WEKA_API_PORT}" \
    WEKA_USERNAME="${WEKA_USERNAME}" \
    WEKA_PASSWORD="${WEKA_PASSWORD}" \
    WEKA_ORGANIZATION="${WEKA_ORGANIZATION}" \
    bash -c "
    cd /opt/stack
    git clone -b stable/2025.1 https://opendev.org/openstack/devstack.git
    cd devstack
    envsubst '\$CI_HOST_IP \$CI_VM_IP \$WEKA_API_SERVER \$WEKA_API_PORT \$WEKA_USERNAME \$WEKA_PASSWORD \$WEKA_ORGANIZATION' < /opt/weka-ci/local.conf.template > local.conf
    ./stack.sh
"

STACK_RC=$?
if [ "$STACK_RC" -ne 0 ]; then
    log "ERROR: DevStack deployment failed with exit code ${STACK_RC}"
    log "Check /opt/stack/logs/stack.sh.log for details"
    log "Fix the issue and re-run: sudo -u stack bash -c 'cd /opt/stack/devstack && ./stack.sh'"
    exit 1
fi

# ── 10. Create share types ───────────────────────────────────────────────────

log "Creating share types"
sudo -u stack bash -c "
    source /opt/stack/devstack/openrc admin admin
    openstack share type create weka-nfs false \
        --extra-specs share_backend_name=weka_nfs \
        snapshot_support=true \
        create_share_from_snapshot_support=true \
        revert_to_snapshot_support=true

    openstack share type create weka-wekafs false \
        --extra-specs share_backend_name=weka_wekafs \
        snapshot_support=true \
        create_share_from_snapshot_support=true \
        revert_to_snapshot_support=true \
        share_proto=WEKAFS

    echo '=== Share services ==='
    openstack share service list

    echo '=== Share types ==='
    openstack share type list

    echo '=== Share pools ==='
    openstack share pool list --detail
"

# ── 11. Start the nightly redeploy timer ─────────────────────────────────────

systemctl start weka-manila-ci-redeploy.timer

# ── Done ──────────────────────────────────────────────────────────────────────

log "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Install the Weka client on this VM (for WekaFS kernel module):"
echo "     curl -k -o /tmp/weka-client.tar https://${WEKA_API_SERVER}:${WEKA_API_PORT}/dist/v1/install/<version>"
echo "     tar xf /tmp/weka-client.tar && sudo ./install.sh && sudo modprobe wekafsio"
echo ""
echo "  2. Start the CI listener:"
echo "     sudo systemctl start weka-manila-ci"
echo ""
echo "  3. Monitor:"
echo "     journalctl -u weka-manila-ci -f"
echo ""
echo "  4. Check logs at: ${CI_LOG_URL}/"
