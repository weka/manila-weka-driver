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

"""Test stub for manila.share.share_types.

In a real Manila deployment this module resolves a share type's extra
specs from the database.  The Weka driver only reads the
``weka:security_policy_group`` extra spec; unit tests mock this function,
so the stub returns an empty mapping by default.
"""


def get_share_type_extra_specs(share_type_id):
    """Return the extra specs for a share type id (stub: empty dict)."""
    return {}
