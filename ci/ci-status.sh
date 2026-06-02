#!/usr/bin/env bash
# =============================================================================
# Weka Manila CI - Status Report
#
# Generates a summary to share with the Manila/Gerrit team when requesting
# voting rights. Run from your local machine.
#
# Usage: ./ci-status.sh <vm_ip> [ssh_user]
# Example: ./ci-status.sh 138.2.136.28 ubuntu
# =============================================================================

set -euo pipefail

VM_IP="${1:?Usage: $0 <vm_ip> [ssh_user]}"
SSH_USER="${2:-ubuntu}"

ssh_cmd() { ssh -o ConnectTimeout=10 "${SSH_USER}@${VM_IP}" "$1" 2>/dev/null; }

echo "============================================================"
echo "  Weka Manila Third-Party CI - Status Report"
echo "  Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "============================================================"

# ── CI VM Info ──
echo ""
echo "## CI VM"
echo "  IP:       ${VM_IP}"
echo "  Logs URL: http://${VM_IP}:8088/"
ssh_cmd "
echo \"  OS:       \$(lsb_release -ds 2>/dev/null || cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2)\"
echo \"  Kernel:   \$(uname -r)\"
echo \"  CPUs:     \$(nproc)\"
echo \"  RAM:      \$(free -h | awk '/Mem:/ {print \$2}')\"
echo \"  Disk:     \$(df -h / | awk 'NR==2 {print \$2 \" total, \" \$4 \" free\"}')\"
echo \"  Uptime:   \$(uptime -p)\"
"

# ── Services ──
echo ""
echo "## Services"
ssh_cmd "
printf '  %-35s %s\n' 'CI Listener (weka-manila-ci):' \"\$(sudo systemctl is-active weka-manila-ci)\"
printf '  %-35s %s\n' 'Nightly Redeploy Timer:' \"\$(sudo systemctl is-active weka-manila-ci-redeploy.timer)\"
printf '  %-35s %s\n' 'Nginx (log server):' \"\$(sudo systemctl is-active nginx)\"
printf '  %-35s %s\n' 'DevStack (devstack@*):' \"\$(sudo systemctl is-active devstack@* 2>/dev/null | head -1 || echo 'N/A')\"
"

# ── Gerrit Connection ──
echo ""
echo "## Gerrit Integration"
ssh_cmd "
GERRIT_USER=\$(grep -oP 'GERRIT_USER=\K[^ ]+' /etc/systemd/system/weka-manila-ci.service 2>/dev/null || echo 'unknown')
echo \"  Gerrit user: \${GERRIT_USER}\"
if ssh -p 29418 -o ConnectTimeout=5 \${GERRIT_USER}@review.opendev.org gerrit version 2>/dev/null; then
    echo \"  Gerrit SSH:  connected\"
else
    echo \"  Gerrit SSH:  FAILED\"
fi
"

# ── Weka Backend ──
echo ""
echo "## Weka Backend"
ssh_cmd "
if [ -f /etc/manila/manila.conf ]; then
    echo \"  API Server:  \$(grep -m1 weka_api_url /etc/manila/manila.conf 2>/dev/null | awk -F'=' '{print \$2}' | xargs || echo 'N/A')\"
fi
# Try environment from service file
WEKA_HOST=\$(grep -oP 'WEKA_API_SERVER=\K[^ ]+' /etc/systemd/system/weka-manila-ci.service 2>/dev/null || echo '')
WEKA_PORT=\$(grep -oP 'WEKA_API_PORT=\K[^ ]+' /etc/systemd/system/weka-manila-ci.service 2>/dev/null || echo '14000')
if [ -n \"\$WEKA_HOST\" ]; then
    echo \"  Cluster:     \${WEKA_HOST}:\${WEKA_PORT}\"
fi
"

# ── Manila Share Services ──
echo ""
echo "## Manila Services & Share Types"
ssh_cmd "
if [ -f /opt/stack/devstack/openrc ]; then
    source /opt/stack/devstack/openrc admin admin 2>/dev/null
    echo '  Share services:'
    openstack share service list -f table 2>/dev/null | sed 's/^/    /'
    echo ''
    echo '  Share types:'
    openstack share type list -f table 2>/dev/null | sed 's/^/    /'
    echo ''
    echo '  Share pools:'
    openstack share pool list -f table 2>/dev/null | sed 's/^/    /'
else
    echo '  DevStack openrc not found'
fi
"

# ── Tempest Tests Configured ──
echo ""
echo "## Tempest Test Scope"
ssh_cmd "
if [ -f /opt/weka-ci/tempest-include.txt ]; then
    echo '  Included test patterns:'
    cat /opt/weka-ci/tempest-include.txt | sed 's/^/    /'
else
    echo '  tempest-include.txt not found'
fi
"

# ── Recent CI Runs ──
echo ""
echo "## Recent CI Runs (last 10)"
ssh_cmd "
if [ -d /var/www/ci-logs ]; then
    ls -1td /var/www/ci-logs/*/ 2>/dev/null | head -10 | while read dir; do
        name=\$(basename \"\$dir\")
        result='unknown'
        if [ -f \"\$dir/result.txt\" ]; then
            result=\$(cat \"\$dir/result.txt\")
        elif [ -f \"\$dir/ci-result.txt\" ]; then
            result=\$(cat \"\$dir/ci-result.txt\")
        fi
        ts=\$(stat -c '%y' \"\$dir\" 2>/dev/null | cut -d. -f1 || echo 'N/A')
        printf '  %-50s %-10s %s\n' \"\$name\" \"\$result\" \"\$ts\"
    done
else
    echo '  No CI runs found yet'
fi
"

# ── Log Retention ──
echo ""
echo "## Log Retention"
ssh_cmd "
if [ -f /etc/cron.d/weka-ci-logs ]; then
    echo '  Policy: 45-day retention (exceeds Manila 30-day minimum)'
    OLDEST=\$(ls -1td /var/www/ci-logs/*/ 2>/dev/null | tail -1 | xargs basename 2>/dev/null || echo 'N/A')
    NEWEST=\$(ls -1td /var/www/ci-logs/*/ 2>/dev/null | head -1 | xargs basename 2>/dev/null || echo 'N/A')
    COUNT=\$(ls -1d /var/www/ci-logs/*/ 2>/dev/null | wc -l)
    echo \"  Total runs:  \$COUNT\"
    echo \"  Oldest:      \$OLDEST\"
    echo \"  Newest:      \$NEWEST\"
else
    echo '  No retention policy found'
fi
"

# ── Voting Status ──
echo ""
echo "## Voting"
ssh_cmd "
VOTING=\$(grep -oP 'VOTING_ENABLED=\K[^ ]+' /etc/systemd/system/weka-manila-ci.service 2>/dev/null || echo 'unknown')
echo \"  Voting enabled: \${VOTING}\"
echo \"  (Set to true once Manila team grants voting rights)\"
"

echo ""
echo "============================================================"
echo "  Log browser: http://${VM_IP}:8088/"
echo "============================================================"
