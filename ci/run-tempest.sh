#!/usr/bin/env bash
# =============================================================================
# Weka Manila CI - Two-pass tempest runner (NFS + WEKAFS)
#
# Runs tempest twice: first with the NFS default (as configured by
# full-redeploy.sh), then with WEKAFS as default.  Restores tempest.conf
# to the original NFS-default state on exit.  Writes a combined summary to
# ${LOG_DIR}/tempest.log so the existing ci-runner.sh result-parser still
# works.
#
# WEKAFS coverage (pass 2): enable_protocols=wekafs,nfs +
# default_share_type_name=weka-wekafs causes the plugin's own lifecycle tests
# and ShareRulesTest to exercise WEKAFS shares directly.  No upstream-plugin
# patching is needed or done.
#
# Environment / args (all optional with defaults):
#   LOG_DIR          (env) or $1  — directory for per-pass and combined logs
#   INCLUDE_LIST     (env) or $2  — tempest include-list file
#                                   default: ${CI_DIR}/tempest-include.txt
#   EXCLUDE_LIST     (env)        — tempest exclude-list file (optional)
#                                   default: ${CI_DIR}/tempest-exclude.txt
#                                   (silently skipped when the file is absent)
#   TIMEOUT_TEMPEST  (env)        — per-pass timeout in seconds  (default 3600)
#   CI_DIR           (env)        — CI scripts directory          (default /opt/weka-ci)
#   TEMPEST_CONF     (env)        — path to tempest.conf
#                                   (default /opt/stack/tempest/etc/tempest.conf)
#
# Exit codes:
#   0  both passes passed
#   1  one or both passes failed
# =============================================================================

# -e deliberately omitted so a failing pass doesn't abort before config restore.
set -uo pipefail

# ── Resolve parameters ────────────────────────────────────────────────────────

LOG_DIR="${LOG_DIR:-${1:-}}"
CI_DIR="${CI_DIR:-/opt/weka-ci}"
INCLUDE_LIST="${INCLUDE_LIST:-${2:-${CI_DIR}/tempest-include.txt}}"
TIMEOUT_TEMPEST="${TIMEOUT_TEMPEST:-3600}"
TEMPEST_CONF="${TEMPEST_CONF:-/opt/stack/tempest/etc/tempest.conf}"
EXCLUDE_LIST="${EXCLUDE_LIST:-${CI_DIR}/tempest-exclude.txt}"

if [ -z "$LOG_DIR" ]; then
    echo "ERROR: LOG_DIR must be set (env or \$1)" >&2
    exit 1
fi

mkdir -p "$LOG_DIR"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] run-tempest: $*"; }

# ── Ensure venv on PATH and that iniset is available ──────────────────────────
# run-tempest.sh is a separate bash process, so it does NOT inherit shell
# functions like iniset from its caller.  We MUST be able to edit tempest.conf
# between passes; if we can't, fail loudly rather than silently re-run NFS.
export PATH="/opt/stack/data/venv/bin:${PATH}"
if ! command -v iniset >/dev/null 2>&1; then
    set +u
    # shellcheck disable=SC1091
    source /opt/stack/devstack/inc/ini-config 2>/dev/null || true
    set -u
fi
if ! command -v iniset >/dev/null 2>&1; then
    echo "ERROR: iniset unavailable (could not source devstack ini-config);" \
         "cannot switch tempest.conf to the WEKAFS pass" >&2
    exit 2
fi

# ── Restore tempest.conf on exit (always) ────────────────────────────────────
# Back up the whole file so the restore is complete regardless of which keys
# pass 2 mutates below.
cp "$TEMPEST_CONF" "${TEMPEST_CONF}.weka-preflight-bak"
restore_conf() {
    log "Restoring tempest.conf to its pre-run state"
    cp "${TEMPEST_CONF}.weka-preflight-bak" "$TEMPEST_CONF" || true
    rm -f "${TEMPEST_CONF}.weka-preflight-bak" || true
}
trap restore_conf EXIT

# ── Run one tempest pass; sets the global RC to tempest's exit code ──────────
run_tempest_pass() {
    local name="$1" logf="$2"
    log "=== Pass: ${name} ==="
    # No `|| true` here: it would run `true` on failure and reset PIPESTATUS to
    # (0), masking a failed run. set -e is not enabled, so a non-zero pipeline
    # does not abort the script; PIPESTATUS[0] captures tempest's real exit.
    local exclude_args=()
    if [ -f "$EXCLUDE_LIST" ]; then
        exclude_args=(--exclude-list "$EXCLUDE_LIST")
    fi
    timeout "$TIMEOUT_TEMPEST" tempest run \
        --include-list "$INCLUDE_LIST" \
        "${exclude_args[@]}" \
        --concurrency 1 \
        2>&1 | tee "$logf"
    RC=${PIPESTATUS[0]}
    log "Pass (${name}) completed with exit code ${RC}"
}

# ── Pass 1: NFS default ───────────────────────────────────────────────────────
# tempest.conf already has enable_protocols=nfs,wekafs and
# default_share_type_name=weka-nfs (set by full-redeploy.sh); no change needed.
run_tempest_pass "NFS default" "${LOG_DIR}/tempest-nfs.log"
RC_NFS=$RC

# ── Pass 2: WEKAFS default ────────────────────────────────────────────────────
# Only enable_protocols[0] and the default share type are position-sensitive;
# enable_ip_rules_for_protocols / enable_ro_access_level_for_protocols are
# membership lists that already include wekafs, so they need no flip.
iniset "$TEMPEST_CONF" share enable_protocols wekafs,nfs
iniset "$TEMPEST_CONF" share default_share_type_name weka-wekafs
run_tempest_pass "WEKAFS default" "${LOG_DIR}/tempest-wekafs.log"
RC_WEKAFS=$RC

# ── Aggregate result (both passes must pass) ─────────────────────────────────
AGGREGATE_RC=1
[ "$RC_NFS" -eq 0 ] && [ "$RC_WEKAFS" -eq 0 ] && AGGREGATE_RC=0

# ── Write combined summary to tempest.log ────────────────────────────────────
# ci-runner.sh parses this via: tail -5 tempest.log | grep -E "Ran|passed|failed"
# | head -1, so the aggregate line must fall within the last 5 lines.
NFS_SUMMARY=$(grep -E "Ran [0-9]|passed|failed" "${LOG_DIR}/tempest-nfs.log" | tail -3 || echo "(no summary)")
WEKAFS_SUMMARY=$(grep -E "Ran [0-9]|passed|failed" "${LOG_DIR}/tempest-wekafs.log" | tail -3 || echo "(no summary)")

{
    echo "=== NFS pass (RC=${RC_NFS}) ==="
    echo "$NFS_SUMMARY"
    echo ""
    echo "=== WEKAFS pass (RC=${RC_WEKAFS}) ==="
    echo "$WEKAFS_SUMMARY"
    echo ""
    if [ "$AGGREGATE_RC" -eq 0 ]; then
        echo "Ran 2 passes - passed (NFS RC=0, WEKAFS RC=0)"
    else
        echo "Ran 2 passes - failed (NFS RC=${RC_NFS}, WEKAFS RC=${RC_WEKAFS})"
    fi
} > "${LOG_DIR}/tempest.log"

log "Combined summary written to ${LOG_DIR}/tempest.log"
log "NFS RC=${RC_NFS}  WEKAFS RC=${RC_WEKAFS}  AGGREGATE=${AGGREGATE_RC}"

exit "$AGGREGATE_RC"
