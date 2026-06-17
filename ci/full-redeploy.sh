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
LOG_FILE="/var/lib/weka-ci/redeploy.log"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
exec > >(tee "$LOG_FILE") 2>&1

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"; }

log "=== Starting full DevStack redeploy ==="

# ── Acquire lock (wait for any running CI job to finish) ──────────────────────

log "Acquiring runner lock..."
exec 9>"$LOCK_FILE"
if ! flock -w 1800 9; then
    log "FATAL: could not acquire runner lock within 1800s;"
    log "a previous run may be stuck. Aborting redeploy."
    exit 1
fi
log "Lock acquired"

# Release the lock and bring the listener back on ANY exit, so a failed
# redeploy never leaves the CI offline (it once stayed down for weeks).
LISTENER_STOPPED=0
cleanup() {
    local rc=$?
    if [ "$LISTENER_STOPPED" = "1" ]; then
        log "Ensuring CI listener is running"
        sudo systemctl start weka-manila-ci 2>/dev/null || true
    fi
    flock -u 9 2>/dev/null || true
    exit "$rc"
}
trap cleanup EXIT

# ── Stop the listener ─────────────────────────────────────────────────────────

log "Stopping CI listener"
LISTENER_STOPPED=1
sudo systemctl stop weka-manila-ci 2>/dev/null || true

# ── Tear down DevStack ────────────────────────────────────────────────────────

log "Tearing down DevStack"

if [ -f "${DEVSTACK_DIR}/unstack.sh" ]; then
    cd "$DEVSTACK_DIR"
    # 9>&- closes the inherited lock fd so async children can't hold it
    ./unstack.sh 9>&- 2>&1 || true
    ./clean.sh 9>&- 2>&1 || true
fi

sudo systemctl stop "devstack@*" 2>/dev/null || true

# Kill orphaned manila daemons left by prior deploys. They survive
# unstack/systemctl-stop by reparenting to init and accumulate across
# redeploys (we found 40+ from 20+ generations dating back weeks). Each keeps
# retrying do_setup with a stale in-memory config and races the live backend
# for the same service identity -> intermittent "Capabilities filter didn't
# succeed" share-build failures. [m] avoids matching this pkill's own cmdline.
sudo pkill -9 -f 'venv/bin/[m]anila-' 2>/dev/null || true

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
export CI_HOST_IP="$(hostname -I | awk '{print $1}')"
envsubst '$CI_HOST_IP $CI_VM_IP $WEKA_API_SERVER $WEKA_API_PORT $WEKA_USERNAME $WEKA_PASSWORD $WEKA_ORGANIZATION' < "${CI_DIR}/local.conf.template" > "${DEVSTACK_DIR}/local.conf"

cd "$DEVSTACK_DIR"
# 9>&- closes the inherited lock fd so stack.sh's async children
# (outfilter.py, fifo readers) can't keep the runner lock held after a crash.
STACK_RC=0
./stack.sh 9>&- 2>&1 || STACK_RC=$?

if [ "$STACK_RC" -ne 0 ]; then
    log "ERROR: stack.sh failed with exit code ${STACK_RC}"
    log "CI will not function until DevStack is fixed"
    # Listener is restarted by the EXIT trap so it can report infra failures
    exit 1
fi

# Refresh job-side CI scripts from the freshly-deployed repo so the VM
# never runs stale copies (a stale ci-runner.sh once broke every job).
WEKA_REPO="/opt/stack/manila-weka-driver"
if [ -d "${WEKA_REPO}/ci" ]; then
    log "Refreshing CI scripts from ${WEKA_REPO}/ci"
    for f in ci-runner.sh post-results.sh collect-logs.sh \
             gerrit-listener.py tempest-include.txt local.conf.template; do
        [ -f "${WEKA_REPO}/ci/${f}" ] && cp "${WEKA_REPO}/ci/${f}" "${CI_DIR}/"
    done
    chmod +x "${CI_DIR}"/*.sh "${CI_DIR}"/*.py 2>/dev/null || true
fi

# ── Create share types ────────────────────────────────────────────────────────

log "Creating share types"
set +u  # devstack openrc/functions uses unbound vars; tolerate during source
source "${DEVSTACK_DIR}/openrc" admin admin
set -u

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
    share_proto=WEKAFS \
    2>&1 || log "WARNING: Failed to create weka-wekafs share type (may already exist)"

# ── Configure tempest [share] ─────────────────────────────────────────────────
# DevStack's configure_tempest runs `rm -f tempest.conf` and regenerates it
# AFTER the [[post-config|$TEMPEST_CONFIG]] block is applied, wiping our
# [share] settings. Apply them here (post-stack) where they persist. Without
# this, every manila test skips at setUpClass (defaults assume Neutron).
TEMPEST_CONF="/opt/stack/tempest/etc/tempest.conf"
if [ -f "$TEMPEST_CONF" ]; then
    log "Configuring tempest [share]"
    # Use devstack's iniset (crudini isn't installed on the CI VM). devstack
    # functions reference unbound vars, so relax set -u while using them.
    set +u
    source "${DEVSTACK_DIR}/functions"
    # DHSS=false, no Neutron: don't create share networks, or all tests skip.
    iniset "$TEMPEST_CONF" share multi_backend true
    iniset "$TEMPEST_CONF" share backend_names weka_nfs,weka_wekafs
    iniset "$TEMPEST_CONF" share multitenancy_enabled false
    iniset "$TEMPEST_CONF" share create_networks_when_multitenancy_enabled false
    iniset "$TEMPEST_CONF" share default_share_type_name weka-nfs
    # nfs is the default protocol; WEKAFS is validated separately (upstream
    # tempest has no wekafs test class to drive it from enable_protocols).
    iniset "$TEMPEST_CONF" share enable_protocols nfs
    iniset "$TEMPEST_CONF" share enable_ip_rules_for_protocols nfs
    iniset "$TEMPEST_CONF" share enable_ro_access_level_for_protocols nfs
    iniset "$TEMPEST_CONF" share run_snapshot_tests true
    iniset "$TEMPEST_CONF" share run_revert_to_snapshot_tests true
    iniset "$TEMPEST_CONF" share run_shrink_tests true
    iniset "$TEMPEST_CONF" share run_extend_tests true
    iniset "$TEMPEST_CONF" share run_quota_tests true
    iniset "$TEMPEST_CONF" share run_manage_unmanage_tests false
    iniset "$TEMPEST_CONF" share run_share_group_tests false
    iniset "$TEMPEST_CONF" share run_replication_tests false
    iniset "$TEMPEST_CONF" share run_migration_tests false
    iniset "$TEMPEST_CONF" share run_ipv6_tests false
    iniset "$TEMPEST_CONF" share capability_snapshot_support true
    iniset "$TEMPEST_CONF" share capability_create_share_from_snapshot_support true
    iniset "$TEMPEST_CONF" share suppress_errors_in_cleanup true
    iniset "$TEMPEST_CONF" share build_timeout 600
    set -u
else
    log "WARNING: tempest.conf missing; skipping tempest [share] config"
fi

# Verify
openstack share service list
openstack share type list
openstack share pool list --detail

# ── Restart the listener ──────────────────────────────────────────────────────

log "Restarting CI listener"
sudo systemctl start weka-manila-ci

# Lock released by the EXIT trap
log "=== Full redeploy complete ==="
