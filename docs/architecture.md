# Architecture

## Component Diagram

```
Manila Service (share-manager process)
│
├── WekaShareDriver  [driver.py]
│   │
│   ├── do_setup()         — Instantiates client, verifies connectivity,
│   │                        ensures filesystem group exists
│   │
│   ├── create_share()     — Creates Weka filesystem, returns export locs
│   ├── delete_share()     — Deletes filesystem (idempotent)
│   ├── extend_share()     — Updates filesystem total_capacity
│   ├── shrink_share()     — Capacity-safe shrink
│   ├── ensure_share()     — Verifies FS exists, re-mounts if needed
│   ├── update_access()    — NFS permissions or WekaFS auth rules
│   ├── create_snapshot()  — Weka snapshot
│   ├── delete_snapshot()  — Idempotent snapshot delete
│   ├── revert_to_snapshot() — In-place restore
│   └── _update_share_stats() — Pulls cluster capacity
│
├── WekaApiClient  [client.py]
│   │
│   ├── Authentication
│   │   ├── POST /login          — Obtain access + refresh tokens
│   │   └── POST /login/refresh  — Refresh access token
│   │
│   ├── Filesystem operations
│   │   ├── GET/POST/PUT/DELETE /fileSystems
│   │   └── POST/DELETE /fileSystems/{uid}/objectStoreBuckets
│   │
│   ├── Filesystem group operations
│   │   └── GET/POST/PUT/DELETE /fileSystemGroups
│   │
│   ├── Quota operations
│   │   ├── GET/POST/PATCH/DELETE /fileSystems/{uid}/quota/{inode}
│   │   └── GET/POST/DELETE /fileSystems/{uid}/defaultQuota
│   │
│   ├── Organization operations
│   │   └── GET/POST/PUT/DELETE /organizations + /limits + /security
│   │
│   ├── NFS operations
│   │   ├── /interfaceGroups
│   │   ├── /nfsPermissions
│   │   └── /clientGroups + /rules
│   │
│   ├── Snapshot operations
│   │   └── GET/POST/PUT/DELETE /snapshots + /restore
│   │
│   └── Supporting operations
│       ├── /capacity, /cluster, /status
│       ├── /users, /kms, /ldap
│       ├── /s3/buckets, /objectStoreBuckets
│       └── /security, /security/tls
│
└── WekaMount  [posix.py]
    │
    ├── mount()          — mount -t wekafs with options
    ├── unmount()        — umount (lazy if force=True)
    ├── is_mounted()     — /proc/mounts parse
    ├── get_or_create_share_path() — mkdir + chmod
    ├── remove_share_path()        — rmdir / shutil.rmtree
    └── get_directory_inode()      — os.stat().st_ino
```

## Design Decisions

### 1. Serverless (driver_handles_share_servers = False)

Weka manages its own networking.  The driver does not create Nova VMs
or Neutron ports.  `get_network_allocations_number()` returns 0.

### 2. Filesystem-per-share

Each Manila share maps to exactly one Weka filesystem.  This provides:
- Strong capacity isolation (filesystem-level quotas)
- Independent encryption, auth, and tiering settings per share
- Clean delete semantics (filesystem delete removes all data)

The filesystem name is `<weka_share_name_prefix><share-uuid>`, defaulting
to `manila_<uuid>`.

### 3. UID caching in export metadata

The Weka filesystem UID is stored in the share's export location metadata:

```json
{
  "path": "10.0.0.1/manila_abc123",
  "metadata": {
    "weka_fs_uid": "fs-uid-xxx",
    "weka_fs_name": "manila_abc123"
  }
}
```

This avoids iterating all filesystems on every operation.  If the UID
is missing (e.g. managed share), the driver falls back to a name lookup.

### 4. Thread safety

- `WekaApiClient._token_lock` (threading.Lock) guards token refresh, so
  concurrent Manila RPC calls cannot trigger multiple simultaneous logins.
- `WekaMount` uses per-mount-point locks (keyed by path) to prevent
  concurrent mount/unmount on the same directory.

### 5. Idempotency

Every create operation:
- Checks for existing resource before creating
- Handles `WekaConflict` (HTTP 409) by returning the existing resource

Every delete operation:
- Catches `WekaNotFound` (HTTP 404) and returns silently

This is required because Manila may call these methods multiple times
(e.g. after a manila-share process restart).

### 6. Unit conversion boundary

All Weka API calls work in **bytes**.  Unit conversion (GiB ↔ bytes)
happens exclusively in `driver.py` using `weka_utils.gb_to_bytes()` and
`weka_utils.bytes_to_gb()`.  `client.py` never performs unit conversion.

### 7. Retry strategy

