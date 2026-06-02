# CLAUDE.md — Manila Weka Driver

## Project Overview

OpenStack Manila share driver for Weka storage. Exposes Weka filesystems as Manila shares via two protocols: WEKAFS (POSIX kernel client, sub-250 µs latency) and NFS (fallback).

## Tech Stack

- Python 3.9+ (tested 3.9, 3.11, 3.12)
- OpenStack Manila 2023.1+ (tested 2024.2)
- Weka cluster 5.x (REST API v2, port 14000)
- Oslo libraries (oslo_config, oslo_log, oslo_concurrency)

## Project Structure

```
manila-weka-driver/
├── manila/share/drivers/weka/    # Driver source code
│   ├── driver.py                 # Main WekaShareDriver (1,047 lines)
│   ├── client.py                 # Weka REST API client (1,132 lines)
│   ├── config.py                 # oslo.config option definitions
│   ├── posix.py                  # WekaFS mount manager
│   ├── exceptions.py             # Custom exception classes
│   └── utils.py                  # Helper utilities
├── tests/unit/                   # Unit tests (~2,500 lines)
│   ├── fakes.py                  # Mock object factories
│   ├── test_driver.py            # Driver tests
│   ├── test_client.py            # API client tests
│   ├── test_posix.py             # Mount manager tests
│   └── test_utils.py             # Utility tests
├── stubs/                        # Test dependency mocks for Manila/Oslo
├── devstack/                     # DevStack plugin for automated setup
├── ci/                           # Third-party CI (Gerrit listener + tempest)
├── docs/                         # Documentation
│   ├── architecture.md
│   ├── configuration.md
│   ├── deployment.md
│   ├── api-mapping.md
│   └── known-issues.md
├── .github/workflows/            # GitHub Actions CI/CD
│   ├── ci.yml                    # Lint + coverage (85% threshold)
│   ├── unit-tests.yml            # Unit test matrix
│   └── release.yml               # Tag-based release
├── tox.ini                       # Test environments
├── test-requirements.txt         # Test dependencies
├── CONTRIBUTING.md
└── README.md
```

## Key Commands

```bash
# Unit tests
tox -e py311
pytest tests/unit/ -v

# Linting (OpenStack Hacking rules, 79 char line limit)
tox -e pep8

# Coverage (85% threshold)
tox -e cover
pytest --cov=manila/share/drivers/weka --cov-fail-under=85

# Run specific test
pytest tests/unit/test_driver.py::TestWekaShareDriver::test_create_share -v
```

## Code Style

- OpenStack Hacking rules enforced via flake8
- Max line length: 79 characters
- PYTHONPATH must include `stubs:` for unit tests (handled by tox.ini)

## Architecture Notes

- `driver.py` implements the Manila `ShareDriver` interface (create, delete, extend, shrink, snapshot, access control, stats)
- `client.py` wraps Weka REST API v2 with auth, retries, and error handling
- `posix.py` manages WekaFS kernel mounts via `mount -t wekafs`
- All operations are idempotent
- WEKAFS access rules are accepted as no-op (no Manila-level access control for WEKAFS)
- NFS access rules use IP-based enforcement

## Agentic Flow

The main agent is an orchestrator. It should delegate work via Task tool and minimize direct tool use. Direct tool use is acceptable only for 1-2 quick checks to orient. Model should always be set explicitly on tasks/subagents.

### Delegation Model (in order)

1. **Haiku task** — all codebase exploration, investigation, searching, and reading files. Even for complex debugging — haiku can read and trace code paths. It's 10-20x cheaper than opus.
2. **Sonnet task** — code edits, test runs, build verification, deploy flows.
3. **Opus task** — only for complex plan generation that requires deep understanding of the codebase. Use it only while having better initial context from a haiku task, or when sonnet is struggling with execution.

## Rules

- On each code change, update CLAUDE.md and README.md if needed
- Run `/simplify` on each code change
