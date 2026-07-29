# Manila Operation → Weka API Endpoint Mapping

This table shows which Weka REST API v2 endpoints are called for each
Manila driver operation.

## Share Lifecycle

| Manila Operation | Weka API Endpoint | Method | Notes |
|-----------------|-------------------|--------|-------|
| `create_share` | `/fileSystems` | POST | Creates new filesystem |
| `create_share` | `/fileSystemGroups` | GET | Verifies group exists |
| `delete_share` | `/fileSystems` | GET | Name lookup |
| `delete_share` | `/nfsPermissions` | GET | Find permissions to remove |
| `delete_share` | `/nfsPermissions/{uid}` | DELETE | Remove each permission |
| `delete_share` | `/clientGroups` | GET | Find per-rule client groups |
| `delete_share` | `/clientGroups/{uid}` | DELETE | Remove per-rule client groups |
| `delete_share` | `/fileSystems/{uid}` | DELETE | Delete filesystem |
| `extend_share` | `/fileSystems/{uid}` | PUT | Update `totalCapacity` |
| `shrink_share` | `/fileSystems/{uid}` | GET | Check used capacity |
| `shrink_share` | `/fileSystems/{uid}` | PUT | Update `totalCapacity` |
| `ensure_share` | `/fileSystems` | GET | Verify filesystem exists |
| `manage_existing` | `/fileSystems` | GET | Find by name |
| `create_share_from_snapshot` | `/snapshots` | GET | Name lookup |
| `create_share_from_snapshot` | `/fileSystems/{uid}` | GET | Resolve source filesystem |
| `create_share_from_snapshot` | `/fileSystems` | POST | Create empty destination filesystem |
| `create_share_from_snapshot` | `/nfs/clientGroups` | POST | Temp client group for NFS copy |
| `create_share_from_snapshot` | `/nfs/clientGroups/{uid}/rules` | POST | Add Manila host IP rule |
| `create_share_from_snapshot` | `/nfs/permissions` | POST | Temp RO + RW NFS exports |
| `create_share_from_snapshot` | `/nfs/permissions` | GET | Cleanup: find temp permissions |
| `create_share_from_snapshot` | `/nfs/permissions/{uid}` | DELETE | Cleanup: remove temp permissions |
| `create_share_from_snapshot` | `/nfs/clientGroups/{uid}/rules/{uid}` | DELETE | Cleanup: remove IP rule |
| `create_share_from_snapshot` | `/nfs/clientGroups/{uid}` | DELETE | Cleanup: remove temp client group |

## Snapshot Operations

| Manila Operation | Weka API Endpoint | Method | Notes |
|-----------------|-------------------|--------|-------|
| `create_snapshot` | `/snapshots` | POST | `isWritable=false` |
| `delete_snapshot` | `/snapshots` | GET | Name lookup |
| `delete_snapshot` | `/snapshots/{uid}` | DELETE | |
| `revert_to_snapshot` | `/snapshots` | GET | Name lookup |
| `revert_to_snapshot` | `/snapshots/{fs_uid}/{uid}/restore` | POST | In-place restore (Weka 5.x) |

## Access Control

