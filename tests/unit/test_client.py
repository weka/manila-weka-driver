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

"""Unit tests for manila.share.drivers.weka.client."""

import unittest
from unittest import mock

import requests

from manila.share.drivers.weka import client as weka_client
from manila.share.drivers.weka import exceptions as weka_exc
from tests.unit import fakes


def _make_response(status_code=200, json_data=None):
    """Build a mock requests.Response."""
    resp = mock.Mock(spec=requests.Response)
    resp.status_code = status_code
    resp.content = b'{}' if json_data is None else b'content'
    json_data = json_data if json_data is not None else {}
    resp.json.return_value = json_data
    resp.text = str(json_data)
    return resp


def _login_response():
    return _make_response(200, {
        'data': {
            'access_token': 'fake-access-token',
            'refresh_token': 'fake-refresh-token',
        }
    })


class TestWekaApiClientAuth(unittest.TestCase):

    def _make_client(self):
        c = weka_client.WekaApiClient(
            host='weka-test', username='admin', password='secret',
            ssl_verify=False, timeout=5, max_retries=0)
        return c

    def test_login_stores_tokens(self):
        c = self._make_client()
        with mock.patch.object(c._session, 'post',
                               return_value=_login_response()):
            c.login()
        self.assertEqual('fake-access-token', c._access_token)
        self.assertEqual('fake-refresh-token', c._refresh_token)

    def test_raise_for_status_409_conflict(self):
        c = self._make_client()
        resp = _make_response(409, {'message': 'already exists'})
        self.assertRaises(
            weka_exc.WekaConflict, c._raise_for_status, resp)

    def test_raise_for_status_500_generic(self):
        c = self._make_client()
        resp = _make_response(500, {'message': 'internal error'})
        with self.assertRaises(weka_exc.WekaApiError) as ctx:
            c._raise_for_status(resp)
        self.assertEqual(500, ctx.exception.status_code)

    def test_raise_for_status_non_json_body(self):
        c = self._make_client()
        resp = mock.Mock()
        resp.status_code = 503
        resp.json.side_effect = ValueError('not json')
        resp.text = 'Service Unavailable'
        with self.assertRaises(weka_exc.WekaApiError) as ctx:
            c._raise_for_status(resp)
        self.assertIn('Unavailable', str(ctx.exception))

    def test_refresh_falls_back_to_login_when_refresh_token_missing(self):
        c = self._make_client()
        c._access_token = 'old-tok'
        c._refresh_token = None  # no refresh token
        with mock.patch.object(c, '_do_login') as do_login:
            c._refresh_or_login()
        do_login.assert_called_once()

    def test_refresh_falls_back_to_login_on_error(self):
        c = self._make_client()
        c._refresh_token = 'bad-refresh'
        refresh_resp = _make_response(401, {'message': 'invalid refresh'})
        with mock.patch.object(c._session, 'post',
                               return_value=refresh_resp):
            with mock.patch.object(c, '_do_login') as do_login:
                c._refresh_or_login()
        do_login.assert_called_once()

    def test_login_raises_auth_error_on_401(self):
        c = self._make_client()
        resp = _make_response(401, {'message': 'bad credentials'})
        with mock.patch.object(c._session, 'post', return_value=resp):
            self.assertRaises(weka_exc.WekaAuthError, c.login)

    def test_request_refreshes_token_on_401(self):
        c = self._make_client()
        c._access_token = 'old-token'
        c._refresh_token = 'old-refresh'

        ok_resp = _make_response(200, {'data': [fakes.fake_filesystem()]})

        # First call returns 401, second returns OK after refresh.
        auth_resp = _make_response(401, {'message': 'expired'})
        refresh_resp = _make_response(200, {
            'data': {'access_token': 'new-token', 'refresh_token': 'new-ref'}
        })

        with mock.patch.object(c._session, 'request',
                               side_effect=[auth_resp, ok_resp]) as req_mock:
            with mock.patch.object(c._session, 'post',
                                   return_value=refresh_resp):
                result = c._request('GET', '/fileSystems',
                                    _retry_auth=True)
        self.assertEqual(ok_resp, result)
        self.assertEqual(2, req_mock.call_count)

    def test_retry_on_429(self):
        c = self._make_client()
        c._access_token = 'tok'
        c._max_retries = 2
        rate_resp = _make_response(429, {'message': 'rate limited'})
        ok_resp = _make_response(200, {'data': []})

        with mock.patch.object(c._session, 'request',
                               side_effect=[rate_resp, rate_resp, ok_resp]):
            with mock.patch('time.sleep'):
                result = c._request('GET', '/fileSystems')
        self.assertEqual(ok_resp, result)

    def test_retry_exhausted_raises(self):
        c = self._make_client()
        c._access_token = 'tok'
        c._max_retries = 1
        rate_resp = _make_response(429, {'message': 'rate limited'})

        with mock.patch.object(c._session, 'request',
                               return_value=rate_resp):
            with mock.patch('time.sleep'):
                self.assertRaises(
                    weka_exc.WekaRateLimited,
                    c._request, 'GET', '/fileSystems')

    def test_404_not_retried(self):
        c = self._make_client()
        c._access_token = 'tok'
        c._max_retries = 3
        not_found = _make_response(404, {'message': 'not found'})

        with mock.patch.object(c._session, 'request',
                               return_value=not_found) as req_mock:
            self.assertRaises(
                weka_exc.WekaNotFound, c._request, 'GET', '/fileSystems/bad')
        # 404 is not retried and does not trigger auth refresh
        self.assertEqual(1, req_mock.call_count)


