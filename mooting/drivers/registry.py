"""Build the driver map the supervisor uses.

Keyed by agent *name* rather than kind, so two seats can run the same CLI with
different working directories or models -- e.g. a `claude-historian` seat pointed
at the research repo and a `claude-engine` seat pointed at the engine.
"""

from __future__ import annotations

import json
from typing import Sequence

from ..store import Store
from .base import Driver
from .spawn import DRIVER_CLASSES


def build_drivers(store: Store, only: Sequence[str] | None = None) -> dict[str, Driver]:
    drivers: dict[str, Driver] = {}
    for a in store.agents():
        name = a["name"]
        if only and name not in only:
            continue
        if a["kind"] in {"human", "external"} or not a["enabled"] or a["driver"] == "none":
            continue  # human seats read the board; they are never woken
        cls = DRIVER_CLASSES.get(a["kind"])
        if cls is None:
            continue
        cfg = json.loads(a["driver_cfg"])
        extra: list[str] = list(cfg.get("extra_argv", []))
        if cfg.get("model"):
            extra += ["--model", cfg["model"]]
        drivers[name] = cls(store.path, timeout_s=cfg.get("timeout_s", 300.0), extra_argv=extra)
    return drivers


__all__ = ["build_drivers"]
