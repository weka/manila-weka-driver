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

"""WekaFS POSIX client mount helper for the Manila share driver.

Manages WekaFS kernel-client mounts on the Manila host.  The POSIX
client is preferred over NFS exports because it provides:

  - Sub-250 µs latency (no NFS protocol overhead)
  - Full POSIX semantics (file locking, mmap, etc.)
  - Adaptive page / dentry cache with coherency guarantees
  - Native directory-quota enforcement
  - Direct access to Weka filesystem metadata

Mount command format::

    mount -t wekafs [options] <backends>/<fs_name> <mount_point>

where <backends> is a comma-separated list of Weka backend addresses,
for example::

    mount -t wekafs -o num_cores=1 10.0.0.1,10.0.0.2/my_fs /mnt/weka/my_fs

Stateless (auth_required) mount format adds a mount token::

    mount -t wekafs -o num_cores=1,mount_token=<token> \\
        10.0.0.1/auth_fs /mnt/weka/auth_fs
"""

import os
import threading

from oslo_concurrency import processutils
from oslo_log import log as logging

from manila.share.drivers.weka import exceptions
from manila.share.drivers.weka import privsep as weka_privsep

LOG = logging.getLogger(__name__)

# Default permissions for Manila share sub-directories on the mount.
_SHARE_DIR_MODE = 0o777

# Per-mount-point lock registry to prevent concurrent mount/unmount races.
_MOUNT_LOCKS = {}
_MOUNT_LOCKS_LOCK = threading.Lock()


def _get_mount_lock(mount_point):
    """Return a per-mount-point threading.Lock (created on first use)."""
    with _MOUNT_LOCKS_LOCK:
        if mount_point not in _MOUNT_LOCKS:
            _MOUNT_LOCKS[mount_point] = threading.Lock()
        return _MOUNT_LOCKS[mount_point]


