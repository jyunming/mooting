"""Mooting: a council where agent CLIs from different vendors deliberate, and a human decides."""

# One source of truth. A hand-maintained literal drifted to three different
# answers -- 0.0.1 here, 0.1.1 in pyproject, 0.1.0 in the installed metadata --
# so a bug report could not say which version it was against. Reported as
# issue #2. The fallback is for a source tree that was never installed.
try:
    from importlib.metadata import PackageNotFoundError, version as _version

    __version__ = _version("mooting")
except (ImportError, PackageNotFoundError):   # pragma: no cover - unusual install
    __version__ = "0+unknown"
