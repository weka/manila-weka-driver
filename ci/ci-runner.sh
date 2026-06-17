#!/usr/bin/env bash
# =============================================================================
# Weka Manila CI - Per-Patch Job Runner
#
# Usage: ci-runner.sh <change_number> <patchset_number> <revision>
#
# Operates against a persistent DevStack installation. Cherry-picks the
# patch under test into Manila, restarts services, runs tempest, and
# posts results back to Gerrit.
#
# Exit codes:
#   0 = tempest passed
#   1 = tempest failed
#   2 = service restart or health check failed
#   3 = infrastructure error
# =============================================================================

set -uo pipefail

CHANGE_NUM="$1"
PATCHSET_NUM="$2"
REVISION="$3"

# ── Configuration ─────────────────────────────────────────────────────────────

CI_DIR="/opt/weka-ci"
DEVSTACK_DIR="/opt/stack/devstack"
MANILA_DIR="/opt/stack/manila"
TEMPEST_DIR="/opt/stack/tempest"
LOG_BASE="/var/www/ci-logs"
LOG_DIR="${LOG_BASE}/${CHANGE_NUM}/${PATCHSET_NUM}"

TIMEOUT_RESTART=300      # 5 min for service restart + health check
TIMEOUT_CLEANUP=300      # 5 min for resource cleanup
TIMEOUT_TEMPEST=2700     # 45 min for tempest

START_TIME=$(date +%s)

# ── Setup logging ─────────────────────────────────────────────────────────────

mkdir -p "$LOG_DIR"
exec > >(tee "${LOG_DIR}/ci-runner.log") 2>&1

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"; }

log "=== CI run starting for change ${CHANGE_NUM},${PATCHSET_NUM} ==="
log "Revision: ${REVISION}"

# ── Helper: restore clean master ─────────────────────────────────────────────

restore_clean_master() {
    cd "$MANILA_DIR" || return 1
    git reset --hard origin/master 2>&1 || return 1
    sudo systemctl restart devstack@m-shr devstack@m-api devstack@m-sch 2>&1 || return 1
}

# ── Helper: post failure and exit ─────────────────────────────────────────────

fail() {
    local msg="$1"
    local exit_code="${2:-3}"
    log "FAILURE: ${msg}"
    "${CI_DIR}/collect-logs.sh" "$LOG_DIR" || true
    "${CI_DIR}/post-results.sh" "$CHANGE_NUM" "$PATCHSET_NUM" "$REVISION" \
        "FAILURE" "$msg" "$LOG_DIR" || true
    # Restore clean master before exiting
    restore_clean_master || true
    exit "$exit_code"
}

# ── Phase 1: Reset Manila to clean master ─────────────────────────────────────

log "Phase 1: Resetting Manila to clean master"
cd "$MANILA_DIR" || fail "Manila directory not found at ${MANILA_DIR}"

git fetch origin master 2>&1 || fail "Failed to fetch origin master"
git reset --hard origin/master 2>&1 || fail "Failed to reset to origin/master"
git clean -fdx 2>&1 || true

# Sync dependencies after reset (master may have new requirements)
log "Installing Manila dependencies..."
sudo rm -rf manila.egg-info 2>/dev/null || true
/opt/stack/data/venv/bin/pip install -e . -q 2>&1 \
    || fail "Failed to install Manila dependencies"

# Re-link the Weka driver into Manila's source tree (git reset removes it)
WEKA_DRIVER_SRC="/opt/stack/manila-weka-driver/manila/share/drivers/weka"
WEKA_DRIVER_DEST="${MANILA_DIR}/manila/share/drivers/weka"
ln -sfn "$WEKA_DRIVER_SRC" "$WEKA_DRIVER_DEST" \
    || fail "Failed to symlink Weka driver into Manila"

# Re-patch WEKAFS into Manila's supported protocols (git reset reverts it)
CONSTANTS_FILE="${MANILA_DIR}/manila/common/constants.py"
if ! grep -q "'WEKAFS'" "$CONSTANTS_FILE" 2>/dev/null; then
    sed -i "s/SUPPORTED_SHARE_PROTOCOLS = (/SUPPORTED_SHARE_PROTOCOLS = (/;s/'MAPRFS')/'MAPRFS', 'WEKAFS')/" \
        "$CONSTANTS_FILE" \
        || fail "Failed to patch WEKAFS into Manila constants"
    log "Patched WEKAFS into SUPPORTED_SHARE_PROTOCOLS"
fi

# ── Phase 2: Cherry-pick the patch under test ─────────────────────────────────

log "Phase 2: Cherry-picking change ${CHANGE_NUM},${PATCHSET_NUM}"

# Gerrit refs format: refs/changes/XX/<change>/<patchset>
# XX is the last two digits of the change number
CHANGE_SUFFIX=$(printf '%02d' $((CHANGE_NUM % 100)))

git fetch "https://review.opendev.org/openstack/manila" \
    "refs/changes/${CHANGE_SUFFIX}/${CHANGE_NUM}/${PATCHSET_NUM}" 2>&1 \
    || fail "Failed to fetch patch from Gerrit"

CHERRY_PICK_FAILED=false
if ! git cherry-pick FETCH_HEAD 2>&1; then
    log "WARNING: Cherry-pick failed (merge conflict)"
    git cherry-pick --abort 2>/dev/null || true
    git reset --hard origin/master 2>&1
    CHERRY_PICK_FAILED=true
fi

# ── Phase 3: Restart Manila services ──────────────────────────────────────────

log "Phase 3: Restarting Manila services"

sudo systemctl restart devstack@m-shr devstack@m-api devstack@m-sch 2>&1 \
    || fail "Failed to restart Manila services" 2

