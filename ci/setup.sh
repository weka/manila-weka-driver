#!/usr/bin/env bash
# =============================================================================
# Weka Manila CI - One-Shot VM Setup
#
# Run once on a fresh Ubuntu 24.04 VM to install all dependencies,
# create the stack user, deploy DevStack, configure nginx for log
# hosting, and install systemd services.
#
# Usage: sudo bash setup.sh
#
# Prerequisites:
#   - Ubuntu 24.04
#   - SSH key for Gerrit already present at /home/ubuntu/.ssh/id_ed25519
#   - Environment variables set (or edit the defaults below):
#       WEKA_API_SERVER, WEKA_API_PORT, WEKA_USERNAME, WEKA_PASSWORD, WEKA_ORGANIZATION
#       CI_ALLOWED_CIDRS  (space-separated CIDRs for root log directory access)
#   - Optional:
#       WEKA_JOIN_IPS     (comma-separated backend ip:port list; installs and
#                          joins the local Weka client when set)
#       WEKA_JOIN_TOKEN   (client join token, if the cluster requires one; for an
#                          operator cluster, key "join-secret" of weka-client-<name>)
#       WEKA_ENABLE_NFS   (false = WEKAFS-only, for clusters with no NFS gateway)
#       CI_TRUSTED_USERS  (Gerrit usernames allowed to trigger runs, comma-separated)
#       CI_TRUSTED_GROUPS (Gerrit groups whose members may trigger, e.g. manila-core)
#       WEKA_ORG_PREFIX, WEKA_ORG_ADMIN_SECRET, WEKA_NFS_SERVER
# =============================================================================

set -euo pipefail

# ── Configuration (override via environment) ──────────────────────────────────

export WEKA_API_SERVER="${WEKA_API_SERVER:?Set WEKA_API_SERVER environment variable}"
export WEKA_API_PORT="${WEKA_API_PORT:-14000}"
export WEKA_USERNAME="${WEKA_USERNAME:-admin}"
export WEKA_PASSWORD="${WEKA_PASSWORD:?Set WEKA_PASSWORD environment variable}"
export WEKA_ORGANIZATION="${WEKA_ORGANIZATION:-Root}"
export WEKA_JOIN_IPS="${WEKA_JOIN_IPS:-}"
export WEKA_JOIN_TOKEN="${WEKA_JOIN_TOKEN:-}"
export WEKA_ENABLE_NFS="${WEKA_ENABLE_NFS:-true}"
export WEKA_ORG_PREFIX="${WEKA_ORG_PREFIX:-manila-}"
export WEKA_ORG_ADMIN_SECRET="${WEKA_ORG_ADMIN_SECRET:-}"
export WEKA_NFS_SERVER="${WEKA_NFS_SERVER:-}"
export CI_TRUSTED_USERS="${CI_TRUSTED_USERS:-}"
export CI_TRUSTED_GROUPS="${CI_TRUSTED_GROUPS:-}"
# Same derivation as full-redeploy.sh, which owns every later deploy.
if [ "$WEKA_ENABLE_NFS" = "true" ]; then
    export WEKA_ENABLED_BACKENDS="weka_nfs,weka_wekafs"
    export WEKA_ENABLED_PROTOCOLS="NFS,WEKAFS"
    export WEKA_DEFAULT_BACKEND="weka_nfs"
    export WEKA_DEFAULT_SHARE_TYPE="weka-nfs"
else
    export WEKA_ENABLED_BACKENDS="weka_wekafs"
    export WEKA_ENABLED_PROTOCOLS="WEKAFS"
    export WEKA_DEFAULT_BACKEND="weka_wekafs"
    export WEKA_DEFAULT_SHARE_TYPE="weka-wekafs"
fi
export CI_VM_IP="${CI_VM_IP:-$(hostname -I | awk '{print $1}')}"
export CI_HOST_IP="$(hostname -I | awk '{print $1}')"
export CI_LOG_URL="http://${CI_VM_IP}:8088"
GERRIT_USER="${GERRIT_USER:-Assaf}"
CI_ALLOWED_CIDRS="${CI_ALLOWED_CIDRS:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] $*"; }

log "=== Weka Manila CI Setup Starting ==="

# ── 1. System packages ───────────────────────────────────────────────────────

log "Installing system packages"
apt-get update
apt-get install -y \
    git python3 python3-pip python3-venv \
    nginx \
    openssh-client \
    bridge-utils \
    iptables \
    net-tools \
    curl \
    jq \
    gettext-base \
    gzip \
    nfs-common

# ── 2. Create the stack user (DevStack requirement) ──────────────────────────

