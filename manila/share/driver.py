# Stub — provides minimal manila.share.driver for standalone testing.
# In production, the real Manila package supplies this module.


class ShareDriver(object):
    """Minimal stub of Manila's ShareDriver base class."""

    def __init__(self, driver_handles_share_servers, *args,
                 config_opts=None, **kwargs):
        self.driver_handles_share_servers = driver_handles_share_servers
        self._stats = {}
        if 'configuration' in kwargs:
            self.configuration = kwargs.pop('configuration')

    def do_setup(self, context):
        pass

    def check_for_setup_error(self):
        pass

    def _update_share_stats(self, data=None):
        if data:
            self._stats.update(data)

    def get_share_stats(self, refresh=False):
        return self._stats

    def get_network_allocations_number(self):
        raise NotImplementedError()

    def create_share(self, context, share, share_server=None):
        raise NotImplementedError()

    def delete_share(self, context, share, share_server=None):
        raise NotImplementedError()

    def extend_share(self, share, new_size, share_server=None):
        raise NotImplementedError()

    def shrink_share(self, share, new_size, share_server=None):
        raise NotImplementedError()

    def ensure_share(self, context, share, share_server=None):
        raise NotImplementedError()

    def update_access(self, context, share, access_rules, add_rules,
                      delete_rules, update_rules, share_server=None):
        pass

    def create_snapshot(self, context, snapshot, share_server=None):
        raise NotImplementedError()

    def delete_snapshot(self, context, snapshot, share_server=None):
        raise NotImplementedError()

    def revert_to_snapshot(self, context, snapshot, share_access_rules,
                           snapshot_access_rules, share_server=None):
        pass

    def manage_existing(self, share, driver_options):
        raise NotImplementedError()

    def unmanage(self, share):
        pass
