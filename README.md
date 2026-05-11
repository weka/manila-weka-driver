# Manila Weka Driver

OpenStack Manila share driver for [Weka](https://www.weka.io/) storage,
using the WekaFS POSIX client for optimal performance.

## Overview

This driver exposes Weka filesystems as Manila shares.  It supports two
access protocols:

- **WEKAFS** (primary) — the WekaFS kernel POSIX client mounted directly
  on the Manila host.  Sub-250 µs latency, full POSIX semantics, native
  quota enforcement.  Requires the WekaFS kernel module to be installed.
  > **Note:** The WekaFS kernel module does not compile on Linux kernel 6.17+.
  > See [Known Issues](docs/known-issues.md#1-wekafs-kernel-module-incompatible-with-linux-kernel-617).
- **NFS** (secondary) — standard NFS exports via Weka's built-in NFS
  gateway.  Works on all Linux kernel versions with no additional client
  software required.

### Why POSIX over NFS?

| Attribute | WekaFS POSIX | NFS |
|-----------|:---:|:---:|
| Latency | < 250 µs | 1–5 ms |
| POSIX compliance | Full | Partial |
| File locking | Yes | Advisory only |
| Adaptive caching | Page + dentry | None |
| Quota enforcement | Native | Post-hoc |
| Throughput | Near bare-metal | Network-bound |
| Kernel compatibility | < 6.17 | All versions |

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 OpenStack Manila                 │
│                  (share service)                 │
└───────────────────────┬─────────────────────────┘
                        │  Manila ShareDriver API
                        ▼
┌─────────────────────────────────────────────────┐
│              WekaShareDriver                    │
│          (manila/share/drivers/weka/)           │
│                                                 │
│  ┌──────────────────┐  ┌─────────────────────┐ │
│  │  WekaApiClient   │  │     WekaMount        │ │
│  │  (client.py)     │  │     (posix.py)       │ │
│  │                  │  │                     │ │
│  │  REST API v2     │  │  mount -t wekafs    │ │
│  │  port 14000      │  │  /proc/mounts check │ │
│  └────────┬─────────┘  └──────────┬──────────┘ │
└───────────┼────────────────────────┼────────────┘
            │ HTTPS                  │ kernel
            ▼                        ▼
┌──────────────────────────────────────────────────┐
│                  Weka Cluster                    │
│                                                 │
│  Filesystems  │  Snapshots  │  NFS  │  Quotas  │
└──────────────────────────────────────────────────┘
```

## Prerequisites

- **Weka cluster version** ≥ 5.0 (tested against 5.1.x). v5.x uses snake_case
  API parameters. v4.x used camelCase and has not been validated against this
  driver — compatibility is not guaranteed.
- **OpenStack Manila** ≥ 2023.1 (Antelope), tested against 2024.2 (Dalmatian)
- **WekaFS client** installed and loaded on the Manila host (required for
  WEKAFS protocol shares only; not needed for NFS):
  ```
  modprobe wekafsio
  ```
  > **Note:** The WekaFS kernel module does not compile on Linux kernel 6.17+.
  > See [Known Issues](docs/known-issues.md#1-wekafs-kernel-module-incompatible-with-linux-kernel-617).
- Network connectivity from the Manila host to the Weka cluster on
  TCP port **14000** (REST API) and the WekaFS data network.

## Documentation

| Document | Description |
|----------|-------------|
| [Step-by-Step Deployment Guide](docs/deployment.md) | Full novice-friendly walkthrough from prerequisites to first mounted share |
| [Architecture Overview](#architecture) | How the driver fits into OpenStack Manila |
| [Configuration Reference](#configuration) | All `weka_*` options explained |
| [Known Issues and Limitations](docs/known-issues.md) | Known constraints, workarounds, and areas for future improvement |

## Installation

Clone the driver repository and symlink it into Manila's source tree:

```bash
git clone https://github.com/weka/manila-weka-driver.git /opt/manila-weka-driver

ln -s /opt/manila-weka-driver/manila/share/drivers/weka \
      /path/to/manila/manila/share/drivers/weka
```

> **Note:** Do **not** `pip install` this package into a Manila environment.
> The namespace package layout conflicts with Manila's own `manila.share`
> module and will break the Manila API service.

## Configuration

### 1. Install the WekaFS kernel module

The WekaFS client package must be downloaded from your Weka cluster to
ensure the client version matches the cluster exactly:

```bash
# Replace with your Weka cluster IP and version
curl -k -o weka-client.tar https://<weka-ip>:14000/dist/v1/install/<weka-version>
tar xf weka-client.tar
sudo ./install.sh

# Load the module
sudo modprobe wekafsio

# Persist across reboots
echo "wekafs" | sudo tee /etc/modules-load.d/wekafs.conf
```

See the [Step-by-Step Deployment Guide](docs/deployment.md#step-2--install-the-wekafs-kernel-module)
for full details including how to find your Weka version.

### 2. Patch Manila constants (WEKAFS protocol)

Manila's hardcoded `SUPPORTED_SHARE_PROTOCOLS` list does not include `WEKAFS`.
Add it before starting the Manila share service:

```python
# In /path/to/manila/manila/common/constants.py
# Find SUPPORTED_SHARE_PROTOCOLS and add 'WEKAFS':
SUPPORTED_SHARE_PROTOCOLS = (
    'NFS', 'CIFS', 'FCP', 'iSCSI', 'FCOE', 'NVMEoF',
    'GLUSTERFS', 'HDFS', 'CEPHFS', 'MAPRFS', 'WEKAFS')
```

If using DevStack, the `devstack/plugin.sh` in this repo patches this automatically.

### 3. manila.conf example

```ini
[DEFAULT]
enabled_share_backends = weka
enabled_share_protocols = NFS,CIFS,WEKAFS

[weka]
share_driver = manila.share.drivers.weka.driver.WekaShareDriver
share_backend_name = weka
driver_handles_share_servers = false
snapshot_support = true
create_share_from_snapshot_support = true
revert_to_snapshot_support = true

# --- Connection ---
weka_api_server      = weka-cluster.example.com
weka_api_port        = 14000
weka_ssl_verify      = true

# --- Authentication ---
weka_username        = manila-driver
weka_password        = your-password-here
weka_organization    = Root

# --- Filesystem management ---
weka_filesystem_group = default
weka_share_name_prefix = manila_

# --- POSIX client on Manila host ---
weka_mount_point_base  = /mnt/weka
weka_num_cores         = 1
# weka_net_device      = eth0   # optional: NIC for DPDK mode

# --- API behaviour ---
weka_api_timeout       = 30
weka_max_api_retries   = 3
```

### 4. Configuration reference

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `weka_api_server` | `HostAddress` | **required** | Hostname or IP of the Weka cluster management endpoint |
| `weka_api_port` | `Port` | `14000` | TCP port for the Weka REST API |
| `weka_ssl_verify` | `Bool` | `true` | Verify the cluster's TLS certificate |
| `weka_username` | `String` | `admin` | API username |
| `weka_password` | `String (secret)` | **required** | API password |
| `weka_organization` | `String` | `Root` | Weka organization name to authenticate against |
| `weka_filesystem_group` | `String` | `default` | Filesystem group for new shares |
| `weka_mount_point_base` | `String` | `/mnt/weka` | Base directory for WekaFS mounts |
| `weka_num_cores` | `Int` (1–19) | `1` | CPU cores for the WekaFS POSIX client |
| `weka_net_device` | `String` | `None` | NIC for DPDK mode (e.g. `eth0`) |
| `weka_posix_mount_timeout` | `Int` | `60` | Seconds to wait for a POSIX mount |
| `weka_api_timeout` | `Int` | `30` | HTTP timeout for API requests (seconds) |
| `weka_max_api_retries` | `Int` | `3` | Maximum retries on transient API errors |
| `weka_share_name_prefix` | `String` | `manila_` | Prefix for Weka filesystem names |

## Supported Operations

| Operation | WEKAFS | NFS | Notes |
|-----------|:------:|:---:|-------|
| create_share | ✓ | ✓ | |
| delete_share | ✓ | ✓ | Idempotent |
| extend_share | ✓ | ✓ | |
| shrink_share | ✓ | ✓ | Guards against data loss |
| ensure_share | ✓ | ✓ | Re-mounts on recovery |
| create_snapshot | ✓ | ✓ | |
| delete_snapshot | ✓ | ✓ | Idempotent |
| revert_to_snapshot | ✓ | ✓ | |
| create_share_from_snapshot | ✓ | ✓ | |
| manage_existing | ✓ | ✓ | |
| unmanage | ✓ | ✓ | |
| get_share_stats | ✓ | ✓ | |

## Access Type Support

| Access Type | WEKAFS | NFS |
|-------------|:------:|:---:|
| `ip` | ✗ Rejected (`error` state) | ✓ Full enforcement |
| `user` | ✗ Rejected (`error` state) | ✗ |
| `cert` | ✗ | ✗ |

> **WEKAFS access rules:** All Manila access rule operations on WEKAFS shares
> return `error` state. Access control is managed via Weka's own authentication
> layer (filesystem `auth_required` flag and mount tokens). Use network-level
> controls (VPC security groups, firewall rules) for WEKAFS share security.
> See [Known Issues §6](docs/known-issues.md#6-wekafs-shares-do-not-support-manila-access-rules).

## Multi-tenancy

Weka organizations map directly to Manila share types.  Each organization
can have independent storage quotas and separate admin credentials.

To create a Manila share type targeting a specific Weka organization:

```bash
openstack share type create weka-org-a false \
  --extra-specs driver_handles_share_servers=false \
                weka_organization=org-a
```

Then configure a separate Manila backend stanza for each organization
with the appropriate `weka_organization`, `weka_username`, and
`weka_password`.

## DevStack Integration

A DevStack plugin is included in `devstack/` for automated test environment setup.

Add to your `local.conf`:

```ini
enable_plugin manila-weka-driver git@github.com:weka/manila-weka-driver.git main
```

The plugin:
- Symlinks the driver into Manila's source tree
- Loads the `wekafs` kernel module
- Patches `manila/common/constants.py` to add `WEKAFS` to `SUPPORTED_SHARE_PROTOCOLS`

See [mbookham7/weka-manila-test-env](https://github.com/mbookham7/weka-manila-test-env)
for a full Terraform-based AWS test environment that uses this plugin.

## Troubleshooting

### `WekaMountError: mount command failed`

The WekaFS kernel module is not loaded.  Run:
```bash
modprobe wekafsio
lsmod | grep wekafs   # should show the module
```

### `WekaAuthError: Weka authentication failed`

Check `weka_username`, `weka_password`, and `weka_organization` in
`manila.conf`.  Verify with:
```bash
curl -k -X POST https://<weka-host>:14000/api/v2/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"secret","org":"Root"}'
```

### `ShareShrinkingPossibleDataLoss`

The filesystem contains more data than the target size.  Free space on
the share before shrinking.

### SSL certificate errors

Set `weka_ssl_verify = false` to disable certificate verification in
test environments.  **Do not disable in production.**

### `FileSystemNotFound` errors in ensure_share

The Weka filesystem was deleted outside of Manila.  Either restore the
filesystem or remove the share from Manila:
```bash
manila delete <share-id>
```

## Running Tests

```bash
# Install test dependencies
pip install -r test-requirements.txt

# Unit tests
tox -e py311

# PEP 8 / style check
tox -e pep8

# Coverage report
tox -e cover
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and
submission guidelines.
