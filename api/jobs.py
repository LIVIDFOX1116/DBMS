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

Jobs only ever INSERT: detectors add flag rows, audits add flag_audit rows,
ingestion upserts wallets/transactions. Nothing here deletes or rewrites
existing analysis.
"""
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

import psycopg2

from api import dbsetup
from graph import audit as audit_module
from graph import detectors as detectors_module
from ingestion import config, ingest_subgraph
from ingestion.etherscan_client import BudgetExhausted, EtherscanClient

log = logging.getLogger("framecheck.jobs")

JOB_TYPES = ("detectors", "audit", "ingest")
MAX_REMEMBERED_JOBS = 50


@dataclass
class Job:
    job_id: str
    job_type: str
    snapshot_id: int
    seed_wallet_id: int
    detector: str | None = None
    limit: int | None = None
    # Job-type-specific input. The three job types do not take the same
    # arguments -- an ingest run has an address, a hop count and a call
    # budget, none of which mean anything to an audit -- and widening the
    # dataclass with one optional field per type would make every job carry
    # every other job's parameters.
    params: dict = field(default_factory=dict)
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
            "detector": self.detector, "limit": self.limit, "params": self.params,
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
          detector: str | None = None, limit: int | None = None,
          params: dict | None = None) -> Job:
    if job_type not in JOB_TYPES:
        raise ValueError(f"unknown job type {job_type!r}")
    existing = active_job()
    if existing is not None:
        article = "an" if existing.job_type[0] in "aeiou" else "a"
        raise RuntimeError(f"{article} {existing.job_type} job is already running")

    job = Job(job_id=uuid.uuid4().hex[:12], job_type=job_type, snapshot_id=snapshot_id,
              seed_wallet_id=seed_wallet_id, detector=detector, limit=limit,
              params=dict(params or {}))
    _remember(job)
    global _running
    with _lock:
        _running = job
    threading.Thread(target=_run, args=(job,), daemon=True, name=f"framecheck-job-{job.job_id}").start()
    return job


def _run(job: Job) -> None:
    job.status = "running"
    try:
        # dbsetup.current_dsn(), not config.PG_DSN: the connection settings
        # can be changed from the Database view at runtime, and a job that
        # ignored that would write to whichever database the environment
        # happened to name at import time.
        conn = psycopg2.connect(**dbsetup.current_dsn())
        try:
            if job.job_type == "detectors":
                _run_detectors(job, conn)
            elif job.job_type == "ingest":
                _run_ingest(job, conn)
            else:
                _run_audit(job, conn)
        finally:
            conn.close()
        job.status = "cancelled" if job._cancel.is_set() else "done"
        if job._cancel.is_set():
            job.message = "cancelled"
        elif job.total:
            job.message = "finished"
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


def _run_ingest(job: Job, conn) -> None:
    """Automatic ingestion: crawl outward from an address, inside the call
    budget the user's Etherscan plan allows.

    Two limits, because they are different things and only one of them is
    about speed:

    - `requests_per_second` paces the calls. Etherscan's free keys are
      rate-limited per second, and the client has always throttled to
      config.REQUESTS_PER_SECOND (4, deliberately under the documented
      free-tier ceiling of 5/s). A user with a paid plan can raise it.
    - `max_calls` is a hard stop. A rate limit alone does not stop a crawl
      from quietly consuming an entire day's allowance over several hours,
      which is someone else's resource to spend -- so the job stops at the
      budget and says how far it got, rather than running until Etherscan
      starts refusing.

    Cancellation and the budget both land in the same place: crawl() calls
    back once per address, right after that address is committed and
    checkpointed, so stopping is clean and the rest resumes later.
    """
    address = (job.params.get("address") or config.CASE_STUDY_ADDRESS).strip()
    hops = int(job.params.get("hops") or config.HOP_COUNT)
    rps = float(job.params.get("requests_per_second") or config.REQUESTS_PER_SECOND)
    max_calls = job.params.get("max_calls")
    max_calls = int(max_calls) if max_calls else None

    client = EtherscanClient(api_key=job.params.get("api_key") or None,
                             requests_per_second=rps, max_calls=max_calls)
    if not client.api_key:
        raise RuntimeError("no Etherscan API key is set -- add one in Settings first")

    job.total = 0          # a BFS frontier is not known in advance; see below
    job.message = f"crawling {hops} hop(s) from {address[:10]}…"

    def progress(stats):
        # `total` stays 0 on purpose: the number of addresses a crawl will
        # visit is discovered as it goes, so a percentage here would be a
        # fabricated denominator. The UI shows a count and the live frontier
        # size instead.
        job.completed = stats["addresses_processed"]
        remaining = client.calls_remaining()
        job.message = (f"hop {stats['hops_completed']}, {stats['addresses_processed']} addresses, "
                       f"{stats.get('frontier_size', 0)} queued, {client.calls_made} API calls"
                       + (f", {remaining} left in budget" if remaining is not None else ""))
        job.result = {"addresses_processed": stats["addresses_processed"],
                      "hops_completed": stats["hops_completed"],
                      "frontier_size": stats.get("frontier_size", 0),
                      "calls_made": client.calls_made,
                      "calls_remaining": remaining}

    try:
        stats = ingest_subgraph.crawl(address, hops, client=client, conn=conn,
                                      resume=True, progress=progress,
                                      should_stop=job._cancel.is_set)
    except BudgetExhausted as e:
        # Not a failure: the user set a budget and it was honoured. The job
        # finishes as done, with the reason recorded.
        job.message = f"stopped: {e}"
        job.result = {**job.result, "stopped_reason": "budget_exhausted"}
        log.info("ingest job %s stopped on budget after %d calls", job.job_id, client.calls_made)
        return

    job.result = {"addresses_processed": stats.get("addresses_processed", 0),
                  "hops_completed": stats.get("hops_completed", 0),
                  "calls_made": client.calls_made,
                  "calls_remaining": client.calls_remaining(),
                  "stopped_reason": "cancelled" if stats.get("stopped_early") else None}
    job.completed = stats.get("addresses_processed", 0)


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
    refresh_cluster_flag_stats(job)


def refresh_cluster_flag_stats(job: Job) -> None:
    """Rebuilds mv_cluster_flag_stats (migration 008) after new flags land.

    The view exists so /clusters/top-flagged does not re-join 780,644
    cluster_member rows to 128,637 flags on every call. That trade is only
    honest if the view is refreshed when flags change -- a stale ranking
    showing counts from the previous detector run is worse than the 0.5s it
    saves. CONCURRENTLY so a dashboard open during the refresh keeps
    reading the old rows instead of blocking.

    Never fatal: the flags themselves are committed by this point, and
    losing a refresh costs accuracy on one endpoint, not analysis.
    """
    job.message = "refreshing cluster flag statistics"
    try:
        # Its own connection: REFRESH CONCURRENTLY cannot run inside the
        # transaction the detectors have been committing through.
        conn = psycopg2.connect(**config.PG_DSN)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY mv_cluster_flag_stats")
        finally:
            conn.close()
    except psycopg2.Error as e:
        log.warning("Could not refresh mv_cluster_flag_stats: %s", e)
        job.result = {**job.result, "cluster_stats_refreshed": False}
        return
    job.result = {**job.result, "cluster_stats_refreshed": True}


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
    if not flag_ids:
        # "done, 0 flags" with no explanation reads as a failure. Say which
        # combination came up empty instead.
        job.message = (f"no {job.detector} flags on snapshot {job.snapshot_id}"
                       if job.detector and job.detector != "all"
                       else f"no flags on snapshot {job.snapshot_id} — run the detectors first")
        job.result = {"verdicts": {}, "audited": 0}
        return
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
