# Configuration Guide

## Overview

The Weka Manila driver is configured via `manila.conf`.  This guide
covers every configuration option, recommended values, and common
deployment patterns.

## Minimal Configuration

```ini
[DEFAULT]
enabled_share_backends = weka

[weka]
share_driver = manila.share.drivers.weka.driver:WekaShareDriver
driver_handles_share_servers = false

weka_api_server   = weka-cluster.example.com
weka_username     = manila-driver
weka_password     = your-password
```

## Full Configuration Reference

### Connection Options

```ini
# Hostname or IP of the Weka cluster management endpoint.
# Must be reachable from the Manila host on the configured port.
weka_api_server = weka-cluster.example.com    # REQUIRED

# TCP port for the Weka REST API. Default: 14000
weka_api_port = 14000

# Whether to verify the Weka cluster TLS certificate.
# Set to false only in dev/test environments.
weka_ssl_verify = true
```

### Authentication

```ini
# Username for Weka REST API authentication.
weka_username = admin                          # REQUIRED

# Password for Weka REST API authentication.
weka_password = your-password                  # REQUIRED

# Weka organization name.  "Root" for the root organization.
# For multi-tenancy, set to the target org name.
weka_organization = Root
```

### Filesystem Management

```ini
# Name of the Weka filesystem group used for new shares.
# Created automatically if it does not exist.
weka_filesystem_group = default

# Prefix prepended to Weka filesystem names.
# Full name: <prefix><share-uuid>
# Must be unique across all Manila backends sharing the same cluster.
weka_share_name_prefix = manila_
```

### POSIX Client

```ini
# Base directory on the Manila host for WekaFS mounts.
weka_mount_point_base = /mnt/weka

# CPU cores for the WekaFS POSIX client. Range: 1–19.
# Higher values improve throughput for IO-intensive workloads.
weka_num_cores = 1

# NIC for DPDK mode (e.g. "eth0", "ens3f0").
# Omit to use kernel networking (UDP mode).
# weka_net_device = eth0

# Timeout to wait for a POSIX mount to complete (seconds).
weka_posix_mount_timeout = 60
```

### API Behaviour

```ini
# HTTP timeout for API requests (connect + read), in seconds.
weka_api_timeout = 30

# Maximum retries on transient errors (429, 5xx).
# Uses exponential back-off (1s, 2s, 4s, ...).
weka_max_api_retries = 3

# Number of urllib3 connection pools for the API session.
# Increase when connecting to multiple backend hosts.
weka_api_pool_connections = 4

# Max connections in the urllib3 pool. Should be >= expected
# concurrent API requests.
weka_api_pool_maxsize = 10
```

## Multi-Cluster Deployment

To manage multiple Weka clusters from one Manila service:

```ini
[DEFAULT]
enabled_share_backends = weka-prod,weka-dev

[weka-prod]
share_driver = manila.share.drivers.weka.driver:WekaShareDriver
share_backend_name = weka-prod
driver_handles_share_servers = false
weka_api_server   = weka-prod.example.com
weka_username     = admin
weka_password     = prod-password
weka_share_name_prefix = manila_prod_

[weka-dev]
share_driver = manila.share.drivers.weka.driver:WekaShareDriver
share_backend_name = weka-dev
driver_handles_share_servers = false
weka_api_server   = weka-dev.example.com
weka_username     = admin
weka_password     = dev-password
weka_share_name_prefix = manila_dev_
```

## Per-Tenant WEKAFS Isolation

### How it works

When `weka_wekafs_isolation = true` (the default), the driver maps each
Manila project to its own Weka organization (tenant).  WEKAFS filesystems
are created inside that org with `auth_required=True`, so only a client
holding a token scoped to that org can mount them — enforced by the Weka
cluster itself.

NFS shares are unaffected by this setting.

On first use of a project, the driver calls `POST /organizations` to
create the Weka org and its admin user in one API call.  All subsequent
filesystem operations for that project (create, delete, extend, shrink)
use an org-scoped session logged in as the per-org admin, because Weka
binds filesystem operations to the authenticated session's org.

The per-org admin password is derived deterministically:
`HMAC-SHA256(weka_org_admin_secret, project_id)`.  No per-tenant
secret is stored on disk.

The Weka org is **retained** when the project's last share is deleted;
it is not torn down automatically.

