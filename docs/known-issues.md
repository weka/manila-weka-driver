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
After creating temporary NFS permissions during snapshot copy, the driver
waits 5 seconds for the Weka NFS gateway to apply the new permissions before
attempting to mount. On heavily loaded clusters or under high NFS gateway
restart load, this delay may occasionally be insufficient, causing the
subsequent NFS mount to fail.

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

## 6. WEKAFS Shares Do Not Support Manila Access Rules

**Affects:** WEKAFS protocol shares only. NFS protocol is unaffected.

**Description:**
The Manila access-rules API (`share access create`) has no direct mapping
onto the WekaFS (POSIX client) security model.  Access control is managed
at the Weka cluster level via per-org filesystem authentication
(`auth_required=True`) and mount tokens scoped to the project's Weka
organization (see
[architecture.md §10](architecture.md#10-per-tenant-wekafs-isolation-via-weka-organizations)).
Every WEKAFS filesystem is created with `auth_required=True` inside the
project's own Weka organization; there is no mode in which a WEKAFS
filesystem can be mounted without a valid org-scoped token.

Manila IP rules, user rules, etc. have no per-client equivalent in this
model and the driver does not implement a translation layer for them.

**Impact:**
Any attempt to add or delete an access rule on a WEKAFS share will return
an `active` state (the rule is accepted) but has no effect on who can
mount the filesystem. Access is controlled entirely by whether the client
holds a valid Weka org token. Example:

```
$ openstack share access create my-wekafs-share ip 10.0.0.5
# Rule shows 'active' but does not grant mount access
```

The rule's `--access-level` (`ro` vs `rw`) is likewise ignored for WEKAFS.
The driver always provisions a single least-privilege `Regular` mount user
per org and returns the same `access_key` regardless of the level
requested, so a rule created with `--access-level ro` still grants the
tenant full `Regular`-role (read-write) access to that org's data. A
read-only WEKAFS share is therefore **not** achievable through the Manila
access-level flag; the value is accepted but has no effect. (For NFS, by
contrast, `--access-level ro`/`rw` maps directly to the export's RO/RW
permission and is enforced.)

**Workaround:**
Create a Manila access rule on the WEKAFS share.  The rule's
`access_key` field carries the mount user's password (self-service,
no operator step required).  See
[Configuration Guide](configuration.md#self-service-credential-delivery-via-access_key)
and [§10 below](#10-wekafs-mount-requires-weka-user-login-cli-step).
Control network-layer access via VPC security groups or firewall rules.

**Future work:**
A future implementation could map Manila IP rules to Weka NFS-style client
groups on the WekaFS mount path, or map user rules to Weka user account
permissions. This is tracked as a future enhancement.

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

The driver now delivers the mount credential self-service: when a
tenant creates a Manila access rule on a WEKAFS share, the rule's
**access_key** field contains the Regular mount user's password
(derived from `HMAC-SHA256(weka_org_admin_secret, project_id + ":mount")`,
then formatted to meet Weka password complexity — not the raw digest).
No operator or out-of-band secret distribution is required.

**Impact:**
Mounting a WEKAFS share is a two-step process that is not pure Manila —
tenants must run a Weka CLI command (`weka user login`) in addition to
the standard OpenStack workflow. This is a Weka platform requirement,
not a Manila limitation.

**Workflow:**
```bash
# 1. Create share and access rule; retrieve the mount credential
openstack share create WEKAFS 100 --name myshare --share-type weka
openstack share access create myshare user weka
KEY=$(openstack share access list myshare \
        -f value -c "Access Key" | head -1)

# 2. Log in to the per-project Weka org
#    weka_org_name and weka_org_user visible in export-location metadata
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
empty Weka organizations. These do not affect cluster operation or
capacity, but they accumulate in the Weka management UI and API response
payloads.

**Cleanup:**
Identify empty orgs (no filesystems) in the Weka management console
under **Organizations**. It is safe to delete any org matching the
`weka_org_prefix` pattern (default `manila-`) that has no filesystems
and does not correspond to an active Manila project.

Via the Weka CLI:

```bash
weka org list
weka org delete <org-name>
```

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
- Tenants whose existing access rules carried the old mount credential
  (via `access_key`) must re-fetch the new `access_key` and re-run
  `weka user login` — their cached auth-token.json is invalid.

**Recovery:**
After rotating the secret, update both user passwords for each existing
Weka org to match the new derived values, or delete and recreate all
WEKAFS shares (destroys share data).  There is no automated migration
path.  Rotating the secret is effectively a re-keying event for all
tenants.

---

## 13. `manage_existing` Is Not Supported for WEKAFS Shares

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
