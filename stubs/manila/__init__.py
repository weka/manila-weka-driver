# Extend the manila package path so that both stubs and the real driver
# code (at the repo root) are importable under the manila namespace.
from pkgutil import extend_path
__path__ = extend_path(__path__, __name__)
