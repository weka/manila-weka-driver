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

"""Unit tests for manila.share.drivers.weka.driver."""

import threading
import unittest
from unittest import mock

from manila.common import constants
from oslo_concurrency import processutils
from oslo_config import cfg

from manila import exception
from manila.share.drivers.weka import driver as weka_driver
from manila.share.drivers.weka import exceptions as weka_exc
from manila.share.drivers.weka import posix as weka_posix
from tests.unit import fakes

CONF = cfg.CONF


def _make_config(**kwargs):
    """Return a mock configuration object."""
    defaults = {
        'weka_api_server': 'weka-test.example.com',
        'weka_api_port': 14000,
        'weka_username': 'admin',
        'weka_password': 'secret',
        'weka_organization': 'Root',
        'weka_ssl_verify': False,
        'weka_filesystem_group': 'default',
        'weka_mount_point_base': '/mnt/weka',
        'weka_num_cores': 1,
        'weka_net_device': None,
        'weka_posix_mount_timeout': 60,
        'weka_api_timeout': 30,
        'weka_max_api_retries': 3,
        'weka_share_name_prefix': 'manila_',
        'weka_nfs_server': None,
        'share_backend_name': 'weka',
    }
    defaults.update(kwargs)

    config = mock.Mock()
    config.safe_get = lambda key: defaults.get(key)
    return config


class TestWekaShareDriverSetup(unittest.TestCase):

    def _make_driver(self, **cfg_kwargs):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config(**cfg_kwargs)
        drv._client = None
        drv._fs_group_uid = None
        return drv

    @mock.patch('manila.share.drivers.weka.client.WekaApiClient')
    def test_do_setup_creates_client_and_logs(self, mock_client_cls):
        drv = self._make_driver()
        mock_client = mock.Mock()
        mock_client.get_cluster_status.return_value = (
            fakes.fake_cluster_status())
        mock_client.get_filesystem_group_by_name.return_value = (
            fakes.fake_filesystem_group())
        mock_client_cls.return_value = mock_client

        drv.do_setup(context=None)

        mock_client.login.assert_called_once()
        self.assertIsNotNone(drv._client)

    @mock.patch('manila.share.drivers.weka.client.WekaApiClient')
    def test_do_setup_creates_fs_group_if_missing(self, mock_client_cls):
        drv = self._make_driver()
        mock_client = mock.Mock()
        mock_client.get_cluster_status.return_value = (
            fakes.fake_cluster_status())
        mock_client.get_filesystem_group_by_name.return_value = None
        mock_client.create_filesystem_group.return_value = (
            fakes.fake_filesystem_group())
        mock_client_cls.return_value = mock_client

        drv.do_setup(context=None)

        mock_client.create_filesystem_group.assert_called_once_with('default')

    def test_check_for_setup_error_missing_required(self):
        drv = self._make_driver(weka_api_server=None)
        self.assertRaises(
            exception.InvalidInput, drv.check_for_setup_error)

    @mock.patch('builtins.open',
                mock.mock_open(read_data='nodev wekafs\n'))
    def test_check_for_setup_error_wekafs_loaded(self):
        drv = self._make_driver()
        drv._client = mock.Mock()
        drv._client.get_cluster_status.return_value = {}
        # Should not raise
        drv.check_for_setup_error()

    def test_check_for_setup_error_auth_failure(self):
        drv = self._make_driver()
        drv._client = mock.Mock()
        drv._client.get_cluster_status.side_effect = (
            weka_exc.WekaAuthError(reason='bad creds'))
        with mock.patch('builtins.open',
                        mock.mock_open(read_data='nodev wekafs\n')):
            self.assertRaises(
                exception.ManilaException, drv.check_for_setup_error)


