# Stub — provides minimal oslo_config.cfg for standalone testing.


class _Opt(object):
    def __init__(self, name, default=None, **kwargs):
        self.name = name
        self.default = default


class StrOpt(_Opt):
    pass


class IntOpt(_Opt):
    pass


class BoolOpt(_Opt):
    pass


class PortOpt(_Opt):
    pass


class HostAddressOpt(_Opt):
    pass


class _ConfigOpts(object):
    def __getattr__(self, name):
        return None

    def register_opts(self, opts, group=None):
        pass


CONF = _ConfigOpts()
