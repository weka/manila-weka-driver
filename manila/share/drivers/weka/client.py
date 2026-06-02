# Copyright 2024 Weka.IO Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Weka REST API client for the Manila share driver.

Implements a complete client for the Weka v2 REST API, covering:
  - Filesystem lifecycle (CRUD + capacity management)
  - Filesystem groups
  - Directory quotas and default quotas
  - Organizations and org-level limits
  - NFS interface groups, client groups, and permissions
  - Snapshots
  - Cluster status / capacity
  - KMS, LDAP, user management stubs

All unit conversions (GiB <-> bytes) happen in driver.py.
This client works exclusively in bytes / raw API types.
"""

import threading
import time

from oslo_log import log as logging
import requests
from requests import adapters as req_adapters

from manila.share.drivers.weka import exceptions
from manila.share.drivers.weka import utils

LOG = logging.getLogger(__name__)

_API_V2 = '/api/v2'
_DEFAULT_TIMEOUT = 30
_DEFAULT_RETRIES = 3


class WekaApiClient(object):
    """Client for the Weka REST API (v2).

    Thread-safe: token refresh is protected by a lock so that concurrent
    driver method calls do not attempt multiple simultaneous re-logins.

    Usage::

        client = WekaApiClient(
            host='weka-cluster.example.com',
            username='admin',
            password='secret',
        )
        filesystems = client.list_filesystems()
    """

    def __init__(self, host, username, password,
                 organization='Root',
                 port=14000,
                 ssl_verify=True,
                 timeout=_DEFAULT_TIMEOUT,
                 max_retries=_DEFAULT_RETRIES,
                 pool_connections=4,
                 pool_maxsize=10):
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._organization = organization
        self._ssl_verify = ssl_verify
        self._timeout = timeout
        self._max_retries = max_retries

        self._base_url = 'https://{host}:{port}{api}'.format(
            host=host, port=port, api=_API_V2)

        self._access_token = None
        self._refresh_token = None
        self._token_lock = threading.Lock()

        self._session = requests.Session()
        adapter = req_adapters.HTTPAdapter(
            max_retries=0,  # handled manually
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
        )
        self._session.mount('https://', adapter)
        self._session.mount('http://', adapter)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _url(self, path):
        """Return a full URL for the given API path."""
        return self._base_url + path

    def _headers(self):
        """Return HTTP headers including the current Bearer token."""
        headers = {'Content-Type': 'application/json'}
        if self._access_token:
            headers['Authorization'] = 'Bearer ' + self._access_token
        return headers

    def _raise_for_status(self, response, context=''):
        """Translate HTTP error responses into WekaApiError subclasses."""
        code = response.status_code
        if code < 400:
            return
        try:
            body = response.json()
            msg = body.get('message') or body.get('error') or str(body)
        except Exception:
            msg = response.text or 'no body'

        if context:
            msg = '{}: {}'.format(context, msg)

        if code == 401:
            raise exceptions.WekaAuthError(reason=msg)
        elif code == 404:
            raise exceptions.WekaNotFound(reason=msg)
        elif code == 409:
            raise exceptions.WekaConflict(reason=msg)
        elif code == 429:
            raise exceptions.WekaRateLimited(reason=msg)
        else:
            raise exceptions.WekaApiError(status_code=code, reason=msg)

    def _request(self, method, path, params=None, json=None,
                 _retry_auth=True):
        """Execute an authenticated HTTP request with retry logic.

        Handles:
          - Automatic token injection
          - 401 → token refresh + single retry
          - 429 / 5xx → exponential back-off up to max_retries
        """
        url = self._url(path)
        safe_params = utils.sanitize_log_params(params or {})
        safe_json = utils.sanitize_log_params(json or {})
        LOG.debug(
            "Weka API %s %s params=%s body=%s",
            method.upper(), path, safe_params, safe_json,
        )

        delay = 1.0
        last_exc = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json,
                    verify=self._ssl_verify,
                    timeout=self._timeout,
                )
                if resp.status_code == 401 and _retry_auth:
                    LOG.debug("Weka API 401 — refreshing token and retrying")
                    self._refresh_or_login()
                    resp = self._session.request(
                        method, url,
                        headers=self._headers(),
                        params=params,
                        json=json,
                        verify=self._ssl_verify,
                        timeout=self._timeout,
                    )
                self._raise_for_status(resp, context=path)
                return resp
            except exceptions.WekaRateLimited as exc:
                last_exc = exc
            except exceptions.WekaApiError as exc:
                if exc.status_code and exc.status_code < 500:
                    raise
                last_exc = exc

            if attempt < self._max_retries:
                LOG.warning(
                    "Transient Weka API error on attempt %d/%d, "
                    "retrying in %.1fs",
                    attempt + 1, self._max_retries, delay,
                )
                time.sleep(delay)
                delay *= 2.0

        raise last_exc

    def _get(self, path, params=None):
        return self._request('GET', path, params=params).json()

    def _post(self, path, json=None):
        return self._request('POST', path, json=json).json()

    def _put(self, path, json=None):
        return self._request('PUT', path, json=json).json()

    def _patch(self, path, json=None):
        return self._request('PATCH', path, json=json).json()

    def _delete(self, path, params=None):
        resp = self._request('DELETE', path, params=params)
        if resp.content:
            try:
                return resp.json()
            except Exception:
                pass
        return {}

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self):
        """Obtain a new access/refresh token pair from the Weka cluster.

        Stores the tokens internally.  Thread-safe via _token_lock.
        """
        with self._token_lock:
            self._do_login()

    def _do_login(self):
        """Inner login — must be called with _token_lock held."""
        LOG.debug("Logging in to Weka cluster at %s as user '%s' org '%s'",
                  self._host, self._username, self._organization)
        payload = {
            'username': self._username,
            'password': self._password,
            'org': self._organization,
        }
        resp = self._session.post(
            self._url('/login'),
            json=payload,
            verify=self._ssl_verify,
            timeout=self._timeout,
        )
        self._raise_for_status(resp, context='/login')
        data = resp.json().get('data', resp.json())
        self._access_token = data['access_token']
        self._refresh_token = data.get('refresh_token')
        LOG.debug("Weka login successful (token acquired)")

    def _refresh_or_login(self):
        """Refresh the access token, falling back to full login."""
        with self._token_lock:
            if self._refresh_token:
                try:
                    resp = self._session.post(
                        self._url('/login/refresh'),
                        json={'refresh_token': self._refresh_token},
                        verify=self._ssl_verify,
                        timeout=self._timeout,
                    )
                    if resp.status_code == 200:
                        data = resp.json().get('data', resp.json())
                        self._access_token = data['access_token']
                        if 'refresh_token' in data:
                            self._refresh_token = data['refresh_token']
                        LOG.debug("Weka access token refreshed")
                        return
                except Exception:
                    pass
            LOG.debug("Token refresh failed — performing full login")
            self._do_login()

    def get_cluster_status(self):
        """Return cluster status dict (name, version, health).

        Weka 5.x: GET /cluster  (replaces the old GET /status endpoint)
        """
        return self._get('/cluster')

    # ------------------------------------------------------------------
    # Filesystem methods
    # ------------------------------------------------------------------

    def list_filesystems(self):
        """Return a list of all filesystems on the cluster.

        GET /fileSystems
        """
        result = self._get('/fileSystems')
        return result.get('data', result)

    def get_filesystem(self, fs_uid):
        """Return metadata for a single filesystem.

        GET /fileSystems/{uid}
        """
        result = self._get('/fileSystems/{uid}'.format(uid=fs_uid))
        return result.get('data', result)

    def get_filesystem_by_name(self, name):
        """Find a filesystem by name.

        Iterates list_filesystems(); returns None if not found.
        """
        for fs in self.list_filesystems():
            if fs.get('name') == name:
                return fs
        return None

    def create_filesystem(self, name, group_name, total_capacity,
                          ssd_capacity=None,
                          obs_buckets=None,
                          encrypted=False,
                          auth_required=False,
                          allow_no_space=False,
                          data_reduction=None):
        """Create a new Weka filesystem.

        POST /fileSystems

        :param name: Filesystem name (must be unique in the cluster).
        :param group_name: Name of the filesystem group to assign to.
        :param total_capacity: Total capacity in bytes.
        :param ssd_capacity: SSD-tier capacity in bytes (optional).
        :param obs_buckets: List of object store bucket UIDs (optional).
        :param encrypted: Whether to enable filesystem encryption.
        :param auth_required: Whether to require mount authentication.
        :param allow_no_space: Whether to allow writes when no space left.
        :param data_reduction: Data reduction setting (None = cluster default).
        :returns: Created filesystem dict.
        """
        payload = {
            'name': name,
            'group_name': group_name,
            'total_capacity': total_capacity,
            'encrypted': encrypted,
            'auth_required': auth_required,
        }
        if ssd_capacity is not None:
            payload['ssd_capacity'] = ssd_capacity
        if obs_buckets:
            payload['obs_buckets'] = obs_buckets
        if data_reduction is not None:
            payload['data_reduction'] = data_reduction
        result = self._post('/fileSystems', json=payload)
        return result.get('data', result)

    def update_filesystem(self, fs_uid, name=None, total_capacity=None,
                          ssd_capacity=None, auth_required=None,
                          allow_no_space=None, data_reduction=None):
        """Update an existing filesystem's settings.

        PUT /fileSystems/{uid}
        """
        payload = {}
        if name is not None:
            payload['name'] = name
        if total_capacity is not None:
            payload['total_capacity'] = total_capacity
        if ssd_capacity is not None:
            payload['ssd_capacity'] = ssd_capacity
        if auth_required is not None:
            payload['auth_required'] = auth_required
        if data_reduction is not None:
            payload['data_reduction'] = data_reduction
        result = self._put(
            '/fileSystems/{uid}'.format(uid=fs_uid), json=payload)
        return result.get('data', result)

    def delete_filesystem(self, fs_uid, purge_from_obs=False):
        """Delete a filesystem.

        DELETE /fileSystems/{uid}
        """
        params = {}
        if purge_from_obs:
            params['purge_from_obs'] = True
        return self._delete(
            '/fileSystems/{uid}'.format(uid=fs_uid), params=params or None)

    def get_filesystem_mount_token(self, fs_uid):
        """Obtain a short-lived mount token for a filesystem.

        POST /fileSystems/{uid}/mountTokens

        Used when auth_required=True to allow stateless POSIX mounting.
        """
        result = self._post(
            '/fileSystems/{uid}/mountTokens'.format(uid=fs_uid))
        return result.get('data', result)

    # ------------------------------------------------------------------
    # Object store bucket attachment
    # ------------------------------------------------------------------

    def attach_obs_bucket(self, fs_uid, obs_bucket_uid,
                          mode='writable',
                          remove_detached=False,
                          tiering_ssd_percent=None):
        """Attach an object store bucket to a filesystem for tiering.

        POST /fileSystems/{uid}/objectStoreBuckets
        """
        payload = {
            'obs_bucket_id': obs_bucket_uid,
            'mode': mode,
            'remove_detached': remove_detached,
        }
        if tiering_ssd_percent is not None:
            payload['tiering_ssd_percent'] = tiering_ssd_percent
        result = self._post(
            '/fileSystems/{uid}/objectStoreBuckets'.format(uid=fs_uid),
            json=payload,
        )
        return result.get('data', result)

    def detach_obs_bucket(self, fs_uid, obs_bucket_id, purge=False):
        """Detach an object store bucket from a filesystem.

        DELETE /fileSystems/{uid}/objectStoreBuckets/{id}
        """
        params = {'purge': purge} if purge else None
        return self._delete(
            '/fileSystems/{uid}/objectStoreBuckets/{bid}'.format(
                uid=fs_uid, bid=obs_bucket_id),
            params=params,
        )

    # ------------------------------------------------------------------
    # Filesystem group methods
    # ------------------------------------------------------------------

    def list_filesystem_groups(self):
        """Return all filesystem groups.

        GET /fileSystemGroups
        """
        result = self._get('/fileSystemGroups')
        return result.get('data', result)

    def get_filesystem_group(self, group_uid):
        """Return a single filesystem group by UID.

        GET /fileSystemGroups/{uid}
        """
        result = self._get('/fileSystemGroups/{uid}'.format(uid=group_uid))
        return result.get('data', result)

    def get_filesystem_group_by_name(self, name):
        """Find a filesystem group by name; returns None if not found."""
        for grp in self.list_filesystem_groups():
            if grp.get('name') == name:
                return grp
        return None

    def create_filesystem_group(self, name, target_ssd_retention=None,
                                start_demote=None):
        """Create a new filesystem group.

        POST /fileSystemGroups
        """
        payload = {'name': name}
        if target_ssd_retention is not None:
            payload['target_ssd_retention'] = target_ssd_retention
        if start_demote is not None:
            payload['start_demote'] = start_demote
        result = self._post('/fileSystemGroups', json=payload)
        return result.get('data', result)

    def update_filesystem_group(self, group_uid, name=None,
                                target_ssd_retention=None,
                                start_demote=None):
        """Update a filesystem group.

        PUT /fileSystemGroups/{uid}
        """
        payload = {}
        if name is not None:
            payload['name'] = name
        if target_ssd_retention is not None:
            payload['target_ssd_retention'] = target_ssd_retention
        if start_demote is not None:
            payload['start_demote'] = start_demote
        result = self._put(
            '/fileSystemGroups/{uid}'.format(uid=group_uid), json=payload)
        return result.get('data', result)

    def delete_filesystem_group(self, group_uid):
        """Delete a filesystem group.

        DELETE /fileSystemGroups/{uid}
        """
        return self._delete('/fileSystemGroups/{uid}'.format(uid=group_uid))

    # ------------------------------------------------------------------
    # Directory quota methods
    # ------------------------------------------------------------------

    def list_directory_quotas(self, fs_uid, path=None):
        """List directory quotas for a filesystem.

        GET /fileSystems/{uid}/quota  (filtered by path if given)
        """
        params = {}
        if path:
            params['path'] = path
        result = self._get(
            '/fileSystems/{uid}/quota'.format(uid=fs_uid),
            params=params or None,
        )
        return result.get('data', result)

    def set_directory_quota(self, fs_uid, inode_id, hard_limit_bytes=None,
                            soft_limit_bytes=None, grace_seconds=None):
        """Set a directory quota identified by inode_id.

        POST /fileSystems/{uid}/quota/{inode_id}

        Note: Weka identifies quota targets by inode_id (an integer
        representing the directory inode number).  The driver resolves
        the inode_id via a POSIX stat on the mounted share path before
        calling this method.
        """
        payload = {}
        if hard_limit_bytes is not None:
            payload['hard_limit_bytes'] = hard_limit_bytes
        if soft_limit_bytes is not None:
            payload['soft_limit_bytes'] = soft_limit_bytes
        if grace_seconds is not None:
            payload['grace_seconds'] = grace_seconds
        result = self._post(
            '/fileSystems/{uid}/quota/{inode}'.format(
                uid=fs_uid, inode=inode_id),
            json=payload,
        )
        return result.get('data', result)

    def update_directory_quota(self, fs_uid, inode_id,
                               hard_limit_bytes=None,
                               soft_limit_bytes=None,
                               grace_seconds=None):
        """Update an existing directory quota.

        PATCH /fileSystems/{uid}/quota/{inode_id}
        """
        payload = {}
        if hard_limit_bytes is not None:
            payload['hard_limit_bytes'] = hard_limit_bytes
        if soft_limit_bytes is not None:
            payload['soft_limit_bytes'] = soft_limit_bytes
        if grace_seconds is not None:
            payload['grace_seconds'] = grace_seconds
        result = self._patch(
            '/fileSystems/{uid}/quota/{inode}'.format(
                uid=fs_uid, inode=inode_id),
            json=payload,
        )
        return result.get('data', result)

    def delete_directory_quota(self, fs_uid, inode_id):
        """Remove a directory quota.

        DELETE /fileSystems/{uid}/quota/{inode_id}
        """
        return self._delete(
            '/fileSystems/{uid}/quota/{inode}'.format(
                uid=fs_uid, inode=inode_id))

    def get_default_quota(self, fs_uid):
        """Get the default directory quota for a filesystem.

        GET /fileSystems/{uid}/defaultQuota
        """
        result = self._get(
            '/fileSystems/{uid}/defaultQuota'.format(uid=fs_uid))
        return result.get('data', result)

    def set_default_quota(self, fs_uid, hard_limit_bytes=None,
                          soft_limit_bytes=None, grace_seconds=None):
        """Set or update the default directory quota for a filesystem.

        POST /fileSystems/{uid}/defaultQuota
        """
        payload = {}
        if hard_limit_bytes is not None:
            payload['hard_limit_bytes'] = hard_limit_bytes
        if soft_limit_bytes is not None:
            payload['soft_limit_bytes'] = soft_limit_bytes
        if grace_seconds is not None:
            payload['grace_seconds'] = grace_seconds
        result = self._post(
            '/fileSystems/{uid}/defaultQuota'.format(uid=fs_uid),
            json=payload,
        )
        return result.get('data', result)

    def delete_default_quota(self, fs_uid):
        """Remove the default directory quota for a filesystem.

        DELETE /fileSystems/{uid}/defaultQuota
        """
        return self._delete(
            '/fileSystems/{uid}/defaultQuota'.format(uid=fs_uid))

    # ------------------------------------------------------------------
    # Organization methods
    # ------------------------------------------------------------------

    def list_organizations(self):
        """Return all organizations.

        GET /organizations
        """
        result = self._get('/organizations')
        return result.get('data', result)

    def get_organization(self, org_uid):
        """Return a single organization by UID.

        GET /organizations/{uid}
        """
        result = self._get('/organizations/{uid}'.format(uid=org_uid))
        return result.get('data', result)

    def get_organization_by_name(self, name):
        """Find an organization by name; returns None if not found."""
        for org in self.list_organizations():
            if org.get('name') == name:
                return org
        return None

    def create_organization(self, name, ssd_quota=None, total_quota=None):
        """Create a new organization.

        POST /organizations
        """
        payload = {'name': name}
        if ssd_quota is not None:
            payload['ssd_quota'] = ssd_quota
        if total_quota is not None:
            payload['total_quota'] = total_quota
        result = self._post('/organizations', json=payload)
        return result.get('data', result)

    def update_organization(self, org_uid, name=None, ssd_quota=None,
                            total_quota=None):
        """Update an organization.

        PUT /organizations/{uid}
        """
        payload = {}
        if name is not None:
            payload['name'] = name
        if ssd_quota is not None:
            payload['ssd_quota'] = ssd_quota
        if total_quota is not None:
            payload['total_quota'] = total_quota
        result = self._put(
            '/organizations/{uid}'.format(uid=org_uid), json=payload)
        return result.get('data', result)

    def delete_organization(self, org_uid):
        """Delete an organization.

        DELETE /organizations/{uid}
        """
        return self._delete('/organizations/{uid}'.format(uid=org_uid))

    def set_organization_limits(self, org_uid, total_capacity=None,
                                ssd_capacity=None,
                                max_download_mbps=None,
                                max_upload_mbps=None):
        """Set resource limits on an organization.

        PUT /organizations/{uid}/limits
        """
        payload = {}
        if total_capacity is not None:
            payload['total_capacity'] = total_capacity
        if ssd_capacity is not None:
            payload['ssd_capacity'] = ssd_capacity
        if max_download_mbps is not None:
            payload['max_download_mbps'] = max_download_mbps
        if max_upload_mbps is not None:
            payload['max_upload_mbps'] = max_upload_mbps
        result = self._put(
            '/organizations/{uid}/limits'.format(uid=org_uid), json=payload)
        return result.get('data', result)

    def set_organization_security(self, org_uid, mode=None):
        """Configure security settings for an organization.

        PUT /organizations/{uid}/security
        """
        payload = {}
        if mode is not None:
            payload['mode'] = mode
        result = self._put(
            '/organizations/{uid}/security'.format(uid=org_uid), json=payload)
        return result.get('data', result)

    # ------------------------------------------------------------------
    # NFS methods
    # ------------------------------------------------------------------

    def list_interface_groups(self):
        """Return all NFS interface groups.

        GET /interfaceGroups
        """
        result = self._get('/interfaceGroups')
        return result.get('data', result)

    def create_interface_group(self, name, subnet, gateway=None,
                               allow_manage_gids=False):
        """Create an NFS interface group.

        POST /interfaceGroups
        """
        payload = {
            'name': name,
            'subnet': subnet,
            'allow_manage_gids': allow_manage_gids,
        }
        if gateway:
            payload['gateway'] = gateway
        result = self._post('/interfaceGroups', json=payload)
        return result.get('data', result)

    def delete_interface_group(self, group_uid):
        """Delete an NFS interface group.

        DELETE /interfaceGroups/{uid}
        """
        return self._delete('/interfaceGroups/{uid}'.format(uid=group_uid))

    def list_nfs_permissions(self):
        """Return all NFS export permissions.

        GET /nfs/permissions
        """
        result = self._get('/nfs/permissions')
        return result.get('data', result)

    def create_nfs_permission(self, client_group, fs_uid, path,
                              access_type='RW',
                              squash=None, anon_uid=None, anon_gid=None):
        """Create an NFS export permission.

        POST /nfs/permissions

        Weka v5.x uses 'filesystem' (name) and 'group' (name) rather
        than UID-based fields; the access type field is 'permission_type'.
        The caller should pass the filesystem name as fs_uid and the
        client group name as client_group.
        """
        payload = {
            'group': client_group,
            'filesystem': fs_uid,
            'path': path,
            'permission_type': access_type,
            'supported_versions': ['V3', 'V4'],
        }
        if squash is not None:
            payload['root_squashing'] = squash
        if anon_uid is not None:
            payload['anon_uid'] = anon_uid
        if anon_gid is not None:
            payload['anon_gid'] = anon_gid
        result = self._post('/nfs/permissions', json=payload)
        return result.get('data', result)

    def delete_nfs_permission(self, permission_uid):
        """Delete an NFS export permission.

        DELETE /nfs/permissions/{uid}
        """
        return self._delete('/nfs/permissions/{uid}'.format(uid=permission_uid))

    def list_client_groups(self):
        """Return all NFS client groups.

        GET /nfs/clientGroups
        """
        result = self._get('/nfs/clientGroups')
        return result.get('data', result)

    def create_client_group(self, name):
        """Create a new NFS client group.

        POST /nfs/clientGroups
        """
        payload = {'name': name}
        result = self._post('/nfs/clientGroups', json=payload)
        return result.get('data', result)

    def add_client_group_rule(self, group_uid, rule_type, rule_value):
        """Add a rule to an NFS client group.

        POST /nfs/clientGroups/{uid}/rules

        Weka v5.x uses {'ip': '<IP/mask>'} or {'dns': '<pattern>'}
        (dotted-decimal subnet mask, not CIDR prefix).
        """
        rule_type_lower = rule_type.lower()
        if rule_type_lower == 'ip':
            payload = {'ip': rule_value}
        else:
            payload = {'dns': rule_value}
        result = self._post(
            '/nfs/clientGroups/{uid}/rules'.format(uid=group_uid), json=payload)
        return result.get('data', result)

    def get_client_group(self, group_uid):
        """Return a single NFS client group by UID.

        GET /nfs/clientGroups/{uid}
        """
        result = self._get('/nfs/clientGroups/{uid}'.format(uid=group_uid))
        return result.get('data', result)

    def delete_client_group_rule(self, group_uid, rule_uid):
        """Delete a single rule from an NFS client group.

        DELETE /nfs/clientGroups/{uid}/rules/{rule_uid}
        """
        return self._delete(
            '/nfs/clientGroups/{uid}/rules/{rule_uid}'.format(
                uid=group_uid, rule_uid=rule_uid))

    def delete_client_group(self, group_uid):
        """Delete an NFS client group and all its rules.

        Weka requires all rules to be removed before the group can be
        deleted.  This method fetches and removes any rules first.

        DELETE /nfs/clientGroups/{uid}
        """
        try:
            cg = self.get_client_group(group_uid)
            for rule in cg.get('rules', []):
                rule_uid = rule.get('uid')
                if rule_uid:
                    try:
                        self.delete_client_group_rule(group_uid, rule_uid)
                    except Exception:
                        pass
        except Exception:
            pass
        return self._delete('/nfs/clientGroups/{uid}'.format(uid=group_uid))

    # ------------------------------------------------------------------
    # Snapshot methods
    # ------------------------------------------------------------------

    def list_snapshots(self, fs_uid=None):
        """Return all snapshots, optionally filtered by filesystem UID.

        GET /snapshots does not support server-side filtering; filter
        client-side using the 'filesystemUid' field returned per snapshot.
        """
        result = self._get('/snapshots')
        snaps = result.get('data', result)
        if fs_uid is not None:
            snaps = [s for s in snaps if s.get('filesystemUid') == fs_uid]
        return snaps

    def get_snapshot(self, snap_uid):
        """Return a single snapshot by UID.

        GET /snapshots/{uid}
        """
        result = self._get('/snapshots/{uid}'.format(uid=snap_uid))
        return result.get('data', result)

    def get_snapshot_by_name(self, name, fs_uid=None):
        """Find a snapshot by name; returns None if not found."""
        for snap in self.list_snapshots(fs_uid=fs_uid):
            if snap.get('name') == name:
                return snap
        return None

    def create_snapshot(self, fs_uid, name, is_writable=False):
        """Create a new snapshot.

        POST /snapshots
        """
        payload = {
            'fs_uid': fs_uid,
            'name': name,
            'is_writable': is_writable,
        }
        result = self._post('/snapshots', json=payload)
        return result.get('data', result)

    def update_snapshot(self, snap_uid, name=None, is_writable=None):
        """Update snapshot attributes.

        PUT /snapshots/{uid}
        """
        payload = {}
        if name is not None:
            payload['name'] = name
        if is_writable is not None:
            payload['is_writable'] = is_writable
        result = self._put(
            '/snapshots/{uid}'.format(uid=snap_uid), json=payload)
        return result.get('data', result)

    def delete_snapshot(self, snap_uid):
        """Delete a snapshot.

        DELETE /snapshots/{uid}
        """
        return self._delete('/snapshots/{uid}'.format(uid=snap_uid))

    def restore_snapshot(self, snap_uid, fs_uid):
        """Restore (revert) a filesystem to a snapshot.

        Weka v5 changed the endpoint to include the filesystem UID as well:
        POST /snapshots/{fs_uid}/{uid}/restore  (Weka 5.x)
        """
        result = self._post(
            '/snapshots/{fs_uid}/{uid}/restore'.format(
                fs_uid=fs_uid, uid=snap_uid))
        return result.get('data', result)

    # ------------------------------------------------------------------
    # Cluster capacity / health
    # ------------------------------------------------------------------

    def get_capacity(self):
        """Return cluster-wide capacity statistics.

        Tries GET /capacity (Weka 4.x); falls back to computing totals
        from GET /drives (Weka 5.x removed the /capacity endpoint).
        Returns a dict with totalBytes and usedBytes keys.
        """
        try:
            result = self._get('/capacity')
            return result.get('data', result)
        except Exception:
            pass

        # Weka 5.x fallback: compute from individual drives
        drives_result = self._get('/drives')
        drives = drives_result.get('data', drives_result)
        if not isinstance(drives, list):
            return {}
        total_bytes = sum(d.get('size_bytes', 0) for d in drives)
        used_bytes = sum(
            int(d.get('size_bytes', 0) * d.get('percentage_used', 0) / 100)
            for d in drives
        )
        return {'totalBytes': total_bytes, 'usedBytes': used_bytes}

    def get_cluster_info(self):
        """Return cluster information (name, version, nodes).

        GET /cluster
        """
        result = self._get('/cluster')
        return result.get('data', result)

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    def list_users(self):
        """Return all users in the current organization.

        GET /users
        """
        result = self._get('/users')
        return result.get('data', result)

    def create_user(self, username, password, role='Regular',
                    posix_uid=None, posix_gid=None):
        """Create a user.

        POST /users
        """
        payload = {
            'username': username,
            'password': password,
            'role': role,
        }
        if posix_uid is not None:
            payload['posix_uid'] = posix_uid
        if posix_gid is not None:
            payload['posix_gid'] = posix_gid
        result = self._post('/users', json=payload)
        return result.get('data', result)

    def delete_user(self, user_uid):
        """Delete a user.

        DELETE /users/{uid}
        """
        return self._delete('/users/{uid}'.format(uid=user_uid))

    # ------------------------------------------------------------------
    # KMS stubs
    # ------------------------------------------------------------------

    def get_kms_config(self):
        """Return KMS configuration.

        GET /kms
        """
        result = self._get('/kms')
        return result.get('data', result)

    def set_kms_config(self, kms_type, master_key_url, token=None,
                       base_url=None):
        """Configure KMS (Key Management Service).

        POST /kms
        """
        payload = {
            'kms_type': kms_type,
            'master_key_url': master_key_url,
        }
        if token:
            payload['token'] = token
        if base_url:
            payload['base_url'] = base_url
        result = self._post('/kms', json=payload)
        return result.get('data', result)

    # ------------------------------------------------------------------
    # LDAP stubs
    # ------------------------------------------------------------------

    def get_ldap_config(self):
        """Return LDAP/Active Directory configuration.

        GET /ldap
        """
        result = self._get('/ldap')
        return result.get('data', result)

    # ------------------------------------------------------------------
    # S3 bucket management stubs
    # ------------------------------------------------------------------

    def list_s3_buckets(self):
        """Return all S3 buckets.

        GET /s3/buckets
        """
        result = self._get('/s3/buckets')
        return result.get('data', result)

    def create_s3_bucket(self, name, fs_uid, path='/'):
        """Create an S3 bucket backed by a Weka filesystem.

        POST /s3/buckets
        """
        payload = {'name': name, 'filesystem_id': fs_uid, 'path': path}
        result = self._post('/s3/buckets', json=payload)
        return result.get('data', result)

    def delete_s3_bucket(self, bucket_name):
        """Delete an S3 bucket.

        DELETE /s3/buckets/{name}
        """
        return self._delete('/s3/buckets/{name}'.format(name=bucket_name))

    # ------------------------------------------------------------------
    # Object store bucket definitions (cluster-level)
    # ------------------------------------------------------------------

    def list_obs_buckets(self):
        """Return all object store bucket definitions.

        GET /objectStoreBuckets
        """
        result = self._get('/objectStoreBuckets')
        return result.get('data', result)

    def create_obs_bucket(self, name, obs_name, bucket_name,
                          access_key_id=None, secret_access_key=None,
                          region=None, endpoint=None):
        """Define a new object store bucket for tiering.

        POST /objectStoreBuckets
        """
        payload = {
            'name': name,
            'obs_name': obs_name,
            'bucket_name': bucket_name,
        }
        if access_key_id:
            payload['access_key_id'] = access_key_id
        if secret_access_key:
            payload['secret_access_key'] = secret_access_key
        if region:
            payload['region'] = region
        if endpoint:
            payload['endpoint'] = endpoint
        result = self._post('/objectStoreBuckets', json=payload)
        return result.get('data', result)

    def delete_obs_bucket(self, obs_bucket_uid):
        """Delete an object store bucket definition.

        DELETE /objectStoreBuckets/{uid}
        """
        return self._delete(
            '/objectStoreBuckets/{uid}'.format(uid=obs_bucket_uid))

    # ------------------------------------------------------------------
    # Security (TLS / certificates)
    # ------------------------------------------------------------------

    def get_tls_config(self):
        """Return TLS configuration.

        GET /security/tls
        """
        result = self._get('/security/tls')
        return result.get('data', result)

    def get_security_config(self):
        """Return global security configuration.

        GET /security
        """
        result = self._get('/security')
        return result.get('data', result)
