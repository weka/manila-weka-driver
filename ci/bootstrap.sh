#!/usr/bin/env bash
# =============================================================================
# Weka Manila CI - Single-Command Bootstrap
#
# Run this from your local machine to set up a fresh Ubuntu 22.04 VM as a
# complete Manila third-party CI system. It copies all CI files to the VM,
# runs setup, generates the SSH key, and verifies everything works.
#
# Usage:
#   ./bootstrap.sh <vm_ip> <weka_api_server> <weka_password> <gerrit_user> [ssh_user]
#
# Example:
#   ./bootstrap.sh 130.61.191.215 10.0.1.210 MyWekaPass123 Assaf ubuntu
#
# Prerequisites:
#   - You can SSH into the VM as <ssh_user> (default: ubuntu)
#   - The VM is Ubuntu 22.04 with at least 4 CPUs, 16GB RAM, 100GB disk
#   - The Weka cluster is reachable from the VM
#   - You have a Gerrit account on review.opendev.org
# =============================================================================

set -euo pipefail

# ── Parse arguments ───────────────────────────────────────────────────────────

if [ $# -lt 4 ]; then
    echo "Usage: $0 <vm_ip> <weka_api_server> <weka_password> <gerrit_user> [ssh_user]"
    echo ""
    echo "  vm_ip            - IP address of the CI VM"
    echo "  weka_api_server  - Weka cluster API IP/hostname"
    echo "  weka_password    - Weka API password"
    echo "  gerrit_user      - Your username on review.opendev.org"
    echo "  ssh_user         - SSH user on the VM (default: ubuntu)"
    exit 1
fi

VM_IP="$1"
WEKA_API_SERVER="$2"
WEKA_PASSWORD="$3"
GERRIT_USER="$4"
SSH_USER="${5:-ubuntu}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Step 1: Verify SSH connectivity ──────────────────────────────────────────

log "Step 1: Verifying SSH to ${SSH_USER}@${VM_IP}..."
if ! ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "${SSH_USER}@${VM_IP}" "echo ok" &>/dev/null; then
    echo "ERROR: Cannot SSH into ${SSH_USER}@${VM_IP}"
    exit 1
fi
log "SSH OK"

# Verify Ubuntu 22.04
DISTRO=$(ssh "${SSH_USER}@${VM_IP}" "lsb_release -cs 2>/dev/null || echo unknown")
if [[ "$DISTRO" != "jammy" ]]; then
    echo "WARNING: Expected Ubuntu 22.04 (jammy), got: ${DISTRO}"
    read -p "Continue anyway? [y/N] " -r
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
fi

# ── Step 2: Copy CI files to VM ──────────────────────────────────────────────

log "Step 2: Copying CI files to VM..."
ssh "${SSH_USER}@${VM_IP}" "rm -rf /tmp/manila-weka-driver-ci && mkdir -p /tmp/manila-weka-driver-ci"
scp -q "${SCRIPT_DIR}"/* "${SSH_USER}@${VM_IP}:/tmp/manila-weka-driver-ci/"
log "Files copied"

# ── Step 3: Generate SSH key for Gerrit (if needed) ──────────────────────────

log "Step 3: Checking SSH key for Gerrit..."
HAS_KEY=$(ssh "${SSH_USER}@${VM_IP}" "test -f ~/.ssh/id_ed25519 && echo yes || echo no")

if [ "$HAS_KEY" = "no" ]; then
    log "Generating SSH key..."
    ssh "${SSH_USER}@${VM_IP}" "ssh-keygen -t ed25519 -C 'weka-manila-ci' -f ~/.ssh/id_ed25519 -N ''"
fi

PUBKEY=$(ssh "${SSH_USER}@${VM_IP}" "cat ~/.ssh/id_ed25519.pub")

# Test if the key is already registered with Gerrit
log "Testing Gerrit SSH connection..."
if ssh "${SSH_USER}@${VM_IP}" "ssh -p 29418 -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 ${GERRIT_USER}@review.opendev.org gerrit version" &>/dev/null; then
    log "Gerrit SSH works"
else
    echo ""
    echo "============================================================"
    echo "SSH key is NOT registered with Gerrit."
    echo ""
    echo "Add this public key to your Gerrit account:"
    echo "  https://review.opendev.org → Settings → SSH Keys"
    echo ""
    echo "  ${PUBKEY}"
    echo ""
    echo "============================================================"
    read -p "Press Enter after adding the key to Gerrit..."

    # Verify again
    if ! ssh "${SSH_USER}@${VM_IP}" "ssh -p 29418 -o ConnectTimeout=10 ${GERRIT_USER}@review.opendev.org gerrit version" &>/dev/null; then
        echo "ERROR: Still cannot connect to Gerrit. Check the key and username."
        exit 1
    fi
    log "Gerrit SSH works"
fi

# ── Step 4: Update configuration with actual values ──────────────────────────

log "Step 4: Updating configuration..."

# Update GERRIT_USER in files on the VM (the default in templates)
ssh "${SSH_USER}@${VM_IP}" "
    sed -i 's|GERRIT_USER = os.environ.get(\"GERRIT_USER\", \"Assaf\")|GERRIT_USER = os.environ.get(\"GERRIT_USER\", \"${GERRIT_USER}\")|' /tmp/manila-weka-driver-ci/gerrit-listener.py
    sed -i 's|GERRIT_USER=\"\${GERRIT_USER:-Assaf}\"|GERRIT_USER=\"\${GERRIT_USER:-${GERRIT_USER}}\"|' /tmp/manila-weka-driver-ci/post-results.sh
    sed -i 's|GERRIT_USER=\"\${GERRIT_USER:-Assaf}\"|GERRIT_USER=\"\${GERRIT_USER:-${GERRIT_USER}}\"|' /tmp/manila-weka-driver-ci/setup.sh
    sed -i 's|Environment=GERRIT_USER=Assaf|Environment=GERRIT_USER=${GERRIT_USER}|' /tmp/manila-weka-driver-ci/weka-manila-ci.service
"

# ── Step 5: Run setup ────────────────────────────────────────────────────────

log "Step 5: Running setup on VM (this takes 20-40 minutes)..."
log "You can follow progress with: ssh ${SSH_USER}@${VM_IP} 'tail -f /tmp/ci-setup.log'"

ssh "${SSH_USER}@${VM_IP}" "
    sudo WEKA_API_SERVER='${WEKA_API_SERVER}' \
         WEKA_API_PORT='14000' \
         WEKA_USERNAME='admin' \
         WEKA_PASSWORD='${WEKA_PASSWORD}' \
         WEKA_ORGANIZATION='Root' \
         CI_VM_IP='${VM_IP}' \
         GERRIT_USER='${GERRIT_USER}' \
         bash /tmp/manila-weka-driver-ci/setup.sh 2>&1 | tee /tmp/ci-setup.log
"

SETUP_RC=$?
if [ "$SETUP_RC" -ne 0 ]; then
    echo "ERROR: Setup failed with exit code ${SETUP_RC}"
    echo "Check logs: ssh ${SSH_USER}@${VM_IP} 'cat /tmp/ci-setup.log'"
    exit 1
fi

# ── Step 6: Start CI listener ────────────────────────────────────────────────

log "Step 6: Starting CI listener..."
ssh "${SSH_USER}@${VM_IP}" "sudo systemctl start weka-manila-ci"

# ── Step 7: Verify ───────────────────────────────────────────────────────────

log "Step 7: Verifying..."

# Check listener is running
LISTENER_STATUS=$(ssh "${SSH_USER}@${VM_IP}" "sudo systemctl is-active weka-manila-ci 2>/dev/null || echo failed")
if [ "$LISTENER_STATUS" = "active" ]; then
    log "CI listener is running"
else
    echo "WARNING: CI listener is not running. Check: ssh ${SSH_USER}@${VM_IP} 'journalctl -u weka-manila-ci -n 50'"
fi

# Check nginx
NGINX_STATUS=$(ssh "${SSH_USER}@${VM_IP}" "curl -s -o /dev/null -w '%{http_code}' http://localhost/")
if [ "$NGINX_STATUS" = "200" ]; then
    log "Nginx log server is running"
else
    echo "WARNING: Nginx returned ${NGINX_STATUS}"
fi

# Check nightly timer
TIMER_STATUS=$(ssh "${SSH_USER}@${VM_IP}" "sudo systemctl is-active weka-manila-ci-redeploy.timer 2>/dev/null || echo failed")
if [ "$TIMER_STATUS" = "active" ]; then
    log "Nightly redeploy timer is active"
fi

echo ""
echo "============================================================"
echo "  Weka Manila CI Setup Complete"
echo "============================================================"
echo ""
echo "  CI VM:        ${VM_IP}"
echo "  Logs:         http://${VM_IP}/ci-logs/"
echo "  Gerrit user:  ${GERRIT_USER}"
echo "  Weka cluster: ${WEKA_API_SERVER}:14000"
echo ""
echo "  Monitor:      ssh ${SSH_USER}@${VM_IP} 'journalctl -u weka-manila-ci -f'"
echo "  Status:       ssh ${SSH_USER}@${VM_IP} 'sudo systemctl status weka-manila-ci'"
echo ""
echo "  NOTE: Voting is disabled by default. Set VOTING_ENABLED=true"
echo "        once the Manila team grants voting rights."
echo "============================================================"