### Two principals per org

The driver creates **two** Weka users inside each per-project org:

1. **TenantAdmin** (`weka_org_user`, e.g. `manila`) — used internally
   for all filesystem operations; its password is
   `HMAC-SHA256(weka_org_admin_secret, project_id)`.  Never exposed to
   tenants.
2. **Regular mount user** (`weka_org_user + "-mnt"`, e.g. `manila-mnt`)
   — least-privilege account for tenant mounts; its password is
   `HMAC-SHA256(weka_org_admin_secret, project_id + ":mount")`.

### Self-service credential delivery via access_key

When a tenant creates a Manila access rule on a WEKAFS share, the driver
ensures the Regular mount user exists and returns its password as the
rule's **access_key** (Manila's `access_key` column, String(255)).

The tenant retrieves the credential with standard OpenStack tooling —
no operator intervention required:

```bash
openstack share create WEKAFS 100 --name myshare --share-type weka
openstack share access create myshare user weka
KEY=$(openstack share access list myshare \
        -f value -c "Access Key" | head -1)
```

The `KEY` value is the mount user's password.  Use it to log in and
mount:

```bash
# Log in to the per-project Weka org (writes ~/.weka/auth-token.json)
weka user login manila-mnt "$KEY" \
    --org <weka_org_name> -H <api-host>

# Mount the share
mount -t wekafs \
    -o auth_token_path=~/.weka/auth-token.json \
    <backend>/<fs_name> <mountpoint>
```

The access rule is still a no-op for **enforcement** (enforcement is at
the org boundary); its only added effect is carrying the credential.

The export-location metadata exposes `weka_org_name` and `weka_org_user`
(the mount user, e.g. `manila-mnt`) for reference.

### Isolation config options

```ini
# Enable per-project Weka org isolation for WEKAFS shares.
# When true, each Manila project gets its own Weka organization.
# NFS shares are not affected.
weka_wekafs_isolation = true

# HMAC-SHA256 key used to derive per-org admin passwords.
# REQUIRED when weka_wekafs_isolation = true.
# do_setup raises WekaConfigurationError if this is missing.
weka_org_admin_secret = <long-random-string>  # SECRET, REQUIRED

# Prefix applied to Weka organization names.
# Full org name: <prefix><project_id>
weka_org_prefix = manila-

# Username of the per-org TenantAdmin user (internal, never exposed).
# The least-privilege mount user is named <weka_org_user>-mnt.
weka_org_user = manila

# Directory where the driver writes its own per-org mount token
# files (mode 0600) for internal operations such as snapshot copy.
weka_auth_token_dir = /var/lib/manila/weka-tokens
```

## Multi-Tenancy (Weka Organizations — Manual Backend per Org)

Each Weka organization can also be a separate Manila backend (manual
approach, without per-project isolation):

```ini
[weka-org-finance]
share_driver = manila.share.drivers.weka.driver:WekaShareDriver
share_backend_name = weka-finance
driver_handles_share_servers = false
weka_api_server   = weka-cluster.example.com
weka_username     = finance-admin
weka_password     = finance-password
weka_organization = finance
weka_filesystem_group = finance-shares
weka_share_name_prefix = manila_finance_
```

## DPDK / High-Performance Configuration

For maximum throughput (100 GbE+ networks):

```ini
[weka-hpc]
weka_num_cores  = 4
weka_net_device = ens3f0
```

Requirements:
- Hugepages configured: `echo 4096 > /proc/sys/vm/nr_hugepages`
- DPDK-compatible NIC
- Weka cluster configured with backend NICs on the same network

## Security Recommendations

1. **TLS verification**: Keep `weka_ssl_verify = true` in production.
   Install the Weka cluster CA certificate:
   ```bash
   cp weka-ca.crt /etc/pki/ca-trust/source/anchors/
   update-ca-trust
   ```

2. **Dedicated API user**: Create a dedicated API user in Weka instead
   of using the admin account:
   ```bash
   weka user add manila-driver \
     --password 'StrongPasswordHere!' \
     --role OrgAdmin
   ```

3. **Credential encryption**: Use `oslo.messaging` credential encryption
   or HashiCorp Vault for `weka_password` rather than plaintext in
   `manila.conf`.

4. **Network segmentation**: The Manila host should access Weka on a
   dedicated management VLAN, separate from the data network.