class TestWekaApiClientFilesystems(unittest.TestCase):

    def setUp(self):
        self.client = weka_client.WekaApiClient(
            host='weka-test', username='admin', password='secret',
            ssl_verify=False, timeout=5, max_retries=0)
        self.client._access_token = 'tok'

    def _mock_get(self, path, json_data):
        def side_effect(method, url, **kwargs):
            self.assertEqual('GET', method)
            self.assertIn(path, url)
            return _make_response(200, json_data)
        return mock.patch.object(
            self.client._session, 'request', side_effect=side_effect)

    def test_list_filesystems(self):
        fs_list = [fakes.fake_filesystem()]
        with self._mock_get('/fileSystems', {'data': fs_list}):
            result = self.client.list_filesystems()
        self.assertEqual(fs_list, result)

    def test_get_filesystem(self):
        fs = fakes.fake_filesystem()
        resp = _make_response(200, {'data': fs})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.get_filesystem(fakes.FAKE_FS_UID)
        self.assertEqual(fs, result)

    def test_create_filesystem(self):
        fs = fakes.fake_filesystem()
        resp = _make_response(200, {'data': fs})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.create_filesystem(
                name=fakes.FAKE_FS_NAME,
                group_name=fakes.FAKE_GROUP_NAME,
                total_capacity=10 * 1024 ** 3,
            )
        self.assertEqual(fs, result)

    def test_update_filesystem(self):
        fs = fakes.fake_filesystem(total_capacity=20 * 1024 ** 3)
        resp = _make_response(200, {'data': fs})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.update_filesystem(
                fakes.FAKE_FS_UID, total_capacity=20 * 1024 ** 3)
        self.assertEqual(fs['totalCapacity'], result['totalCapacity'])

    def test_delete_filesystem(self):
        resp = _make_response(200, {})
        resp.content = b''
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.delete_filesystem(fakes.FAKE_FS_UID)
        self.assertEqual({}, result)

    def test_get_filesystem_by_name_found(self):
        fs_list = [fakes.fake_filesystem()]
        with mock.patch.object(self.client, 'list_filesystems',
                               return_value=fs_list):
            result = self.client.get_filesystem_by_name(fakes.FAKE_FS_NAME)
        self.assertEqual(fakes.FAKE_FS_UID, result['uid'])

    def test_get_filesystem_by_name_not_found(self):
        with mock.patch.object(self.client, 'list_filesystems',
                               return_value=[]):
            result = self.client.get_filesystem_by_name('nonexistent')
        self.assertIsNone(result)

    def test_get_filesystem_mount_token(self):
        token = fakes.fake_mount_token()
        resp = _make_response(200, {'data': token})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.get_filesystem_mount_token(
                fakes.FAKE_FS_UID)
        self.assertEqual(token, result)