| Manila Operation | Weka API Endpoint | Method | Notes |
|-----------------|-------------------|--------|-------|
| `update_access` (NFS add) | `/clientGroups` | GET | Check for existing group (idempotent) |
| `update_access` (NFS add) | `/clientGroups` | POST | Create group if not present |
| `update_access` (NFS add) | `/clientGroups/{uid}` | GET | Fetch existing IP rules |
| `update_access` (NFS add) | `/clientGroups/{uid}/rules` | POST | Add IP rule if not present |
| `update_access` (NFS add) | `/nfsPermissions` | GET | Check for existing permission |
| `update_access` (NFS add) | `/nfsPermissions` | POST | Create or recreate RW/RO permission |
| `update_access` (NFS add) | `/nfsPermissions/{uid}` | DELETE | Recreate if access level changed |
| `update_access` (NFS del) | `/nfsPermissions` | GET | Find permissions by FS+rule ID |
| `update_access` (NFS del) | `/nfsPermissions/{uid}` | DELETE | Remove permission |
| `update_access` (NFS del) | `/clientGroups` | GET | Find client group by name |
| `update_access` (NFS del) | `/clientGroups/{uid}` | DELETE | Remove per-rule client group |
| `update_access` (WEKAFS) | `/users` | POST | Ensure Regular mount user exists (idempotent); its password is in export-location metadata as `weka_mount_password` |
| `update_access` (WEKAFS ip add) | `/security/policies` | GET | Find the per-share rw/ro policy by name (`manila-<share8>-<rw\|ro>`) |
| `update_access` (WEKAFS ip add) | `/security/policies` | POST | Create the per-share `Allow` policy (first ip at that level; `read_only` for ro) |
| `update_access` (WEKAFS ip add) | `/security/policies/{uid}` | PATCH | `add_ip` the client IP to an existing policy |
| `update_access` (WEKAFS ip add) | `/fileSystems/{uid}/securityPolicy/attach` | POST | Attach the policy to the share's filesystem |
| `update_access` (WEKAFS ip del) | `/security/policies/{uid}` | PATCH | `remove_ip` the client IP from the policy |
| `update_access` (WEKAFS ip del) | `/fileSystems/{uid}/securityPolicy/detach` | POST | Detach when the policy becomes empty |
| `update_access` (WEKAFS ip del) | `/security/policies/{uid}` | DELETE | Delete the now-empty per-share policy |
| `update_access` (WEKAFS user/cert) | *(credential only)* | — | No IP policy; rule returned `active`. Mount credential is in export-location metadata (`weka_mount_password`). Enforcement is the org boundary (`auth_required=True`) plus any attached ip policies. See [known-issues.md §6](known-issues.md#6-wekafs-access-control-scope-and-limits) |
| `create_share` (WEKAFS, Model B) | `/security/policies` + `/fileSystems/{uid}/securityPolicy/attach` | GET/POST | For a share type with `weka:security_policy_group`, ensure and attach the shared group policies (`manila-grp-<group>-<rw\|ro>`) |

## Driver Setup

| Operation | Weka API Endpoint | Method | Notes |
|-----------|-------------------|--------|-------|
| `do_setup` | `/login` | POST | Obtain access/refresh tokens |
| `do_setup` | `/status` | GET | Verify connectivity + version |
| `do_setup` | `/fileSystemGroups` | GET | Check group exists |
| `do_setup` | `/fileSystemGroups` | POST | Create if missing |
| `check_for_setup_error` | `/status` | GET | Verify auth |
| `login refresh` | `/login/refresh` | POST | Refresh access token |
| WEKAFS share ops (isolation) | `/login` | POST | Log in as the per-org TenantAdmin for an org-scoped session |
| WEKAFS `create_share` (isolation) | `/organizations` | GET | Check if the project's org exists |
| WEKAFS `create_share` (isolation) | `/organizations` | POST | Create the org **and its TenantAdmin in one call** if absent |
| WEKAFS `update_access` (isolation) | `/users` | POST | Lazily ensure the Regular mount user (`<weka_org_user>-mnt`); its password is in export-location metadata as `weka_mount_password` |

## Statistics

| Manila Operation | Weka API Endpoint | Method | Notes |
|-----------------|-------------------|--------|-------|
| `_update_share_stats` | `/capacity` | GET | Cluster capacity |

## Authentication Token Flow

### Driver API session (root org or per-org admin)

```
Manila host                    Weka Cluster
    │                               │
    │── POST /login ───────────────►│
    │   {username, password, org}   │
    │◄── {access_token,             │
    │     refresh_token} ──────────►│
    │                               │
    │  [access_token valid 5 min]   │
    │                               │
    │── GET /fileSystems ──────────►│
    │   Authorization: Bearer <tok> │
    │◄── 200 {filesystems} ────────►│
    │                               │
    │  [token expires → 401]        │
    │                               │
    │── POST /login/refresh ───────►│
    │   {refresh_token}             │
    │◄── {new access_token} ───────►│
    │                               │
    │  [refresh_token valid 1 year] │
```

### WekaFS mount token (tenant, self-service via export metadata)

There is **no** `/fileSystems/{uid}/mountTokens` REST endpoint.  Mount
tokens are obtained by calling `POST /login` with the Regular mount
user's credentials (scoped to the project org), which returns a
`refresh_token` written to a JSON file by `weka user login`.  The
`auth_token_path` WekaFS mount option points to that file.

The mount user's password is delivered self-service: the driver places
it in the share's export-location metadata as `weka_mount_password`
when the share is created.  The short HMAC-derived password fits in
this metadata value; a raw Weka JWT would not.

```
Tenant client                  Weka Cluster
    │                               │
    │  (retrieves password from     │
    │   openstack share show        │
    │   → export metadata           │
    │     weka_mount_password)      │
    │                               │
    │── POST /login ───────────────►│
    │   {username: manila-mnt,      │
    │    password: <weka_mount_password>,│
    │    org: <weka_org_name>}      │
    │◄── {access_token,             │
    │     refresh_token} ──────────►│
    │                               │
    │  token written to             │
    │  ~/.weka/auth-token.json      │
    │                               │
    │── mount -t wekafs             │
    │   -o auth_token_path=<file>   │
    │   <backend>/<fs> <mnt> ──────►│ (kernel client reads token file)
```

## Complete Weka API v2 Endpoint Coverage

The `WekaApiClient` implements the following endpoints.  Those marked
with (D) are driver-critical; (S) are stubs included for SDK completeness.

### Filesystem
- GET/POST `/fileSystems` (D)
- GET/PUT/DELETE `/fileSystems/{uid}` (D)
- POST/DELETE `/fileSystems/{uid}/objectStoreBuckets` (S)
- GET/POST/PATCH/DELETE `/fileSystems/{uid}/quota/{inode_id}` (D)
- GET/POST/DELETE `/fileSystems/{uid}/defaultQuota` (S)

### Filesystem Groups
- GET/POST `/fileSystemGroups` (D)
- GET/PUT/DELETE `/fileSystemGroups/{uid}` (D)

### Organizations
- GET/POST `/organizations` (D) — used by WEKAFS isolation to look up
  or create per-project org
- GET/PUT/DELETE `/organizations/{uid}` (D)
- PUT `/organizations/{uid}/limits` (S)
- PUT `/organizations/{uid}/security` (S)

### NFS
- GET/POST `/interfaceGroups` (S)
- DELETE `/interfaceGroups/{uid}` (S)
- GET/POST `/nfsPermissions` (D)
- DELETE `/nfsPermissions/{uid}` (D)
- GET/POST `/clientGroups` (D)
- POST `/clientGroups/{uid}/rules` (D)
- GET `/clientGroups/{uid}` (D)
- DELETE `/clientGroups/{uid}` (D)

### Snapshots
- GET/POST `/snapshots` (D)
- GET/PUT/DELETE `/snapshots/{uid}` (D)
- POST `/snapshots/{fs_uid}/{uid}/restore` (D) — Weka 5.x

### Cluster
- GET `/status` (D)
- GET `/cluster` (S)
- GET `/capacity` (D)

### Users
- GET/POST `/users` (D) — the TenantAdmin is created as part of
  `POST /organizations` (create_organization), not via `/users`.
  `POST /users` is used only for the Regular mount user (role: Regular,
  username: `<weka_org_user>-mnt`), created lazily by `update_access`
  (WEKAFS) and idempotent if it already exists
- DELETE `/users/{uid}` (S)

### KMS / Security
- GET/POST `/kms` (S)
- GET `/ldap` (S)
- GET `/security` (S)
- GET `/security/tls` (S)

### Object Storage
- GET/POST `/objectStoreBuckets` (S)
- DELETE `/objectStoreBuckets/{uid}` (S)

### S3
- GET/POST `/s3/buckets` (S)
- DELETE `/s3/buckets/{name}` (S)
