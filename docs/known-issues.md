# Known Issues and Limitations

This page documents known limitations, constraints, and issues with the
Manila Weka driver, along with workarounds where available.

---

## 1. WekaFS Kernel Module Incompatible with Linux Kernel 6.17+

**Affects:** WEKAFS protocol shares only. NFS protocol is unaffected.

**Description:**
Linux kernel 6.17 introduced a breaking change in the `inode_operations`
struct: the `mkdir` function pointer return type changed from `int` to
`struct dentry *`. The Weka 5.x kernel client module (`weka-driver`) was
compiled against the older signature and fails to build on kernel 6.17+:

```
gw_dirops.c:574:27: error: initialization of 'struct dentry * (*)(...)' from
incompatible pointer type 'int (*)(...)'
[-Werror=incompatible-pointer-types]
```

**Impact:**
- `WEKAFS` protocol shares cannot be mounted on Manila hosts running kernel ≥ 6.17.
- `NFS` protocol shares are fully functional on all kernel versions.
- The driver starts and operates normally; only the POSIX kernel-client mount
  path is affected.

**Workaround:**
Pin the Manila host kernel to a version prior to 6.17. On Ubuntu/Debian:

```bash
# Identify the current kernel package
uname -r

# Pin it — prevents apt from upgrading to 6.17+
sudo apt-mark hold linux-aws linux-image-aws linux-headers-aws

# Verify
apt-mark showhold
```

On Ubuntu 22.04 (kernel 5.15 LTS) this issue does not arise. Ubuntu 24.04
with AWS AMI kernels may ship with or upgrade to 6.17+ without pinning.

**Resolution:**
A fix requires Weka to update the kernel module source to use the new
`struct dentry *` return type. Until then, use NFS protocol or pin the
kernel below 6.17.

---

## 2. `create_share_from_snapshot` Uses NFS-Based Data Copy

**Affects:** All protocol shares when creating a share from a snapshot.

**Description:**
The Weka v2 API does not expose a direct "clone filesystem from snapshot"
operation for read-only snapshots. The driver therefore copies snapshot
data by:

1. Creating an empty destination filesystem.
2. Temporarily mounting source and destination filesystems via NFS.
3. Using `rsync` to copy the snapshot contents across.
4. Unmounting and cleaning up the temporary NFS mounts.

**Impact:**
- Copy time scales linearly with the amount of data in the snapshot.
  For large filesystems (hundreds of GB to TB range) this can take a
  significant amount of time.
- Network bandwidth between the Manila host and the Weka NFS gateway
  is the bottleneck, not Weka cluster performance.
