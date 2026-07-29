#!/usr/bin/env bash
# =============================================================================
# Weka Manila CI - Tenant (org) reaper
#
# WEKAFS per-tenant isolation maps every Manila project to its own Weka org
# and, by design, retains that org when the project's last share is deleted
# (driver.py delete_share; docs/known-issues.md #11). Tempest churns through
# many ephemeral projects per run, so orgs accumulate every run and are never
# reaped -- until the cluster's tenant table fills and every WEKAFS share
# create fails with "Could not create tenant: Tenant table is full", tipping
# a whole WEKAFS pass into cascading ERRORs.
#
# This reaps leftover CI orgs (name prefix "manila-") so the table stays
# clear. It is CI-only: every manila-* org on this cluster is an ephemeral CI
# test tenant, so it is safe to remove them all. It runs pre-tempest, when no
# share is live, and is invoked with "|| true" -- it never fails a build.
#
# Uses the weka CLI's own (auto-refreshed) session, so it performs NO password
# login: that avoids the login-lockout the CI already guards against, and it
# works now that WEKA_PASSWORD is no longer stored in /opt/weka-ci/ci-env.
#
# Usage: weka-org-cleanup.sh                 # reap all manila-* orgs
#        WEKA_ORG_PREFIX=foo- weka-org-cleanup.sh
# =============================================================================

set -uo pipefail

PREFIX="${WEKA_ORG_PREFIX:-manila-}"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] weka-org-cleanup: $*"; }

if ! command -v weka >/dev/null 2>&1; then
    log "weka CLI not found on PATH; skipping org reap"
    exit 0
fi

# `weka tenant` (alias for the deprecated `weka org`) lists every tenant.
# Parse its JSON for names matching the prefix. Root (id 0) never matches, so
# it is never a candidate.
names="$(weka tenant --format json 2>/dev/null | python3 -c '
import sys, json
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if isinstance(rows, dict):
    rows = rows.get("data", [])
pref = sys.argv[1]
for r in rows:
    name = str(r.get("name", ""))
    if name.startswith(pref):
        print(name)
' "$PREFIX")"

if [ -z "${names//[[:space:]]/}" ]; then
    log "no orgs matching prefix '${PREFIX}' to reap"
    exit 0
fi

total=0
reaped=0
failed=0
while IFS= read -r name; do
    [ -z "$name" ] && continue
    total=$((total + 1))
    if weka tenant remove "$name" --force >/dev/null 2>&1; then
        reaped=$((reaped + 1))
    else
        failed=$((failed + 1))
        log "WARNING: could not remove org '${name}' (skipping)"
    fi
done <<< "$names"

log "reaped ${reaped}/${total} orgs matching '${PREFIX}' (${failed} skipped)"
exit 0