class TestWekaShareDriverCreateShare(unittest.TestCase):

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        return drv

    def test_create_share_wekafs(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = None
        drv._client.create_filesystem.return_value = fakes.fake_filesystem()

        share = fakes.fake_share(proto='WEKAFS')
        result = drv.create_share(context=None, share=share)

        drv._client.create_filesystem.assert_called_once()
        self.assertEqual(1, len(result))
        path = result[0]['path']
        self.assertIn(fakes.FAKE_FS_NAME, path)

    def test_create_share_nfs(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = None
        drv._client.create_filesystem.return_value = fakes.fake_filesystem()

        share = fakes.fake_share(proto='NFS')
        result = drv.create_share(context=None, share=share)

        self.assertEqual(1, len(result))
        self.assertIn(':/', result[0]['path'])

    def test_create_share_unsupported_protocol(self):
        drv = self._make_driver()
        share = fakes.fake_share(proto='CEPHFS')
        self.assertRaises(
            exception.InvalidShare,
            drv.create_share, None, share)

    def test_create_share_idempotent_when_fs_exists(self):
        drv = self._make_driver()
        existing_fs = fakes.fake_filesystem()
        drv._client.get_filesystem_by_name.return_value = existing_fs

        share = fakes.fake_share(proto='WEKAFS')
        result = drv.create_share(context=None, share=share)

        drv._client.create_filesystem.assert_not_called()
        self.assertEqual(1, len(result))

    def test_create_share_stores_fs_uid_in_metadata(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = None
        drv._client.create_filesystem.return_value = fakes.fake_filesystem()

        share = fakes.fake_share(proto='WEKAFS')
        result = drv.create_share(context=None, share=share)

        meta = result[0].get('metadata', {})
        self.assertEqual(fakes.FAKE_FS_UID, meta.get('weka_fs_uid'))


_PATCH_EXECUTE = 'manila.share.drivers.weka.driver.processutils.execute'
_PATCH_MAKEDIRS = 'manila.share.drivers.weka.driver.os.makedirs'
_PATCH_MKDTEMP = 'manila.share.drivers.weka.driver.tempfile.mkdtemp'
_PATCH_RMDIR = 'manila.share.drivers.weka.driver.os.rmdir'
_PATCH_SOCKET = 'manila.share.drivers.weka.driver.socket.socket'
_PATCH_SLEEP = 'manila.share.drivers.weka.driver.time.sleep'
_PATCH_SPAWN = 'manila.share.drivers.weka.driver.eventlet.spawn'


class TestWekaShareDriverCreateFromSnapshot(unittest.TestCase):
    """Unit tests for WekaShareDriver.create_share_from_snapshot.

    create_share_from_snapshot is async: it creates the filesystem
    synchronously, spawns a background thread, and returns immediately
    with STATUS_CREATING_FROM_SNAPSHOT.  The actual data copy runs in
    _copy_snapshot_nfs (NFS) or _copy_snapshot_wekafs (WEKAFS).

    Test coverage:
      - Pre-condition failures (snapshot not found, no NFS server)
      - Async dispatch: return dict with creating status + locations
      - Happy path for WEKAFS (_copy_snapshot_wekafs direct call)
      - Happy path for NFS (_copy_snapshot_nfs direct call)
      - Each mount/rsync failure scenario in _copy_snapshot_nfs
      - Cleanup resilience tests via _copy_snapshot_nfs direct calls
    """

    NFS_SERVER = 'nfs.example.com'
    TMP_CG_NAME = 'manila-snap-' + fakes.FAKE_NEW_SHARE_ID[:8]

    def _make_driver(self, nfs_server=NFS_SERVER):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config(weka_nfs_server=nfs_server)
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        drv._async_copies = {}
        drv._async_copies_lock = threading.Lock()
        drv._nfs_server = nfs_server
        return drv

    def _setup_happy_path_client(self, drv):
        """Configure client mocks for a fully successful operation."""
        snap = fakes.fake_snapshot()
        src_fs = fakes.fake_filesystem()
        new_fs = fakes.fake_new_filesystem()
        cg = fakes.fake_client_group()
        perm_src = fakes.fake_nfs_permission(
            uid='perm-src', fs_name=fakes.FAKE_FS_NAME,
            cg_name=self.TMP_CG_NAME)
        perm_dst = fakes.fake_nfs_permission(
            uid='perm-dst', fs_name=fakes.FAKE_NEW_FS_NAME,
            cg_name=self.TMP_CG_NAME)

        drv._client.get_snapshot_by_name.return_value = snap
        drv._client.get_filesystem.return_value = src_fs
        drv._client.get_filesystem_by_name.return_value = None
        drv._client.create_filesystem.return_value = new_fs
        drv._client.create_client_group.return_value = cg
        drv._client.add_client_group_rule.return_value = {
            'uid': fakes.FAKE_CG_RULE_UID}
        drv._client.list_nfs_permissions.return_value = [perm_src, perm_dst]
        return snap, new_fs, cg

    def _new_share(self, proto='WEKAFS'):
        return fakes.fake_share(
            share_id=fakes.FAKE_NEW_SHARE_ID, proto=proto)

    # ── Pre-condition failures ────────────────────────────────────────────

    def test_snapshot_not_found_raises(self):
        drv = self._make_driver()
        drv._client.get_snapshot_by_name.return_value = None

        self.assertRaises(
            exception.ShareSnapshotNotFound,
            drv.create_share_from_snapshot,
            None, self._new_share(), fakes.fake_snapshot_model())
        drv._client.create_filesystem.assert_not_called()

    def test_no_nfs_server_raises_before_fs_create(self):
        """create_share_from_snapshot raises fast when NFS server absent.

        With NFS protocol and no weka_nfs_server configured the outer
        function must raise ShareBackendException before creating the
        filesystem or spawning the copy greenlet.
        """
        drv = self._make_driver(nfs_server=None)
        drv._client.get_snapshot_by_name.return_value = fakes.fake_snapshot()
        drv._client.get_filesystem.return_value = fakes.fake_filesystem()
        self.assertRaises(
            exception.ShareBackendException,
            drv.create_share_from_snapshot,
            None, self._new_share(proto='NFS'),
            fakes.fake_snapshot_model())
        drv._client.create_filesystem.assert_not_called()

    # ── Async dispatch ────────────────────────────────────────────────────

    @mock.patch(_PATCH_SPAWN)
    def test_create_from_snapshot_returns_creating_status(
            self, mock_spawn):
        """create_share_from_snapshot returns dict with creating status."""
        drv = self._make_driver()
        snap, new_fs, _ = self._setup_happy_path_client(drv)

        result = drv.create_share_from_snapshot(
            None, self._new_share(), fakes.fake_snapshot_model())

        self.assertIsInstance(result, dict)
        self.assertEqual(
            constants.STATUS_CREATING_FROM_SNAPSHOT,
            result['status'])
        self.assertIn('export_locations', result)
        self.assertGreater(len(result['export_locations']), 0)
        mock_spawn.assert_called_once()

    # ── Happy path copy logic ─────────────────────────────────────────────

    @mock.patch(_PATCH_SLEEP)
    @mock.patch(_PATCH_SOCKET)
    @mock.patch(_PATCH_RMDIR)
    @mock.patch(_PATCH_MKDTEMP)
    @mock.patch(_PATCH_EXECUTE)
    def test_happy_path_nfs_copy(
            self, mock_exec, mock_mkdtemp, mock_rmdir,
            mock_socket, mock_sleep):
        """_copy_snapshot_nfs: full success — mounts, rsync, cleanup."""
        drv = self._make_driver()
        self._setup_happy_path_client(drv)
        mock_socket.return_value.getsockname.return_value = (
            '192.0.2.1', 0)
        mock_mkdtemp.side_effect = ['/tmp/snap_src', '/tmp/snap_dst']
        snap = fakes.fake_snapshot()

        drv._copy_snapshot_nfs(
            self._new_share(), fakes.fake_snapshot_model(),
            snap, fakes.FAKE_FS_NAME, fakes.FAKE_NEW_FS_NAME)

        drv._client.create_client_group.assert_called_once()
        drv._client.add_client_group_rule.assert_called_once()
        self.assertEqual(
            2, drv._client.create_nfs_permission.call_count)
        exec_cmds = [c[0][0] for c in mock_exec.call_args_list]
        self.assertEqual(2, exec_cmds.count('mount'))
        self.assertIn('rsync', exec_cmds)
        self.assertEqual(2, exec_cmds.count('umount'))
        drv._client.delete_nfs_permission.assert_called()
        drv._client.delete_client_group.assert_called_once_with(
            fakes.FAKE_CG_UID)

    @mock.patch(_PATCH_EXECUTE)
    def test_happy_path_wekafs_copy(self, mock_exec):
        """_copy_snapshot_wekafs: rsync called with WekaMount mounts."""
        drv = self._make_driver()
        snap = fakes.fake_snapshot()

        with mock.patch(
                'manila.share.drivers.weka.driver.tempfile.mkdtemp',
                side_effect=['/tmp/weka_src', '/tmp/weka_dst']):
            with mock.patch.object(
                    weka_posix.WekaMount, 'mount'):
                with mock.patch.object(
                        weka_posix.WekaMount, 'unmount'):
                    drv._copy_snapshot_wekafs(
                        self._new_share(),
                        fakes.fake_snapshot_model(),
                        snap,
                        fakes.FAKE_FS_NAME,
                        fakes.FAKE_NEW_FS_NAME)

        exec_cmds = [c[0][0] for c in mock_exec.call_args_list]
        self.assertIn('rsync', exec_cmds)

    @mock.patch(_PATCH_SLEEP)
    @mock.patch(_PATCH_SOCKET)
    @mock.patch(_PATCH_RMDIR)
    @mock.patch(_PATCH_MKDTEMP)
    @mock.patch(_PATCH_EXECUTE)
    def test_happy_path_nfs_protocol_returns_nfs_path(
            self, mock_exec, mock_mkdtemp, mock_rmdir,
            mock_socket, mock_sleep):
        """create_share_from_snapshot with NFS proto returns nfs path."""
        drv = self._make_driver()
        self._setup_happy_path_client(drv)
        mock_socket.return_value.getsockname.return_value = (
            '192.0.2.1', 0)
        mock_mkdtemp.side_effect = ['/tmp/snap_src', '/tmp/snap_dst']

        with mock.patch(_PATCH_SPAWN):
            result = drv.create_share_from_snapshot(
                None, self._new_share(proto='NFS'),
                fakes.fake_snapshot_model())

        self.assertIsInstance(result, dict)
        self.assertEqual(
            constants.STATUS_CREATING_FROM_SNAPSHOT,
            result['status'])
        self.assertIn(':/', result['export_locations'][0]['path'])

    # ── Failure paths — verify cleanup runs ──────────────────────────────

    @mock.patch(_PATCH_SLEEP)
    @mock.patch(_PATCH_SOCKET)
    @mock.patch(_PATCH_RMDIR)
    @mock.patch(_PATCH_MKDTEMP)
    @mock.patch(_PATCH_EXECUTE)
    def test_src_mount_fails_reraises_and_cleans_up(
            self, mock_exec, mock_mkdtemp, mock_rmdir,
            mock_socket, mock_sleep):
        """Src mount failure in _copy_snapshot_nfs: exception re-raised.

        No umounts (nothing was mounted); NFS permissions and client
        group still deleted.
        """
        drv = self._make_driver()
        self._setup_happy_path_client(drv)
        mock_socket.return_value.getsockname.return_value = (
            '192.0.2.1', 0)
        mock_mkdtemp.side_effect = ['/tmp/snap_src', '/tmp/snap_dst']
        mock_exec.side_effect = processutils.ProcessExecutionError(
            'mount src failed')
        snap = fakes.fake_snapshot()

        self.assertRaises(
            processutils.ProcessExecutionError,
            drv._copy_snapshot_nfs,
            self._new_share(), fakes.fake_snapshot_model(),
            snap, fakes.FAKE_FS_NAME, fakes.FAKE_NEW_FS_NAME)

        exec_cmds = [c[0][0] for c in mock_exec.call_args_list]
        self.assertNotIn('umount', exec_cmds)
        drv._client.delete_nfs_permission.assert_called()
        drv._client.delete_client_group.assert_called_once()

    @mock.patch(_PATCH_SLEEP)
    @mock.patch(_PATCH_SOCKET)
    @mock.patch(_PATCH_RMDIR)
    @mock.patch(_PATCH_MKDTEMP)
    @mock.patch(_PATCH_EXECUTE)
    def test_dst_mount_fails_reraises_and_cleans_up(
            self, mock_exec, mock_mkdtemp, mock_rmdir,
            mock_socket, mock_sleep):
        """Dst mount failure: src is unmounted, perms and CG deleted."""
        drv = self._make_driver()
        self._setup_happy_path_client(drv)
        mock_socket.return_value.getsockname.return_value = (
            '192.0.2.1', 0)
        mock_mkdtemp.side_effect = ['/tmp/snap_src', '/tmp/snap_dst']
        mock_exec.side_effect = [
            None,
            processutils.ProcessExecutionError('mount dst failed'),
        ]
        snap = fakes.fake_snapshot()

        self.assertRaises(
            processutils.ProcessExecutionError,
            drv._copy_snapshot_nfs,
            self._new_share(), fakes.fake_snapshot_model(),
            snap, fakes.FAKE_FS_NAME, fakes.FAKE_NEW_FS_NAME)

        exec_cmds = [c[0][0] for c in mock_exec.call_args_list]
        self.assertEqual(1, exec_cmds.count('umount'))
        drv._client.delete_nfs_permission.assert_called()
        drv._client.delete_client_group.assert_called_once()

    @mock.patch(_PATCH_SLEEP)
    @mock.patch(_PATCH_SOCKET)
    @mock.patch(_PATCH_RMDIR)
    @mock.patch(_PATCH_MKDTEMP)
    @mock.patch(_PATCH_EXECUTE)
    def test_rsync_fails_reraises_and_cleans_up(
            self, mock_exec, mock_mkdtemp, mock_rmdir,
            mock_socket, mock_sleep):
        """Rsync failure: both mounts unmounted, perms and CG deleted."""
        drv = self._make_driver()
        self._setup_happy_path_client(drv)
        mock_socket.return_value.getsockname.return_value = (
            '192.0.2.1', 0)
        mock_mkdtemp.side_effect = ['/tmp/snap_src', '/tmp/snap_dst']
        mock_exec.side_effect = [
            None, None,
            processutils.ProcessExecutionError('rsync failed'),
        ]
        snap = fakes.fake_snapshot()

        self.assertRaises(
            processutils.ProcessExecutionError,
            drv._copy_snapshot_nfs,
            self._new_share(), fakes.fake_snapshot_model(),
            snap, fakes.FAKE_FS_NAME, fakes.FAKE_NEW_FS_NAME)

        exec_cmds = [c[0][0] for c in mock_exec.call_args_list]
        self.assertEqual(2, exec_cmds.count('umount'))
        drv._client.delete_nfs_permission.assert_called()
        drv._client.delete_client_group.assert_called_once()

    # ── Cleanup resilience ───────────────────────────────────────────────

    @mock.patch(_PATCH_SLEEP)
    @mock.patch(_PATCH_SOCKET)
    @mock.patch(_PATCH_RMDIR)
    @mock.patch(_PATCH_MKDTEMP)
    @mock.patch(_PATCH_EXECUTE)
    def test_umount_failure_does_not_mask_rsync_exception(
            self, mock_exec, mock_mkdtemp, mock_rmdir,
            mock_socket, mock_sleep):
        """A umount error in the finally block must not hide the original.

        The rsync error is what the caller should see.
        """
        drv = self._make_driver()
        self._setup_happy_path_client(drv)
        mock_socket.return_value.getsockname.return_value = (
            '192.0.2.1', 0)
        mock_mkdtemp.side_effect = ['/tmp/snap_src', '/tmp/snap_dst']
        rsync_err = processutils.ProcessExecutionError('rsync failed')
        umount_err = processutils.ProcessExecutionError('umount failed')
        mock_exec.side_effect = [
            None, None, rsync_err, umount_err, umount_err]
        snap = fakes.fake_snapshot()

        with self.assertRaises(processutils.ProcessExecutionError) as cm:
            drv._copy_snapshot_nfs(
                self._new_share(), fakes.fake_snapshot_model(),
                snap, fakes.FAKE_FS_NAME, fakes.FAKE_NEW_FS_NAME)

        self.assertIs(rsync_err, cm.exception)

    @mock.patch(_PATCH_SLEEP)
    @mock.patch(_PATCH_SOCKET)
    @mock.patch(_PATCH_RMDIR)
    @mock.patch(_PATCH_MKDTEMP)
    @mock.patch(_PATCH_EXECUTE)
    def test_permission_delete_failure_does_not_raise_on_success(
            self, mock_exec, mock_mkdtemp, mock_rmdir,
            mock_socket, mock_sleep):
        """A permission cleanup failure must not propagate on success."""
        drv = self._make_driver()
        self._setup_happy_path_client(drv)
        mock_socket.return_value.getsockname.return_value = (
            '192.0.2.1', 0)
        mock_mkdtemp.side_effect = ['/tmp/snap_src', '/tmp/snap_dst']
        drv._client.delete_nfs_permission.side_effect = Exception(
            'API error during cleanup')
        snap = fakes.fake_snapshot()

        # Should not raise — copy succeeded
        drv._copy_snapshot_nfs(
            self._new_share(), fakes.fake_snapshot_model(),
            snap, fakes.FAKE_FS_NAME, fakes.FAKE_NEW_FS_NAME)


class TestWekaShareDriverDeleteShare(unittest.TestCase):

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        return drv

    def test_delete_share(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())
        drv._client.list_nfs_permissions.return_value = []

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=False):
            drv.delete_share(context=None, share=fakes.fake_share())

        drv._client.delete_filesystem.assert_called_once_with(
            fakes.FAKE_FS_UID)

    def test_delete_share_idempotent_when_not_found(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = None

        # Should not raise
        drv.delete_share(context=None, share=fakes.fake_share())
        drv._client.delete_filesystem.assert_not_called()

    def test_delete_share_removes_nfs_permissions(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())
        perm = fakes.fake_nfs_permission()
        drv._client.list_nfs_permissions.return_value = [perm]

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=False):
            drv.delete_share(context=None, share=fakes.fake_share())

        drv._client.delete_nfs_permission.assert_called_once_with(
            fakes.FAKE_PERM_UID)


class TestWekaShareDriverExtendShrink(unittest.TestCase):

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        return drv

    def test_extend_share(self):
        drv = self._make_driver()
        share = fakes.fake_share(size=10)
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())

        drv.extend_share(share, new_size=20)

        drv._client.update_filesystem.assert_called_once_with(
            fakes.FAKE_FS_UID,
            total_capacity=20 * 1024 ** 3,
        )

    def test_shrink_share_success(self):
        drv = self._make_driver()
        share = fakes.fake_share(size=10)
        # used = 1 GiB, shrinking to 5 GiB — OK
        fs = fakes.fake_filesystem(
            total_capacity=10 * 1024 ** 3,
            used_size_bytes=1 * 1024 ** 3,
        )
        drv._client.get_filesystem_by_name.return_value = fs
        drv._client.get_filesystem.return_value = fs

        drv.shrink_share(share, new_size=5)

        drv._client.update_filesystem.assert_called_once_with(
            fakes.FAKE_FS_UID,
            total_capacity=5 * 1024 ** 3,
        )

    def test_shrink_share_raises_when_used_gt_new_size(self):
        drv = self._make_driver()
        share = fakes.fake_share(size=10)
        # used = 8 GiB, trying to shrink to 5 GiB
        fs = fakes.fake_filesystem(
            total_capacity=10 * 1024 ** 3,
            used_size_bytes=8 * 1024 ** 3,
        )
        drv._client.get_filesystem_by_name.return_value = fs
        drv._client.get_filesystem.return_value = fs

        self.assertRaises(
            exception.ShareShrinkingPossibleDataLoss,
            drv.shrink_share, share, new_size=5,
        )


class TestWekaShareDriverSnapshots(unittest.TestCase):

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        return drv

    def test_create_snapshot(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())
        snap_model = fakes.fake_snapshot_model()

        drv.create_snapshot(context=None, snapshot=snap_model)

        drv._client.create_snapshot.assert_called_once()

    def test_delete_snapshot(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())
        snap = fakes.fake_snapshot()
        drv._client.get_snapshot_by_name.return_value = snap
        snap_model = fakes.fake_snapshot_model()

        drv.delete_snapshot(context=None, snapshot=snap_model)

        drv._client.delete_snapshot.assert_called_once_with(
            fakes.FAKE_SNAP_UID)

    def test_delete_snapshot_idempotent_when_not_found(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())
        drv._client.get_snapshot_by_name.return_value = None
        snap_model = fakes.fake_snapshot_model()

        # Should not raise
        drv.delete_snapshot(context=None, snapshot=snap_model)
        drv._client.delete_snapshot.assert_not_called()

    def test_delete_snapshot_idempotent_when_share_not_found(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = None
        # Share with no export metadata: _get_fs_uid_for_share falls back
        # to get_filesystem_by_name (returns None -> ShareNotFound).
        share_no_meta = fakes.fake_share(export_locations=[])
        snap_model = fakes.fake_snapshot_model()
        snap_model['share'] = share_no_meta

        # Should not raise
        drv.delete_snapshot(context=None, snapshot=snap_model)
        drv._client.delete_snapshot.assert_not_called()

    def test_revert_to_snapshot(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())
        snap = fakes.fake_snapshot()
        drv._client.get_snapshot_by_name.return_value = snap
        snap_model = fakes.fake_snapshot_model()

        drv.revert_to_snapshot(
            context=None, snapshot=snap_model,
            share_access_rules=[], snapshot_access_rules=[])

        drv._client.restore_snapshot.assert_called_once_with(
            fakes.FAKE_SNAP_UID, fakes.FAKE_FS_UID)

    def test_revert_to_snapshot_raises_when_not_found(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())
        drv._client.get_snapshot_by_name.return_value = None
        snap_model = fakes.fake_snapshot_model()

        self.assertRaises(
            exception.ShareSnapshotNotFound,
            drv.revert_to_snapshot,
            None, snap_model, [], [],
        )


class TestWekaShareDriverUpdateAccess(unittest.TestCase):

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        return drv

    def test_update_access_nfs_add_ip_rule(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())
        # No pre-existing client groups or permissions — new rule path.
        drv._client.list_client_groups.return_value = []
        drv._client.list_nfs_permissions.return_value = []
        drv._client.create_client_group.return_value = (
            fakes.fake_client_group())
        drv._client.add_client_group_rule.return_value = {}
        drv._client.create_nfs_permission.return_value = (
            fakes.fake_nfs_permission())

        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(access_type='ip',
                                      access_to='192.0.2.0/24')
        result = drv.update_access(
            context=None, share=share,
            access_rules=[], add_rules=[rule], delete_rules=[],
            update_rules=[],
        )

        drv._client.create_client_group.assert_called_once()
        drv._client.create_nfs_permission.assert_called_once()
        self.assertEqual('active', result[rule['access_id']]['state'])

    def test_update_access_nfs_full_sync(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())
        drv._client.list_client_groups.return_value = []
        drv._client.list_nfs_permissions.return_value = []
        drv._client.create_client_group.return_value = (
            fakes.fake_client_group())
        drv._client.add_client_group_rule.return_value = {}
        drv._client.create_nfs_permission.return_value = (
            fakes.fake_nfs_permission())

        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(access_type='ip',
                                      access_to='198.51.100.0/24')
        # Full sync: access_rules populated, add/delete/update empty
        drv.update_access(
            context=None, share=share,
            access_rules=[rule], add_rules=[], delete_rules=[],
            update_rules=[],
        )

        drv._client.create_nfs_permission.assert_called_once()

    def test_update_access_nfs_invalid_type_sets_error(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())

        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(access_type='user',
                                      access_to='bob')
        rule_id = rule['access_id']
        result = drv.update_access(
            context=None, share=share,
            access_rules=[], add_rules=[rule], delete_rules=[],
            update_rules=[],
        )

        self.assertIn(rule_id, result)
        self.assertEqual('error', result[rule_id]['state'])

    def test_update_access_wekafs_ip_rule_accepted_as_noop(self):
        """WEKAFS shares accept all access rules as no-op."""
        drv = self._make_driver()
        share = fakes.fake_share(proto='WEKAFS')
        rule = fakes.fake_access_rule(access_type='ip',
                                      access_to='10.0.0.1')
        result = drv.update_access(
            context=None, share=share,
            access_rules=[], add_rules=[rule], delete_rules=[],
            update_rules=[],
        )
        self.assertEqual('active', result[rule['access_id']]['state'])
        # No cluster API calls should be made
        drv._client.create_client_group.assert_not_called()
        drv._client.create_nfs_permission.assert_not_called()

    def test_update_access_wekafs_user_rule_accepted_as_noop(self):
        """WEKAFS shares accept all access rules as no-op."""
        drv = self._make_driver()
        share = fakes.fake_share(proto='WEKAFS')
        rule = fakes.fake_access_rule(access_type='user',
                                      access_to='bob')
        result = drv.update_access(
            context=None, share=share,
            access_rules=[], add_rules=[rule], delete_rules=[],
            update_rules=[],
        )
        self.assertEqual('active', result[rule['access_id']]['state'])

    def test_update_access_nfs_ipv6_rule_raises_invalid_access(self):
        """IPv6 ip rules on NFS shares raise InvalidShareAccess."""
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())
        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(
            access_type='ip', access_to='2001:db8::1')
        self.assertRaises(
            exception.InvalidShareAccess,
            drv.update_access,
            None, share,
            [], [rule], [], [],
        )


class TestWekaShareDriverStats(unittest.TestCase):

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        # Provide a stub for _update_share_stats super call
        drv._stats = {}
        return drv

    def test_update_share_stats_fields(self):
        drv = self._make_driver()
        cap = fakes.fake_capacity(
            total_bytes=100 * 1024 ** 3,
            used_bytes=30 * 1024 ** 3,
        )
        drv._client.get_capacity.return_value = cap

        captured = {}

        def _capture(stats):
            captured.update(stats)

        with mock.patch.object(
                weka_driver.driver.ShareDriver, '_update_share_stats',
                side_effect=_capture):
            drv._update_share_stats()

        self.assertEqual('WEKAFS_NFS', captured['storage_protocol'])
        self.assertIsInstance(captured['storage_protocol'], str)
        self.assertAlmostEqual(100.0, captured['total_capacity_gb'], places=0)
        self.assertAlmostEqual(70.0, captured['free_capacity_gb'], places=0)

    def test_update_share_stats_storage_protocol_is_underscore_joined(self):
        """storage_protocol must be an underscore-joined string.

        Manila's scheduler CapabilitiesFilter exact-matches the reported
        storage_protocol string against the share type's
        capability_storage_protocol extra-spec (both must be "WEKAFS_NFS").
        manila-tempest-plugin's ShareMultiBackendTest also calls
        storage_protocol.lower().split('_'), which requires a string — a
        Python list would raise AttributeError on .lower().
        """
        drv = self._make_driver()
        drv._client.get_capacity.return_value = fakes.fake_capacity()

        captured = {}

        def _capture(stats):
            captured.update(stats)

        with mock.patch.object(
                weka_driver.driver.ShareDriver, '_update_share_stats',
                side_effect=_capture):
            drv._update_share_stats()

        proto = captured['storage_protocol']
        self.assertIsInstance(proto, str,
                              "storage_protocol must be a string, not a list")
        self.assertEqual('WEKAFS_NFS', proto)

    def test_update_share_stats_handles_api_error(self):
        drv = self._make_driver()
        drv._client.get_capacity.side_effect = Exception("API down")

        with mock.patch.object(
                weka_driver.driver.ShareDriver, '_update_share_stats'):
            # Should not raise; falls back to zeros.
            drv._update_share_stats()

    def test_update_share_stats_reserved_percentage(self):
        """reserved_percentage comes from config, not hardcoded 0."""
        drv = self._make_driver()
        drv.configuration = _make_config(reserved_percentage=5)
        drv._client.get_capacity.return_value = fakes.fake_capacity()

        captured = {}

        def _capture(stats):
            captured.update(stats)

        with mock.patch.object(
                weka_driver.driver.ShareDriver,
                '_update_share_stats',
                side_effect=_capture):
            drv._update_share_stats()

        self.assertEqual(5, captured['reserved_percentage'])


class TestWekaShareDriverManage(unittest.TestCase):

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        return drv

    def test_manage_existing_success(self):
        drv = self._make_driver()
        fs = fakes.fake_filesystem(total_capacity=20 * 1024 ** 3)
        drv._client.get_filesystem_by_name.return_value = fs

        share = fakes.fake_share()
        result = drv.manage_existing(share, driver_options={})

        self.assertIn('size', result)
        self.assertEqual(20, result['size'])

    def test_manage_existing_not_found(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = None

        share = fakes.fake_share()
        self.assertRaises(
            exception.ManageInvalidShare,
            drv.manage_existing, share, {},
        )

    def test_unmanage_does_not_delete(self):
        drv = self._make_driver()
        drv.unmanage(share=fakes.fake_share())
        drv._client.delete_filesystem.assert_not_called()

    def test_manage_existing_calls_remove_all_nfs_permissions(self):
        drv = self._make_driver()
        fs = fakes.fake_filesystem(total_capacity=20 * 1024 ** 3)
        drv._client.get_filesystem_by_name.return_value = fs

        share = fakes.fake_share()
        with mock.patch.object(
                drv, '_remove_all_nfs_permissions') as mock_rm:
            drv.manage_existing(share, driver_options={})
        mock_rm.assert_called_once()


class TestWekaShareDriverMiscellaneous(unittest.TestCase):

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        return drv

    def test_get_network_allocations_number(self):
        drv = self._make_driver()
        self.assertEqual(0, drv.get_network_allocations_number())

    def test_share_name_uses_prefix(self):
        drv = self._make_driver()
        # Hyphens are stripped from the share UUID before appending.
        name = drv._share_name('my-uuid')
        self.assertEqual('manila_myuuid', name)

    def test_snapshot_name(self):
        drv = self._make_driver()
        # Driver uses 's_' prefix and strips hyphens from the snapshot UUID.
        name = drv._snapshot_name('snap-uuid')
        self.assertEqual('s_snapuuid', name)

    def test_mount_point(self):
        drv = self._make_driver()
        mp = drv._mount_point('manila_my-uuid')
        self.assertEqual('/mnt/weka/manila_my-uuid', mp)

    def test_ensure_share_re_mounts_if_not_mounted(self):
        drv = self._make_driver()
        fs = fakes.fake_filesystem()
        drv._client.get_filesystem_by_name.return_value = fs

        share = fakes.fake_share(proto='WEKAFS')

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=False):
            with mock.patch.object(
                    weka_posix.WekaMount, 'mount') as mock_mnt:
                drv._ensure_share(context=None, share=share)
        mock_mnt.assert_called_once()

    def test_ensure_share_not_found_raises(self):
        drv = self._make_driver()
        drv._client.get_filesystem_by_name.return_value = None
        share = fakes.fake_share()
        self.assertRaises(
            exception.ShareNotFound,
            drv._ensure_share, None, share,
        )

    def test_get_backend_info(self):
        drv = self._make_driver()
        result = drv.get_backend_info(context=None)
        self.assertEqual(
            'weka-test.example.com', result['weka_api_server'])
        self.assertEqual(
            '/mnt/weka', result['weka_mount_point_base'])


class TestWekaShareDriverNFSHelpers(unittest.TestCase):
    """Tests for NFS permission helpers and internal utility methods."""

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        return drv

    # ------------------------------------------------------------------
    # _remove_nfs_rule
    # ------------------------------------------------------------------

    def test_remove_nfs_rule_removes_matching_permission(self):
        drv = self._make_driver()
        rule_id = 'abcdefgh-1234-5678-0000-111111111111'
        cg_name = 'manila-shareuui-abcdefgh'
        # cg_name embeds the first 8 chars of the rule access_id.
        perm = fakes.fake_nfs_permission(
            fs_name=fakes.FAKE_FS_NAME,
            cg_name=cg_name,
        )
        cg = fakes.fake_client_group(name=cg_name)
        drv._client.list_nfs_permissions.return_value = [perm]
        drv._client.list_client_groups.return_value = [cg]
        rule = fakes.fake_access_rule(rule_id=rule_id)

        drv._remove_nfs_rule(fakes.FAKE_FS_NAME, rule)

        drv._client.delete_nfs_permission.assert_called_once_with(
            fakes.FAKE_PERM_UID)
        drv._client.delete_client_group.assert_called_once_with(
            fakes.FAKE_CG_UID)

    def test_remove_nfs_rule_skips_different_filesystem(self):
        drv = self._make_driver()
        rule_id = 'abcdefgh-1234-5678-0000-111111111111'
        perm = fakes.fake_nfs_permission(
            fs_name='other-filesystem',
            cg_name='manila-shareuui-abcdefgh',
        )
        drv._client.list_nfs_permissions.return_value = [perm]
        drv._client.list_client_groups.return_value = []
        rule = fakes.fake_access_rule(rule_id=rule_id)

        drv._remove_nfs_rule(fakes.FAKE_FS_NAME, rule)

        drv._client.delete_nfs_permission.assert_not_called()

    def test_remove_nfs_rule_skips_non_matching_rule_id(self):
        drv = self._make_driver()
        rule_id = 'abcdefgh-1234-5678-0000-111111111111'
        # cg_name does NOT contain the first 8 chars of rule_id.
        perm = fakes.fake_nfs_permission(
            fs_name=fakes.FAKE_FS_NAME,
            cg_name='manila-shareuui-xxxxxxxx',
        )
        drv._client.list_nfs_permissions.return_value = [perm]
        drv._client.list_client_groups.return_value = []
        rule = fakes.fake_access_rule(rule_id=rule_id)

        drv._remove_nfs_rule(fakes.FAKE_FS_NAME, rule)

        drv._client.delete_nfs_permission.assert_not_called()

    def test_remove_nfs_rule_empty_permissions(self):
        drv = self._make_driver()
        drv._client.list_nfs_permissions.return_value = []
        drv._client.list_client_groups.return_value = []
        rule = fakes.fake_access_rule()

        # Should not raise, nothing to delete.
        drv._remove_nfs_rule(fakes.FAKE_FS_NAME, rule)
        drv._client.delete_nfs_permission.assert_not_called()

    # ------------------------------------------------------------------
    # _remove_all_nfs_permissions
    # ------------------------------------------------------------------

    def test_remove_all_nfs_permissions_deletes_matching(self):
        drv = self._make_driver()
        perm1 = fakes.fake_nfs_permission(
            uid='perm-uid-0001', fs_name=fakes.FAKE_FS_NAME,
            cg_name='manila-share111-rule1111')
        perm2 = fakes.fake_nfs_permission(
            uid='perm-uid-0002', fs_name=fakes.FAKE_FS_NAME,
            cg_name='manila-share222-rule2222')
        perm_other = fakes.fake_nfs_permission(
            uid='perm-uid-0003', fs_name='other-filesystem')
        drv._client.list_nfs_permissions.return_value = [
            perm1, perm2, perm_other]
        # No pre-existing client groups (simplifies the delete path).
        drv._client.list_client_groups.return_value = []

        drv._remove_all_nfs_permissions(fakes.FAKE_FS_NAME)

        self.assertEqual(2, drv._client.delete_nfs_permission.call_count)
        drv._client.delete_nfs_permission.assert_any_call('perm-uid-0001')
        drv._client.delete_nfs_permission.assert_any_call('perm-uid-0002')

    def test_remove_all_nfs_permissions_empty(self):
        drv = self._make_driver()
        drv._client.list_nfs_permissions.return_value = []
        drv._client.list_client_groups.return_value = []

        # Should not raise.
        drv._remove_all_nfs_permissions(fakes.FAKE_FS_NAME)
        drv._client.delete_nfs_permission.assert_not_called()

    def test_remove_all_nfs_permissions_silences_not_found(self):
        drv = self._make_driver()
        perm = fakes.fake_nfs_permission(fs_name=fakes.FAKE_FS_NAME)
        drv._client.list_nfs_permissions.return_value = [perm]
        drv._client.list_client_groups.return_value = []
        from manila.share.drivers.weka import exceptions as weka_exc
        drv._client.delete_nfs_permission.side_effect = (
            weka_exc.WekaNotFound(reason='already gone'))

        # WekaNotFound should be swallowed, not re-raised.
        drv._remove_all_nfs_permissions(fakes.FAKE_FS_NAME)

    # ------------------------------------------------------------------
    # _apply_nfs_rule / idempotency / access-level update
    # ------------------------------------------------------------------

    def test_apply_nfs_rule_returns_active_state(self):
        """add path returns {'state':'active'} for a valid ip rule."""
        drv = self._make_driver()
        drv._client.list_client_groups.return_value = []
        drv._client.list_nfs_permissions.return_value = []
        drv._client.create_client_group.return_value = (
            fakes.fake_client_group())

        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(
            access_type='ip', access_to='10.1.1.1')
        result = drv._update_nfs_access(share, [rule], [])

        self.assertEqual('active', result[rule['access_id']]['state'])
        drv._client.create_client_group.assert_called_once()
        drv._client.create_nfs_permission.assert_called_once()

    def test_apply_nfs_rule_reuses_existing_client_group(self):
        """add path REUSES existing CG — create_client_group not called."""
        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(
            access_type='ip', access_to='10.2.2.2')
        cg_name = 'manila-{}-{}'.format(
            share['id'][:8], rule['access_id'][:8])
        cg = fakes.fake_client_group(name=cg_name)

        drv = self._make_driver()
        # CG already exists; get_client_group returns no existing IP rules.
        drv._client.list_client_groups.return_value = [cg]
        drv._client.get_client_group.return_value = (
            fakes.fake_client_group_detail(
                uid=cg['uid'], name=cg_name, rules=[]))
        drv._client.list_nfs_permissions.return_value = []

        result = drv._update_nfs_access(share, [rule], [])

        drv._client.create_client_group.assert_not_called()
        drv._client.add_client_group_rule.assert_called_once()
        self.assertEqual('active', result[rule['access_id']]['state'])

    def test_apply_nfs_rule_idempotent_existing_ip(self):
        """Re-add skips add_client_group_rule when the IP already exists."""
        from manila.share.drivers.weka.driver import _cidr_to_weka_ip
        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(
            access_type='ip', access_to='10.3.3.3')
        cg_name = 'manila-{}-{}'.format(
            share['id'][:8], rule['access_id'][:8])
        cg = fakes.fake_client_group(name=cg_name)
        weka_ip = _cidr_to_weka_ip('10.3.3.3')

        drv = self._make_driver()
        drv._client.list_client_groups.return_value = [cg]
        # get_client_group returns the IP already present.
        drv._client.get_client_group.return_value = (
            fakes.fake_client_group_detail(
                uid=cg['uid'], name=cg_name,
                rules=[{'ip': weka_ip}]))
        drv._client.list_nfs_permissions.return_value = []

        drv._update_nfs_access(share, [rule], [])

        drv._client.add_client_group_rule.assert_not_called()

    def test_apply_nfs_rule_access_level_change(self):
        """access-level change: existing RO permission recreated as RW."""
        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(
            access_type='ip', access_to='10.4.4.4', access_level='rw')
        cg_name = 'manila-{}-{}'.format(
            share['id'][:8], rule['access_id'][:8])
        cg = fakes.fake_client_group(name=cg_name)
        from manila.share.drivers.weka.driver import _cidr_to_weka_ip
        weka_ip = _cidr_to_weka_ip('10.4.4.4')

        # Existing permission is RO; rule requests RW.
        perm = fakes.fake_nfs_permission(
            fs_name=fakes.FAKE_FS_NAME,
            cg_name=cg_name,
            permission_type='RO')

        drv = self._make_driver()
        drv._client.list_client_groups.return_value = [cg]
        drv._client.get_client_group.return_value = (
            fakes.fake_client_group_detail(
                uid=cg['uid'], name=cg_name,
                rules=[{'ip': weka_ip}]))
        drv._client.list_nfs_permissions.return_value = [perm]

        drv._update_nfs_access(share, [rule], [])

        drv._client.delete_nfs_permission.assert_called_once_with(
            fakes.FAKE_PERM_UID)
        drv._client.create_nfs_permission.assert_called_once()
        _, kwargs = drv._client.create_nfs_permission.call_args
        self.assertEqual('RW', kwargs.get('access_type'))

    def test_update_rules_path_applied_not_ignored(self):
        """update_rules path: rule reaches 'active', not full-sync."""
        drv = self._make_driver()
        drv._client.list_client_groups.return_value = []
        drv._client.list_nfs_permissions.return_value = []
        drv._client.create_client_group.return_value = (
            fakes.fake_client_group())

        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(
            access_type='ip', access_to='10.5.5.5', access_level='rw')
        result = drv.update_access(
            context=None, share=share,
            access_rules=[],
            add_rules=[],
            delete_rules=[],
            update_rules=[rule],
        )

        self.assertEqual('active', result[rule['access_id']]['state'])
        drv._client.create_client_group.assert_called_once()

    def test_remove_nfs_rule_leak_fix_deletes_client_group(self):
        """LEAK FIX on rule delete: permission AND client group deleted."""
        rule_id = 'abcdefgh-1234-5678-0000-222222222222'
        cg_name = 'manila-shareuui-abcdefgh'
        cg = fakes.fake_client_group(
            uid='cg-uid-leak', name=cg_name)
        perm = fakes.fake_nfs_permission(
            uid='perm-uid-leak',
            fs_name=fakes.FAKE_FS_NAME,
            cg_name=cg_name)

        drv = self._make_driver()
        drv._client.list_nfs_permissions.return_value = [perm]
        drv._client.list_client_groups.return_value = [cg]
        rule = fakes.fake_access_rule(rule_id=rule_id)

        drv._remove_nfs_rule(fakes.FAKE_FS_NAME, rule)

        drv._client.delete_nfs_permission.assert_called_once_with(
            'perm-uid-leak')
        drv._client.delete_client_group.assert_called_once_with(
            'cg-uid-leak')

    def test_remove_all_nfs_permissions_leak_fix_deletes_client_groups(self):
        """Share delete removes both permissions and client groups."""
        cg1_name = 'manila-share111-rule1111'
        cg2_name = 'manila-share222-rule2222'
        perm1 = fakes.fake_nfs_permission(
            uid='perm-uid-1', fs_name=fakes.FAKE_FS_NAME,
            cg_name=cg1_name)
        perm2 = fakes.fake_nfs_permission(
            uid='perm-uid-2', fs_name=fakes.FAKE_FS_NAME,
            cg_name=cg2_name)
        cg1 = fakes.fake_client_group(uid='cg-uid-1', name=cg1_name)
        cg2 = fakes.fake_client_group(uid='cg-uid-2', name=cg2_name)

        drv = self._make_driver()
        drv._client.list_nfs_permissions.return_value = [perm1, perm2]
        drv._client.list_client_groups.return_value = [cg1, cg2]

        drv._remove_all_nfs_permissions(fakes.FAKE_FS_NAME)

        self.assertEqual(2, drv._client.delete_nfs_permission.call_count)
        self.assertEqual(2, drv._client.delete_client_group.call_count)
        drv._client.delete_client_group.assert_any_call('cg-uid-1')
        drv._client.delete_client_group.assert_any_call('cg-uid-2')

    def test_apply_nfs_rule_tolerates_existing_ip_rule(self):
        """Tolerate 'Rule already exists' on re-add; ro->rw still runs."""
        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(
            access_type='ip', access_to='2.2.2.2', access_level='rw')
        cg_name = 'manila-{}-{}'.format(
            share['id'][:8], rule['access_id'][:8])
        cg = fakes.fake_client_group(name=cg_name)
        # Existing permission is RO; rule requests RW — reconcile must run.
        perm = fakes.fake_nfs_permission(
            fs_name=fakes.FAKE_FS_NAME,
            cg_name=cg_name,
            permission_type='RO')

        drv = self._make_driver()
        drv._client.list_client_groups.return_value = [cg]
        # existing_ips set is empty (normalized form not matched locally).
        drv._client.get_client_group.return_value = (
            fakes.fake_client_group_detail(
                uid=cg['uid'], name=cg_name, rules=[]))
        drv._client.list_nfs_permissions.return_value = [perm]
        # Weka returns 400 "Rule already exists" on the add call.
        drv._client.add_client_group_rule.side_effect = (
            weka_exc.WekaApiError(
                status_code=400,
                reason='/nfs/clientGroups/x/rules: Rule already exists'))

        # Must not raise; result for this rule must be 'active'.
        result = drv._update_nfs_access(share, [rule], [])

        self.assertEqual('active', result[rule['access_id']]['state'])
        # Permission reconcile (ro->rw) must have run despite the add error.
        drv._client.delete_nfs_permission.assert_called_once_with(
            fakes.FAKE_PERM_UID)
        drv._client.create_nfs_permission.assert_called_once()
        _, kwargs = drv._client.create_nfs_permission.call_args
        self.assertEqual('RW', kwargs.get('access_type'))

    def test_apply_nfs_rule_reraises_other_add_errors(self):
        """Non-benign add errors (not 'already exists') are re-raised."""
        share = fakes.fake_share(proto='NFS')
        rule = fakes.fake_access_rule(
            access_type='ip', access_to='3.3.3.3')
        cg_name = 'manila-{}-{}'.format(
            share['id'][:8], rule['access_id'][:8])
        cg = fakes.fake_client_group(name=cg_name)

        drv = self._make_driver()
        drv._client.list_client_groups.return_value = [cg]
        drv._client.get_client_group.return_value = (
            fakes.fake_client_group_detail(
                uid=cg['uid'], name=cg_name, rules=[]))
        drv._client.list_nfs_permissions.return_value = []
        drv._client.add_client_group_rule.side_effect = (
            weka_exc.WekaApiError(
                status_code=400,
                reason='bad request: invalid something'))

        result = drv._update_nfs_access(share, [rule], [])

        self.assertEqual('error', result[rule['access_id']]['state'])

    # ------------------------------------------------------------------
    # _get_backends
    # ------------------------------------------------------------------

    def test_get_backends_returns_api_server(self):
        drv = self._make_driver()
        self.assertEqual('weka-test.example.com', drv._get_backends())

    def test_get_backends_empty_when_not_configured(self):
        drv = self._make_driver()
        drv.configuration = _make_config(weka_api_server=None)
        self.assertEqual('', drv._get_backends())

    # ------------------------------------------------------------------
    # _build_export_locations
    # ------------------------------------------------------------------

    def test_build_export_locations_nfs_uses_api_server_by_default(self):
        drv = self._make_driver()
        share = fakes.fake_share(proto='NFS')
        result = drv._build_export_locations(
            share, fakes.FAKE_FS_NAME, fakes.FAKE_FS_UID, 'NFS')
        self.assertEqual(
            'weka-test.example.com:/{}'.format(fakes.FAKE_FS_NAME),
            result[0]['path'],
        )

    def test_build_export_locations_nfs_uses_nfs_server_when_set(self):
        drv = self._make_driver()
        drv.configuration = _make_config(
            weka_nfs_server='nfs-lb.example.com')
        share = fakes.fake_share(proto='NFS')
        result = drv._build_export_locations(
            share, fakes.FAKE_FS_NAME, fakes.FAKE_FS_UID, 'NFS')
        self.assertEqual(
            'nfs-lb.example.com:/{}'.format(fakes.FAKE_FS_NAME),
            result[0]['path'],
        )

    def test_build_export_locations_wekafs_uses_api_server(self):
        drv = self._make_driver()
        drv.configuration = _make_config(
            weka_nfs_server='nfs-lb.example.com')
        share = fakes.fake_share(proto='WEKAFS')
        result = drv._build_export_locations(
            share, fakes.FAKE_FS_NAME, fakes.FAKE_FS_UID, 'WEKAFS')
        # WEKAFS path must use API server, not the NFS server
        self.assertIn('weka-test.example.com', result[0]['path'])
        self.assertNotIn('nfs-lb.example.com', result[0]['path'])

    # ------------------------------------------------------------------
    # _get_fs_uid_for_share
    # ------------------------------------------------------------------

    def test_get_fs_uid_from_export_metadata(self):
        drv = self._make_driver()
        share = fakes.fake_share()  # has weka_fs_uid in export metadata

        uid = drv._get_fs_uid_for_share(share)

        self.assertEqual(fakes.FAKE_FS_UID, uid)
        drv._client.get_filesystem_by_name.assert_not_called()

    def test_get_fs_uid_falls_back_to_api(self):
        drv = self._make_driver()
        # Share with no export_locations — must fall back to API lookup.
        share = fakes.fake_share(export_locations=[])
        drv._client.get_filesystem_by_name.return_value = (
            fakes.fake_filesystem())

        uid = drv._get_fs_uid_for_share(share)

        self.assertEqual(fakes.FAKE_FS_UID, uid)
        drv._client.get_filesystem_by_name.assert_called_once()

    def test_get_fs_uid_raises_when_not_found(self):
        drv = self._make_driver()
        share = fakes.fake_share(export_locations=[])
        drv._client.get_filesystem_by_name.return_value = None

        from manila import exception
        self.assertRaises(
            exception.ShareNotFound,
            drv._get_fs_uid_for_share, share,
        )


class TestCidrToWekaIp(unittest.TestCase):
    """Tests for the _cidr_to_weka_ip module-level helper."""

    def test_cidr_prefix_converted_to_dotted_mask(self):
        result = weka_driver._cidr_to_weka_ip('192.168.1.0/24')
        self.assertEqual('192.168.1.0/255.255.255.0', result)

    def test_single_ip_unchanged(self):
        result = weka_driver._cidr_to_weka_ip('10.0.0.5')
        self.assertEqual('10.0.0.5', result)

    def test_slash_zero_all_hosts(self):
        result = weka_driver._cidr_to_weka_ip('0.0.0.0/0')
        self.assertEqual('0.0.0.0/0.0.0.0', result)

    def test_slash_32_single_host(self):
        result = weka_driver._cidr_to_weka_ip('10.1.2.3/32')
        self.assertEqual('10.1.2.3/255.255.255.255', result)

    def test_host_bits_set_normalised(self):
        # strict=False — host bits are masked off.
        result = weka_driver._cidr_to_weka_ip('192.168.1.5/24')
        self.assertEqual('192.168.1.0/255.255.255.0', result)

    def test_invalid_input_raises_value_error(self):
        self.assertRaises(
            ValueError,
            weka_driver._cidr_to_weka_ip, 'not-an-ip/24')


class TestWekaShareDriverGetShareStatus(unittest.TestCase):
    """Tests for WekaShareDriver.get_share_status."""

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        drv._async_copies = {}
        drv._async_copies_lock = threading.Lock()
        return drv

    def test_get_share_status_creating(self):
        drv = self._make_driver()
        share = fakes.fake_share()
        drv._async_copies[share['id']] = {
            'status': constants.STATUS_CREATING_FROM_SNAPSHOT,
            'fs_uid': fakes.FAKE_FS_UID,
            'fs_name': fakes.FAKE_FS_NAME,
        }

        result = drv.get_share_status(context=None, share=share)

        self.assertEqual(
            constants.STATUS_CREATING_FROM_SNAPSHOT,
            result['status'])

    def test_get_share_status_available(self):
        drv = self._make_driver()
        share = fakes.fake_share()
        drv._async_copies[share['id']] = {
            'status': constants.STATUS_AVAILABLE,
            'fs_uid': fakes.FAKE_FS_UID,
            'fs_name': fakes.FAKE_FS_NAME,
        }
        # No API call expected — fs_uid/fs_name come from the dict.

        result = drv.get_share_status(context=None, share=share)

        self.assertEqual(constants.STATUS_AVAILABLE, result['status'])
        self.assertIn('export_locations', result)
        drv._client.get_filesystem_by_name.assert_not_called()

    def test_get_share_status_error(self):
        drv = self._make_driver()
        share = fakes.fake_share()
        drv._async_copies[share['id']] = {
            'status': constants.STATUS_ERROR,
            'fs_uid': fakes.FAKE_FS_UID,
            'fs_name': fakes.FAKE_FS_NAME,
        }

        result = drv.get_share_status(context=None, share=share)

        self.assertEqual(constants.STATUS_ERROR, result['status'])

    def test_get_share_status_missing_key(self):
        """Key absent (process restart): return available with warning."""
        drv = self._make_driver()
        share = fakes.fake_share()
        # _async_copies is empty — simulates process restart

        result = drv.get_share_status(context=None, share=share)

        self.assertEqual(constants.STATUS_AVAILABLE, result['status'])


class TestWekaShareDriverEnsureShares(unittest.TestCase):
    """Tests for WekaShareDriver.ensure_shares."""

    def _make_driver(self):
        drv = weka_driver.WekaShareDriver.__new__(weka_driver.WekaShareDriver)
        drv.configuration = _make_config()
        drv._client = mock.Mock()
        drv._fs_group_uid = fakes.FAKE_GROUP_UID
        return drv

    def test_ensure_shares_happy_path(self):
        drv = self._make_driver()
        share = fakes.fake_share(proto='NFS')
        fs = fakes.fake_filesystem()
        # ensure_shares now calls list_filesystems() once.
        drv._client.list_filesystems.return_value = [fs]

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=True):
            result = drv.ensure_shares(context=None, shares=[share])

        self.assertIn(share['id'], result)
        self.assertIn('export_locations', result[share['id']])
        drv._client.list_filesystems.assert_called_once()
        # No per-share get_filesystem_by_name call expected.
        drv._client.get_filesystem_by_name.assert_not_called()

    def test_ensure_shares_not_found_returns_error(self):
        drv = self._make_driver()
        share = fakes.fake_share()
        # Filesystem not in the list — should map to STATUS_ERROR.
        drv._client.list_filesystems.return_value = []

        result = drv.ensure_shares(context=None, shares=[share])

        self.assertIn(share['id'], result)
        self.assertEqual(
            constants.STATUS_ERROR,
            result[share['id']]['status'])


if __name__ == '__main__':
    unittest.main()
