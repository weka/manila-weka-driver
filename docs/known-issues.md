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
onto the WekaFS (POSIX client) security model. For the WekaFS protocol,
access control is managed entirely within the Weka cluster via:

- **Filesystem-level authentication** (`auth_required` flag) — forces
  clients to present a valid Weka mount token before mounting.
- **Mount tokens** — scoped credentials issued per-user or per-client
  by the Weka cluster.
- **Weka user accounts** — the cluster enforces per-user filesystem
  permissions at the protocol level.

These mechanisms have no equivalent in the Manila access-rules model
(IP rules, user rules, etc.) and the driver does not currently implement
a translation layer between them.

**Impact:**
Any attempt to add or delete an access rule on a WEKAFS share will return
an `error` state for the rule with a warning logged. The share itself
remains available — only the rule operation is rejected. Example:

```
$ openstack share access create my-wekafs-share ip 10.0.0.5
# Rule will show status 'error'
```

**Workaround:**
Control access to WEKAFS shares at the network layer (VPC security groups,
firewall rules) and via Weka cluster user management. Do not rely on Manila
access rules for WEKAFS share security.

**Future work:**
A future implementation could map Manila IP rules to Weka NFS-style client
groups on the WekaFS mount path, or map user rules to Weka user account
permissions. This requires a deeper integration with Weka's authentication
API and is tracked as a future enhancement.

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

## 8. No Quality of Service (QoS) Support

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
