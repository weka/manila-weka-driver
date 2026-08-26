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
BROKEN_MARKER="/var/lib/weka-ci/deploy-broken"

# stack.sh needs well over the ~3 GiB Weka's default trace retention leaves
# free on this 96 GB disk. Checked before teardown so a full disk skips the
# run instead of destroying a working DevStack it then cannot rebuild.
MIN_FREE_GB=25

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
exec > >(tee "$LOG_FILE") 2>&1

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"; }

# manila-share worker children reparent to init and SURVIVE a `systemctl
# stop`/`restart`, becoming orphan generations that double-process RPCs
# (symptoms: "name already exists" on snapshot create, capabilities races).
# Reap any reparented workers so exactly one generation runs.
_manila_kill_orphans() {
    local _n=0
    while pgrep -f 'venv/bin/[m]anila-share' >/dev/null 2>&1 \
            && [ "$_n" -lt 15 ]; do
        sudo pkill -9 -f 'venv/bin/[m]anila-share' 2>/dev/null || true
        sleep 1
        _n=$((_n + 1))
    done
}

# Restart manila-share as a SINGLE clean generation (stop, reap orphans,
# start) so post-stack restarts never leave a stale generation behind.
_restart_manila_single_gen() {
    sudo systemctl stop devstack@m-shr 2>&1 || true
    sleep 2
    _manila_kill_orphans
    sudo systemctl reset-failed devstack@m-shr 2>/dev/null || true
    sudo systemctl start devstack@m-shr 2>&1 || true
}

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

# ── Self-update: refresh the weka repo clone, then re-exec if this script
#    changed.  Runs only on the first exec (WEKA_REDEPLOY_REEXEC != 1).
#    Any failure here is non-fatal: just log a warning and continue. ──────────

WEKA_REPO="/opt/stack/manila-weka-driver"

if [ "${WEKA_REDEPLOY_REEXEC:-}" != "1" ]; then
    # Refresh the weka driver repo so CI scripts and driver code are latest.
    git -C "$WEKA_REPO" fetch --quiet origin \
        && git -C "$WEKA_REPO" reset --hard origin/main \
        || log "WARNING: could not refresh weka repo clone; \
continuing with current revision"

    # Re-exec this script if a newer copy exists in the repo.
    _NEW_SCRIPT="${WEKA_REPO}/ci/full-redeploy.sh"
    _SELF="/opt/weka-ci/full-redeploy.sh"
    if [ -f "$_NEW_SCRIPT" ] && ! diff -q "$_NEW_SCRIPT" "$_SELF" \
            >/dev/null 2>&1; then
        log "full-redeploy.sh updated; copying and re-execing..."
        cp "$_NEW_SCRIPT" "$_SELF" \
            || { log "WARNING: could not copy updated script; continuing"; }
        if [ -x "$_SELF" ]; then
            # Close the lock fd (9>&-) across the re-exec. Otherwise the
            # re-exec'd copy's `exec > >(tee ...)` at the top spawns a tee
            # that inherits fd 9 (this held lock), and its own `exec 9>` +
            # `flock` then deadlock for 1800s waiting on the lock that tee
            # keeps alive -> "a previous run may be stuck". Releasing it here
            # lets the re-exec'd copy re-acquire the lock cleanly. Same
            # inherited-lock-fd guard already used for unstack/stack.sh below.
            exec env WEKA_REDEPLOY_REEXEC=1 bash "$_SELF" "$@" 9>&-
        fi
    fi
    unset _NEW_SCRIPT _SELF
fi

# Release the lock and bring the listener back on ANY exit, so a failed
# redeploy never leaves the CI offline (it once stayed down for weeks).
#
# But a listener running over a broken DevStack is not neutral: it accepts
# jobs and votes FAILURE on changes that are perfectly fine.  So record the
# outcome in a marker file; ci-runner.sh reads it and reports an infra
# failure instead of blaming the change.
LISTENER_STOPPED=0
cleanup() {
    local rc=$?
    if [ "$rc" -eq 0 ]; then
        sudo rm -f "$BROKEN_MARKER" 2>/dev/null || true
    else
        log "Marking deploy broken for ci-runner: ${BROKEN_MARKER}"
        printf 'redeploy failed at %s (exit %s); see %s\n' \
            "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$rc" "$LOG_FILE" \
            | sudo tee "$BROKEN_MARKER" >/dev/null 2>&1 || true
    fi
    if [ "$LISTENER_STOPPED" = "1" ]; then
        log "Ensuring CI listener is running"
        sudo systemctl start weka-manila-ci 2>/dev/null || true
    fi
    flock -u 9 2>/dev/null || true
    exit "$rc"
}
trap cleanup EXIT

