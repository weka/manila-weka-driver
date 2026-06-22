# Stub for manila.privsep — used in unit tests only.
#
# Mirrors the real manila/privsep/__init__.py which exposes a shared
# PrivContext.  The entrypoint decorator is a passthrough (see
# stubs/oslo_privsep/priv_context.py), so all weka_privsep functions
# are plain callables that tests can patch with unittest.mock.patch.

from oslo_privsep import capabilities  # noqa: F401
from oslo_privsep import priv_context

sys_admin_pctxt = priv_context.PrivContext(
    'manila',
    cfg_section='manila_sys_admin',
    pypath=__name__ + '.sys_admin_pctxt',
    capabilities=[
        capabilities.CAP_CHOWN,
        capabilities.CAP_DAC_OVERRIDE,
        capabilities.CAP_DAC_READ_SEARCH,
        capabilities.CAP_FOWNER,
        capabilities.CAP_NET_ADMIN,
        capabilities.CAP_SYS_ADMIN,
    ],
)
