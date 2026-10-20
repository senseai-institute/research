# SPDX-License-Identifier: Apache-2.0
"""Background experiments: at most one runs at a time; the rest wait in order (spec §6).

``submit`` returns at once with the id the patched experiment will have. Listeners receive
progress dicts (``experiment_id``, ``phase``, ``frac``, ``message``) as bench reports them,
which the server forwards as ``job_progress`` SSE events.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from senseai_research.config import Settings
from senseai_research.experiments import ExperimentSpec, run_experiment
from senseai_research.ledger.models import Experiment, new_id
from senseai_research.ledger.store import Store

Listener = Callable[[dict[str, Any]], None]


@dataclass
class Job:
    id: str
    spec: ExperimentSpec
    actor: str
    status: str = "queued"  # queued | running | done | failed
    error: str | None = None
    last: dict[str, Any] = field(default_factory=dict)
    baseline_id: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


class ExperimentQueue:
    def __init__(self, store: Store, cfg: Settings, *, runner: Any = run_experiment) -> None:
        self.store = store
        self.cfg = cfg
        self._runner = runner
        self._q: queue.Queue[Job | None] = queue.Queue()
        self.jobs: dict[str, Job] = {}
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._work, daemon=True, name="experiments")
        self._worker.start()

    def subscribe(self, fn: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(fn)

        def unsubscribe() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)

        return unsubscribe

    def _emit(self, job: Job, payload: dict[str, Any]) -> None:
        msg = {"job_id": job.id, "status": job.status, **payload}
        job.last = msg
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(msg)
            except Exception:  # a broken listener must not stop the run
                continue

    def submit(self, spec: ExperimentSpec, actor: str) -> Job:
        job = Job(id=new_id("exp"), spec=spec, actor=actor)
        self.jobs[job.id] = job
        ahead = sum(1 for j in self.jobs.values() if j.status in ("queued", "running")) - 1
        self._q.put(job)
        self._emit(job, {"message": f"queued ({ahead} ahead)" if ahead else "starting"})
        return job

    def position(self, job_id: str) -> int:
        waiting = [j.id for j in self.jobs.values() if j.status == "queued"]
        return waiting.index(job_id) + 1 if job_id in waiting else 0

    def _work(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                return
            job.status = "running"
            try:
                baseline, _ = self._runner(
                    self.store,
                    self.cfg,
                    job.spec,
                    actor=job.actor,
                    progress=lambda p, j=job: self._emit(j, p),
                    exp_id=job.id,
                )
                job.baseline_id = baseline.id
                job.status = "done"
                self._emit(job, {"frac": 1.0, "message": "experiment finished"})
            except Exception as e:
                job.status = "failed"
                job.error = str(e)
                if self.store.get_raw("experiment", job.id) is None:
                    self.store.put_experiment(
                        Experiment(
                            id=job.id,
                            created_by=job.actor,
                            status="failed",
                            error=str(e),
                            sequences=job.spec.sequences,
                            arms=job.spec.arms,
                        ),
                        job.actor,
                    )
                self._emit(job, {"message": f"experiment failed: {e}"})
            finally:
                job.done.set()

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        job = self.jobs[job_id]
        job.done.wait(timeout)
        return job

    def close(self) -> None:
        self._q.put(None)


__all__ = ["ExperimentQueue", "Job"]