class TestWekaApiClientFilesystemGroups(unittest.TestCase):

    def setUp(self):
        self.client = weka_client.WekaApiClient(
            host='weka-test', username='admin', password='secret',
            ssl_verify=False, timeout=5, max_retries=0)
        self.client._access_token = 'tok'

    def test_list_filesystem_groups(self):
        groups = [fakes.fake_filesystem_group()]
        resp = _make_response(200, {'data': groups})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.list_filesystem_groups()
        self.assertEqual(groups, result)

    def test_create_filesystem_group(self):
        grp = fakes.fake_filesystem_group()
        resp = _make_response(200, {'data': grp})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.create_filesystem_group(
                fakes.FAKE_GROUP_NAME)
        self.assertEqual(grp, result)

    def test_get_filesystem_group_by_name_found(self):
        groups = [fakes.fake_filesystem_group()]
        with mock.patch.object(self.client, 'list_filesystem_groups',
                               return_value=groups):
            result = self.client.get_filesystem_group_by_name(
                fakes.FAKE_GROUP_NAME)
        self.assertEqual(fakes.FAKE_GROUP_UID, result['uid'])

    def test_get_filesystem_group_by_name_not_found(self):
        with mock.patch.object(self.client, 'list_filesystem_groups',
                               return_value=[]):
            result = self.client.get_filesystem_group_by_name('missing')
        self.assertIsNone(result)


class TestWekaApiClientSnapshots(unittest.TestCase):

    def setUp(self):
        self.client = weka_client.WekaApiClient(
            host='weka-test', username='admin', password='secret',
            ssl_verify=False, timeout=5, max_retries=0)
        self.client._access_token = 'tok'

    def test_create_snapshot(self):
        snap = fakes.fake_snapshot()
        resp = _make_response(200, {'data': snap})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp) as mock_req:
            result = self.client.create_snapshot(
                fakes.FAKE_FS_UID, fakes.FAKE_SNAP_NAME)
        self.assertEqual(snap, result)
        call_kwargs = mock_req.call_args[1]
        self.assertIn('fs_uid', call_kwargs.get('json', {}))
        self.assertNotIn('filesystem_id', call_kwargs.get('json', {}))
        self.assertNotIn('filesystemId', call_kwargs.get('json', {}))

    def test_delete_snapshot(self):
        resp = _make_response(200, {})
        resp.content = b''
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.delete_snapshot(fakes.FAKE_SNAP_UID)
        self.assertEqual({}, result)

    def test_restore_snapshot(self):
        resp = _make_response(200, {'data': {'status': 'ok'}})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp) as mock_req:
            result = self.client.restore_snapshot(
                fakes.FAKE_SNAP_UID, fakes.FAKE_FS_UID)
        self.assertIsNotNone(result)
        # Verify the v5 endpoint includes both fs_uid and snap_uid in the path
        url = mock_req.call_args[1].get('url') or mock_req.call_args[0][1]
        self.assertIn(fakes.FAKE_FS_UID, url)
        self.assertIn(fakes.FAKE_SNAP_UID, url)

    def test_list_snapshots_returns_all(self):
        snaps = [fakes.fake_snapshot(), fakes.fake_snapshot(uid='snap-2')]
        resp = _make_response(200, {'data': snaps})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.list_snapshots()
        self.assertEqual(2, len(result))

    def test_list_snapshots_filtered_by_fs_uid(self):
        snap_match = fakes.fake_snapshot(uid='snap-match',
                                         fs_uid='target-fs-uid')
        snap_other = fakes.fake_snapshot(uid='snap-other',
                                         fs_uid='other-fs-uid')
        resp = _make_response(200, {'data': [snap_match, snap_other]})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.list_snapshots(fs_uid='target-fs-uid')
        self.assertEqual(1, len(result))
        self.assertEqual('snap-match', result[0]['uid'])

    def test_get_snapshot(self):
        snap = fakes.fake_snapshot()
        resp = _make_response(200, {'data': snap})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.get_snapshot(fakes.FAKE_SNAP_UID)
        self.assertEqual(snap, result)

    def test_get_snapshot_by_name_found(self):
        snap = fakes.fake_snapshot()
        with mock.patch.object(self.client, 'list_snapshots',
                               return_value=[snap]):
            result = self.client.get_snapshot_by_name(fakes.FAKE_SNAP_NAME)
        self.assertEqual(fakes.FAKE_SNAP_UID, result['uid'])

    def test_get_snapshot_by_name_not_found(self):
        with mock.patch.object(self.client, 'list_snapshots',
                               return_value=[]):
            result = self.client.get_snapshot_by_name('missing')
        self.assertIsNone(result)