class WekaMount(object):
    """Manages a single WekaFS POSIX mount on the Manila host.

    Supports use as a context manager for temporary mounts::

        with WekaMount(backends='10.0.0.1', fs_name='my_fs',
                       mount_point='/mnt/weka/my_fs') as m:
            path = m.get_or_create_share_path(
                m.mount_point, 'share-uuid-1234')
            # … work with share path …
        # auto-unmount on exit

    :param backends: Comma-separated string of Weka backend addresses.
    :param fs_name: Weka filesystem name to mount.
    :param mount_point: Local directory where the filesystem will be mounted.
    :param mount_token: Optional authentication token for auth_required FSes.
    :param num_cores: Number of POSIX client CPU cores (default 1).
    :param net: Optional NIC name or DPDK identifier (e.g. "eth0").
    :param read_cache: Enable client-side read cache (default True).
    :param writecache: Enable write-back cache (default False).
    :param sync_on_close: Flush on close (default False).
    :param max_io_size: Override maximum IO size in bytes (optional).
    :param iops_limit: IOPS limit for this mount (optional).
    """

    def __init__(self, backends, fs_name, mount_point,
                 mount_token=None,
                 num_cores=1,
                 net=None,
                 read_cache=True,
                 writecache=False,
                 sync_on_close=False,
                 max_io_size=None,
                 iops_limit=None):
        self.backends = backends
        self.fs_name = fs_name
        self.mount_point = mount_point
        self.mount_token = mount_token
        self.num_cores = num_cores
        self.net = net
        self.read_cache = read_cache
        self.writecache = writecache
        self.sync_on_close = sync_on_close
        self.max_io_size = max_io_size
        self.iops_limit = iops_limit
        self._lock = _get_mount_lock(mount_point)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        self.mount()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.unmount()
        except Exception as exc:
            LOG.warning("Failed to unmount %s on context exit: %s",
                        self.mount_point, exc)
        return False  # do not suppress exceptions

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mount(self):
        """Mount the WekaFS filesystem at self.mount_point.

        Idempotent: if the filesystem is already mounted, returns immediately.

        :raises WekaMountError: if the mount command fails.
        """
        with self._lock:
            if self.is_mounted(self.mount_point):
                LOG.debug(
                    "WekaFS %s already mounted at %s — skipping",
                    self.fs_name, self.mount_point,
                )
                return

            self._ensure_mount_point_dir(self.mount_point)

            # A joined (stateful) Weka client mounts by bare filesystem
            # name and reuses its existing cluster attachment; only a
            # stateless client needs the explicit backends prefix.
            if self.backends:
                source = '{backends}/{fs_name}'.format(
                    backends=self.backends, fs_name=self.fs_name)
            else:
                source = self.fs_name
            mount_options = self._build_mount_options()

            LOG.info(
                "Mounting WekaFS filesystem '%s' at '%s'",
                self.fs_name, self.mount_point,
            )
            try:
                weka_privsep.wekafs_mount(
                    source,
                    self.mount_point,
                    ','.join(mount_options) if mount_options else None,
                )
            except processutils.ProcessExecutionError as exc:
                raise exceptions.WekaMountError(
                    reason='mount command failed: {}'.format(exc))

    def unmount(self, force=False):
        """Unmount the WekaFS filesystem.

        :param force: If True, use ``umount -l`` (lazy unmount).
        :raises WekaUnmountError: if umount fails.
        """
        with self._lock:
            if not self.is_mounted(self.mount_point):
                LOG.debug(
                    "WekaFS %s not mounted at %s — nothing to unmount",
                    self.fs_name, self.mount_point,
                )
                return

            LOG.info(
                "Unmounting WekaFS filesystem '%s' from '%s'",
                self.fs_name, self.mount_point,
            )
            try:
                weka_privsep.umount(self.mount_point, lazy=force)
            except processutils.ProcessExecutionError as exc:
                raise exceptions.WekaUnmountError(
                    reason='umount command failed: {}'.format(exc))

    @staticmethod
    def is_mounted(mount_point):
        """Return True if *mount_point* currently has a WekaFS mount.

        Reads /proc/mounts to determine mount status.
        """
        try:
            with open('/proc/mounts', 'r') as fh:
                for line in fh:
                    parts = line.split()
                    # fields: device mount_point fstype options dump pass
                    if len(parts) >= 3:
                        if (parts[1] == mount_point
                                and parts[2] == 'wekafs'):
                            return True
        except IOError:
            pass
        return False

    def get_or_create_share_path(self, mount_point, sub_path,
                                 mode=_SHARE_DIR_MODE):
        """Return the absolute path for a share sub-directory.

        Creates the directory (and any parents) if it does not exist,
        and sets permissions to *mode*.

        :param mount_point: The mounted WekaFS root directory.
        :param sub_path: Relative sub-path for the share (e.g. 'shares/uuid').
        :param mode: POSIX permissions (default 0o777).
        :returns: Absolute path string.
        :raises WekaMountError: if directory creation fails.
        """
        # Normalise: strip leading slash from sub_path
        sub_path = sub_path.lstrip('/')
        abs_path = os.path.join(mount_point, sub_path)

        if not os.path.isdir(abs_path):
            LOG.debug("Creating share directory: %s", abs_path)
            try:
                os.makedirs(abs_path, mode=mode)
            except OSError as exc:
                raise exceptions.WekaMountError(
                    reason='Failed to create share directory {}: {}'.format(
                        abs_path, exc))
        else:
            # Ensure correct permissions even if dir already exists.
            try:
                os.chmod(abs_path, mode)
            except OSError as exc:
                LOG.warning(
                    "Could not set permissions on %s: %s", abs_path, exc)

        return abs_path

    def remove_share_path(self, mount_point, sub_path, force=False):
        """Remove a share sub-directory.

        :param mount_point: The mounted WekaFS root directory.
        :param sub_path: Relative sub-path of the share directory.
        :param force: If True, remove the directory even if non-empty.
        :raises WekaMountError: if removal fails.
        """
        sub_path = sub_path.lstrip('/')
        abs_path = os.path.join(mount_point, sub_path)

        if not os.path.exists(abs_path):
            LOG.debug("Share path %s does not exist — skipping removal",
                      abs_path)
            return

        try:
            if force:
                import shutil
                shutil.rmtree(abs_path)
            else:
                os.rmdir(abs_path)
        except OSError as exc:
            raise exceptions.WekaMountError(
                reason='Failed to remove share directory {}: {}'.format(
                    abs_path, exc))

    def get_directory_inode(self, path):
        """Return the inode number for *path*.

        The inode is required to set directory quotas via the Weka API.

        :param path: Absolute path to the directory.
        :returns: Integer inode number.
        :raises WekaMountError: if stat fails.
        """
        try:
            return os.stat(path).st_ino
        except OSError as exc:
            raise exceptions.WekaMountError(
                reason='Failed to stat {}: {}'.format(path, exc))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_mount_options(self):
        """Build the list of WekaFS mount options."""
        opts = []
        opts.append('num_cores={}'.format(self.num_cores))
        if self.mount_token:
            opts.append('mount_token={}'.format(self.mount_token))
        if self.net:
            opts.append('net={}'.format(self.net))
        if not self.read_cache:
            opts.append('readcache=off')
        if self.writecache:
            opts.append('writecache')
        if self.sync_on_close:
            opts.append('sync_on_close')
        if self.max_io_size is not None:
            opts.append('max_io_size={}'.format(self.max_io_size))
        if self.iops_limit is not None:
            opts.append('iops_limit={}'.format(self.iops_limit))
        return opts

    @staticmethod
    def _ensure_mount_point_dir(path):
        """Create the mount point directory if it does not exist."""
        if not os.path.isdir(path):
            try:
                os.makedirs(path, exist_ok=True)
            except OSError as exc:
                raise exceptions.WekaMountError(
                    reason='Cannot create mount point {}: {}'.format(
                        path, exc))
