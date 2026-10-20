#!/usr/bin/env bash
# Sync the vendored driver + unit tests from the upstream openstack/manila
# tree into this repo.
#
# The driver merged upstream (change 989998, commit 024ce60b7), so
# ~/projects/manila is the source of truth and the copies here are a
# mirror.  They are NOT dead weight: devstack/plugin.sh symlinks
# manila/share/drivers/weka into the DevStack manila tree, and
# ci/ci-runner.sh uses it as the fallback when a Gerrit patch does not
# carry the driver itself.  A stale mirror therefore means devstack and
# the integration tests silently exercise old driver code while
# reporting success -- which is why this script exists.
#
#   tools/sync-from-upstream.sh           # copy upstream -> here
#   tools/sync-from-upstream.sh --check   # report drift, change nothing
#
# MANILA_DIR overrides the manila checkout (default ~/projects/manila).
#
# One-way only.  Edit the driver upstream, run this, then run the repo's
# own gates (tox -e py311,pep8,cover).

set -uo pipefail

MANILA_DIR="${MANILA_DIR:-$HOME/projects/manila}"
REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

[ -d "$MANILA_DIR" ] || { echo "no manila checkout at $MANILA_DIR"; exit 2; }

# Driver sources: same path upstream and here.
DRIVER_FILES="
manila/share/drivers/weka/__init__.py
manila/share/drivers/weka/client.py
manila/share/drivers/weka/config.py
manila/share/drivers/weka/driver.py
manila/share/drivers/weka/exceptions.py
manila/share/drivers/weka/posix.py
manila/share/drivers/weka/utils.py
manila/privsep/weka.py
"

# Unit tests: upstream manila/tests/share/drivers/weka/<f> -> tests/unit/<f>.
TEST_FILES="fakes.py test_client.py test_driver.py test_posix.py test_utils.py"

# Deliberately NOT synced:
#   stubs/**                     hand-written fakes for the manila framework,
#                                incl. stubs/manila/privsep/weka.py, which has
#                                drifted from the real one on purpose.
#   manila/privsep/__init__.py   repo-local (Weka copyright, trimmed).

drift=0
synced=0

copy_one() {
    local src="$1" dst="$2" label="$3"
    if [ ! -f "$src" ]; then
        echo "  MISSING upstream: $label"
        drift=1
        return
    fi
    if cmp -s "$src" "$dst"; then
        return
    fi
    drift=1
    if [ "$CHECK" = 1 ]; then
        echo "  DRIFT: $label"
    else
        cp "$src" "$dst"
        echo "  updated: $label"
        synced=$((synced + 1))
    fi
}

echo "==> driver sources"
for f in $DRIVER_FILES; do
    copy_one "$MANILA_DIR/$f" "$REPO_DIR/$f" "$f"
done

# The only edit the mirror needs: upstream tests import fakes by its
# manila package path, which does not exist under the stubs layout.
echo "==> unit tests"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
for f in $TEST_FILES; do
    src="$MANILA_DIR/manila/tests/share/drivers/weka/$f"
    if [ ! -f "$src" ]; then
        echo "  MISSING upstream: $f"
        drift=1
        continue
    fi
    sed 's|^from manila\.tests\.share\.drivers\.weka import fakes$|from tests.unit import fakes|' \
        "$src" > "$tmp"
    copy_one "$tmp" "$REPO_DIR/tests/unit/$f" "tests/unit/$f"
done

echo
if [ "$CHECK" = 1 ]; then
    if [ "$drift" = 0 ]; then
        echo "IN SYNC with $MANILA_DIR"
        exit 0
    fi
    echo "OUT OF SYNC with $MANILA_DIR -- run tools/sync-from-upstream.sh"
    exit 1
fi

if [ "$synced" = 0 ]; then
    echo "already in sync with $MANILA_DIR -- nothing copied"
else
    echo "synced $synced file(s) from $MANILA_DIR"
    echo "next: tox -e py311,pep8,cover   (py39 runs in GitHub Actions)"
fi
exit 0
