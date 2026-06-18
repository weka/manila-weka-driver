#!/usr/bin/env python3
"""Weka Manila CI - Gerrit Event Listener

Connects to review.opendev.org via SSH stream-events, watches for
openstack/manila patchset-created events and "recheck"/"run-weka-ci"
comments, and triggers CI runs serially.
"""

import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time

# ── Configuration ─────────────────────────────────────────────────────────────

GERRIT_HOST = "review.opendev.org"
GERRIT_PORT = 29418
GERRIT_USER = os.environ.get("GERRIT_USER", "Assaf")
PROJECT = "openstack/manila"
BRANCH = "master"

# Initial trusted rollout: the Weka driver is out-of-tree, so the CI runs
# only when a reviewer explicitly comments "run-weka-ci" on a patch. Set
# AUTO_RUN = True to also auto-run on every patchset-created event and on
# generic "recheck" comments once the CI is trusted.
AUTO_RUN = False

CI_DIR = "/opt/weka-ci"
STATE_DIR = "/var/lib/weka-ci"
LOCK_FILE = os.path.join(STATE_DIR, "runner.lock")
STATE_FILE = os.path.join(STATE_DIR, "tested.json")

JOB_TIMEOUT = 3600  # 1 hour hard timeout per job

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("weka-manila-ci")

# ── State management ─────────────────────────────────────────────────────────

def load_state():
    """Load the set of already-tested (change, patchset) pairs."""
    try:
        with open(STATE_FILE, "r") as f:
            return set(tuple(x) for x in json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_state(tested):
    """Persist the tested set."""
    with open(STATE_FILE, "w") as f:
        json.dump(list(tested), f)


def mark_tested(tested, change_num, patchset_num):
    """Record that we tested this patchset."""
    tested.add((str(change_num), str(patchset_num)))
    save_state(tested)


def clear_tested(tested, change_num, patchset_num):
    """Clear tested status (for recheck)."""
    tested.discard((str(change_num), str(patchset_num)))
    save_state(tested)

# ── Gerrit stream ────────────────────────────────────────────────────────────

def stream_events():
    """Connect to Gerrit SSH and yield parsed JSON events.

    Auto-reconnects on failure with exponential backoff.
    """
    backoff = 1
    while True:
        try:
            log.info(
                "Connecting to %s:%d as %s...",
                GERRIT_HOST, GERRIT_PORT, GERRIT_USER,
            )
            proc = subprocess.Popen(
                [
                    "ssh",
                    "-p", str(GERRIT_PORT),
                    "-o", "ServerAliveInterval=60",
                    "-o", "ServerAliveCountMax=3",
                    "-o", "StrictHostKeyChecking=no",
                    f"{GERRIT_USER}@{GERRIT_HOST}",
                    "gerrit", "stream-events",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            log.info("Connected to Gerrit stream-events")
            backoff = 1  # reset on successful connect

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Failed to parse event: %s", line[:200])

            # If we get here, the stream ended
            rc = proc.wait()
            log.warning("SSH stream ended with code %d", rc)

        except Exception as e:
            log.error("SSH connection error: %s", e)

        log.info("Reconnecting in %ds...", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)

# ── Event filtering ──────────────────────────────────────────────────────────

def parse_event(event):
    """Extract (change_num, patchset_num, revision, is_recheck) if this
    event should trigger a CI run, else return None."""
    etype = event.get("type")
    change = event.get("change", {})
    project = change.get("project", "")

    if project != PROJECT:
        return None

    branch = change.get("branch", "")
    if branch != BRANCH:
        return None

    if etype == "patchset-created":
        # Auto-run on new patchsets only once the CI is trusted.
        if not AUTO_RUN:
            return None
        ps = event.get("patchSet", {})
        return (
            str(change["number"]),
            str(ps["number"]),
            ps["revision"],
            False,
        )

    if etype == "comment-added":
        comment = event.get("comment", "")
        # "run-weka-ci" is the explicit Weka opt-in and always triggers.
        # Generic "recheck" re-runs all CIs; honor it only in AUTO_RUN mode
        # so the scoped rollout isn't pulled in by every unrelated recheck.
        triggered = re.search(
            r"\brun-weka-ci\b", comment, re.IGNORECASE) or (
            AUTO_RUN and re.search(r"\brecheck\b", comment, re.IGNORECASE))
        if triggered:
            ps = event.get("patchSet", {})
            return (
                str(change["number"]),
                str(ps["number"]),
                ps["revision"],
                True,
            )

    return None

# ── Job execution ────────────────────────────────────────────────────────────

def run_job(change_num, patchset_num, revision):
    """Run the CI job for a single patchset. Acquires file lock for
    serial execution."""
    log.info(
        "Starting CI job for change %s patchset %s (rev %s)",
        change_num, patchset_num, revision[:12],
    )

    lock_fd = open(LOCK_FILE, "w")
    try:
        # Block until lock is available (serial execution)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        log.info("Acquired lock, running ci-runner.sh")

        result = subprocess.run(
            [
                os.path.join(CI_DIR, "ci-runner.sh"),
                change_num,
                patchset_num,
                revision,
            ],
            timeout=JOB_TIMEOUT,
            capture_output=False,
        )
        log.info(
            "CI job completed with exit code %d for %s,%s",
            result.returncode, change_num, patchset_num,
        )

    except subprocess.TimeoutExpired:
        log.error(
            "CI job timed out after %ds for %s,%s",
            JOB_TIMEOUT, change_num, patchset_num,
        )
        # Post timeout failure
        try:
            subprocess.run(
                [
                    os.path.join(CI_DIR, "post-results.sh"),
                    change_num, patchset_num, revision,
                    "FAILURE", "CI job timed out after %ds" % JOB_TIMEOUT,
                    "/var/www/ci-logs/%s/%s" % (change_num, patchset_num),
                ],
                timeout=60,
            )
        except Exception as e:
            log.error("Failed to post timeout result: %s", e)

    except Exception as e:
        log.error("CI job failed with exception: %s", e)

    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

# ── Main loop ────────────────────────────────────────────────────────────────

def main():
    # Ensure state directory exists
    os.makedirs(STATE_DIR, exist_ok=True)

    tested = load_state()

    log.info("Weka Manila CI listener starting")
    log.info("Watching project: %s branch: %s", PROJECT, BRANCH)
    log.info(
        "Trigger mode: %s",
        "AUTO_RUN (patchset-created + recheck + run-weka-ci)" if AUTO_RUN
        else "comment-triggered only (run-weka-ci)",
    )

    for event in stream_events():
        parsed = parse_event(event)
        if parsed is None:
            continue

        change_num, patchset_num, revision, is_recheck = parsed

        if is_recheck:
            log.info(
                "Recheck requested for %s,%s", change_num, patchset_num,
            )
            clear_tested(tested, change_num, patchset_num)

        key = (change_num, patchset_num)
        if key in tested:
            log.debug(
                "Already tested %s,%s, skipping", change_num, patchset_num,
            )
            continue

        log.info(
            "Triggering CI for %s,%s (recheck=%s)",
            change_num, patchset_num, is_recheck,
        )
        mark_tested(tested, change_num, patchset_num)
        run_job(change_num, patchset_num, revision)


if __name__ == "__main__":
    # Handle SIGTERM gracefully
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        main()
    except KeyboardInterrupt:
        log.info("Shutting down")
        sys.exit(0)
