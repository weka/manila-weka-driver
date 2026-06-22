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

"""Utility helpers for the Weka Manila share driver."""

import functools
import time

from oslo_log import log as logging
from oslo_utils import units

from manila.share.drivers.weka import exceptions

LOG = logging.getLogger(__name__)

# 1 GiB in bytes — use this constant for all GiB <-> bytes conversions.
GiB = units.Gi


def gb_to_bytes(size_gb):
    """Convert a size in GiB to bytes (integer)."""
    return int(size_gb * GiB)


def bytes_to_gb(size_bytes):
    """Convert a size in bytes to GiB (float, rounded to 2 decimal places)."""
    return round(float(size_bytes) / GiB, 2)


def retry_on_transient(max_retries=3, initial_delay=1.0, backoff=2.0,
                       transient_codes=(429, 500, 502, 503, 504)):
    """Decorator: retry *func* on transient Weka API errors.

    Uses exponential back-off.  Non-transient errors are re-raised
    immediately without retrying.

    :param max_retries: Maximum number of retry attempts.
    :param initial_delay: Initial sleep time in seconds before first retry.
    :param backoff: Multiplier applied to delay on each successive retry.
    :param transient_codes: HTTP status codes considered transient.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions.WekaApiError as exc:
                    if exc.status_code not in transient_codes:
                        raise
                    last_exc = exc
                    if attempt < max_retries:
                        LOG.warning(
                            "Weka API transient error %s on attempt %d/%d, "
                            "retrying in %.1fs: %s",
                            exc.status_code, attempt + 1, max_retries, delay,
                            exc,
                        )
                        time.sleep(delay)
                        delay *= backoff
            raise last_exc
        return wrapper
    return decorator


def sanitize_log_params(params, secret_keys=('password', 'token', 'secret')):
    """Return a copy of *params* with secret values replaced by '***'.

    Used to safely log API request parameters.
    """
    if not isinstance(params, dict):
        return params
    sanitized = {}
    for key, value in params.items():
        if any(s in key.lower() for s in secret_keys):
            sanitized[key] = '***'
        else:
            sanitized[key] = value
    return sanitized


def build_export_location(backends, fs_name, is_admin_only=False,
                          preferred=True, metadata=None):
    """Build a Manila export location dict for a WekaFS share.

    :param backends: Comma-separated list of Weka backend host addresses.
    :param fs_name: Weka filesystem name.
    :param is_admin_only: Whether this location is only for admin access.
    :param preferred: Whether this is the preferred export location.
    :param metadata: Optional dict of extra metadata.
    :returns: Export location dict compatible with Manila's expected format.
    """
    path = '{backends}/{fs_name}'.format(
        backends=backends, fs_name=fs_name)
    loc = {
        'path': path,
        'is_admin_only': is_admin_only,
        'metadata': metadata or {},
    }
    return loc
