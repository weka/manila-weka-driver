# Copyright 2026 Weka.IO Ltd.
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

Implements the subset of the Weka v2 REST API used by the driver:
  - Filesystem lifecycle (CRUD + capacity management)
  - Filesystem groups
  - NFS client groups and permissions
  - Snapshots
  - Cluster status / capacity

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


def _is_capacity_error(message):
    """True if a Weka API error message indicates capacity exhaustion."""
    low = (message or '').lower()
    return 'capacity' in low and any(
        kw in low for kw in ('not enough', 'insufficient', 'no space')
    )


# Fallback defaults; overridden by weka_api_timeout and
# weka_max_api_retries config options wired in via do_setup.
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
        # pool_connections/pool_maxsize are passed from the driver
        # constructor; callers set them via config options.
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
        elif code == 400 and _is_capacity_error(msg):
            raise exceptions.WekaCapacityError(reason=msg)
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
        :param data_reduction: Data reduction setting (None = cluster
            default).
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
        result = self._get(
            '/fileSystemGroups/{uid}'.format(uid=group_uid))
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

    # ------------------------------------------------------------------
    # NFS methods
    # ------------------------------------------------------------------

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
        return self._delete(
            '/nfs/permissions/{uid}'.format(uid=permission_uid))

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
            '/nfs/clientGroups/{uid}/rules'.format(uid=group_uid),
            json=payload)
        return result.get('data', result)

    def get_client_group(self, group_uid):
        """Return a single NFS client group by UID.

        GET /nfs/clientGroups/{uid}
        """
        result = self._get(
            '/nfs/clientGroups/{uid}'.format(uid=group_uid))
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
        return self._delete(
            '/nfs/clientGroups/{uid}'.format(uid=group_uid))

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
            snaps = [s for s in snaps
                     if s.get('filesystemUid') == fs_uid]
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

    def delete_snapshot(self, snap_uid):
        """Delete a snapshot.

        DELETE /snapshots/{uid}
        """
        return self._delete('/snapshots/{uid}'.format(uid=snap_uid))

    def restore_snapshot(self, snap_uid, fs_uid):
        """Restore (revert) a filesystem to a snapshot.

        Weka v5 changed the endpoint to include the filesystem UID:
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
