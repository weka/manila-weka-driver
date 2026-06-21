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

"""OpenStack Manila share driver for Weka storage (WekaFS POSIX client).

This driver exposes Weka filesystems as Manila shares using the WekaFS
POSIX client for primary access.  NFS is supported as a secondary protocol
for compatibility with legacy clients.

Architecture
------------

  Manila ShareDriver API
         │
         ▼
  WekaShareDriver (this module)
         │
         ├── WekaApiClient  — REST API calls to Weka cluster (port 14000)
         │
         └── WekaMount      — POSIX mount management on the Manila host

Configuration example (manila.conf)
------------------------------------

  [DEFAULT]
  enabled_share_backends = weka

  [weka]
  share_driver = manila.share.drivers.weka.driver:WekaShareDriver
  share_backend_name = weka
  driver_handles_share_servers = false
  snapshot_support = true
  create_share_from_snapshot_support = true
  revert_to_snapshot_support = true

  weka_api_server      = weka-cluster.example.com
  weka_username        = admin
  weka_password        = secret
  weka_organization    = Root
  weka_filesystem_group = default
  weka_mount_point_base = /mnt/weka
  weka_num_cores       = 1
  weka_ssl_verify      = true

Critical implementation notes
------------------------------
  - All Weka API calls use *bytes*; conversion from/to GiB happens here.
  - Every create/delete is idempotent: already-exists and not-found are
    handled silently.
  - The share UUID is used as the Weka filesystem name (with prefix).
  - The Weka filesystem UID is stored in the share's export metadata so
    subsequent operations never need to scan all filesystems.
"""

import ipaddress
import os
import socket
import time

from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import units

from manila.common import constants
from manila import exception
from manila.i18n import _
from manila.share import driver
from manila.share.drivers.weka import client as weka_client
from manila.share.drivers.weka import config as weka_config
from manila.share.drivers.weka import exceptions as weka_exc
from manila.share.drivers.weka import posix as weka_posix
from manila.share.drivers.weka import utils as weka_utils

LOG = logging.getLogger(__name__)

CONF = cfg.CONF

# Driver version — increment on each release.
DRIVER_VERSION = '1.0.0'

# Protocols this driver supports.
_WEKAFS_PROTO = 'WEKAFS'
_NFS_PROTO = 'NFS'
_SUPPORTED_PROTOCOLS = (_WEKAFS_PROTO, _NFS_PROTO)

# GiB constant for unit conversion.
_GiB = units.Gi


def _cidr_to_weka_ip(cidr_str):
    """Convert CIDR notation to Weka v5 IP/dotted-mask format.

    Weka v5 client group rules require dotted-decimal subnet masks
    (e.g. 192.168.1.0/255.255.255.0) rather than CIDR prefix notation
    (e.g. 192.168.1.0/24).  Single IP addresses are returned unchanged.
    """
    if '/' not in cidr_str:
        return cidr_str
    try:
        net = ipaddress.IPv4Network(cidr_str, strict=False)
        return '{}/{}'.format(str(net.network_address), str(net.netmask))
    except ValueError:
        return cidr_str


def _is_already_exists_error(exc):
    """True if a Weka API error means the resource already exists.

    Weka may reject a duplicate client-group rule with either a 409
    conflict or a 400 whose message says the rule already exists; a
    single IP can also be stored in a normalized CIDR/mask form that
    our local de-dup check misses, so we rely on the error itself.
    """
    if isinstance(exc, weka_exc.WekaConflict):
        return True
    return 'already exist' in str(exc).lower()