# ── Cap Weka trace retention ──────────────────────────────────────────────────
#
# Weka's default is "50GB per IO-node, minimum 100GB", which a 96 GB root
# disk can never satisfy: traces grow until only the 3 GiB reserve is left
# and stack.sh dies out of space. Re-applied every run because the setting
# is lost whenever the client container or cluster is rebuilt.

log "Capping Weka trace retention (client-max 10GB, ensure-free 25GB)"
if command -v weka >/dev/null 2>&1; then
    weka debug traces retention set \
        --client-max 10GB --client-ensure-free 25GB 2>&1 \
        || log "WARNING: could not set trace retention; disk may fill"
else
    log "WARNING: weka CLI not found; skipping trace retention cap"
fi

# ── Pre-flight: disk space ────────────────────────────────────────────────────
#
# Before teardown, not after: an aborted run leaves the previous DevStack
# serving, whereas tearing down first turns a full disk into an outage.

FREE_GB=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')
log "Free space on /: ${FREE_GB}G (need ${MIN_FREE_GB}G)"
if [ -n "$FREE_GB" ] && [ "$FREE_GB" -lt "$MIN_FREE_GB" ]; then
    log "ERROR: only ${FREE_GB}G free on /; need ${MIN_FREE_GB}G to rebuild"
    log "Largest consumers:"
    sudo du -xh --max-depth=2 / 2>/dev/null | sort -rh | head -10 | \
        while read -r line; do log "  ${line}"; done
    log "Refusing to tear down a working DevStack it could not rebuild."
    exit 1
fi

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
    git clone -b master https://opendev.org/openstack/devstack.git
else
    cd "$DEVSTACK_DIR"
    git fetch origin
    git reset --hard origin/master
fi

# Refresh local.conf.template from the persisted repo clone BEFORE generating
# local.conf. The post-stack CI-script refresh below also copies it, but that
# runs too late for this run's local.conf — without this each run would use the
# PREVIOUS run's template (one run stale), which once re-introduced a removed
# secret line. The clone survives teardown (only manila/tempest/data are rm'd).
# (WEKA_REPO is set in the self-update block at the top of the script.)
if [ -f "${WEKA_REPO}/ci/local.conf.template" ]; then
    cp "${WEKA_REPO}/ci/local.conf.template" "${CI_DIR}/local.conf.template"
fi

