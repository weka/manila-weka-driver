#!/usr/bin/env bash
# Pre-push gate for Weka driver changes in the upstream openstack/manila tree.
#
# Replaces the old GitHub Actions pipeline (lint + unit tests + 100% coverage)
# with the upstream toolchain, so what you run locally is what Zuul runs --
# plus the Weka-specific 100% coverage bar that upstream's whole-tree `cover`
# job does not enforce.
#
#   tools/weka-precheck.sh              # lint + tests + coverage + mirror matrix
#   tools/weka-precheck.sh --full       # also run the full pre-commit pep8 gate
#   tools/weka-precheck.sh --no-mirror  # skip the mirror matrix (fast inner loop)
#
# MANILA_DIR overrides the manila checkout (default ~/projects/manila).
# MIRROR_DIR overrides the vendored mirror (default ~/projects/manila-weka-driver).

set -uo pipefail

MANILA_DIR="${MANILA_DIR:-$HOME/projects/manila}"
MIRROR_DIR="${MIRROR_DIR:-$HOME/projects/manila-weka-driver}"
TESTS="manila.tests.share.drivers.weka"
SRC="manila/share/drivers/weka"
FULL=0
MIRROR=1
for arg in "$@"; do
    case "$arg" in
        --full)      FULL=1 ;;
        --no-mirror) MIRROR=0 ;;
        *) echo "unknown option: $arg"; exit 2 ;;
    esac
done

cd "$MANILA_DIR" || { echo "no manila checkout at $MANILA_DIR"; exit 2; }

PY=".tox/py3/bin"
if [ ! -x "$PY/python" ]; then
    echo "==> .tox/py3 missing; building it (one-time, slow)"
    tox -e py3 --notest || exit 2
fi

rc=0
step() { printf '\n==> %s\n' "$1"; }
fail() { echo "FAILED: $1"; rc=1; }

step "lint"
if [ "$FULL" = 1 ]; then
    # What Zuul actually gates on.  Slow (whole tree, every hook).
    tox -e pep8 || fail "pep8 (pre-commit, full tree)"
else
    # flake8 + hacking on the weka paths only, using manila's own [flake8]
    # config from tox.ini.  (Upstream's `tox -e fast8` does not run on
    # macOS: tools/fast8.sh needs GNU diff's --from-file and flake8's
    # long-removed --diff flag.)
    "$PY/python" -m flake8 "$SRC" manila/tests/share/drivers/weka \
        || fail "flake8 (weka paths)"
fi

step "unit tests ($TESTS)"
"$PY/python" -m stestr run "$TESTS" || fail "unit tests"

step "coverage (100% of $SRC)"
"$PY/coverage" erase
PYTHON="$PY/coverage run --source $SRC --parallel-mode" \
    "$PY/python" -m stestr run "$TESTS" >/dev/null 2>&1
"$PY/coverage" combine >/dev/null 2>&1
# Statement coverage only: manila's .coveragerc turns branch coverage on,
# so the headline % reads ~99 even at zero missed statements.  The bar the
# driver has always been held to is "Miss == 0", which is what we gate on.
"$PY/coverage" report --no-skip-covered 2>/dev/null | tail -n 12
missed=$("$PY/coverage" json -o - 2>/dev/null \
    | "$PY/python" -c 'import json,sys; print(json.load(sys.stdin)["totals"]["missing_lines"])')
if [ "${missed:-1}" != 0 ]; then
    fail "$missed uncovered statement(s) in $SRC (see Miss column above)"
fi

step "mirror matrix (manila-weka-driver stubs harness)"
# The vendored copy in this repo is the payload devstack symlinks, and its
# CI runs the stubs harness across several interpreters -- coverage the
# upstream gate above does not give you, since it runs one interpreter
# against the real framework.  Sync in, run it, revert: the mirror is a
# deployment artifact and must only be *committed* once the change has
# actually merged upstream.
if [ "$MIRROR" = 0 ]; then
    echo "  skipped (--no-mirror)"
elif [ ! -d "$MIRROR_DIR" ]; then
    echo "  skipped (no mirror checkout at $MIRROR_DIR)"
elif [ -n "$(git -C "$MIRROR_DIR" status --porcelain -- manila tests)" ]; then
    # Reverting would destroy whatever is in there.
    fail "mirror has uncommitted changes under manila/ or tests/; commit or
        stash them in $MIRROR_DIR first"
else
    # Restore the mirror no matter how we leave this block.
    trap 'git -C "$MIRROR_DIR" checkout -q -- manila tests 2>/dev/null' EXIT

    if ! "$MIRROR_DIR/tools/sync-from-upstream.sh" >/dev/null 2>&1; then
        fail "sync-from-upstream.sh"
    else
        envs=""
        for ver in 3.11 3.12 3.14; do
            if command -v "python$ver" >/dev/null 2>&1; then
                envs="${envs}py${ver//./},"
            else
                echo "  note: skipping py${ver//./} -- no python$ver on PATH"
            fi
        done
        envs="${envs}pep8,cover"
        echo "  running: tox -e $envs"
        ( cd "$MIRROR_DIR" && tox -e "$envs" ) >/dev/null 2>&1 \
            || fail "mirror matrix (cd $MIRROR_DIR && tox -e $envs)"
    fi

    git -C "$MIRROR_DIR" checkout -q -- manila tests 2>/dev/null
    trap - EXIT
fi

step "release note present?"
# Tracked changes plus any new, still-untracked note file.
notes=$( { git diff --name-only gerrit/master -- releasenotes/notes
           git ls-files -o --exclude-standard -- releasenotes/notes
         } 2>/dev/null | wc -l | tr -d ' ')
if [ "$notes" = 0 ]; then
    echo "WARNING: no release note added -- run 'tox -e newnote' if this"
    echo "         change is user-visible."
fi

printf '\n'
[ "$rc" = 0 ] && echo "PRECHECK PASSED -- safe to 'git review -w' and comment run-weka-ci" \
             || echo "PRECHECK FAILED -- do not push"
exit "$rc"
