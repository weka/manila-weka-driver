#!/usr/bin/env bash
# =============================================================================
# Weka Manila CI - Full DevStack Redeploy
#
# Run nightly (via systemd timer) to tear down and rebuild DevStack
# from scratch, preventing state drift and DB schema issues.
#
# This script:
#   1. Stops the CI listener (to prevent jobs during redeploy)
#   2. Tears down DevStack completely
#   3. Redeploys DevStack with the Weka driver
#   4. Creates share types
#   5. Restarts the CI listener
# =============================================================================

set -euo pipefail

CI_DIR="/opt/weka-ci"
DEVSTACK_DIR="/opt/stack/devstack"
LOCK_FILE="/var/lib/weka-ci/runner.lock"

exec > >(tee /var/log/weka-ci-redeploy.log) 2>&1

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"; }

log "=== Starting full DevStack redeploy ==="

# ── Acquire lock (wait for any running CI job to finish) ──────────────────────

log "Acquiring runner lock..."
exec 9>"$LOCK_FILE"
flock 9
log "Lock acquired"

# ── Stop the listener ─────────────────────────────────────────────────────────

log "Stopping CI listener"
sudo systemctl stop weka-manila-ci 2>/dev/null || true

# ── Tear down DevStack ────────────────────────────────────────────────────────

log "Tearing down DevStack"

if [ -f "${DEVSTACK_DIR}/unstack.sh" ]; then
    cd "$DEVSTACK_DIR"
    ./unstack.sh 2>&1 || true
    ./clean.sh 2>&1 || true
fi

sudo systemctl stop "devstack@*" 2>/dev/null || true
sudo umount -l /mnt/weka/* 2>/dev/null || true

# Clean up but preserve the devstack repo to speed up re-clone
sudo rm -rf /opt/stack/data /opt/stack/logs
sudo rm -rf /opt/stack/manila /opt/stack/tempest
sudo rm -rf /opt/stack/manila-tempest-plugin
sudo mysql -e "DROP DATABASE IF EXISTS manila; DROP DATABASE IF EXISTS keystone;" 2>/dev/null || true

# ── Re-deploy DevStack ────────────────────────────────────────────────────────

log "Re-deploying DevStack"

if [ ! -d "$DEVSTACK_DIR" ]; then
    sudo mkdir -p /opt/stack
    sudo chown stack:stack /opt/stack
    cd /opt/stack
    git clone -b stable/2025.1 https://opendev.org/openstack/devstack.git
else
    cd "$DEVSTACK_DIR"
    git fetch origin
    git reset --hard origin/stable/2025.1
fi

# Install local.conf from template, substituting environment variables
envsubst '$CI_VM_IP $WEKA_API_SERVER $WEKA_API_PORT $WEKA_USERNAME $WEKA_PASSWORD $WEKA_ORGANIZATION' < "${CI_DIR}/local.conf.template" > "${DEVSTACK_DIR}/local.conf"

cd "$DEVSTACK_DIR"
./stack.sh 2>&1
STACK_RC=$?

if [ "$STACK_RC" -ne 0 ]; then
    log "ERROR: stack.sh failed with exit code ${STACK_RC}"
    log "CI will not function until DevStack is fixed"
    # Still restart listener so it can report infrastructure failures
    sudo systemctl start weka-manila-ci 2>/dev/null || true
    exit 1
fi

# ── Create share types ────────────────────────────────────────────────────────

log "Creating share types"
source "${DEVSTACK_DIR}/openrc" admin admin

openstack share type create weka-nfs false \
    --extra-specs share_backend_name=weka_nfs \
    snapshot_support=true \
    create_share_from_snapshot_support=true \
    revert_to_snapshot_support=true \
    2>&1 || log "WARNING: Failed to create weka-nfs share type (may already exist)"

openstack share type create weka-wekafs false \
    --extra-specs share_backend_name=weka_wekafs \
    snapshot_support=true \
    create_share_from_snapshot_support=true \
    revert_to_snapshot_support=true \
    2>&1 || log "WARNING: Failed to create weka-wekafs share type (may already exist)"

# Verify
openstack share service list
openstack share type list
openstack share pool list --detail

# ── Restart the listener ──────────────────────────────────────────────────────

log "Restarting CI listener"
sudo systemctl start weka-manila-ci

# Release lock
flock -u 9

log "=== Full redeploy complete ==="
