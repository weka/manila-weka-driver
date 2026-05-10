# Contributing to the Manila Weka Driver

## Development Setup

```bash
git clone git@github.com:weka/manila-weka-driver.git
cd manila-weka-driver
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pip install -r test-requirements.txt
```

## Running Tests

```bash
# Unit tests
tox -e py311

# Linting
tox -e pep8

# Coverage
tox -e cover
```

## Code Style

This project follows OpenStack Hacking rules. Run `tox -e pep8` before
submitting a patch. Key rules:
- Max line length: 79 characters
- Use oslo.log, not stdlib logging
- No print() calls
- Python 3 only (no six)

## Submitting Changes

1. Fork the repository
2. Create a feature branch
3. Write tests for new functionality
4. Ensure `tox -e pep8` and `tox -e py311` both pass
5. Open a pull request with a clear description

## Reporting Bugs

Please open a GitHub issue with:
- Weka cluster version
- Manila version
- Driver version
- Relevant logs (sanitize credentials)
- Steps to reproduce
