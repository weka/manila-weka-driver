#!/usr/bin/env bash
# =============================================================================
# Weka Manila CI - Log Report
#
# Parses CI run logs and produces a human-readable report.
# Run from your local machine.
#
# Usage: ./ci-report.sh <vm_ip> [count] [ssh_user]
# Example: ./ci-report.sh 89.168.91.125
#          ./ci-report.sh 89.168.91.125 20 ubuntu
# =============================================================================

set -euo pipefail

VM_IP="${1:?Usage: $0 <vm_ip> [count] [ssh_user]}"
COUNT="${2:-10}"
SSH_USER="${3:-ubuntu}"
LOG_URL="http://${VM_IP}:8088"

echo "============================================================"
echo "  Weka Manila CI - Log Report"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "  Log server: ${LOG_URL}/"
echo "============================================================"

# Everything runs in a single SSH session
ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no \
    "${SSH_USER}@${VM_IP}" bash -s "${COUNT}" 2>/dev/null <<'REMOTE'
COUNT="$1"
LOG_BASE=/var/www/ci-logs

fmt_duration() {
    local s="$1"
    [ -n "$s" ] && echo "$((s / 60))m$((s % 60))s" || echo "-"
}

# Parse a ci-runner.log in one pass with awk
# Returns: result|tempest_secs|total_msg|infra_msg|cherry_pick
parse_log() {
    awk '
    /CI run complete \(SUCCESS\)/ { result="PASS" }
    /CI run complete \(FAILURE\)/ { result="FAIL" }
    /FAILURE:/ { infra=$0; sub(/.*FAILURE: /, "", infra) }
    /Tempest completed in [0-9]+s/ {
        match($0, /in ([0-9]+)s/, m); tempest=m[1]
    }
    /Total: [0-9]+s/ {
        match($0, /Total: ([0-9]+)s/, m); total=m[1]
    }
    /Cherry-pick failed/ { cherry=1 }
    END {
        if (result == "") {
            if (infra != "") result="INFRA"
            else result="???"
        }
        printf "%s|%s|%s|%s|%s\n", result, tempest, total, infra, cherry
    }
    ' "$1"
}

# ── Discover all runs (single find) ─────────────────────────────────────────

ALL_RUNS=$(find "$LOG_BASE" -mindepth 2 -maxdepth 2 -type d \
    -printf '%T@ %p\n' 2>/dev/null | sort -rn)
TOTAL=$(echo "$ALL_RUNS" | grep -c . 2>/dev/null || echo 0)

# ── Summary stats ────────────────────────────────────────────────────────────

PASS=0; FAIL=0; INFRA=0; UNKNOWN=0
while IFS= read -r line; do
    [ -z "$line" ] && continue
    d="${line#* }"
    log="$d/ci-runner.log"
    [ -f "$log" ] || continue
    parsed=$(parse_log "$log")
    r="${parsed%%|*}"
    case "$r" in
        PASS)  PASS=$((PASS + 1)) ;;
        FAIL)  FAIL=$((FAIL + 1)) ;;
        INFRA) INFRA=$((INFRA + 1)) ;;
        *)     UNKNOWN=$((UNKNOWN + 1)) ;;
    esac
done <<< "$ALL_RUNS"

RUNNING=""
if pgrep -f ci-runner.sh >/dev/null 2>&1; then
    RUNNING=$(ps -eo args | grep '[c]i-runner.sh' | head -1)
fi

echo ""
echo "## Summary"
echo "  Total runs:    $TOTAL"
echo "  Passed:        $PASS"
echo "  Test failures: $FAIL"
echo "  Infra errors:  $INFRA"
[ -n "$RUNNING" ] && echo "  In progress:   1"
[ "$UNKNOWN" -gt 0 ] && echo "  Unknown:       $UNKNOWN"
[ "$TOTAL" -gt 0 ] && echo "  Pass rate:     $(( (PASS * 100) / TOTAL ))%"

# ── Recent runs detail ───────────────────────────────────────────────────────

RECENT=$(echo "$ALL_RUNS" | head -"$COUNT")

echo ""
echo "## Recent Runs (last ${COUNT})"
echo ""
printf "  %-14s %-10s %-8s %-8s %s\n" \
    "CHANGE/PS" "RESULT" "TEMPEST" "TOTAL" "DETAILS"