class TestWekaApiClientOrganizations(unittest.TestCase):

    def setUp(self):
        self.client = weka_client.WekaApiClient(
            host='weka-test', username='admin', password='secret',
            ssl_verify=False, timeout=5, max_retries=0)
        self.client._access_token = 'tok'

    def test_create_organization(self):
        org = fakes.fake_organization()
        resp = _make_response(200, {'data': org})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.create_organization('TestOrg')
        self.assertEqual(org, result)

    def test_get_organization_by_name_not_found(self):
        with mock.patch.object(self.client, 'list_organizations',
                               return_value=[]):
            result = self.client.get_organization_by_name('missing')
        self.assertIsNone(result)


class TestWekaApiClientNFS(unittest.TestCase):

    def setUp(self):
        self.client = weka_client.WekaApiClient(
            host='weka-test', username='admin', password='secret',
            ssl_verify=False, timeout=5, max_retries=0)
        self.client._access_token = 'tok'

    def test_create_nfs_permission(self):
        perm = fakes.fake_nfs_permission()
        resp = _make_response(200, {'data': perm})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.create_nfs_permission(
                client_group=fakes.FAKE_CG_UID,
                fs_uid=fakes.FAKE_FS_UID,
                path='/',
                access_type='RW',
            )
        self.assertEqual(perm, result)

    def test_delete_nfs_permission(self):
        resp = _make_response(200, {})
        resp.content = b''
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            self.client.delete_nfs_permission(fakes.FAKE_PERM_UID)

    def test_create_client_group(self):
        cg = fakes.fake_client_group()
        resp = _make_response(200, {'data': cg})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.create_client_group('test-group')
        self.assertEqual(cg, result)

    def test_add_client_group_rule_ip(self):
        resp = _make_response(200, {'data': {}})

        def check(method, url, **kwargs):
            self.assertEqual('POST', method)
            self.assertIn('/nfs/clientGroups/', url)
            self.assertIn('/rules', url)
            self.assertEqual({'ip': '10.0.0.0/255.255.255.0'},
                             kwargs.get('json'))
            return resp

        with mock.patch.object(self.client._session, 'request',
                               side_effect=check):
            self.client.add_client_group_rule(
                fakes.FAKE_CG_UID, 'IP', '10.0.0.0/255.255.255.0')

    def test_add_client_group_rule_dns(self):
        resp = _make_response(200, {'data': {}})

        def check(method, url, **kwargs):
            self.assertEqual('POST', method)
            self.assertIn('/nfs/clientGroups/', url)
            self.assertEqual({'dns': '*.example.com'}, kwargs.get('json'))
            return resp

        with mock.patch.object(self.client._session, 'request',
                               side_effect=check):
            self.client.add_client_group_rule(
                fakes.FAKE_CG_UID, 'DNS', '*.example.com')


