#!/usr/bin/env python3
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

"""Add WEKAFS to Manila's SUPPORTED_SHARE_PROTOCOLS, in place.

Needed until the driver merges upstream: manila.conf sets
enabled_share_protocols=NFS,WEKAFS, and manila-api refuses to start if a
listed protocol is not in the tuple.

This replaces a `sed s/'MAPRFS')/'MAPRFS', 'WEKAFS')/` that assumed
'MAPRFS' was the last entry.  When the Lustre driver added 'LUSTRE' after
it the substitution silently matched nothing, manila-api failed to start
with ConfigFileValueError, and every API call returned HTTP 500.  Parsing
the tuple instead of pattern-matching one member keeps that from
recurring the next time a protocol is added.

Exit codes:
    0  WEKAFS is present (added now, or already there)
    1  could not patch -- caller must treat this as fatal
"""

import re
import sys

MARKER = 'WEKAFS'
PATTERN = re.compile(
    r'(SUPPORTED_SHARE_PROTOCOLS\s*=\s*\()(?P<body>[^)]*?)(,?\s*)(\))',
    re.MULTILINE,
)


def main(argv):
    if len(argv) != 2:
        sys.stderr.write('usage: patch-wekafs-protocol.py <constants.py>\n')
        return 1
    path = argv[1]

    try:
        with open(path) as fh:
            src = fh.read()
    except OSError as exc:
        sys.stderr.write('cannot read %s: %s\n' % (path, exc))
        return 1

    match = PATTERN.search(src)
    if not match:
        sys.stderr.write(
            'SUPPORTED_SHARE_PROTOCOLS tuple not found in %s -- Manila may '
            'have restructured it; this patcher needs updating\n' % path)
        return 1

    if re.search(r"['\"]%s['\"]" % MARKER, match.group('body')):
        print('WEKAFS already in SUPPORTED_SHARE_PROTOCOLS')
        return 0

    # Match the quoting and indentation of the last existing entry so the
    # result still passes pep8 and reads like the surrounding code.
    entries = re.findall(r"(['\"])(\w+)\1", match.group('body'))
    quote = entries[-1][0] if entries else "'"
    body = match.group('body').rstrip()
    last_line = body.rsplit('\n', 1)[-1]
    indent = re.match(r'\s*', last_line).group(0)

    addition = '%s%s%s' % (quote, MARKER, quote)
    if len(last_line) + len(addition) + 3 > 79:
        new_body = '%s,\n%s%s' % (body, indent or '    ', addition)
    else:
        new_body = '%s, %s' % (body, addition)

    patched = '%s%s%s' % (match.group(1), new_body, match.group(4))
    src = src[:match.start()] + patched + src[match.end():]

    try:
        with open(path, 'w') as fh:
            fh.write(src)
    except OSError as exc:
        sys.stderr.write('cannot write %s: %s\n' % (path, exc))
        return 1

    # Read back rather than trust the write: a silent no-op here is exactly
    # the failure mode this script exists to eliminate.
    with open(path) as fh:
        check = PATTERN.search(fh.read())
    if not check or not re.search(r"['\"]%s['\"]" % MARKER,
                                  check.group('body')):
        sys.stderr.write('patch did not take effect in %s\n' % path)
        return 1

    print('Patched WEKAFS into SUPPORTED_SHARE_PROTOCOLS (%s)'
          % check.group('body').strip().replace('\n', ' '))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