# Wait for services to be healthy
log "Waiting for Manila services to become healthy..."
# devstack openrc/functions references unbound vars; tolerate under set -u
set +u
source "${DEVSTACK_DIR}/openrc" admin admin 2>/dev/null
set -u

HEALTH_START=$(date +%s)
HEALTHY=false
while [ $(($(date +%s) - HEALTH_START)) -lt "$TIMEOUT_RESTART" ]; do
    # Check if all Manila services report 'up'
    SERVICE_STATUS=$(timeout 30 openstack share service list -f value -c State 2>/dev/null || echo "")
    if [ -n "$SERVICE_STATUS" ]; then
        DOWN_COUNT=$(echo "$SERVICE_STATUS" | grep -cv "up" || true)
        if [ "$DOWN_COUNT" -eq 0 ]; then
            HEALTHY=true
            break
        fi
    fi
    sleep 5
done

if [ "$HEALTHY" != "true" ]; then
    timeout 30 openstack share service list 2>&1 | tee "${LOG_DIR}/share-service-list.txt" || true
    fail "Manila services did not become healthy within ${TIMEOUT_RESTART}s" 2
fi

log "Manila services are healthy"
timeout 30 openstack share service list 2>&1 | tee "${LOG_DIR}/share-service-list.txt"

# ── Phase 4: Clean leftover test resources ────────────────────────────────────

log "Phase 4: Cleaning leftover test resources"

# Delete any leftover shares (from previous test runs)
CLEANUP_START=$(date +%s)
for share_id in $(timeout 30 openstack share list -f value -c ID 2>/dev/null); do
    log "Deleting leftover share: ${share_id}"
    openstack share delete "$share_id" --wait 2>/dev/null || true
    if [ $(($(date +%s) - CLEANUP_START)) -gt "$TIMEOUT_CLEANUP" ]; then
        log "WARNING: Resource cleanup timeout, proceeding anyway"
        break
    fi
done

# Delete any leftover snapshots
for snap_id in $(timeout 30 openstack share snapshot list -f value -c ID 2>/dev/null); do
    log "Deleting leftover snapshot: ${snap_id}"
    openstack share snapshot delete "$snap_id" --wait 2>/dev/null || true
    if [ $(($(date +%s) - CLEANUP_START)) -gt "$TIMEOUT_CLEANUP" ]; then
        log "WARNING: Resource cleanup timeout, proceeding anyway"
        break
    fi
done

# ── Phase 5: Run tempest ─────────────────────────────────────────────────────

log "Phase 5: Running tempest tests"
cd "$TEMPEST_DIR" || fail "Tempest directory not found at ${TEMPEST_DIR}"

TEMPEST_START=$(date +%s)

timeout "$TIMEOUT_TEMPEST" tempest run \
    --include-list "${CI_DIR}/tempest-include.txt" \
    --concurrency 1 \
    2>&1 | tee "${LOG_DIR}/tempest.log"
TEMPEST_RC=${PIPESTATUS[0]}

TEMPEST_END=$(date +%s)
TEMPEST_DURATION=$((TEMPEST_END - TEMPEST_START))

log "Tempest completed in ${TEMPEST_DURATION}s with exit code ${TEMPEST_RC}"

# Generate HTML results
if [ -d .stestr ]; then
    stestr last --subunit > "${LOG_DIR}/testrepository.subunit" 2>/dev/null || true
    if command -v subunit2html &>/dev/null; then
        subunit2html "${LOG_DIR}/testrepository.subunit" \
            "${LOG_DIR}/testr_results.html" 2>/dev/null || true
    fi
fi

# ── Phase 6: Collect logs ────────────────────────────────────────────────────

log "Phase 6: Collecting logs"
"${CI_DIR}/collect-logs.sh" "$LOG_DIR"

# ── Phase 7: Post results ────────────────────────────────────────────────────

END_TIME=$(date +%s)
TOTAL_DURATION=$((END_TIME - START_TIME))

if [ "$CHERRY_PICK_FAILED" = "true" ]; then
    RESULT="FAILURE"
    MSG="Cherry-pick failed (merge conflict). Tested against clean master. Tempest: exit ${TEMPEST_RC} (${TEMPEST_DURATION}s). Total: ${TOTAL_DURATION}s"
elif [ "$TEMPEST_RC" -eq 0 ]; then
    RESULT="SUCCESS"
    # Extract test counts from tempest output
    SUMMARY=$(tail -5 "${LOG_DIR}/tempest.log" | grep -E "Ran|passed|failed" | head -1 || echo "")
    MSG="All tests passed. ${SUMMARY} (${TEMPEST_DURATION}s). Total: ${TOTAL_DURATION}s"
else
    RESULT="FAILURE"
    SUMMARY=$(tail -5 "${LOG_DIR}/tempest.log" | grep -E "Ran|passed|failed" | head -1 || echo "")
    MSG="Tests failed. ${SUMMARY} (${TEMPEST_DURATION}s). Total: ${TOTAL_DURATION}s"
fi

log "Phase 7: Posting results (${RESULT})"
"${CI_DIR}/post-results.sh" "$CHANGE_NUM" "$PATCHSET_NUM" "$REVISION" \
    "$RESULT" "$MSG" "$LOG_DIR"

# ── Phase 8: Restore clean master ────────────────────────────────────────────

log "Phase 8: Restoring clean master"
restore_clean_master || log "WARNING: Failed to restore clean master"

log "=== CI run complete (${RESULT}) ==="
[ "$RESULT" = "SUCCESS" ] && exit 0 || exit 1