class WekaShareDriver(driver.ShareDriver):
    """Manila share driver for Weka storage using the WekaFS POSIX client.

    This is a serverless driver (driver_handles_share_servers = False).
    Weka manages its own networking; no Nova/Neutron integration is needed.

    Supported operations
    --------------------
    - create_share            (WEKAFS + NFS)
    - delete_share            (idempotent)
    - extend_share
    - shrink_share            (with in-use capacity check)
    - ensure_share
    - update_access           (ip and user rules; add / delete / full-sync)
    - create_snapshot
    - delete_snapshot         (idempotent)
    - revert_to_snapshot
    - create_share_from_snapshot
    - get_share_stats
    - manage_existing / unmanage
    """

    # Driver capability flags
    _is_driver_handles_share_servers = False

    def __init__(self, *args, **kwargs):
        super(WekaShareDriver, self).__init__(
            False, *args, config_opts=[weka_config.weka_opts], **kwargs)
        self._client = None
        self._fs_group_uid = None

    # ------------------------------------------------------------------
    # Setup / validation
    # ------------------------------------------------------------------

    def do_setup(self, context):
        """Initialise the driver: create API client, verify connectivity."""
        cfg_get = self.configuration.safe_get

        host = cfg_get('weka_api_server')
        port = cfg_get('weka_api_port') or 14000
        username = cfg_get('weka_username')
        password = cfg_get('weka_password')
        organization = cfg_get('weka_organization') or 'Root'
        ssl_verify = cfg_get('weka_ssl_verify')
        if ssl_verify is None:
            ssl_verify = True
        timeout = cfg_get('weka_api_timeout') or 30
        max_retries = cfg_get('weka_max_api_retries') or 3
        pool_connections = (
            cfg_get('weka_api_pool_connections') or 4
        )
        pool_maxsize = cfg_get('weka_api_pool_maxsize') or 10

        self._client = weka_client.WekaApiClient(
            host=host,
            username=username,
            password=password,
            organization=organization,
            port=port,
            ssl_verify=ssl_verify,
            timeout=timeout,
            max_retries=max_retries,
            pool_connections=pool_connections,
            pool_maxsize=pool_maxsize,
        )
        self._client.login()

        # Verify connectivity and log cluster version.
        try:
            status = self._client.get_cluster_status()
            cluster_name = status.get('name', 'unknown')
            cluster_version = status.get('release', 'unknown')
        except Exception as exc:
            LOG.warning("Could not fetch cluster status: %s", exc)
            cluster_name = 'unknown'
            cluster_version = 'unknown'

        LOG.info(
            "WekaShareDriver %s connected to cluster '%s' "
            "(Weka version %s)",
            DRIVER_VERSION, cluster_name, cluster_version,
        )

        # Ensure the default filesystem group exists.
        group_name = cfg_get('weka_filesystem_group') or 'default'
        self._ensure_filesystem_group(group_name)

    def check_for_setup_error(self):
        """Validate configuration and environment before starting."""
        required_opts = ['weka_api_server', 'weka_username', 'weka_password']
        missing = []
        for opt in required_opts:
            if not self.configuration.safe_get(opt):
                missing.append(opt)
        if missing:
            raise exception.InvalidInput(
                reason=_(
                    'Weka driver: required config options not set: %s'
                ) % ', '.join(missing)
            )

        # Verify POSIX client is available on the Manila host.
        proc_fs_file = '/proc/filesystems'
        wekafs_available = False
        try:
            with open(proc_fs_file, 'r') as fh:
                wekafs_available = 'wekafs' in fh.read()
        except IOError:
            pass

        if not wekafs_available:
            LOG.warning(
                "WekaFS kernel module not found in %s. "
                "POSIX shares will fail until 'wekafsio' module is loaded "
                "(run: modprobe wekafsio).",
                proc_fs_file,
            )

        # Test API auth.
        if self._client:
            try:
                self._client.get_cluster_status()
            except weka_exc.WekaAuthError as exc:
                raise exception.ManilaException(
                    message=_(
                        'Weka driver: API authentication failed: %s') % exc)
            except Exception as exc:
                LOG.warning("Could not verify cluster status during setup: %s",
                            exc)

    # ------------------------------------------------------------------
    # Share lifecycle
    # ------------------------------------------------------------------

    def create_share(self, context, share, share_server=None):
        """Create a Weka filesystem and return export locations.

        :param context: Request context.
        :param share: Share model dict.
        :param share_server: Unused (serverless driver).
        :returns: List of export location dicts.
        """
        share_proto = share['share_proto'].upper()
        if share_proto not in _SUPPORTED_PROTOCOLS:
            raise exception.InvalidShare(
                reason=_(
                    'Unsupported share protocol: %s. '
                    'Supported: %s'
                ) % (share_proto, ', '.join(_SUPPORTED_PROTOCOLS))
            )

        fs_name = self._share_name(share['id'])
        size_bytes = weka_utils.gb_to_bytes(share['size'])
        group_name = (self.configuration.safe_get('weka_filesystem_group')
                      or 'default')

        LOG.info(
            "Creating share %s (protocol %s, size %s GiB) "
            "as Weka filesystem '%s'",
            share['id'], share_proto, share['size'], fs_name,
        )

        # Create filesystem (idempotent — handle conflict).
        fs = self._create_filesystem_idempotent(
            fs_name, group_name, size_bytes)
        fs_uid = fs['uid']

        export_locations = self._build_export_locations(
            share, fs_name, fs_uid, share_proto)

        LOG.info(
            "Share %s created successfully (fs_uid=%s)", share['id'], fs_uid)
        return export_locations

    def create_share_from_snapshot(self, context, share, snapshot,
                                   share_server=None, parent_share=None):
        """Create a new share populated with data from a snapshot.

        Creates a new Weka filesystem, then copies the snapshot contents
        using the share's own protocol: WEKAFS shares copy over the Weka
        POSIX client (no NFS gateway required); NFS shares copy over NFS
        so a node without the Weka client installed can still serve them.

        :param context: Request context.
        :param share: New share model dict.
        :param snapshot: Source snapshot model dict.
        :param share_server: Unused.
        :returns: List of export location dicts.
        """
        snap_name = self._snapshot_name(snapshot['id'])

        snap = self._client.get_snapshot_by_name(snap_name)
        if not snap:
            raise exception.SnapshotNotFound(snapshot_id=snapshot['id'])

        # Resolve the source filesystem name from the snapshot's filesystemUid.
        src_fs = self._client.get_filesystem(snap['filesystemUid'])
        src_fs_name = src_fs['name']

        new_fs_name = self._share_name(share['id'])
        share_proto = share['share_proto'].upper()
        group_name = (self.configuration.safe_get('weka_filesystem_group')
                      or 'default')
        size_bytes = weka_utils.gb_to_bytes(share['size'])

        LOG.info(
            "Creating share %s from snapshot %s (src fs: %s, snap: %s)",
            share['id'], snapshot['id'], src_fs_name, snap_name,
        )

        fs = self._create_filesystem_idempotent(
            new_fs_name, group_name, size_bytes)

        # The data copy uses the share's own protocol: WEKAFS shares copy
        # over the Weka POSIX client (no NFS gateway required); NFS shares
        # copy over NFS so the node need not have the Weka client installed.
        if share_proto == _WEKAFS_PROTO:
            self._copy_snapshot_wekafs(
                src_fs_name, new_fs_name, snap, snap_name, share, snapshot)
        else:
            self._copy_snapshot_nfs(
                src_fs_name, new_fs_name, snap, snap_name, share, snapshot)

        export_locations = self._build_export_locations(
            share, new_fs_name, fs['uid'], share_proto)
        return export_locations

    def _copy_snapshot_wekafs(self, src_fs_name, new_fs_name,
                              snap, snap_name, share, snapshot):
        """Copy snapshot data into new_fs_name over the WekaFS POSIX client.

        Mounts both the source and destination filesystems using the Weka
        kernel client (backends=None — joined client) and rsyncs the
        snapshot directory (.snapshots/<accessPoint>/) to the new filesystem.
        Mounts are always cleaned up in a finally block.
        """
        net = self.configuration.safe_get('weka_net_device')
        src_mnt = '/tmp/manila_snap_src_{}'.format(share['id'][:8])
        dst_mnt = '/tmp/manila_snap_dst_{}'.format(share['id'][:8])
        # backends=None: Manila host is a joined Weka client; mount by bare
        # fs name to reuse the existing cluster attachment.
        src_mount = weka_posix.WekaMount(
            backends=None, fs_name=src_fs_name, mount_point=src_mnt,
            net=net)
        dst_mount = weka_posix.WekaMount(
            backends=None, fs_name=new_fs_name, mount_point=dst_mnt,
            net=net)

        try:
            src_mount.mount()
            dst_mount.mount()

            snap_dir = os.path.join(
                src_mnt, '.snapshots',
                snap.get('accessPoint') or snap_name)

            LOG.info("Rsyncing snapshot data from %s to %s",
                     snap_dir, dst_mnt)
            processutils.execute(
                'rsync', '-a',
                snap_dir.rstrip('/') + '/',
                dst_mnt.rstrip('/') + '/',
                run_as_root=True, root_helper='sudo',
            )
            LOG.info(
                "Copied snapshot %s content to new filesystem %s via "
                "WekaFS", snap_name, new_fs_name,
            )
        except Exception as exc:
            LOG.error(
                "Failed to populate share %s from snapshot %s: %s",
                share['id'], snapshot['id'], exc,
            )
            raise
        finally:
            for mount_obj in (dst_mount, src_mount):
                try:
                    mount_obj.unmount()
                except Exception as e:
                    LOG.warning("Failed to unmount %s: %s",
                                mount_obj.mount_point, e)
            for mnt in (src_mnt, dst_mnt):
                try:
                    os.rmdir(mnt)
                except Exception:
                    pass

    def _copy_snapshot_nfs(self, src_fs_name, new_fs_name, snap,
                           snap_name, share, snapshot):
        """Copy snapshot data into new_fs_name over NFS.

        Requires weka_nfs_server to be configured. Creates a temporary
        client group and NFS permissions, mounts both filesystems locally
        via NFS v3, rsyncs the snapshot directory, then removes all
        temporary resources in a finally block.
        """
        nfs_server = self.configuration.safe_get('weka_nfs_server')
        if not nfs_server:
            raise exception.ManilaException(
                message=_('weka_nfs_server must be configured for '
                          'create_share_from_snapshot'))

        # Determine the local IP that routes to the NFS server.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((nfs_server, 2049))
            local_ip = s.getsockname()[0]
        except Exception:
            local_ip = socket.gethostbyname(socket.gethostname())
        finally:
            s.close()

        tmp_cg_name = 'manila-snap-{}'.format(share['id'][:8])
        cg_uid = None
        rule_uid = None
        src_mnt = '/tmp/manila_snap_src_{}'.format(share['id'][:8])
        dst_mnt = '/tmp/manila_snap_dst_{}'.format(share['id'][:8])
        src_mounted = False
        dst_mounted = False

        try:
            cg = self._client.create_client_group(tmp_cg_name)
            cg_uid = cg['uid']
            rule = self._client.add_client_group_rule(
                cg_uid, 'IP', local_ip)
            rule_uid = (rule.get('uid')
                        if isinstance(rule, dict) else None)

            self._client.create_nfs_permission(
                client_group=tmp_cg_name,
                fs_uid=src_fs_name,
                path='/',
                access_type='RO',
                squash=False,
            )
            self._client.create_nfs_permission(
                client_group=tmp_cg_name,
                fs_uid=new_fs_name,
                path='/',
                access_type='RW',
                squash=False,
            )

            # Allow the NFS server to apply the new permissions.
            time.sleep(5)

            os.makedirs(src_mnt, exist_ok=True)
            os.makedirs(dst_mnt, exist_ok=True)

            processutils.execute(
                'mount', '-t', 'nfs',
                '{}:/{}'.format(nfs_server, src_fs_name),
                src_mnt,
                run_as_root=True, root_helper='sudo',
            )
            src_mounted = True

            processutils.execute(
                'mount', '-t', 'nfs',
                '{}:/{}'.format(nfs_server, new_fs_name),
                dst_mnt,
                run_as_root=True, root_helper='sudo',
            )
            dst_mounted = True

            snap_dir = os.path.join(
                src_mnt, '.snapshots',
                snap.get('accessPoint') or snap_name)

            LOG.info("Rsyncing snapshot data from %s to %s",
                     snap_dir, dst_mnt)
            processutils.execute(
                'rsync', '-a',
                snap_dir.rstrip('/') + '/',
                dst_mnt.rstrip('/') + '/',
                run_as_root=True, root_helper='sudo',
            )
            LOG.info(
                "Copied snapshot %s content to new filesystem %s via NFS",
                snap_name, new_fs_name,
            )
        except Exception as exc:
            LOG.error(
                "Failed to populate share %s from snapshot %s: %s",
                share['id'], snapshot['id'], exc,
            )
            raise
        finally:
            if dst_mounted:
                try:
                    processutils.execute(
                        'umount', dst_mnt,
                        run_as_root=True, root_helper='sudo')
                except Exception as e:
                    LOG.warning("Failed to umount %s: %s", dst_mnt, e)
            if src_mounted:
                try:
                    processutils.execute(
                        'umount', src_mnt,
                        run_as_root=True, root_helper='sudo')
                except Exception as e:
                    LOG.warning("Failed to umount %s: %s", src_mnt, e)
            for mnt in (src_mnt, dst_mnt):
                try:
                    os.rmdir(mnt)
                except Exception:
                    pass
            # Clean up temporary NFS permissions by client group name.
            try:
                for perm in self._client.list_nfs_permissions():
                    if perm.get('group') == tmp_cg_name:
                        try:
                            self._client.delete_nfs_permission(perm['uid'])
                        except Exception:
                            pass
            except Exception as e:
                LOG.warning(
                    "Failed to clean up NFS permissions for %s: %s",
                    tmp_cg_name, e)
            if cg_uid:
                if rule_uid:
                    try:
                        self._client.delete_client_group_rule(
                            cg_uid, rule_uid)
                    except Exception:
                        pass
                try:
                    self._client.delete_client_group(cg_uid)
                except Exception as e:
                    LOG.warning(
                        "Failed to delete client group %s: %s",
                        tmp_cg_name, e)

    def delete_share(self, context, share, share_server=None):
        """Delete a share's underlying Weka filesystem.

        Idempotent: if the filesystem does not exist, returns silently.
        """
        fs_name = self._share_name(share['id'])
        LOG.info("Deleting share %s (Weka FS '%s')", share['id'], fs_name)

        fs = self._client.get_filesystem_by_name(fs_name)
        if not fs:
            LOG.info(
                "Filesystem '%s' not found — share %s already deleted",
                fs_name, share['id'],
            )
            return

        fs_uid = fs['uid']

        # Remove NFS permissions before deleting.
        try:
            self._remove_all_nfs_permissions(fs_name)
        except Exception as exc:
            LOG.warning(
                "Failed to remove NFS permissions for share %s: %s",
                share['id'], exc,
            )

        # Unmount locally if mounted.
        mount_point = self._mount_point(fs_name)
        if weka_posix.WekaMount.is_mounted(mount_point):
            try:
                mnt = weka_posix.WekaMount(
                    backends=self._get_backends(),
                    fs_name=fs_name,
                    mount_point=mount_point,
                )
                mnt.unmount(force=True)
            except Exception as exc:
                LOG.warning(
                    "Failed to unmount %s during delete of share %s: %s",
                    mount_point, share['id'], exc,
                )

        # Delete the filesystem.
        try:
            self._client.delete_filesystem(fs_uid)
        except weka_exc.WekaNotFound:
            pass  # already gone

        LOG.info("Share %s deleted", share['id'])

    def extend_share(self, share, new_size, share_server=None):
        """Extend share capacity.

        :param share: Share model.
        :param new_size: New size in GiB.
        """
        fs_uid = self._get_fs_uid_for_share(share)
        new_bytes = weka_utils.gb_to_bytes(new_size)
        LOG.info(
            "Extending share %s to %s GiB", share['id'], new_size)
        self._client.update_filesystem(fs_uid, total_capacity=new_bytes)

    def shrink_share(self, share, new_size, share_server=None):
        """Shrink share capacity.

        Raises ShareShrinkingPossibleDataLoss if in-use > new_size.
        """
        fs_uid = self._get_fs_uid_for_share(share)
        fs = self._client.get_filesystem(fs_uid)
        used_bytes = fs.get('used_total', fs.get('usedSizeBytes', 0)) or 0
        new_bytes = weka_utils.gb_to_bytes(new_size)

        if used_bytes > new_bytes:
            raise exception.ShareShrinkingPossibleDataLoss(
                share_id=share['id'])

        LOG.info(
            "Shrinking share %s to %s GiB", share['id'], new_size)
        self._client.update_filesystem(fs_uid, total_capacity=new_bytes)

    def ensure_share(self, context, share, share_server=None):
        """Verify share is exported and return current export locations.

        Called by Manila on restart/recovery to re-verify shares.
        """
        fs_name = self._share_name(share['id'])
        fs = self._client.get_filesystem_by_name(fs_name)
        if not fs:
            raise exception.ShareNotFound(share_id=share['id'])

        fs_uid = fs['uid']
        share_proto = share['share_proto'].upper()

        # Re-mount POSIX if needed.
        mount_point = self._mount_point(fs_name)
        if (share_proto == _WEKAFS_PROTO
                and not weka_posix.WekaMount.is_mounted(mount_point)):
            LOG.info(
                "Re-mounting WekaFS share %s at %s",
                share['id'], mount_point,
            )
            mnt = weka_posix.WekaMount(
                backends=self._get_backends(),
                fs_name=fs_name,
                mount_point=mount_point,
                num_cores=self.configuration.safe_get('weka_num_cores') or 1,
                net=self.configuration.safe_get('weka_net_device'),
            )
            mnt.mount()

        return self._build_export_locations(
            share, fs_name, fs_uid, share_proto)

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    def update_access(self, context, share, access_rules, add_rules,
                      delete_rules, update_rules=None, share_server=None):
        """Update access rules for a share.

        Supports full-sync (all rules in access_rules, empty
        add/delete/update) and incremental (add/delete/update) modes.

        For WEKAFS protocol: access is controlled via Weka filesystem
        authentication and mount tokens; rules are accepted as a no-op.
        For NFS protocol: rules translate to Weka NFS client groups and
        export permissions.
        """
        share_proto = share['share_proto'].upper()

        add_rules = list(add_rules or [])
        delete_rules = list(delete_rules or [])
        update_rules = list(update_rules or [])

        # Full-sync mode: Manila passes the full rule set in access_rules
        # with empty add/delete/update lists.
        if not add_rules and not delete_rules and not update_rules:
            add_rules = list(access_rules or [])

        # Access-level updates re-apply through the same idempotent path
        # as additions.
        apply_rules = add_rules + update_rules

        if share_proto == _NFS_PROTO:
            return self._update_nfs_access(share, apply_rules, delete_rules)
        elif share_proto == _WEKAFS_PROTO:
            return self._update_wekafs_access(
                share, apply_rules, delete_rules)
        return {}

    def _update_nfs_access(self, share, add_rules, delete_rules):
        """Add / delete NFS permissions on the Weka cluster."""
        rule_state_map = {}
        fs_name = self._share_name(share['id'])

        for rule in add_rules or []:
            if rule['access_type'] != 'ip':
                LOG.warning(
                    "NFS shares only support 'ip' access type; "
                    "skipping rule %s (type=%s)",
                    rule['access_id'], rule['access_type'],
                )
                rule_state_map[rule['access_id']] = {'state': 'error'}
                continue
            try:
                self._apply_nfs_rule(share, fs_name, rule)
                rule_state_map[rule['access_id']] = {'state': 'active'}
            except Exception as exc:
                LOG.error(
                    "Failed to add NFS rule %s on share %s: %s",
                    rule['access_id'], share['id'], exc,
                )
                rule_state_map[rule['access_id']] = {'state': 'error'}

        for rule in delete_rules or []:
            try:
                self._remove_nfs_rule(fs_name, rule)
            except Exception as exc:
                LOG.warning(
                    "Failed to delete NFS rule %s: %s",
                    rule['access_id'], exc,
                )

        return rule_state_map

    def _apply_nfs_rule(self, share, fs_name, rule):
        """Idempotently apply a single NFS 'ip' access rule.

        Reuses (or creates) a per-rule client group, ensures the client
        IP rule is present exactly once, and creates the NFS export
        permission with the requested access level (recreating it if the
        level changed).  Safe to call repeatedly: full-sync, recovery
        resyncs and access-level updates all funnel through here without
        leaking or duplicating cluster resources.
        """
        nfs_type = (
            'RW' if rule['access_level'] == constants.ACCESS_LEVEL_RW
            else 'RO')
        cg_name = 'manila-{}-{}'.format(
            share['id'][:8], rule['access_id'][:8])
        weka_ip = _cidr_to_weka_ip(rule['access_to'])

        # Get-or-create the client group so re-applying an existing rule
        # neither hits a duplicate-name error nor leaks a new group.
        cg = self._get_client_group_by_name(cg_name)
        if cg is None:
            cg = self._client.create_client_group(cg_name)
            existing_ips = set()
        else:
            detail = self._client.get_client_group(cg['uid'])
            existing_ips = {
                r.get('ip')
                for r in detail.get('rules', [])
                if r.get('ip')
            }
        if weka_ip not in existing_ips:
            try:
                self._client.add_client_group_rule(
                    cg['uid'], 'IP', weka_ip)
            except weka_exc.WekaApiError as exc:
                # Idempotent re-apply during a reconcile: the IP rule
                # may already be present under a normalized form we
                # did not match, so tolerate "already exists" and
                # continue to reconcile the export permission (e.g.
                # an ro->rw level change).
                if not _is_already_exists_error(exc):
                    raise

        # Ensure the export permission exists with the right access level.
        perm = self._find_nfs_permission(fs_name, cg_name)
        if perm is None:
            self._client.create_nfs_permission(
                client_group=cg_name, fs_uid=fs_name, path='/',
                access_type=nfs_type, squash=False)
        elif perm.get('permission_type') != nfs_type:
            self._client.delete_nfs_permission(perm['uid'])
            self._client.create_nfs_permission(
                client_group=cg_name, fs_uid=fs_name, path='/',
                access_type=nfs_type, squash=False)
        LOG.debug(
            "Applied NFS %s access for %s on share %s",
            nfs_type, rule['access_to'], share['id'],
        )

    def _get_client_group_by_name(self, name):
        """Return the NFS client group dict with this name, or None."""
        for cg in self._client.list_client_groups() or []:
            if cg.get('name') == name:
                return cg
        return None

    def _find_nfs_permission(self, fs_name, cg_name):
        """Return the NFS permission for (filesystem, group), or None."""
        for perm in self._client.list_nfs_permissions() or []:
            perm_fs = perm.get(
                'filesystem', perm.get('filesystem_id', ''))
            perm_cg = perm.get(
                'group', perm.get('client_group_name', ''))
            if perm_fs == fs_name and perm_cg == cg_name:
                return perm
        return None

    def _delete_client_group_by_name(self, name):
        """Delete the NFS client group with this name if it exists."""
        cg = self._get_client_group_by_name(name)
        if cg is None:
            return
        try:
            self._client.delete_client_group(cg['uid'])
        except weka_exc.WekaNotFound:
            pass

    def _update_wekafs_access(self, share, add_rules, delete_rules):
        """Handle WekaFS access rules.

        Access control for the WekaFS (POSIX client) protocol is managed
        entirely within the Weka cluster via filesystem-level authentication
        and mount tokens.  The Manila access-rules API has no mapping onto
        those mechanisms in the current driver implementation.

        All rules are accepted as a no-op so that the Manila access rule
        workflow completes normally.
        """
        rule_state_map = {}
        for rule in add_rules or []:
            LOG.info(
                "WekaFS shares do not enforce Manila access rules "
                "(type=%s, rule=%s). Access control for WEKAFS shares is "
                "managed via Weka filesystem authentication and mount tokens.",
                rule['access_type'], rule['access_id'],
            )
            rule_state_map[rule['access_id']] = {'state': 'active'}
        return rule_state_map

    def _remove_nfs_rule(self, fs_name, rule):
        """Remove the NFS permission AND client group for a rule.

        Deletes both the export permission and the per-rule client group
        so the cluster-wide client-group pool is not leaked across rule
        add/delete cycles.

        :param fs_name: Weka filesystem name (used to match permissions).
        """
        cg_names = set()
        for perm in self._client.list_nfs_permissions() or []:
            perm_fs = perm.get(
                'filesystem', perm.get('filesystem_id', ''))
            if perm_fs != fs_name:
                continue
            # Match by client group name which encodes the rule ID.
            cg_name = perm.get(
                'group', perm.get('client_group_name', ''))
            if rule['access_id'][:8] in cg_name:
                self._client.delete_nfs_permission(perm['uid'])
                if cg_name:
                    cg_names.add(cg_name)
        for cg_name in cg_names:
            self._delete_client_group_by_name(cg_name)

    def _remove_all_nfs_permissions(self, fs_name):
        """Remove all NFS permissions AND client groups for a filesystem.

        Used during share delete.  Deletes the per-rule client groups as
        well as the export permissions to avoid leaking the cluster-wide
        client-group pool.

        :param fs_name: Weka filesystem name (used to match permissions).
        """
        cg_names = set()
        for perm in self._client.list_nfs_permissions() or []:
            perm_fs = perm.get(
                'filesystem', perm.get('filesystem_id', ''))
            if perm_fs != fs_name:
                continue
            cg_name = perm.get(
                'group', perm.get('client_group_name', ''))
            try:
                self._client.delete_nfs_permission(perm['uid'])
            except weka_exc.WekaNotFound:
                pass
            if cg_name:
                cg_names.add(cg_name)
        for cg_name in cg_names:
            self._delete_client_group_by_name(cg_name)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def create_snapshot(self, context, snapshot, share_server=None):
        """Create a snapshot of a share's underlying filesystem."""
        share = snapshot['share']
        fs_uid = self._get_fs_uid_for_share(share)
        snap_name = self._snapshot_name(snapshot['id'])

        LOG.info(
            "Creating snapshot %s (name='%s') for share %s",
            snapshot['id'], snap_name, share['id'],
        )
        try:
            self._client.create_snapshot(
                fs_uid, name=snap_name, is_writable=False)
        except weka_exc.WekaApiError as exc:
            if not _is_already_exists_error(exc):
                raise
            LOG.info(
                "Snapshot %s already exists for share %s — treating as "
                "created", snap_name, share['id'],
            )

    def delete_snapshot(self, context, snapshot, share_server=None):
        """Delete a snapshot.

        Idempotent: silently ignores not-found.
        """
        share = snapshot['share']
        fs_uid = None
        try:
            fs_uid = self._get_fs_uid_for_share(share)
        except exception.ShareNotFound:
            LOG.info(
                "Parent share %s not found — skipping snapshot delete",
                share['id'],
            )
            return

        snap_name = self._snapshot_name(snapshot['id'])
        LOG.info(
            "Deleting snapshot %s (name='%s')",
            snapshot['id'], snap_name,
        )
        snap = self._client.get_snapshot_by_name(snap_name, fs_uid=fs_uid)
        if not snap:
            LOG.info(
                "Snapshot '%s' not found — already deleted", snap_name)
            return
        try:
            self._client.delete_snapshot(snap['uid'])
        except weka_exc.WekaNotFound:
            pass

    def revert_to_snapshot(self, context, snapshot, share_access_rules,
                           snapshot_access_rules, share_server=None):
        """Revert a share to a snapshot (in-place restore)."""
        share = snapshot['share']
        fs_uid = self._get_fs_uid_for_share(share)
        snap_name = self._snapshot_name(snapshot['id'])

        snap = self._client.get_snapshot_by_name(snap_name, fs_uid=fs_uid)
        if not snap:
            raise exception.SnapshotNotFound(snapshot_id=snapshot['id'])

        LOG.info(
            "Reverting share %s to snapshot %s",
            share['id'], snapshot['id'],
        )
        self._client.restore_snapshot(snap['uid'], fs_uid)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def _update_share_stats(self, data=None):
        """Collect and publish backend statistics to Manila."""
        try:
            capacity = self._client.get_capacity()
        except Exception as exc:
            LOG.warning("Failed to fetch Weka capacity stats: %s", exc)
            capacity = {}

        total_bytes = capacity.get('totalBytes', 0) or 0
        used_bytes = capacity.get('usedBytes', 0) or 0
        free_bytes = max(0, total_bytes - used_bytes)

        backend_name = (
            self.configuration.safe_get('share_backend_name') or 'weka')
        group_name = (
            self.configuration.safe_get('weka_filesystem_group') or 'default')

        stats = {
            'share_backend_name': backend_name,
            'vendor_name': 'Weka',
            'driver_version': DRIVER_VERSION,
            # Report as an underscore-joined string (e.g. "WEKAFS_NFS") so
            # the scheduler CapabilitiesFilter exact-matches the value against
            # a share type's storage_protocol extra-spec (operators set
            # capability_storage_protocol to the same value), and so
            # manila-tempest-plugin's ShareMultiBackendTest (which calls
            # storage_protocol.lower().split('_')) works — a Python list
            # would raise AttributeError on .lower().
            'storage_protocol': '_'.join(_SUPPORTED_PROTOCOLS),
            'total_capacity_gb': weka_utils.bytes_to_gb(total_bytes),
            'free_capacity_gb': weka_utils.bytes_to_gb(free_bytes),
            'reserved_percentage': 0,
            'reserved_snapshot_percentage': 0,
            'snapshot_support': True,
            'create_share_from_snapshot_support': True,
            'revert_to_snapshot_support': True,
            'mount_snapshot_support': False,
            'qos': False,
            'thin_provisioning': False,
            'pools': [{
                'pool_name': group_name,
                'total_capacity_gb': weka_utils.bytes_to_gb(total_bytes),
                'free_capacity_gb': weka_utils.bytes_to_gb(free_bytes),
                'reserved_percentage': 0,
                'reserved_snapshot_percentage': 0,
                'reserved_share_extend_percentage': 0,
            }],
        }
        super(WekaShareDriver, self)._update_share_stats(stats)

    # ------------------------------------------------------------------
    # Manage / unmanage
    # ------------------------------------------------------------------

    def manage_existing(self, share, driver_options):
        """Bring an existing Weka filesystem under Manila management.

        :param share: Share model (share['export_locations'] holds the path).
        :param driver_options: Dict of driver-specific options (unused).
        :returns: Dict with 'size' key (GiB) for Manila to record.
        :raises ManageInvalidShare: if the filesystem cannot be found.
        """
        # Derive the filesystem name from the export path supplied to
        # 'manila manage'. For WEKAFS the path is '<backend>/<fs_name>'
        # or just '<fs_name>'; for NFS it is '<server>:/<fs_name>'.
        fs_name = None
        for loc in share.get('export_locations', []):
            path = loc.get('path', '')
            if path:
                fs_name = path.rsplit('/', 1)[-1] if '/' in path else path
                break

        if not fs_name:
            raise exception.ManageInvalidShare(
                reason=_('Cannot determine filesystem name from share export '
                         'location. Pass the filesystem name as the export '
                         'path to manila manage.'))

        fs = self._client.get_filesystem_by_name(fs_name)
        if not fs:
            raise exception.ManageInvalidShare(
                reason=_(
                    'Weka filesystem "%s" not found') % fs_name)

        size_bytes = fs.get('total_budget', fs.get('totalCapacity', 0)) or 0
        size_gb = max(1, int(weka_utils.bytes_to_gb(size_bytes)))
        fs_uid = fs.get('uid') or fs.get('id', '')

        LOG.info(
            "Managing existing share %s (FS '%s', size %s GiB)",
            share['id'], fs_name, size_gb,
        )
        share_proto = share.get('share_proto', _WEKAFS_PROTO).upper()
        export_locations = self._build_export_locations(
            share, fs_name, fs_uid, share_proto)
        return {'size': size_gb, 'export_locations': export_locations}

    def unmanage(self, share):
        """Remove share from Manila management without deleting the filesystem.

        This is a no-op: Manila simply stops tracking the share.
        The underlying Weka filesystem is left intact.
        """
        LOG.info(
            "Unmanaging share %s — Weka filesystem '%s' preserved",
            share['id'], self._share_name(share['id']),
        )

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def get_network_allocations_number(self):
        """Return 0 — this driver manages its own networking via Weka."""
        return 0

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _share_name(self, share_id):
        """Return the Weka filesystem name for a Manila share ID.

        Weka enforces a 32-character maximum on filesystem names.
        Uses 'manila_' prefix (7 chars) + first 25 hex chars of the UUID
        (hyphens stripped) = 32 chars total.
        """
        prefix = (self.configuration.safe_get('weka_share_name_prefix')
                  or 'manila_')
        id_hex = share_id.replace('-', '')
        max_id_len = 32 - len(prefix)
        return prefix + id_hex[:max_id_len]

    def _share_name_from_share(self, share):
        """Attempt to derive filesystem name from a share model."""
        return self._share_name(share['id'])

    def _snapshot_name(self, snapshot_id):
        """Return the Weka snapshot name for a Manila snapshot ID.

        Weka enforces a 32-character maximum on snapshot names.
        Uses 's_' prefix (2 chars) + first 30 hex chars of the UUID = 32 chars.
        """
        id_hex = snapshot_id.replace('-', '')
        return 's_' + id_hex[:30]

    def _mount_point(self, fs_name):
        """Return the local mount point directory for a filesystem."""
        base = (self.configuration.safe_get('weka_mount_point_base')
                or '/mnt/weka')
        return os.path.join(base, fs_name)

    def _get_backends(self):
        """Return the Weka backend address string for POSIX mounts."""
        return self.configuration.safe_get('weka_api_server') or ''

    def _get_fs_uid_for_share(self, share):
        """Look up the Weka filesystem UID for a share.

        First checks export locations metadata for a cached UID.
        Falls back to listing filesystems by name.

        :raises ShareNotFound: if the filesystem cannot be found.
        """
        # Try to get UID from export location metadata.
        for loc in share.get('export_locations', []) or []:
            try:
                meta = loc.get('metadata') or {}
                if isinstance(meta, dict):
                    uid = meta.get('weka_fs_uid')
                else:
                    # Manila ORM object — iterate key/value pairs
                    uid = next(
                        (v for k, v in meta.items() if k == 'weka_fs_uid'),
                        None)
                if uid:
                    return uid
            except (AttributeError, TypeError):
                pass

        # Try the standard manila-generated filesystem name.
        fs_name = self._share_name(share['id'])
        fs = self._client.get_filesystem_by_name(fs_name)
        if fs:
            return fs['uid']

        # For managed shares the filesystem keeps its original name, which is
        # the last path component of the export location path.
        for loc in share.get('export_locations', []) or []:
            path = ''
            try:
                path = loc.get('path', '') if isinstance(loc, dict) else str(
                    getattr(loc, 'path', ''))
            except (AttributeError, TypeError):
                pass
            if path:
                candidate = path.rsplit('/', 1)[-1] if '/' in path else path
                # Strip NFS server prefix (server:/fs_name → fs_name)
                if ':' in candidate:
                    candidate = candidate.split(':', 1)[-1].lstrip('/')
                if candidate:
                    fs = self._client.get_filesystem_by_name(candidate)
                    if fs:
                        return fs['uid']

        raise exception.ShareNotFound(share_id=share['id'])

    def _ensure_filesystem_group(self, group_name):
        """Ensure the default filesystem group exists; create if not."""
        grp = self._client.get_filesystem_group_by_name(group_name)
        if grp:
            self._fs_group_uid = grp['uid']
            LOG.debug(
                "Using existing Weka filesystem group '%s' (uid=%s)",
                group_name, self._fs_group_uid,
            )
        else:
            LOG.info(
                "Creating Weka filesystem group '%s'", group_name)
            grp = self._client.create_filesystem_group(group_name)
            self._fs_group_uid = grp['uid']

    def _create_filesystem_idempotent(self, fs_name, group_name, size_bytes):
        """Create a filesystem, returning existing one if already present."""
        existing = self._client.get_filesystem_by_name(fs_name)
        if existing:
            LOG.debug(
                "Filesystem '%s' already exists — reusing uid=%s",
                fs_name, existing.get('uid'),
            )
            return existing
        try:
            return self._client.create_filesystem(
                name=fs_name,
                group_name=group_name,
                total_capacity=size_bytes,
            )
        except weka_exc.WekaConflict:
            # Race: created by another thread/request.
            fs = self._client.get_filesystem_by_name(fs_name)
            if fs:
                return fs
            raise

    def _build_export_locations(self, share, fs_name, fs_uid, share_proto):
        """Build Manila export location list for a share.

        :param share: Share model dict.
        :param fs_name: Weka filesystem name.
        :param fs_uid: Weka filesystem UID.
        :param share_proto: Protocol string (WEKAFS or NFS).
        :returns: List of export location dicts.
        """
        backends = self._get_backends()
        if share_proto == _WEKAFS_PROTO:
            path = '{backends}/{fs_name}'.format(
                backends=backends, fs_name=fs_name)
        else:
            # NFS: use dedicated NFS server if configured, else fall back to
            # the API server address.
            nfs_server = (
                self.configuration.safe_get('weka_nfs_server') or backends)
            path = '{server}:/{fs_name}'.format(
                server=nfs_server, fs_name=fs_name)

        metadata = {
            'weka_fs_uid': fs_uid,
            'weka_fs_name': fs_name,
        }
        return [{
            'path': path,
            'is_admin_only': False,
            'metadata': metadata,
        }]