class TestWekaApiClientCapacity(unittest.TestCase):

    def setUp(self):
        self.client = weka_client.WekaApiClient(
            host='weka-test', username='admin', password='secret',
            ssl_verify=False, timeout=5, max_retries=0)
        self.client._access_token = 'tok'

    def test_get_capacity(self):
        cap = fakes.fake_capacity()
        resp = _make_response(200, {'data': cap})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.get_capacity()
        self.assertEqual(cap, result)

    def test_get_cluster_status(self):
        status = fakes.fake_cluster_status()
        resp = _make_response(200, status)
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.get_cluster_status()
        self.assertEqual(status, result)


class TestWekaApiClientQuotas(unittest.TestCase):

    def setUp(self):
        self.client = weka_client.WekaApiClient(
            host='weka-test', username='admin', password='secret',
            ssl_verify=False, timeout=5, max_retries=0)
        self.client._access_token = 'tok'

    def _ok(self, data=None):
        return _make_response(200, {'data': data or {}})

    def test_list_directory_quotas(self):
        quota = fakes.fake_quota()
        resp = _make_response(200, {'data': [quota]})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.list_directory_quotas(fakes.FAKE_FS_UID)
        self.assertEqual([quota], result)

    def test_set_directory_quota(self):
        quota = fakes.fake_quota()
        with mock.patch.object(self.client._session, 'request',
                               return_value=self._ok(quota)):
            result = self.client.set_directory_quota(
                fakes.FAKE_FS_UID, inode_id=12345,
                hard_limit_bytes=10 * 1024 ** 3)
        self.assertEqual(quota, result)

    def test_update_directory_quota(self):
        quota = fakes.fake_quota()
        with mock.patch.object(self.client._session, 'request',
                               return_value=self._ok(quota)):
            result = self.client.update_directory_quota(
                fakes.FAKE_FS_UID, inode_id=12345,
                hard_limit_bytes=20 * 1024 ** 3)
        self.assertEqual(quota, result)

    def test_delete_directory_quota(self):
        resp = _make_response(200, {})
        resp.content = b''
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            self.client.delete_directory_quota(fakes.FAKE_FS_UID, 12345)

    def test_get_default_quota(self):
        quota = fakes.fake_quota()
        with mock.patch.object(self.client._session, 'request',
                               return_value=self._ok(quota)):
            result = self.client.get_default_quota(fakes.FAKE_FS_UID)
        self.assertEqual(quota, result)

    def test_set_default_quota(self):
        quota = fakes.fake_quota()
        with mock.patch.object(self.client._session, 'request',
                               return_value=self._ok(quota)):
            result = self.client.set_default_quota(
                fakes.FAKE_FS_UID, hard_limit_bytes=5 * 1024 ** 3)
        self.assertEqual(quota, result)

    def test_delete_default_quota(self):
        resp = _make_response(200, {})
        resp.content = b''
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            self.client.delete_default_quota(fakes.FAKE_FS_UID)


