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

"""Unit tests for manila.share.drivers.weka.posix."""

import unittest
from unittest import mock

from oslo_concurrency import processutils

from manila.share.drivers.weka import exceptions as weka_exc
from manila.share.drivers.weka import posix as weka_posix


_BACKENDS = '10.0.0.1,10.0.0.2'
_FS_NAME = 'manila_test-fs'
_MOUNT_POINT = '/mnt/weka/manila_test-fs'


def _make_mount(**kwargs):
    defaults = dict(
        backends=_BACKENDS,
        fs_name=_FS_NAME,
        mount_point=_MOUNT_POINT,
        execute=mock.Mock(return_value=('', '')),
    )
    defaults.update(kwargs)
    return weka_posix.WekaMount(**defaults)


class TestWekaMountBuildOptions(unittest.TestCase):

    def test_default_options(self):
        m = _make_mount(num_cores=1)
        opts = m._build_mount_options()
        self.assertIn('num_cores=1', opts)
        self.assertNotIn('mount_token=', ' '.join(opts))
        self.assertNotIn('net=', ' '.join(opts))

    def test_mount_token_included_when_set(self):
        m = _make_mount(mount_token='abc123')
        opts = m._build_mount_options()
        self.assertIn('mount_token=abc123', opts)

    def test_net_included_when_set(self):
        m = _make_mount(net='eth0')
        opts = m._build_mount_options()
        self.assertIn('net=eth0', opts)

    def test_writecache_option(self):
        m = _make_mount(writecache=True)
        opts = m._build_mount_options()
        self.assertIn('writecache', opts)

    def test_sync_on_close_option(self):
        m = _make_mount(sync_on_close=True)
        opts = m._build_mount_options()
        self.assertIn('sync_on_close', opts)

    def test_readcache_off(self):
        m = _make_mount(read_cache=False)
        opts = m._build_mount_options()
        self.assertIn('readcache=off', opts)

    def test_iops_limit(self):
        m = _make_mount(iops_limit=10000)
        opts = m._build_mount_options()
        self.assertIn('iops_limit=10000', opts)

    def test_max_io_size(self):
        m = _make_mount(max_io_size=131072)
        opts = m._build_mount_options()
        self.assertIn('max_io_size=131072', opts)

    def test_num_cores_in_options(self):
        m = _make_mount(num_cores=4)
        opts = m._build_mount_options()
        self.assertIn('num_cores=4', opts)


class TestWekaMountIsMount(unittest.TestCase):

    def test_is_mounted_true(self):
        proc_content = (
            '10.0.0.1/my_fs /mnt/weka/my_fs wekafs rw 0 0\n'
        )
        mock_open = mock.mock_open(read_data=proc_content)
        with mock.patch('builtins.open', mock_open):
            result = weka_posix.WekaMount.is_mounted('/mnt/weka/my_fs')
        self.assertTrue(result)

    def test_is_mounted_false_different_path(self):
        proc_content = (
            '10.0.0.1/other_fs /mnt/weka/other_fs wekafs rw 0 0\n'
        )
        mock_open = mock.mock_open(read_data=proc_content)
        with mock.patch('builtins.open', mock_open):
            result = weka_posix.WekaMount.is_mounted('/mnt/weka/my_fs')
        self.assertFalse(result)

    def test_is_mounted_false_wrong_fstype(self):
        proc_content = (
            'tmpfs /mnt/weka/my_fs tmpfs rw 0 0\n'
        )
        mock_open = mock.mock_open(read_data=proc_content)
        with mock.patch('builtins.open', mock_open):
            result = weka_posix.WekaMount.is_mounted('/mnt/weka/my_fs')
        self.assertFalse(result)

    def test_is_mounted_ioerror_returns_false(self):
        with mock.patch('builtins.open', side_effect=IOError):
            result = weka_posix.WekaMount.is_mounted('/mnt/weka/any')
        self.assertFalse(result)


class TestWekaMountMount(unittest.TestCase):

    def test_mount_calls_correct_command(self):
        execute = mock.Mock(return_value=('', ''))
        m = _make_mount(execute=execute, num_cores=2)

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=False):
            with mock.patch('os.path.isdir', return_value=True):
                m.mount()

        execute.assert_called_once()
        args = execute.call_args[0]
        self.assertEqual('mount', args[0])
        self.assertIn('-t', args)
        self.assertIn('wekafs', args)
        source = '{}/{}'.format(_BACKENDS, _FS_NAME)
        self.assertIn(source, args)
        self.assertIn(_MOUNT_POINT, args)

    def test_mount_idempotent_when_already_mounted(self):
        execute = mock.Mock(return_value=('', ''))
        m = _make_mount(execute=execute)

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=True):
            m.mount()

        execute.assert_not_called()

    def test_mount_raises_on_process_error(self):
        execute = mock.Mock(
            side_effect=processutils.ProcessExecutionError('fail'))
        m = _make_mount(execute=execute)

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=False):
            with mock.patch('os.path.isdir', return_value=True):
                self.assertRaises(weka_exc.WekaMountError, m.mount)

    def test_mount_creates_mount_point_dir(self):
        execute = mock.Mock(return_value=('', ''))
        m = _make_mount(execute=execute)

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=False):
            with mock.patch('os.path.isdir', return_value=False):
                with mock.patch('os.makedirs') as makedirs:
                    m.mount()
        makedirs.assert_called_once_with(_MOUNT_POINT, exist_ok=True)


