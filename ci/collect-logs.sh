#!/usr/bin/env bash
# =============================================================================
# Weka Manila CI - Log Collection
#
# Usage: collect-logs.sh <log_dir>
#
# Gathers Manila logs, tempest results, system info into the log directory.
# Redacts passwords from config files.
# =============================================================================

set -uo pipefail

LOG_DIR="$1"
mkdir -p "$LOG_DIR"

echo "Collecting logs to ${LOG_DIR}"

# ── DevStack logs ─────────────────────────────────────────────────────────────

cp /opt/stack/logs/*.log "$LOG_DIR/" 2>/dev/null || true

# ── Manila service logs ───────────────────────────────────────────────────────

journalctl -u devstack@m-shr --no-pager -n 5000 > "$LOG_DIR/manila-share.log" 2>/dev/null || true
journalctl -u devstack@m-api --no-pager -n 5000 > "$LOG_DIR/manila-api.log" 2>/dev/null || true
journalctl -u devstack@m-sch --no-pager -n 5000 > "$LOG_DIR/manila-scheduler.log" 2>/dev/null || true

# ── Configuration files (redacted) ────────────────────────────────────────────

if [ -f /etc/manila/manila.conf ]; then
    sed -E 's/(password|secret|token)\s*=\s*.*/\1 = ***REDACTED***/gi' \
        /etc/manila/manila.conf > "$LOG_DIR/manila.conf"
fi

if [ -f /opt/stack/devstack/local.conf ]; then
    sed -E 's/(PASSWORD|password|secret|token)\s*=\s*.*/\1=***REDACTED***/gi' \
        /opt/stack/devstack/local.conf > "$LOG_DIR/local.conf"
fi

if [ -f /opt/stack/tempest/etc/tempest.conf ]; then
    sed -E 's/(password|secret|token)\s*=\s*.*/\1 = ***REDACTED***/gi' \
        /opt/stack/tempest/etc/tempest.conf > "$LOG_DIR/tempest.conf"
fi

# ── Tempest results ───────────────────────────────────────────────────────────

if [ -d /opt/stack/tempest/.stestr ]; then
    cp -r /opt/stack/tempest/.stestr "$LOG_DIR/stestr" 2>/dev/null || true
fi

# ── System info ───────────────────────────────────────────────────────────────

{
    echo "=== Date ==="
    date -u
    echo ""
    echo "=== Uname ==="
    uname -a
    echo ""
    echo "=== Memory ==="
    free -h
    echo ""
    echo "=== Disk ==="
    df -h
    echo ""
    echo "=== Manila git log ==="
    cd /opt/stack/manila 2>/dev/null && git log --oneline -5
    echo ""
    echo "=== Weka mounts ==="
    mount | grep weka || echo "(none)"
} > "$LOG_DIR/sysinfo.txt" 2>&1

# ── Compress large logs ───────────────────────────────────────────────────────

find "$LOG_DIR" -name '*.log' -size +10M -exec gzip {} \;

# ── Set permissions ───────────────────────────────────────────────────────────

chmod -R o+r "$LOG_DIR"

echo "Log collection complete"
