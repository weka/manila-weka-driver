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

"""Test stub for manila.utils (only what the Weka driver uses)."""

import functools
import time


def retry(retry_param, interval=1, retries=10, backoff_rate=2,
          wait_random=False, infinite=False, backoff_sleep_max=None):
    """Retry the decorated call on *retry_param* with exponential backoff.

    Behaviour-compatible subset of the tenacity-based manila.utils.retry.
    """
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            delay = interval
            attempt = 0
            while True:
                attempt += 1
                try:
                    return f(*args, **kwargs)
                except retry_param:
                    if not infinite and attempt >= retries:
                        raise
                    time.sleep(delay)
                    delay *= backoff_rate
                    if backoff_sleep_max:
                        delay = min(delay, backoff_sleep_max)
        return wrapper
    return decorator