class TestWekaMountUnmount(unittest.TestCase):

    def test_unmount_calls_umount(self):
        execute = mock.Mock(return_value=('', ''))
        m = _make_mount(execute=execute)

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=True):
            m.unmount()

        execute.assert_called_once()
        args = execute.call_args[0]
        self.assertEqual('umount', args[0])
        self.assertIn(_MOUNT_POINT, args)

    def test_unmount_lazy_uses_l_flag(self):
        execute = mock.Mock(return_value=('', ''))
        m = _make_mount(execute=execute)

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=True):
            m.unmount(force=True)

        args = execute.call_args[0]
        self.assertIn('-l', args)

    def test_unmount_noop_when_not_mounted(self):
        execute = mock.Mock(return_value=('', ''))
        m = _make_mount(execute=execute)

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=False):
            m.unmount()

        execute.assert_not_called()

    def test_unmount_raises_on_process_error(self):
        execute = mock.Mock(
            side_effect=processutils.ProcessExecutionError('fail'))
        m = _make_mount(execute=execute)

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               return_value=True):
            self.assertRaises(weka_exc.WekaUnmountError, m.unmount)


class TestWekaMountContextManager(unittest.TestCase):

    def test_context_manager_mounts_and_unmounts(self):
        execute = mock.Mock(return_value=('', ''))
        m = _make_mount(execute=execute)

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               side_effect=[False, True]):
            with mock.patch('os.path.isdir', return_value=True):
                with m:
                    pass

        # mount() + unmount() each call execute once
        self.assertEqual(2, execute.call_count)

    def test_context_manager_unmounts_even_on_exception(self):
        execute = mock.Mock(return_value=('', ''))
        m = _make_mount(execute=execute)

        with mock.patch.object(weka_posix.WekaMount, 'is_mounted',
                               side_effect=[False, True]):
            with mock.patch('os.path.isdir', return_value=True):
                try:
                    with m:
                        raise ValueError("test error")
                except ValueError:
                    pass

        self.assertEqual(2, execute.call_count)


class TestWekaMountSharePath(unittest.TestCase):

    def test_get_or_create_share_path_creates_dir(self):
        m = _make_mount()
        with mock.patch('os.path.isdir', return_value=False):
            with mock.patch('os.makedirs') as makedirs:
                with mock.patch('os.chmod'):
                    path = m.get_or_create_share_path(
                        '/mnt/weka/fs', 'my-share')
        self.assertEqual('/mnt/weka/fs/my-share', path)
        makedirs.assert_called_once()

    def test_get_or_create_share_path_existing_dir(self):
        m = _make_mount()
        with mock.patch('os.path.isdir', return_value=True):
            with mock.patch('os.chmod') as chmod:
                path = m.get_or_create_share_path(
                    '/mnt/weka/fs', 'my-share')
        self.assertEqual('/mnt/weka/fs/my-share', path)
        chmod.assert_called_once()

    def test_get_or_create_share_path_strips_leading_slash(self):
        m = _make_mount()
        with mock.patch('os.path.isdir', return_value=True):
            with mock.patch('os.chmod'):
                path = m.get_or_create_share_path(
                    '/mnt/weka/fs', '/my-share')
        self.assertEqual('/mnt/weka/fs/my-share', path)

    def test_remove_share_path_removes_dir(self):
        m = _make_mount()
        with mock.patch('os.path.exists', return_value=True):
            with mock.patch('os.rmdir') as rmdir:
                m.remove_share_path('/mnt/weka/fs', 'my-share')
        rmdir.assert_called_once_with('/mnt/weka/fs/my-share')

    def test_remove_share_path_noop_if_not_exists(self):
        m = _make_mount()
        with mock.patch('os.path.exists', return_value=False):
            with mock.patch('os.rmdir') as rmdir:
                m.remove_share_path('/mnt/weka/fs', 'my-share')
        rmdir.assert_not_called()

    def test_remove_share_path_force_uses_shutil(self):
        m = _make_mount()
        with mock.patch('os.path.exists', return_value=True):
            with mock.patch('shutil.rmtree') as rmtree:
                m.remove_share_path('/mnt/weka/fs', 'my-share', force=True)
        rmtree.assert_called_once_with('/mnt/weka/fs/my-share')

    def test_get_directory_inode(self):
        m = _make_mount()
        stat_result = mock.Mock()
        stat_result.st_ino = 12345
        with mock.patch('os.stat', return_value=stat_result):
            inode = m.get_directory_inode('/mnt/weka/fs/my-share')
        self.assertEqual(12345, inode)

    def test_get_directory_inode_raises_on_error(self):
        m = _make_mount()
        with mock.patch('os.stat', side_effect=OSError('no such file')):
            self.assertRaises(
                weka_exc.WekaMountError,
                m.get_directory_inode, '/nonexistent')


if __name__ == '__main__':
    unittest.main()
