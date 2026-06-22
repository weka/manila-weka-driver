# Stub for oslo_privsep.priv_context — used in unit tests only.
#
# The entrypoint decorator is a passthrough so that privsep-decorated
# functions behave as plain callables during unit tests.  Tests mock
# them directly via unittest.mock.patch.


class PrivContext(object):
    """Minimal stub: no daemon, no privilege elevation."""

    def __init__(self, *args, **kwargs):
        pass

    def entrypoint(self, func):
        """Return func unchanged — no daemon wrapping in tests."""
        return func
