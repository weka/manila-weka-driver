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
| `update_access` (WEKAFS) | *(no Weka API calls)* | — | All rules accepted as no-op (`active`); see [known-issues.md §6](known-issues.md#6-wekafs-shares-do-not-support-manila-access-rules) |

## Driver Setup

| Operation | Weka API Endpoint | Method | Notes |
|-----------|-------------------|--------|-------|
| `do_setup` | `/login` | POST | Obtain access/refresh tokens |
| `do_setup` | `/status` | GET | Verify connectivity + version |
| `do_setup` | `/fileSystemGroups` | GET | Check group exists |
| `do_setup` | `/fileSystemGroups` | POST | Create if missing |
| `check_for_setup_error` | `/status` | GET | Verify auth |
| `login refresh` | `/login/refresh` | POST | Refresh access token |

## Statistics

| Manila Operation | Weka API Endpoint | Method | Notes |
|-----------------|-------------------|--------|-------|
| `_update_share_stats` | `/capacity` | GET | Cluster capacity |

## Authentication Token Flow

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

## Complete Weka API v2 Endpoint Coverage

The `WekaApiClient` implements the following endpoints.  Those marked
with (D) are driver-critical; (S) are stubs included for SDK completeness.

### Filesystem
- GET/POST `/fileSystems` (D)
- GET/PUT/DELETE `/fileSystems/{uid}` (D)
- POST `/fileSystems/{uid}/mountTokens` (D)
- POST/DELETE `/fileSystems/{uid}/objectStoreBuckets` (S)
- GET/POST/PATCH/DELETE `/fileSystems/{uid}/quota/{inode_id}` (D)
- GET/POST/DELETE `/fileSystems/{uid}/defaultQuota` (S)

### Filesystem Groups
- GET/POST `/fileSystemGroups` (D)
- GET/PUT/DELETE `/fileSystemGroups/{uid}` (D)

### Organizations
- GET/POST `/organizations` (S)
- GET/PUT/DELETE `/organizations/{uid}` (S)
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
- GET/POST `/users` (S)
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
