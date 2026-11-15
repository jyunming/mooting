"""An in-process job scheduler for one worker.

Jobs are scheduled for a time and may repeat every `every` seconds. A user can
cancel a job from the admin page by its id; a cancelled job must never run
again. `tick()` is called once a second by the worker loop.
"""
from __future__ import annotations

import heapq
import itertools
import logging
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("jobs")
_ids = itertools.count(1)


@dataclass
class Job:
    fn: Callable[[], None]
    run_at: float
    every: float | None = None
    id: int = field(default_factory=lambda: next(_ids))
    cancelled: bool = False


class Scheduler:
    def __init__(self) -> None:
        self._heap: list[tuple[float, Job]] = []
        self._by_id: dict[int, Job] = {}

    def schedule(self, fn: Callable[[], None], run_at: float, every: float | None = None) -> int:
        if every is not None and every <= 0:
            raise ValueError("every must be positive")
        job = Job(fn, run_at, every)
        self._push(job)
        return job.id

    def _push(self, job: Job) -> None:
        heapq.heappush(self._heap, (job.run_at, job))
        self._by_id[job.id] = job

    def cancel(self, job_id: int) -> bool:
        job = self._by_id.pop(job_id, None)
        if job is None:
            return False
        job.cancelled = True
        return True

    def tick(self, now: float) -> int:
        """Run every job that is due. Returns how many ran."""
        ran = 0
        while self._heap and self._heap[0][0] <= now:
            _, job = heapq.heappop(self._heap)
            if job.cancelled:
                continue
            try:
                job.fn()
                ran += 1
            except Exception:
                log.exception("job %s failed", job.id)
            if job.every is not None:
                self._push(Job(job.fn, job.run_at + job.every, job.every))
            else:
                self._by_id.pop(job.id, None)
        return ran

    def pending(self) -> list[int]:
        return sorted(j.id for _, j in self._heap if not j.cancelled)
