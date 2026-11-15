"""A small key-value store kept on disk, sharded across a fixed number of files.

Each shard is a log of JSON lines. A put appends; a delete appends a tombstone;
`compact` rewrites a shard keeping only the live value of each key. The store is
opened by a web worker at start-up and by a nightly batch job.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

SHARDS = 16


class Store:
    def __init__(self, root: str | os.PathLike) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ layout

    def _shard_for(self, key: str) -> Path:
        return self.root / f"shard-{hash(key) % SHARDS:02d}.jsonl"

    def _records(self, shard: Path) -> Iterator[dict[str, Any]]:
        if not shard.exists():
            return
        with shard.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    # ------------------------------------------------------------------ access

    def put(self, key: str, value: Any) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("keys are non-empty strings")
        rec = json.dumps({"k": key, "v": value}, ensure_ascii=False)
        with self._shard_for(key).open("a", encoding="utf-8") as f:
            f.write(rec + "\n")

    def delete(self, key: str) -> None:
        with self._shard_for(key).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"k": key, "deleted": True}) + "\n")

    def get(self, key: str, default: Any = None) -> Any:
        for rec in self._records(self._shard_for(key)):
            if rec["k"] == key:
                return default if rec.get("deleted") else rec["v"]
        return default

    def keys(self) -> list[str]:
        live: dict[str, bool] = {}
        for shard in sorted(self.root.glob("shard-*.jsonl")):
            for rec in self._records(shard):
                live[rec["k"]] = not rec.get("deleted")
        return sorted(k for k, alive in live.items() if alive)

    # ------------------------------------------------------------- maintenance

    def compact(self, shard: Path) -> int:
        """Rewrite one shard with only its live records. Returns how many remain."""
        latest: dict[str, dict[str, Any]] = {}
        for rec in self._records(shard):
            latest[rec["k"]] = rec
        keep = [r for r in latest.values() if not r.get("deleted")]
        tmp = shard.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in keep:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, shard)
        return len(keep)

    def compact_all(self) -> int:
        return sum(self.compact(s) for s in sorted(self.root.glob("shard-*.jsonl")))