`WekaApiClient._request()` retries on HTTP 429 (rate limited) and 5xx
(server errors) with exponential back-off:
- Initial delay: 1 second
- Multiplier: 2× per attempt
- Maximum retries: configurable via `weka_max_api_retries` (default 3)

4xx errors (except 429) are not retried — they indicate client errors.

### 8. POSIX vs NFS protocol selection

WEKAFS is the preferred protocol for its performance advantages (sub-250 µs
latency, full POSIX semantics, native quota enforcement).  NFS is supported
for hosts where the WekaFS kernel module cannot be installed, including
hosts running Linux kernel 6.17+ where the module does not compile.  See
[Known Issues](known-issues.md#1-wekafs-kernel-module-incompatible-with-linux-kernel-617).

When `share_proto == 'NFS'`:
- A Weka NFS client group is created per access rule
- NFS permissions are created/deleted via the Weka API
- Export path: `<weka_api_server>:/<fs_name>`

When `share_proto == 'WEKAFS'`:
- The filesystem is mounted locally on the Manila host via `mount -t wekafs`
- Export path: `<weka_api_server>/<fs_name>`
- The filesystem is always created inside the project's own Weka org
  with `auth_required=True`; only a client holding a token scoped to
  that org can mount it.  See §10 below.
- Manila `ip` access rules are enforced per share via Weka security
  policies (deny-by-default once attached; `--access-level` honored), on
  top of the org-auth boundary; `user`/`cert` rules grant the mount
  credential only. See
  [Known Issues §6](known-issues.md#6-wekafs-access-control-scope-and-limits).

### 9. `create_share_from_snapshot` data copy

The Weka v2 API does not expose a direct filesystem-clone-from-snapshot
operation for read-only snapshots.  The driver implements this by:

1. Creating an empty destination filesystem via the Weka API.
2. Temporarily mounting source and destination filesystems via NFS.
3. Using `rsync -a` to copy the snapshot directory contents across.
4. Unmounting and cleaning up temporary NFS client groups and permissions.

This approach works for both NFS and WEKAFS protocol shares but copy time
scales with snapshot data size.  See
[Known Issues](known-issues.md#2-create_share_from_snapshot-uses-nfs-based-data-copy).

### 10. Per-tenant WEKAFS isolation via Weka organizations

Every Manila project gets its own Weka organization (tenant).  This is
mandatory for all WEKAFS shares and requires a Weka cluster with
multi-tenant organization support (5.x REST v2):

```
Manila project_id ──► Weka org  <weka_org_prefix><project_id>
                            │
                            └── WEKAFS filesystem (auth_required=True)
                                  Only mountable with a token scoped
                                  to this org — enforced by the cluster.
```

**Two principals per org:**
`POST /organizations {name, username, password}` creates the Weka org
and its **TenantAdmin** user in one call.  The driver then calls
`POST /users` to create a least-privilege **Regular** mount user
(username = `weka_org_user + "-mnt"`, e.g. `manila-mnt`).

Passwords are derived deterministically — no per-tenant secret stored:
- TenantAdmin: `HMAC-SHA256(weka_org_admin_secret, project_id)`
- Mount user:  `HMAC-SHA256(weka_org_admin_secret, project_id + ":mount")`

Each digest is then formatted to satisfy Weka's password-complexity
rules, so the actual password is not the raw hex output.

**Org-scoped sessions:**
Weka binds filesystem operations to the authenticated session's org,
not to a request field.  For every create/delete/extend/shrink on a
WEKAFS share, the driver logs in as the per-org admin (`POST /login`
with `org=<org_name>`) and performs the operation in that session.
This is fundamentally different from NFS, which uses IP-based
enforcement through client groups and NFS permissions on the root
session.

**Access control comparison:**

| Protocol | Isolation mechanism | Manila access rules |
|----------|--------------------|--------------------|
| WEKAFS   | Weka org + `auth_required=True` on FS | Accepted; returns mount credential via `access_key` |
| NFS      | IP-based client groups + NFS perms | Enforced |

**Org lifecycle:**
The org and its admin user are retained when the project's last share
is deleted.  They are never torn down automatically.

**Self-service credential delivery:**
When a tenant creates a Manila access rule on a WEKAFS share,
`_update_wekafs_access` ensures the Regular mount user exists and
returns its password as the rule's **access_key** (Manila's
`access_key` column is String(255); the short HMAC-derived password
fits — a raw Weka JWT would not).  Tenants retrieve it with:

```
openstack share access list <share> -c "Access Key"
openstack share access show <id> -c access_key
```

The access rule has no enforcement effect; its only function is to
carry the credential.  Export-location metadata exposes
`weka_org_name` and `weka_org_user` (the mount user, e.g.
`manila-mnt`) for reference.

The driver writes its own internal mount tokens (mode 0600) to
`weka_auth_token_dir` (default `/var/lib/manila/weka-tokens`) for
operations such as snapshot data copy.
