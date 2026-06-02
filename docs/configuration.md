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

## Multi-Tenancy (Weka Organizations)

Each Weka organization can be a separate Manila backend:

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