log "Creating stack user"
if ! id -u stack &>/dev/null; then
    useradd -m -s /bin/bash stack
    echo "stack ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/stack
    chmod 0440 /etc/sudoers.d/stack
fi

# ── 3. Set up CI working directories ─────────────────────────────────────────

log "Setting up directories"
mkdir -p /opt/weka-ci
mkdir -p /var/lib/weka-ci
mkdir -p /var/www/ci-logs
mkdir -p /opt/stack
chown -R stack:stack /opt/weka-ci /var/lib/weka-ci /var/www/ci-logs /opt/stack

# ── 4. Copy CI scripts ───────────────────────────────────────────────────────

log "Installing CI scripts"
cp "${SCRIPT_DIR}/gerrit-listener.py" /opt/weka-ci/
cp "${SCRIPT_DIR}/ci-runner.sh" /opt/weka-ci/
cp "${SCRIPT_DIR}/post-results.sh" /opt/weka-ci/
cp "${SCRIPT_DIR}/collect-logs.sh" /opt/weka-ci/
cp "${SCRIPT_DIR}/full-redeploy.sh" /opt/weka-ci/
cp "${SCRIPT_DIR}/weka-org-cleanup.sh" /opt/weka-ci/
cp "${SCRIPT_DIR}/local.conf.template" /opt/weka-ci/
cp "${SCRIPT_DIR}/tempest-include.txt" /opt/weka-ci/
chmod +x /opt/weka-ci/*.sh /opt/weka-ci/*.py
chown -R stack:stack /opt/weka-ci

# ── 4a. Persist CI environment variables ─────────────────────────────────────

log "Persisting CI environment"
cat > /opt/weka-ci/ci-env <<EOF
CI_VM_IP=${CI_VM_IP}
CI_HOST_IP=${CI_HOST_IP}
WEKA_API_SERVER=${WEKA_API_SERVER}
WEKA_API_PORT=${WEKA_API_PORT}
WEKA_USERNAME=${WEKA_USERNAME}
WEKA_PASSWORD=${WEKA_PASSWORD}
WEKA_ORGANIZATION=${WEKA_ORGANIZATION}
WEKA_ENABLE_NFS=${WEKA_ENABLE_NFS}
WEKA_ORG_PREFIX=${WEKA_ORG_PREFIX}
GERRIT_USER=${GERRIT_USER}
EOF
# Optional keys are written only when set, so the scripts' defaults still apply.
for _key in WEKA_ORG_ADMIN_SECRET WEKA_NFS_SERVER WEKA_JOIN_IPS CI_TRUSTED_USERS CI_TRUSTED_GROUPS; do
    if [ -n "${!_key}" ]; then
        echo "${_key}=${!_key}" >> /opt/weka-ci/ci-env
    fi
done
unset _key
chmod 600 /opt/weka-ci/ci-env
chown stack:stack /opt/weka-ci/ci-env

# ── 5. SSH key for Gerrit ────────────────────────────────────────────────────

log "Configuring SSH for stack user"
mkdir -p /home/stack/.ssh
cp /home/ubuntu/.ssh/id_* /home/stack/.ssh/ 2>/dev/null || true
chown -R stack:stack /home/stack/.ssh
chmod 700 /home/stack/.ssh
chmod 600 /home/stack/.ssh/id_* 2>/dev/null || true

# Add Gerrit host key
sudo -u stack ssh-keyscan -p 29418 review.opendev.org \
    >> /home/stack/.ssh/known_hosts 2>/dev/null || true

# ── 6. Configure nginx ───────────────────────────────────────────────────────

log "Configuring nginx"

# Build IP-restrict directives for root directory listing.
# If CI_ALLOWED_CIDRS is set, only those CIDRs can browse /.
# All deeper paths (linked from Gerrit) remain public.
NGINX_ALLOW_RULES=""
if [ -n "$CI_ALLOWED_CIDRS" ]; then
    NGINX_ALLOW_RULES="        allow 127.0.0.1;\n"
    for cidr in $CI_ALLOWED_CIDRS; do
        NGINX_ALLOW_RULES="${NGINX_ALLOW_RULES}        allow ${cidr};\n"
    done
    NGINX_ALLOW_RULES="${NGINX_ALLOW_RULES}        deny all;"
fi
# Resolved here, not inside the heredoc: bash ends a $(...) in a heredoc at the
# first ")", so a default containing parentheses breaks the whole block.
NGINX_ALLOW_RULES=$(printf '%b' \
    "${NGINX_ALLOW_RULES:-        # unrestricted (set CI_ALLOWED_CIDRS to restrict)}")

cat > /etc/nginx/sites-available/weka-ci-logs <<NGINX
server {
    listen 8088;
    server_name _;
    root /var/www/ci-logs;
    location = / {
${NGINX_ALLOW_RULES}
        autoindex on;
        autoindex_exact_size off;
        autoindex_localtime on;
    }
    location / {
        autoindex on;
        autoindex_exact_size off;
        autoindex_localtime on;
        types {
            text/html  html;
            text/plain log txt conf ini;
            application/gzip gz;
        }
        default_type text/plain;
        gzip_static on;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/weka-ci-logs /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
systemctl restart nginx
systemctl enable nginx

# Oracle's Ubuntu images ship iptables rules that REJECT every new inbound
# connection except SSH, so the log links posted to Gerrit would be dead even
# with the cloud firewall open. Allow 8088 ahead of the REJECT and persist it.
if ! iptables -C INPUT -p tcp -m state --state NEW -m tcp --dport 8088 -j ACCEPT 2>/dev/null; then
    _reject=$(iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" {print $1; exit}')
    iptables -I INPUT "${_reject:-1}" -p tcp -m state --state NEW -m tcp --dport 8088 -j ACCEPT
    unset _reject
fi
if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save
fi

# ── 7. Install systemd services ──────────────────────────────────────────────

log "Installing systemd services"

# Listener service
cp "${SCRIPT_DIR}/weka-manila-ci.service" /etc/systemd/system/
cp "${SCRIPT_DIR}/weka-manila-ci-redeploy.service" /etc/systemd/system/
cp "${SCRIPT_DIR}/weka-manila-ci-redeploy.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable weka-manila-ci
systemctl enable weka-manila-ci-redeploy.timer

# ── 8. Log rotation cron ─────────────────────────────────────────────────────

log "Setting up log rotation"
cat > /etc/cron.d/weka-ci-logs <<'CRON'
# Clean CI logs older than 45 days (Manila requires 30 day minimum retention)
0 3 * * * root find /var/www/ci-logs -type d -mindepth 1 -maxdepth 1 -mtime +45 -exec rm -rf {} \;
CRON

# ── 8a. Install and join the Weka client ─────────────────────────────────────
# The driver mounts wekafs on this host (snapshot copies, _ensure_share), and
# full-redeploy.sh's `weka` CLI calls go through the local client, so it must
# be joined to the same cluster as WEKA_API_SERVER. The client is installed
# from the cluster itself so its version matches the backends exactly.
# Skipped when WEKA_JOIN_IPS is unset (the client was set up some other way).

if [ -n "$WEKA_JOIN_IPS" ]; then
    # Reinstalling restarts the agent under an already-joined client, so a
    # re-run leaves an existing install alone.
    if ! command -v weka >/dev/null 2>&1; then
        log "Installing Weka client from ${WEKA_API_SERVER}:${WEKA_API_PORT}"
        curl -k --fail -o /tmp/weka-install.sh \
            "https://${WEKA_API_SERVER}:${WEKA_API_PORT}/dist/v1/install"
        bash /tmp/weka-install.sh
        rm -f /tmp/weka-install.sh
    fi

    # The installer only sets up the agent; fetch the cluster's own release.
    weka user login "${WEKA_USERNAME}" "${WEKA_PASSWORD}" \
        -H "${WEKA_API_SERVER}" -P "${WEKA_API_PORT}" >/dev/null
    _release=$(weka status -J -H "${WEKA_API_SERVER}" -P "${WEKA_API_PORT}" \
        | python3 -c 'import json, sys; print(json.load(sys.stdin)["release"])')
    # --set-current can report "Version missing" while still finishing the
    # switch, so judge by the resulting current version instead.
    weka version get "${_release}" \
        --from "https://${WEKA_API_SERVER}:${WEKA_API_PORT}" \
        --client-only --set-current --no-progress-bar || true
    [ "$(weka version current)" = "${_release}" ] \
        || { log "ERROR: Weka client version is not ${_release}"; exit 1; }
    unset _release

    if weka local ps 2>/dev/null | grep -qw client; then
        log "Weka client container already exists; not re-joining"
    else
        # Clients authenticate with a join token (--join-token, not shown in
        # --help); --join-secret is for backends and is rejected with --client.
        log "Joining Weka client (UDP mode) to ${WEKA_JOIN_IPS}"
        _join_token_args=()
        if [ -n "$WEKA_JOIN_TOKEN" ]; then
            _join_token_args=(--join-token "$WEKA_JOIN_TOKEN")
        fi
        weka local setup container --name client --client \
            --only-frontend-cores --cores 1 --net udp \
            --management-ips "${CI_HOST_IP}" \
            --join-ips "${WEKA_JOIN_IPS}" "${_join_token_args[@]}"
        unset _join_token_args
    fi
    modprobe wekafsio
fi

# ── 9. Deploy DevStack ───────────────────────────────────────────────────────

log "Deploying DevStack (this will take 20-40 minutes)"

sudo -u stack \
    CI_HOST_IP="${CI_HOST_IP}" \
    CI_VM_IP="${CI_VM_IP}" \
    WEKA_API_SERVER="${WEKA_API_SERVER}" \
    WEKA_API_PORT="${WEKA_API_PORT}" \
    WEKA_USERNAME="${WEKA_USERNAME}" \
    WEKA_PASSWORD="${WEKA_PASSWORD}" \
    WEKA_ORGANIZATION="${WEKA_ORGANIZATION}" \
    WEKA_ENABLED_BACKENDS="${WEKA_ENABLED_BACKENDS}" \
    WEKA_ENABLED_PROTOCOLS="${WEKA_ENABLED_PROTOCOLS}" \
    WEKA_DEFAULT_BACKEND="${WEKA_DEFAULT_BACKEND}" \
    WEKA_DEFAULT_SHARE_TYPE="${WEKA_DEFAULT_SHARE_TYPE}" \
    WEKA_ORG_PREFIX="${WEKA_ORG_PREFIX}" \
    bash -c "
    # master, like full-redeploy.sh: local.conf pulls manila and its plugins
    # from master, which a stable DevStack's constraints cannot install.
    # Re-runs reuse the clone left by an earlier attempt.
    if [ -d /opt/stack/devstack ]; then
        cd /opt/stack/devstack
        git fetch origin
        git reset --hard origin/master
    else
        cd /opt/stack
        git clone -b master https://opendev.org/openstack/devstack.git
        cd devstack
    fi
    envsubst '\$CI_HOST_IP \$CI_VM_IP \$WEKA_API_SERVER \$WEKA_API_PORT \$WEKA_USERNAME \$WEKA_PASSWORD \$WEKA_ORGANIZATION \$WEKA_ENABLED_BACKENDS \$WEKA_ENABLED_PROTOCOLS \$WEKA_DEFAULT_BACKEND \$WEKA_DEFAULT_SHARE_TYPE \$WEKA_ORG_PREFIX' < /opt/weka-ci/local.conf.template > local.conf
    ./stack.sh
"

STACK_RC=$?
if [ "$STACK_RC" -ne 0 ]; then
    log "ERROR: DevStack deployment failed with exit code ${STACK_RC}"
    log "Check /opt/stack/logs/stack.sh.log for details"
    log "Fix the issue and re-run: sudo -u stack bash -c 'cd /opt/stack/devstack && ./stack.sh'"
    exit 1
fi

# ── 10. Create share types ───────────────────────────────────────────────────

log "Creating share types"
# cd first: the caller's cwd (e.g. /home/ubuntu) is unreadable to stack, and
# the openstack CLI then dies stat()ing a relative entry on its sys.path.
sudo -u stack WEKA_ENABLE_NFS="${WEKA_ENABLE_NFS}" bash -c "
    cd /opt/stack
    source /opt/stack/devstack/openrc admin admin
    if [ \"\$WEKA_ENABLE_NFS\" = true ]; then
        openstack share type create weka-nfs false \
            --extra-specs share_backend_name=weka_nfs \
            snapshot_support=true \
            create_share_from_snapshot_support=true \
            revert_to_snapshot_support=true
    fi

    openstack share type create weka-wekafs false \
        --extra-specs share_backend_name=weka_wekafs \
        snapshot_support=true \
        create_share_from_snapshot_support=true \
        revert_to_snapshot_support=true \
        share_proto=WEKAFS

    echo '=== Share services ==='
    openstack share service list

    echo '=== Share types ==='
    openstack share type list

    echo '=== Share pools ==='
    openstack share pool list --detail
"

# ── 11. Start the nightly redeploy timer ─────────────────────────────────────

systemctl start weka-manila-ci-redeploy.timer

# ── Done ──────────────────────────────────────────────────────────────────────

log "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Check the Weka client is joined (set WEKA_JOIN_IPS to have setup.sh do it):"
echo "     weka local ps   # 'client' container Running"
echo ""
echo "  2. Start the CI listener:"
echo "     sudo systemctl start weka-manila-ci"
echo ""
echo "  3. Monitor:"
echo "     journalctl -u weka-manila-ci -f"
echo ""
echo "  4. Check logs at: ${CI_LOG_URL}/"
