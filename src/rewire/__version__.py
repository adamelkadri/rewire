"""Single source of truth for the package version."""

from importlib.metadata import PackageNotFoundError, version

try:  # pragma: no cover - trivial packaging fallback
    __version__ = version("rewire")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