# Install local.conf from template, substituting environment variables
export CI_HOST_IP="$(hostname -I | awk '{print $1}')"
# WEKA_PASSWORD is deliberately excluded: it must never be substituted into
# local.conf (which DevStack echoes via `set -x` into stack.sh.log). It is set
# directly in manila.conf post-stack, untraced (see below).
envsubst '$CI_HOST_IP $CI_VM_IP $WEKA_API_SERVER $WEKA_API_PORT $WEKA_USERNAME $WEKA_ORGANIZATION' < "${CI_DIR}/local.conf.template" > "${DEVSTACK_DIR}/local.conf"

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
# (WEKA_REPO is set in the self-update block at the top of the script.)
if [ -d "${WEKA_REPO}/ci" ]; then
    log "Refreshing CI scripts from ${WEKA_REPO}/ci"
    for f in ci-runner.sh post-results.sh collect-logs.sh \
             gerrit-listener.py tempest-include.txt tempest-exclude.txt \
             local.conf.template \
             run-tempest.sh weka-org-cleanup.sh; do
        [ -f "${WEKA_REPO}/ci/${f}" ] && cp "${WEKA_REPO}/ci/${f}" "${CI_DIR}/"
    done
    chmod +x "${CI_DIR}"/*.sh "${CI_DIR}"/*.py 2>/dev/null || true
fi

# ── Pin manila-tempest-plugin to the WEKAFS access_key change ──────────────────
# TEMPORARY: until change 999687 merges, master asserts access_key is None for
# WEKAFS ip rules and fails now that the driver returns the mount credential in
# access_key. Check that change out over the master clone stack.sh pulled and
# reinstall it editable so tempest discovers the patched tests.
# https://review.opendev.org/c/openstack/manila-tempest-plugin/+/999687
#
# The patchset is resolved from Gerrit rather than hardcoded. It used to read
# refs/changes/87/999687/1; the change reached patchset 3 and nobody noticed,
# because fetching an old patchset still succeeds -- CI kept reporting on a
# plugin two revisions behind the one that ships with the driver.
#
# This block retires itself: once 999687 merges, master carries the change and
# the pin is skipped.
TEMPEST_PLUGIN_DIR="/opt/stack/manila-tempest-plugin"
TEMPEST_PLUGIN_CHANGE="999687"
TEMPEST_PLUGIN_PROJECT="openstack%2Fmanila-tempest-plugin"

# Echo "<status> <ref>" for the change's current patchset, or nothing.
resolve_tempest_plugin_ref() {
    local url attempt
    url="https://review.opendev.org/changes/${TEMPEST_PLUGIN_PROJECT}~${TEMPEST_PLUGIN_CHANGE}?o=CURRENT_REVISION"
    for attempt in 1 2 3; do
        # Gerrit prefixes JSON with )]}' as XSSI protection; tail -c +6 drops it.
        curl -sf --max-time 20 "$url" 2>/dev/null | tail -c +6 | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(d["status"], d["revisions"][d["current_revision"]]["ref"])
' 2>/dev/null && return 0
        sleep $((attempt * 5))
    done
    return 1
}

if [ -d "${TEMPEST_PLUGIN_DIR}/.git" ]; then
    if ! _resolved=$(resolve_tempest_plugin_ref); then
        # Falling back to master would run tests that assert access_key is
        # None for WEKAFS and fail every change under review. Stop instead:
        # the cleanup trap marks the deploy broken, so ci-runner reports an
        # infrastructure failure rather than voting against contributors.
        log "ERROR: could not resolve change ${TEMPEST_PLUGIN_CHANGE} from Gerrit"
        exit 1
    fi
    _status=${_resolved%% *}
    TEMPEST_PLUGIN_REF=${_resolved#* }

    if [ "$_status" = "MERGED" ]; then
        log "Change ${TEMPEST_PLUGIN_CHANGE} has merged; using plugin master" \
            "(this pin block can now be deleted)"
    else
        log "Pinning manila-tempest-plugin to ${TEMPEST_PLUGIN_REF}"
        git -C "${TEMPEST_PLUGIN_DIR}" fetch \
            https://review.opendev.org/openstack/manila-tempest-plugin \
            "${TEMPEST_PLUGIN_REF}" 2>&1 \
            || { log "ERROR: could not fetch ${TEMPEST_PLUGIN_REF}"; exit 1; }
        git -C "${TEMPEST_PLUGIN_DIR}" checkout FETCH_HEAD 2>&1 \
            || { log "ERROR: could not check out ${TEMPEST_PLUGIN_REF}"; exit 1; }
        /opt/stack/data/venv/bin/pip install -e "${TEMPEST_PLUGIN_DIR}" -q 2>&1 \
            || { log "ERROR: could not reinstall manila-tempest-plugin"; exit 1; }
    fi
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

# NOTE: do NOT set a share_proto extra-spec — CapabilitiesFilter would match
# it against the driver's storage_protocol ("WEKAFS_NFS"), so 'WEKAFS' alone
# fails the filter (NoValidHost). Protocol is chosen per-share at create time;
# the backend is pinned via share_backend_name.
openstack share type create weka-wekafs false \
    --extra-specs share_backend_name=weka_wekafs \
    snapshot_support=true \
    create_share_from_snapshot_support=true \
    revert_to_snapshot_support=true \
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
    # Both protocols are tested: nfs is the pass-1 default; ci-runner's
    # run-tempest.sh runs a second pass with wekafs as the default (and
    # default_share_type_name=weka-wekafs), which runs the plugin's own
    # lifecycle tests and ShareRulesTest against WEKAFS shares directly.
    # No upstream-plugin patching is needed or done.
    iniset "$TEMPEST_CONF" share enable_protocols nfs,wekafs
    iniset "$TEMPEST_CONF" share enable_ip_rules_for_protocols nfs,wekafs
    iniset "$TEMPEST_CONF" share enable_ro_access_level_for_protocols nfs,wekafs
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
    # Must equal the driver's reported storage_protocol ("WEKAFS_NFS") so
    # share-type tests that key on storage_protocol (CapabilitiesFilter
    # exact-match) and ShareMultiBackendTest (splits on "_") pass.
    iniset "$TEMPEST_CONF" share capability_storage_protocol WEKAFS_NFS
    # NFS gateway is reachable (host firewall opened in gateway bootstrap;
    # ganesha is kernel-mode on 0.0.0.0:2049). Enable the snapshot-copy path.
    iniset "$TEMPEST_CONF" share capability_create_share_from_snapshot_support true
    iniset "$TEMPEST_CONF" share suppress_errors_in_cleanup true
    iniset "$TEMPEST_CONF" share build_timeout 600
    set -u
else
    log "WARNING: tempest.conf missing; skipping tempest [share] config"
fi

# ── Configure oslo.privsep helper ─────────────────────────────────────────────
# The driver runs mount/umount/rsync inside manila's sys_admin privsep daemon
# (manila.privsep.sys_admin_pctxt). This minimal devstack doesn't set up the
# privsep helper, so launch privsep-helper by full path (authorized via a
# sudoers rule) and point [manila_sys_admin] at it. Without this the daemon
# can't start and create_share_from_snapshot fails.
log "Configuring oslo.privsep helper for manila"
PRIVSEP_HELPER=/opt/stack/data/venv/bin/privsep-helper
sudo tee /etc/sudoers.d/weka-privsep-helper >/dev/null <<SUDOERS
stack ALL=(root) NOPASSWD: ${PRIVSEP_HELPER} *
SUDOERS
sudo chmod 440 /etc/sudoers.d/weka-privsep-helper
set +u
source "${DEVSTACK_DIR}/functions"
iniset /etc/manila/manila.conf manila_sys_admin helper_command \
    "sudo ${PRIVSEP_HELPER} --config-file /etc/manila/manila.conf"
set -u

# ── Set the Weka credential (untraced, kept out of every log) ─────────────────
# weka_password is NOT in local.conf's [[post-config]] block on purpose: DevStack
# applies that block with `iniset` under `set -x`, echoing the value into the
# published stack.sh.log. Set it directly in manila.conf here with command-echo
# disabled, so the secret is never written to a log while still reaching the
# driver. This script runs without `set -x`; `{ set +x; }` guards against a
# `bash -x` invocation too.
log "Setting Weka credential in manila.conf (untraced)"
# Stop manila-share while we (re)configure creds and discover the NFS gateway.
# Otherwise its driver's REST-login retries compete for the Weka admin account
# and trip the login lockout (403 "locked out for N s") that empties gateway
# discovery. It is restarted once, cleanly, at the end of this block.
sudo systemctl stop devstack@m-shr 2>&1 || true
sleep 2
_manila_kill_orphans   # reap reparented workers so none linger during discovery
set +u
source "${DEVSTACK_DIR}/functions"
{ set +x; } 2>/dev/null
iniset /etc/manila/manila.conf weka_nfs    weka_password "$WEKA_PASSWORD"
iniset /etc/manila/manila.conf weka_wekafs weka_password "$WEKA_PASSWORD"
# weka_org_admin_secret is mandatory (WEKAFS per-tenant isolation is
# always on). It is an HMAC key chosen by the operator to derive per-org
# credentials, not a pre-existing cluster credential, so a stable
# CI-local value is fine; override with $WEKA_ORG_ADMIN_SECRET if set.
iniset /etc/manila/manila.conf weka_nfs    weka_org_admin_secret "${WEKA_ORG_ADMIN_SECRET:-ci-only-weka-org-hmac}"
iniset /etc/manila/manila.conf weka_wekafs weka_org_admin_secret "${WEKA_ORG_ADMIN_SECRET:-ci-only-weka-org-hmac}"
# weka_auth_token_dir: the driver makedirs this path, but its default parent
# (/var/lib/manila) is root-owned and not writable by the `stack` user.
# /opt/stack/data is created by DevStack with stack:stack ownership.
iniset /etc/manila/manila.conf weka_nfs    weka_auth_token_dir /opt/stack/data/manila/weka-tokens
iniset /etc/manila/manila.conf weka_wekafs weka_auth_token_dir /opt/stack/data/manila/weka-tokens
set -u

# ── Point manila at the live Weka NFS gateway ─────────────────────────────────
# The CI host is a Weka client, so discover the UP nfs-gateway container's IP.
# ganesha listens on 0.0.0.0:2049 there; mountable once its host firewall is
# open (opened by the gateway bootstrap). Set weka_nfs_server in BOTH
# [weka_nfs] AND [weka_wekafs] sections: a cross-backend operation such as
# create_share_from_snapshot scheduled onto the wekafs backend needs the NFS
# copy path too, and hits weka_nfs_server=None without this.
#
# Lockout-safe: `weka` CLI login triggers a lockout (403 "locked out for N s")
# when the cluster has recently rate-limited auth.  Retry a few times with a
# short sleep before giving up; never hard-fail the redeploy on discovery error.
_discover_gw_ip() {
    python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = []
print(next((c["ips"][0] for c in d
            if "nfs-gateway" in (c.get("hostname") or "")
            and c.get("status") == "UP" and c.get("ips")), ""))
'
}

# Establish a FRESH Weka CLI session before discovery. Without this the
# discovery query below relies on a persisted login that is often stale/expired
# at (unattended) redeploy time, returns nothing, and leaves weka_nfs_server
# unset -> create_share_from_snapshot fails. Lockout-safe; never hard-fails.
# Discovery is best-effort: never let a login/query non-zero exit (or the
# `[ n -lt 5 ]` test evaluating false on the last attempt) abort the whole
# redeploy under `set -e`. Restore `set -e` after the block.
set +e
for _attempt in 1 2 3 4 5; do
    _login=$(weka user login admin "${WEKA_PASSWORD:-}" \
        -H "${WEKA_API_SERVER:-}" -P "${WEKA_API_PORT:-14000}" 2>&1)
    if echo "$_login" | grep -qi "locked out"; then
        # The lockout is ~2 minutes ("locked out for 2 minutes"); sleep past
        # it with a fixed wait (do NOT parse "2" as seconds — that's too short).
        log "Weka login locked out; waiting 130s (login ${_attempt}/5)"
        sleep 130
        continue
    fi
    weka status >/dev/null 2>&1 && break
    [ "$_attempt" -lt 5 ] && sleep 5
done
unset _attempt _login

GW_IP=""
for _attempt in 1 2 3 4 5; do
    _raw=$(weka cluster container -J 2>&1)
    if echo "$_raw" | grep -qi "locked out"; then
        log "Weka login locked out; waiting 130s before retry ${_attempt}/5"
        sleep 130
        continue
    fi
    GW_IP=$(echo "$_raw" | _discover_gw_ip 2>/dev/null)
    [ -n "$GW_IP" ] && break
    [ "$_attempt" -lt 5 ] && sleep 5
done
unset _attempt _raw
set -e

# Operator-pinned fallback: if discovery failed, use WEKA_NFS_SERVER from
# ci-env as a safety net.  This lets the operator hard-code a known-good
# gateway IP without touching deployment code.
if [ -z "$GW_IP" ] && [ -n "${WEKA_NFS_SERVER:-}" ]; then
    log "Weka NFS gateway not discovered; falling back to" \
        "WEKA_NFS_SERVER=${WEKA_NFS_SERVER} from ci-env"
    GW_IP="$WEKA_NFS_SERVER"
fi

if [ -n "$GW_IP" ]; then
    if [ "$GW_IP" = "${WEKA_API_SERVER:-}" ]; then
        log "WARNING: discovered weka_nfs_server=${GW_IP} equals the Weka API" \
            "host — this is likely the management IP, not an NFS gateway;" \
            "create_share_from_snapshot may fail"
    else
        log "Weka NFS gateway at ${GW_IP}; setting weka_nfs_server on both backends"
    fi
    set +u
    source "${DEVSTACK_DIR}/functions"
    iniset /etc/manila/manila.conf weka_nfs    weka_nfs_server "$GW_IP"
    iniset /etc/manila/manila.conf weka_wekafs weka_nfs_server "$GW_IP"
    set -u
else
    log "WARNING: no UP Weka NFS gateway found after 5 attempts and no" \
        "WEKA_NFS_SERVER fallback; weka_nfs_server left unconfigured" \
        "(create_share_from_snapshot will fail)"
fi

# Restart manila-share unconditionally so the post-stack weka_password (and
# weka_nfs_server, when discovered) take effect. Since weka_password is now set
# here rather than in local.conf, the backend can only authenticate after this
# restart — it must NOT depend on gateway discovery (which is best-effort).
_restart_manila_single_gen

# ── Post-deploy HEALTH GATE ───────────────────────────────────────────────────
# Informational: never exit non-zero here (the listener must still start so
# failures are reported), but be LOUD so errors are greppable in redeploy.log.
_health_check() {
    # Returns 0 (healthy) or 1 (degraded). Prints reason on failure.

    # 1. Both backends must appear in share pool list.
    set +u
    source "${DEVSTACK_DIR}/openrc" admin admin >/dev/null 2>&1
    set -u
    local _pools
    _pools=$(openstack share pool list --detail 2>/dev/null || true)
    if ! echo "$_pools" | grep -q "weka_nfs"; then
        echo "weka_nfs backend not in share pool list"
        return 1
    fi
    if ! echo "$_pools" | grep -q "weka_wekafs"; then
        echo "weka_wekafs backend not in share pool list"
        return 1
    fi

    # 2. weka_nfs_server must be set (non-empty) in both sections.
    local _nfs_srv_nfs _nfs_srv_wfs
    _nfs_srv_nfs=$(grep -A20 '^\[weka_nfs\]' /etc/manila/manila.conf \
        | grep 'weka_nfs_server' | head -1 | awk -F'=' '{print $2}' \
        | tr -d ' ' || true)
    _nfs_srv_wfs=$(grep -A20 '^\[weka_wekafs\]' /etc/manila/manila.conf \
        | grep 'weka_nfs_server' | head -1 | awk -F'=' '{print $2}' \
        | tr -d ' ' || true)
    if [ -z "$_nfs_srv_nfs" ] || [ -z "$_nfs_srv_wfs" ]; then
        echo "weka_nfs_server missing in manila.conf" \
             "(weka_nfs='${_nfs_srv_nfs}' weka_wekafs='${_nfs_srv_wfs}')"
        return 1
    fi

    echo "$_nfs_srv_nfs"   # used as the IP in the success log line
    return 0
}

log "Running post-deploy health gate (up to 90s)..."
set +e
_hg_ok=0
for _hg_attempt in 1 2; do
    _hg_deadline=$((SECONDS + 90))
    while [ "$SECONDS" -lt "$_hg_deadline" ]; do
        _hg_result=$(_health_check 2>&1)
        _hg_rc=$?
        if [ "$_hg_rc" -eq 0 ]; then
            _hg_ok=1
            break
        fi
        sleep 5
    done
    if [ "$_hg_ok" -eq 1 ]; then
        break
    fi
    if [ "$_hg_attempt" -eq 1 ]; then
        log "ERROR: post-deploy health gate FAILED: ${_hg_result}"
        log "Performing one additional manila-share restart..."
        _restart_manila_single_gen
    fi
done
set -e

if [ "$_hg_ok" -eq 1 ]; then
    log "Health gate OK: both backends up, weka_nfs_server=${_hg_result}"
else
    log "ERROR: CI deploy is DEGRADED — health gate failed after restart:" \
        "${_hg_result}"
    log "ERROR: Tempest runs will likely fail until this is resolved."
fi
unset _hg_ok _hg_attempt _hg_deadline _hg_result _hg_rc

# Verify (informational; never fail the redeploy on a query hiccup)
openstack share service list || true
openstack share type list || true
openstack share pool list --detail || true

# ── Restart the listener ──────────────────────────────────────────────────────

log "Restarting CI listener"
sudo systemctl start weka-manila-ci

# Lock released by the EXIT trap
log "=== Full redeploy complete ==="
