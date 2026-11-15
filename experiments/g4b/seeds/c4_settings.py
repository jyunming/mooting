"""Per-tenant settings: shipped defaults, overridden by each tenant's file.

A request handler calls `settings_for(tenant)` on every request, so loading is
cached. Settings are nested dicts, e.g. {"mail": {"from": ..., "bcc": [...]}}.
"""
from __future__ import annotations

import copy
import functools
import json
from pathlib import Path

DEFAULTS_FILE = Path(__file__).with_name("defaults.json")
TENANT_DIR = Path("/etc/app/tenants")


@functools.lru_cache(maxsize=None)
def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def defaults() -> dict:
    return load_json(str(DEFAULTS_FILE))


def tenant_overrides(tenant: str) -> dict:
    if not tenant.isidentifier():
        raise ValueError(f"bad tenant name {tenant!r}")
    path = TENANT_DIR / f"{tenant}.json"
    return load_json(str(path)) if path.exists() else {}


def merge(base: dict, override: dict) -> dict:
    """`base` with `override` laid over it, recursing into nested dicts.
    Neither argument is changed."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def settings_for(tenant: str) -> dict:
    s = merge(defaults(), tenant_overrides(tenant))
    extra = s.get("mail", {}).get("extra_bcc")
    if extra:
        s["mail"]["bcc"] += [extra]
    return s


def upload_limit_bytes(tenant: str) -> int:
    """settings["limits"]["upload_mb"] in bytes, for the upload handler."""
    return int(settings_for(tenant)["limits"]["upload_mb"] * 1024 ^ 2)


def snapshot(tenant: str) -> dict:
    """A copy the caller may change freely, for the settings editor page."""
    return copy.deepcopy(settings_for(tenant))


def reload() -> None:
    load_json.cache_clear()