class TestWekaApiClientSDKStubs(unittest.TestCase):
    """Smoke tests for SDK stub methods.

    Not called by driver but part of the public client API.  Validates
    that every stub method makes the expected HTTP call to the correct
    path.
    """

    def setUp(self):
        self.client = weka_client.WekaApiClient(
            host='weka-test', username='admin', password='secret',
            ssl_verify=False, timeout=5, max_retries=0)
        self.client._access_token = 'tok'

    def _patch_request(self, expected_method, expected_path,
                       response_data=None):
        resp = _make_response(200, {'data': response_data or {}})

        def check(method, url, **kwargs):
            assert method.upper() == expected_method.upper(), (
                'Expected %s got %s' % (expected_method, method))
            assert expected_path in url, (
                'Expected path %s in %s' % (expected_path, url))
            return resp

        return mock.patch.object(
            self.client._session, 'request', side_effect=check)

    def test_get_cluster_info(self):
        with self._patch_request('GET', '/cluster', {'name': 'c'}):
            self.client.get_cluster_info()

    def test_list_users(self):
        with self._patch_request('GET', '/users', []):
            self.client.list_users()

    def test_create_user(self):
        with self._patch_request('POST', '/users', {'uid': 'u1'}):
            self.client.create_user('bob', 'pass')

    def test_delete_user(self):
        resp = _make_response(200, {})
        resp.content = b''
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            self.client.delete_user('uid-1')

    def test_get_kms_config(self):
        with self._patch_request('GET', '/kms', {}):
            self.client.get_kms_config()

    def test_get_ldap_config(self):
        with self._patch_request('GET', '/ldap', {}):
            self.client.get_ldap_config()

    def test_get_tls_config(self):
        with self._patch_request('GET', '/security/tls', {}):
            self.client.get_tls_config()

    def test_get_security_config(self):
        with self._patch_request('GET', '/security', {}):
            self.client.get_security_config()

    def test_list_s3_buckets(self):
        with self._patch_request('GET', '/s3/buckets', []):
            self.client.list_s3_buckets()

    def test_list_obs_buckets(self):
        with self._patch_request('GET', '/objectStoreBuckets', []):
            self.client.list_obs_buckets()

    def test_attach_obs_bucket(self):
        with self._patch_request('POST', '/objectStoreBuckets', {}):
            self.client.attach_obs_bucket(
                fakes.FAKE_FS_UID, 'obs-uid-1')

    def test_detach_obs_bucket(self):
        resp = _make_response(200, {})
        resp.content = b''
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            self.client.detach_obs_bucket(fakes.FAKE_FS_UID, 'obs-bid-1')

    def test_list_interface_groups(self):
        with self._patch_request('GET', '/interfaceGroups', []):
            self.client.list_interface_groups()

    def test_list_nfs_permissions(self):
        with self._patch_request('GET', '/nfs/permissions', []):
            self.client.list_nfs_permissions()

    def test_list_client_groups(self):
        with self._patch_request('GET', '/clientGroups', []):
            self.client.list_client_groups()

    def test_delete_client_group(self):
        resp = _make_response(200, {})
        resp.content = b''
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            self.client.delete_client_group('cg-1')

    def test_update_snapshot(self):
        snap = fakes.fake_snapshot()
        resp = _make_response(200, {'data': snap})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.update_snapshot(
                fakes.FAKE_SNAP_UID, is_writable=True)
        self.assertEqual(snap, result)

    def test_update_organization(self):
        org = fakes.fake_organization()
        resp = _make_response(200, {'data': org})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.update_organization(
                fakes.FAKE_ORG_UID, name='NewName')
        self.assertEqual(org, result)

    def test_delete_organization(self):
        resp = _make_response(200, {})
        resp.content = b''
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            self.client.delete_organization(fakes.FAKE_ORG_UID)

    def test_set_organization_limits(self):
        with self._patch_request('PUT', '/organizations', {}):
            self.client.set_organization_limits(
                fakes.FAKE_ORG_UID, total_capacity=100 * 1024 ** 3)

    def test_set_organization_security(self):
        with self._patch_request('PUT', '/organizations', {}):
            self.client.set_organization_security(
                fakes.FAKE_ORG_UID, mode='strict')

    def test_update_filesystem_group(self):
        grp = fakes.fake_filesystem_group()
        resp = _make_response(200, {'data': grp})
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            result = self.client.update_filesystem_group(
                fakes.FAKE_GROUP_UID, name='new-name')
        self.assertEqual(grp, result)

    def test_delete_filesystem_group(self):
        resp = _make_response(200, {})
        resp.content = b''
        with mock.patch.object(self.client._session, 'request',
                               return_value=resp):
            self.client.delete_filesystem_group(fakes.FAKE_GROUP_UID)


if __name__ == '__main__':
    unittest.main()