- A `weka_nfs_server` address must be configured (see
  [Configuration Reference](../README.md#configuration)).

**Workaround:**
For large shares, plan for `create_share_from_snapshot` to take longer
than other share operations. There is no way to reduce copy time beyond
ensuring good network connectivity between the Manila host and the Weka
NFS gateway.

**Future improvement:**
If Weka exposes a native snapshot-clone API in a future release, the
driver should be updated to use it, eliminating the NFS copy entirely.

---

## 3. Orphan Resources After `create_share_from_snapshot` Failure

**Affects:** `create_share_from_snapshot` only.

**Description:**
During `create_share_from_snapshot`, the driver creates temporary resources
on the Weka cluster:

- An NFS client group named `manila-snap-<share-id-prefix>`
- NFS permissions for the source and destination filesystems

If the Manila process is killed or crashes after these resources are created
but before cleanup runs, they are left as orphans on the Weka cluster.

**Impact:**
Orphan client groups and NFS permissions accumulate on the cluster. They do
not affect cluster operation but should be cleaned up periodically.

**Cleanup:**
Orphan resources can be identified by their name prefix `manila-snap-` in
the Weka management console under **NFS → Client Groups**. It is safe to
delete any client group matching this prefix that does not correspond to an
in-progress Manila operation.

Via the Weka CLI:

```bash
weka nfs client-group list | grep manila-snap-
weka nfs client-group delete <group-name>
```

---

## 4. NFS Permission Propagation Delay

**Affects:** `create_share_from_snapshot` only.

**Description:**
After creating temporary NFS permissions during snapshot copy, the Weka NFS
gateway needs a moment to apply them. The driver retries the mount with
exponential back-off (6 attempts, ~25 s total) instead of sleeping. On
heavily loaded clusters or under high NFS gateway restart load, the
permissions may still not be live within that window, causing the mount to
fail.

**Impact:**
`create_share_from_snapshot` fails with an NFS mount error. The operation
can be retried.

**Workaround:**
If this is observed regularly, increase the NFS gateway restart grace period
on the Weka cluster, or reduce the load on the NFS gateway at the time of
share creation.

---

## 5. WEKAFS Protocol Requires WekaFS Client on Manila Host

**Affects:** WEKAFS protocol shares only. NFS protocol is unaffected.

**Description:**
For `WEKAFS` protocol shares, the Manila host must have the WekaFS client
package installed and the `wekafs` kernel module loaded. The client version
must match the Weka cluster version exactly.

```bash
# Check module is loaded
lsmod | grep wekafsio

# If not loaded
sudo modprobe wekafsio
```

If the module is absent or fails to load, the driver logs a warning at
startup and all WEKAFS share operations will fail. NFS shares are unaffected.

See the [Deployment Guide](deployment.md#step-2--install-the-wekafs-kernel-module)
for full installation instructions.

---

## 6. WEKAFS Access Control: Scope and Limits

**Affects:** WEKAFS protocol shares only. NFS protocol is unaffected.

**Summary:** WEKAFS shares now enforce per-share access with Weka
security policies. `ip` access rules are honored and `--access-level`
(`ro`/`rw`) is enforced. This section documents what is enforced and the
remaining limits.

**How access is enforced (two layers):**

1. **Org boundary (always on).** Every WEKAFS filesystem is created with
   `auth_required=True` inside the project's own Weka organization, so a
   client can mount only with an org-scoped token (see
   [architecture.md §10](architecture.md#10-per-tenant-wekafs-isolation-via-weka-organizations)).
2. **Per-share IP policy.** On top of the org boundary, the driver maps
   each `ip` access rule to a Weka per-filesystem **security policy**,
   which the cluster evaluates at native mount time by the client's
   source IP:
   - Once any policy is attached, a client whose IP matches no policy is
     **denied** the mount (deny-by-default). With no `ip` rule, the share
     is mountable by any holder of the org token (allow-all within org).
   - `--access-level ro` installs a read-only policy, so the mount is
     read-only; `rw` allows writes. (This replaces the previous behavior
     where `ro` was silently ignored.)

`user`/`cert` rules do not map to an IP policy; they are returned as
`active` but impose no per-IP restriction. The mount credential is in
export-location metadata (`weka_mount_password`).

Two models (see [configuration.md](configuration.md)):
- **Model A (default):** per-share policies driven by each share's `ip`
  rules (`manila-<share8>-rw` / `-ro`).
- **Model B:** a share type with the `weka:security_policy_group` extra
  spec attaches shared, reusable group policies defined by
  `weka_security_policy_group`.

**Limits:**

- **IPv4 only.** IPv6 `ip` rules are rejected with an `error` state.
- **Client-IP granularity, not per-user.** Access is by source IP/CIDR,
  not by an authenticated user identity. Two users behind the same IP are
  indistinguishable (the same is true of the NFS path's `ip` rules). True
  per-user access would require POSIX ACLs (uid-based, trusts the client)
  or NFS Kerberos — neither is implemented.
- **Native mount path only.** Security policies gate the native WEKAFS
  (POSIX/DPDK) mount. They are **not** evaluated for clients arriving
  through an NFS/SMB **interface group** (interface hosts are exempt by
  default and the NFS mount path does not consult filesystem security
  policies) — NFS access is controlled by NFS client-groups + export
  permissions instead.
- **Per-org policy budget.** Weka caps distinct security policies per
  organization (production: 128) and attached policies per filesystem
  (16). Model A uses at most two policies per share, so a single
  project-org supports on the order of ~64–128 access-controlled shares
  before the budget is exhausted; beyond that, use **Model B** so many
  shares reuse a small set of shared group policies.

**Self-service credential:** the mount user's password is in the share's
export-location metadata as `weka_mount_password` (no operator step). See
[Configuration Guide](configuration.md#self-service-credential-delivery-via-export-location-metadata)
and [§10 below](#10-wekafs-mount-requires-weka-user-login-cli-step).

---

## 7. Standard (Thick) Provisioning Only

**Affects:** All shares.

**Description:**
Weka supports both standard and thin provisioning. With thin provisioning,
administrators specify a minimum guaranteed SSD capacity
(`thin-provision-min-ssd`) and a maximum capacity (`thin-provision-max-ssd`),
allowing the cluster to over-commit SSD capacity across filesystems.

The driver currently creates filesystems with standard provisioning only —
capacity is fully reserved at creation time. The driver reports
`thin_provisioning=False` in backend statistics so the Manila scheduler
does not over-commit capacity.

**Impact:**
The total capacity of all Manila shares cannot exceed the cluster's
available SSD capacity. There is no capacity over-subscription.

**Future work:**
A future release could add thin provisioning support by mapping Manila's
provisioned capacity to Weka's `thin-provision-min-ssd` /
`thin-provision-max-ssd` parameters, enabling over-subscription when the
Manila `max_over_subscription_ratio` option is configured.

---

## 8. ~~NFS Client-Group Leak Under Repeated Access-Rule Changes~~ (Fixed)

**Affects:** NFS protocol shares only. **Resolved in current version.**

**Description (historical):**
The original `update_access` implementation called
`create_client_group` unconditionally on every add-rule call, and
`_remove_nfs_rule` deleted only the NFS permission without deleting the
client group. Over many add/delete cycles, orphan client groups
accumulated on the Weka cluster until the cluster hit its per-cluster
NFS client-group cap, causing all subsequent `ip` rule additions to
fail with:

```
Weka API error 400: /nfs/clientGroups: An attempt was made to add more
NFS client groups than the system supports
```

**Resolution:**
`_apply_nfs_rule` now performs a get-or-create for the client group
(reusing an existing one if present), and both `_remove_nfs_rule` and
`_remove_all_nfs_permissions` delete the per-rule client group together
with the NFS permission. Access-level updates (e.g. RO → RW) are
handled idempotently by recreating only the permission, not the group.

---

## 9. No Quality of Service (QoS) Support

**Affects:** All shares.

**Description:**
Weka does not expose per-filesystem QoS controls such as IOPS limits or
bandwidth throttling. Resource management is handled at the capacity level
through filesystem quotas and directory quotas. The driver reports
`qos=False` in backend statistics.

**Impact:**
Manila share types with QoS extra-specs (e.g. `max_iops`, `max_bandwidth`)
cannot be enforced by this driver. All shares get equal access to the
cluster's full performance.

---

## 10. WEKAFS Mount Requires `weka user login` (CLI Step)

**Affects:** All WEKAFS protocol shares.

**Description:**
WEKAFS filesystems are created with `auth_required=True` inside the
project's Weka organization. A client cannot mount the share until it
has used `weka user login` to obtain a token file and passed that file
via the `auth_token_path` mount option.

The driver now delivers the mount credential self-service: when a WEKAFS
share is created, the Regular mount user's password is placed in the
share's export-location metadata as **`weka_mount_password`** (derived
from `HMAC-SHA256(weka_org_admin_secret, project_id + ":mount")`,
then formatted to meet Weka password complexity — not the raw digest).
No operator or out-of-band secret distribution is required.

**Impact:**
Mounting a WEKAFS share is a two-step process that is not pure Manila —
tenants must run a Weka CLI command (`weka user login`) in addition to
the standard OpenStack workflow. This is a Weka platform requirement,
not a Manila limitation.

**Workflow:**
```bash
# 1. Create share; retrieve the mount credential from export metadata
openstack share create WEKAFS 100 --name myshare --share-type weka
KEY=$(openstack share show myshare -f json \
        | jq -r '.export_locations[0].metadata.weka_mount_password')

# 2. Log in to the per-project Weka org
#    weka_org_name and weka_org_user also in export-location metadata
weka user login manila-mnt "$KEY" --org <weka_org_name> -H <api-host>
# Writes ~/.weka/auth-token.json

# 3. Mount
mount -t wekafs \
    -o auth_token_path=~/.weka/auth-token.json \
    <backend>/<fs_name> <mountpoint>
```

---

## 11. Empty Weka Orgs Accumulate Over Time

**Affects:** All WEKAFS deployments.

**Description:**
When a project's last WEKAFS share is deleted, the driver deletes the
Weka filesystem but retains the Weka organization and its admin user.
Orgs are never torn down automatically.

**Impact:**
Over time, projects that have had all their shares deleted leave behind
empty Weka organizations. These do not usually affect cluster operation
or capacity, but they accumulate in the Weka management UI and API
response payloads. At sufficient scale they exhaust the cluster's tenant
table: once it is full, every new WEKAFS share create fails with
`Could not create tenant: Tenant table is full` (a `WekaOrgError`), which
tips a whole batch of share creates into cascading `ERROR` status. This
is most visible in CI, which churns through many ephemeral projects.

**Cleanup:**
Identify empty orgs (no filesystems) in the Weka management console
under **Organizations**. It is safe to delete any org matching the
`weka_org_prefix` pattern (default `manila-`) that has no filesystems
and does not correspond to an active Manila project.

Via the Weka CLI (`weka org` is a deprecated alias for `weka tenant`):

```bash
weka tenant                       # list all tenants (orgs)
weka tenant remove <name> --force # delete one by name or ID
```

**CI:** the third-party CI reaps leftover `manila-*` orgs automatically
before every tempest run (`ci/weka-org-cleanup.sh`, invoked as Phase 4.5
of `ci/ci-runner.sh`), so the tenant table stays clear between runs and
the "Tenant table is full" cascade does not recur. The reaper uses the
`weka` CLI's own cached session (no password login, so it neither trips
nor is tripped by the login-lockout in issue #13) and is non-fatal.

---

## 12. Rotating `weka_org_admin_secret` Re-keys All Credentials

**Affects:** All WEKAFS deployments.

**Description:**
Both per-org passwords are derived from `weka_org_admin_secret`:
- TenantAdmin: `HMAC-SHA256(weka_org_admin_secret, project_id)`
- Mount user:  `HMAC-SHA256(weka_org_admin_secret, project_id + ":mount")`

(Each digest is then formatted to meet Weka password complexity; the
actual password is not the raw hex output.)

If `weka_org_admin_secret` is rotated in `manila.conf`, the driver
derives new passwords that no longer match those stored in the Weka
cluster for existing orgs.

**Impact:**
- All subsequent driver operations on existing WEKAFS shares fail with
  an authentication error (TenantAdmin password mismatch).
- Tenants must re-fetch the new `weka_mount_password` from export-location
  metadata and re-run `weka user login` — their cached auth-token.json
  is invalid.

**Recovery:**
After rotating the secret, update both user passwords for each existing
Weka org to match the new derived values, or delete and recreate all
WEKAFS shares (destroys share data).  There is no automated migration
path.  Rotating the secret is effectively a re-keying event for all
tenants.

---

## 13. CI Cluster Hardening (Operator Actions)

**Context:** The third-party CI (ci/full-redeploy.sh) connects to a live Weka
cluster.  The following one-time operator steps reduce failures in that
environment.  None of them are required for production driver deployments.

**1. Dedicated CI user**

Create a Weka user specifically for CI rather than reusing the shared `admin`
account.  Isolating CI login churn protects the real admin account from
lockouts triggered by rapid redeployments.

```bash
# On the Weka cluster (as admin):
weka user add ci-manila --role RegularUser --org Root
# (grant ClusterAdmin or OrgAdmin as required by your test scope)
weka user update ci-manila --role ClusterAdmin
```

Set the credentials in `/opt/weka-ci/ci-env` on the CI VM:

```bash
WEKA_USERNAME=ci-manila
WEKA_PASSWORD=<ci-user-password>
```

**2. Raise the login-attempt lockout threshold**

Rapid DevStack redeploys cause multiple backend processes to authenticate
in quick succession.  Increasing (or temporarily disabling) the cluster's
per-user lockout threshold prevents the lockout cascade that stalls gateway
discovery.

```bash
# Show current policy
weka security login-lockout status
# Raise the failed-attempt threshold (example: 20 attempts, 1-minute window)
weka security login-lockout set --failed-login-attempts 20 \
    --lockout-time 60
```

> Verify the exact subcommand and flag names against your cluster's Weka
> version (`weka security --help`); the lockout-policy CLI has varied across
> 5.x releases.

**3. Pin the NFS gateway IP (optional fallback)**

If Weka CLI gateway discovery is unreliable in the CI environment (e.g. CLI
auth is slow), pin a known-good gateway IP in `/opt/weka-ci/ci-env`:

```bash
WEKA_NFS_SERVER=<nfs-gateway-ip>
```

full-redeploy.sh will use this value when live discovery returns nothing,
instead of leaving `weka_nfs_server` unconfigured.

---

## 14. `manage_existing` Is Not Supported for WEKAFS Shares

**Affects:** `manage_existing` for WEKAFS protocol shares. NFS shares can
still be managed.

**Description:**
WEKAFS per-tenant isolation is mandatory: every WEKAFS filesystem must
live inside its Manila project's own Weka organization with
`auth_required=True`. Weka has no API to move an existing filesystem
between organizations, so an arbitrary pre-existing filesystem (which
normally lives in the root org) cannot be safely adopted as an isolated
share — its org-scoped lifecycle operations would look it up in the
wrong org and orphan it.

**Impact:**
`manage_existing` raises `ManageInvalidShare` for WEKAFS shares, before
any filesystem lookup. NFS shares are unaffected (they are not
org-isolated) and can be managed as before.

**Workaround:**
Create WEKAFS shares through Manila from the start, so the driver
provisions them inside the correct project org with `auth_required=True`.
There is no supported way to adopt a pre-existing filesystem as an
isolated WEKAFS share.