printf "  %-14s %-10s %-8s %-8s %s\n" \
    "---------" "------" "-------" "-----" "-------"

while IFS= read -r line; do
    [ -z "$line" ] && continue
    d="${line#* }"
    CHANGE=$(basename "$(dirname "$d")")
    PS=$(basename "$d")
    LABEL="${CHANGE}/${PS}"
    log="$d/ci-runner.log"

    if [ ! -f "$log" ]; then
        printf '  %-14s %-10s %-8s %-8s %s\n' \
            "$LABEL" "NO LOG" "-" "-" ""
        continue
    fi

    IFS='|' read -r RESULT TSEC DSEC INFRA_MSG CHERRY <<< "$(parse_log "$log")"

    TEMPEST_TIME=$(fmt_duration "$TSEC")
    TOTAL_TIME=$(fmt_duration "$DSEC")

    DETAILS=""
    if [ "$RESULT" = "INFRA" ]; then
        DETAILS="$INFRA_MSG"
    elif [ "$RESULT" = "FAIL" ]; then
        if [ "$CHERRY" = "1" ]; then
            DETAILS="cherry-pick conflict"
        else
            TLOG="$d/tempest.log"
            if [ -f "$TLOG" ]; then
                DETAILS=$(tail -10 "$TLOG" 2>/dev/null \
                    | grep -iE '(failed|passed)' | tail -1 || true)
            fi
        fi
    fi

    # Truncate long details
    [ ${#DETAILS} -gt 50 ] && DETAILS="${DETAILS:0:47}..."

    printf '  %-14s %-10s %-8s %-8s %s\n' \
        "$LABEL" "$RESULT" "$TEMPEST_TIME" "$TOTAL_TIME" "$DETAILS"
done <<< "$RECENT"

# ── Failed test names for recent failures ────────────────────────────────────

echo ""
echo "## Failed Tests (from recent failures)"

SHOWN=0
while IFS= read -r line; do
    [ -z "$line" ] && continue
    d="${line#* }"
    log="$d/ci-runner.log"
    [ -f "$log" ] || continue

    IFS='|' read -r RESULT _ _ _ CHERRY <<< "$(parse_log "$log")"
    [ "$RESULT" = "FAIL" ] || continue
    [ "$CHERRY" = "1" ] && continue

    CHANGE=$(basename "$(dirname "$d")")
    PS=$(basename "$d")
    TLOG="$d/tempest.log"
    [ -f "$TLOG" ] || continue

    FAILED=$(grep -E '^\{[0-9]+\}.*\[FAIL\]' "$TLOG" 2>/dev/null \
        | sed 's/^{[0-9]*} //; s/ \[.*$//' || true)
    if [ -z "$FAILED" ]; then
        FAILED=$(grep -E 'FAILED|FAIL:' "$TLOG" 2>/dev/null \
            | grep -v '^Traceback' | head -10 || true)
    fi

    if [ -n "$FAILED" ]; then
        echo ""
        echo "  ${CHANGE}/${PS}:"
        echo "$FAILED" | head -15 | sed 's/^/    /'
        SHOWN=$((SHOWN + 1))
    fi

    [ $SHOWN -ge 5 ] && break
done <<< "$RECENT"

[ $SHOWN -eq 0 ] && echo "  (none in recent runs)"

# ── Currently running ────────────────────────────────────────────────────────

if [ -n "$RUNNING" ]; then
    echo ""
    echo "## Currently Running"
    echo "  $RUNNING"
    LATEST=$(echo "$ALL_RUNS" | head -1 | awk '{print $2}')
    if [ -n "$LATEST" ] && [ -f "$LATEST/ci-runner.log" ]; then
        PHASE=$(grep '\] Phase' "$LATEST/ci-runner.log" | tail -1 \
            | sed 's/.*] //')
        [ -n "$PHASE" ] && echo "  Current: $PHASE"
    fi
fi
REMOTE

echo ""
echo "============================================================"
echo "  Full logs: ${LOG_URL}/<change>/<patchset>/"
echo "  Key files: ci-runner.log, tempest.log, manila-share.log"
echo "============================================================"
