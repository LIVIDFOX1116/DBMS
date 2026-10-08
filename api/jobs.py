"""
FrameCheck: running detectors and audits from the UI instead of a terminal.

Deliberately a small in-process job registry, not a task queue. This is a
single-operator desktop tool whose backend is a sidecar the app owns: a
Celery/RQ deployment would add a broker to install and supervise for work
that is always one user pressing one button. The honest cost of that choice
is written down rather than hidden:

- jobs live in memory, so a backend restart forgets them. What they wrote to
  Postgres survives, because detectors and audits commit as they go -- a
  forgotten job is a lost progress bar, never lost analysis.
- one job runs at a time (a lock, not a pool). Two concurrent audit passes on
  one snapshot would interleave writes to the same flags and compete for the
  same CPU, which is slower AND harder to reason about than running them in
  order.

Jobs only ever INSERT: detectors add flag rows, audits add flag_audit rows.
Nothing here deletes or rewrites existing analysis.
"""
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

import psycopg2

from graph import audit as audit_module
from graph import detectors as detectors_module
from ingestion import config

log = logging.getLogger("framecheck.jobs")

JOB_TYPES = ("detectors", "audit")
MAX_REMEMBERED_JOBS = 50


@dataclass
class Job:
    job_id: str
    job_type: str
    snapshot_id: int
    seed_wallet_id: int
    detector: str | None = None
    limit: int | None = None
    status: str = "queued"       # queued | running | done | failed | cancelled
    total: int = 0
    completed: int = 0
    message: str = ""
    error: str | None = None
    result: dict = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id, "job_type": self.job_type,
            "snapshot_id": self.snapshot_id, "seed_wallet_id": self.seed_wallet_id,
            "detector": self.detector, "limit": self.limit,
            "status": self.status, "total": self.total, "completed": self.completed,
            "message": self.message, "error": self.error, "result": self.result,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "elapsed_seconds": round((self.finished_at or time.time()) - self.started_at, 1),
        }


_jobs: dict[str, Job] = {}
_order: list[str] = []
_lock = threading.Lock()
_running: Job | None = None


def _remember(job: Job) -> None:
    with _lock:
        _jobs[job.job_id] = job
        _order.append(job.job_id)
        while len(_order) > MAX_REMEMBERED_JOBS:
            _jobs.pop(_order.pop(0), None)


def get_job(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def list_jobs() -> list[dict]:
    with _lock:
        return [_jobs[j].as_dict() for j in reversed(_order) if j in _jobs]


def active_job() -> Job | None:
    with _lock:
        return _running if _running and _running.status in ("queued", "running") else None


def cancel(job_id: str) -> bool:
    """Asks a job to stop at its next checkpoint. Work already committed
    stays committed -- this is 'stop early', not 'undo'."""
    job = get_job(job_id)
    if job is None or job.status not in ("queued", "running"):
        return False
    job._cancel.set()
    job.message = "cancelling…"
    return True


def start(job_type: str, snapshot_id: int, seed_wallet_id: int,
          detector: str | None = None, limit: int | None = None) -> Job:
    if job_type not in JOB_TYPES:
        raise ValueError(f"unknown job type {job_type!r}")
    existing = active_job()
    if existing is not None:
        raise RuntimeError(f"a {existing.job_type} job is already running")

    job = Job(job_id=uuid.uuid4().hex[:12], job_type=job_type, snapshot_id=snapshot_id,
              seed_wallet_id=seed_wallet_id, detector=detector, limit=limit)
    _remember(job)
    global _running
    with _lock:
        _running = job
    threading.Thread(target=_run, args=(job,), daemon=True, name=f"framecheck-job-{job.job_id}").start()
    return job


def _run(job: Job) -> None:
    job.status = "running"
    try:
        conn = psycopg2.connect(**config.PG_DSN)
        try:
            if job.job_type == "detectors":
                _run_detectors(job, conn)
            else:
                _run_audit(job, conn)
        finally:
            conn.close()
        job.status = "cancelled" if job._cancel.is_set() else "done"
        job.message = "cancelled" if job._cancel.is_set() else "finished"
    except Exception as e:
        # The UI gets the type and message, never a traceback; the full
        # detail goes to the backend log the desktop app can surface.
        log.exception("Job %s (%s) failed", job.job_id, job.job_type)
        job.status = "failed"
        job.error = f"{type(e).__name__}: {e}"
        job.message = "failed"
    finally:
        job.finished_at = time.time()
        global _running
        with _lock:
            if _running is job:
                _running = None


def _run_detectors(job: Job, conn) -> None:
    names = detectors_module.DETECTOR_NAMES if job.detector in (None, "all") else (job.detector,)
    job.total = len(names)
    runners = {
        "bfs_proximity": detectors_module.run_bfs_proximity,
        "sybil_cluster": detectors_module.run_sybil_cluster,
        "pagerank_threshold": detectors_module.run_pagerank_threshold,
        "impurity": detectors_module.run_impurity,
        "engaged_proximity": detectors_module.run_engaged_proximity,
    }
    flags_per_detector = {}
    for name in names:
        if job._cancel.is_set():
            break
        job.message = f"running {name}"
        flags_per_detector[name] = runners[name](conn, job.snapshot_id, job.seed_wallet_id)
        job.completed += 1
        job.result = {"flags_per_detector": dict(flags_per_detector)}
    job.result = {"flags_per_detector": flags_per_detector,
                  "flags_total": sum(flags_per_detector.values())}


def _run_audit(job: Job, conn) -> None:
    with conn.cursor() as cur:
        query = """
            SELECT f.flag_id FROM flag f JOIN analysis_run ar ON ar.run_id = f.run_id
            WHERE ar.snapshot_id = %s
        """
        params = [job.snapshot_id]
        if job.detector and job.detector != "all":
            query += " AND f.detector = %s"
            params.append(job.detector)
        # Same ordering as graph/audit.py's CLI, so a bounded run from the
        # UI samples the same flags as the same bounded run from a terminal.
        query += " ORDER BY f.flag_id"
        if job.limit:
            query += " LIMIT %s"
            params.append(job.limit)
        cur.execute(query, params)
        flag_ids = [row[0] for row in cur.fetchall()]

    job.total = len(flag_ids)
    verdicts = {"frameable": 0, "robust": 0}
    for i, flag_id in enumerate(flag_ids, start=1):
        if job._cancel.is_set():
            break
        job.message = f"auditing flag {flag_id} ({i}/{len(flag_ids)})"
        result = audit_module.audit_flag(conn, flag_id, job.seed_wallet_id)
        verdicts[result["verdict"]] = verdicts.get(result["verdict"], 0) + 1
        job.completed = i
        job.result = {"verdicts": dict(verdicts)}
    job.result = {"verdicts": verdicts, "audited": job.completed}
