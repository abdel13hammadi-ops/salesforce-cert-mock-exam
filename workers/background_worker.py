"""
CertBound background job worker.

Entry command
-------------
    python -m workers.background_worker \\
        --worker-id <unique-id> \\
        [--job-types resource_ingestion,other] \\
        [--once] \\
        [--sleep 5.0] \\
        [--lease-seconds 300] \\
        [--log-level INFO]

Environment variables required
-------------------------------
    SUPABASE_URL                 Supabase project URL
    SUPABASE_SERVICE_ROLE_KEY    Service-role secret key

Design rules
------------
* The worker NEVER updates background_jobs directly.
* All state transitions happen through RPCs:
    claim_background_job_v1
    heartbeat_background_job_v1
    complete_background_job_v1
    fail_background_job_v1
* Handlers are resolved from workers.job_handlers.HANDLER_REGISTRY by job_type.
* An unknown job_type is failed immediately via fail_background_job_v1.
* Any unhandled exception in a handler is caught, logged, and forwarded to
  fail_background_job_v1 — it never crashes the worker process.
* SIGINT and SIGTERM set a shutdown flag; the current job completes before exit.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import time
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Supabase client factory
# =============================================================================

def build_supabase_client():
    """Create a service-role Supabase client from environment variables.

    Separated from BackgroundWorker so tests can inject a mock client without
    triggering real network calls.

    Raises RuntimeError when required environment variables are absent or when
    the supabase package is not installed.
    """
    try:
        from supabase import create_client  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "supabase package is not installed; run: pip install supabase"
        ) from exc

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    if not url:
        raise RuntimeError(
            "SUPABASE_URL environment variable is required for the worker"
        )
    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY environment variable is required for the worker"
        )

    return create_client(url, key)


# =============================================================================
# Worker
# =============================================================================

class BackgroundWorker:
    """Claims and dispatches background_jobs through service-role RPCs.

    Parameters
    ----------
    worker_id:
        Unique string identifier for this worker process.  Used as the
        lease_owner in every RPC call.
    client:
        Any object that exposes ``.rpc(name, params).execute()`` returning an
        object with ``.data`` (list) and ``.error`` attributes.  In production
        this is the supabase Python client; in tests it is a mock.
    handlers:
        Dict mapping job_type strings to callables matching the handler
        signature (see workers/job_handlers.py).
    job_types:
        Optional allowlist of job_type strings to claim.  None means claim
        any type.
    lease_seconds:
        Duration of each lease, 30–3600.  Also used for heartbeat renewals.
    sleep_interval:
        Seconds to sleep between poll iterations when no job is available.
    """

    def __init__(
        self,
        worker_id: str,
        client,
        handlers: Dict[str, Callable],
        job_types: Optional[List[str]] = None,
        lease_seconds: int = 300,
        sleep_interval: float = 5.0,
    ) -> None:
        if not str(worker_id).strip():
            raise ValueError("worker_id must not be empty")
        self.worker_id = str(worker_id).strip()
        self.client = client
        self.handlers = handlers
        self.job_types = job_types
        self.lease_seconds = lease_seconds
        self.sleep_interval = sleep_interval
        self._shutdown_requested = False

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def request_shutdown(self) -> None:
        """Signal the polling loop to stop after the current job completes."""
        self._shutdown_requested = True
        logger.info("shutdown requested for worker=%s", self.worker_id)

    # ------------------------------------------------------------------
    # RPC helpers — no direct table access
    # ------------------------------------------------------------------

    def _rpc(self, name: str, params: dict) -> list:
        """Invoke a Supabase RPC and return its data list.

        Raises RuntimeError if the response carries an error.
        """
        result = self.client.rpc(name, params).execute()
        if getattr(result, "error", None):
            raise RuntimeError(f"RPC {name!r} returned error: {result.error}")
        return result.data or []

    def _claim_job(self) -> Optional[dict]:
        """Claim one pending job.  Returns the job row dict or None."""
        rows = self._rpc(
            "claim_background_job_v1",
            {
                "p_worker_id":     self.worker_id,
                "p_lease_seconds": self.lease_seconds,
                "p_job_types":     self.job_types,
            },
        )
        return rows[0] if rows else None

    def _heartbeat(self, job_id: str, checkpoint: Optional[dict] = None) -> None:
        """Extend the lease and transition status to running."""
        self._rpc(
            "heartbeat_background_job_v1",
            {
                "p_job_id":        job_id,
                "p_worker_id":     self.worker_id,
                "p_lease_seconds": self.lease_seconds,
                "p_checkpoint":    checkpoint,
            },
        )

    def _complete_job(self, job_id: str, result: dict) -> None:
        """Mark the job as completed and store its result."""
        self._rpc(
            "complete_background_job_v1",
            {
                "p_job_id":     job_id,
                "p_worker_id":  self.worker_id,
                "p_result":     result,
            },
        )

    def _fail_job(self, job_id: str, error_message: str) -> None:
        """Fail the job (schedule retry or dead-letter based on attempt count)."""
        self._rpc(
            "fail_background_job_v1",
            {
                "p_job_id":        job_id,
                "p_worker_id":     self.worker_id,
                "p_error_message": error_message,
            },
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _process_one(self, job: dict) -> None:
        """Dispatch a single claimed job to its registered handler.

        Sends an initial heartbeat before handing off to the handler.
        Completes the job on handler success; fails it on any exception.
        An unsupported job_type is failed immediately without calling any
        handler.
        """
        job_id   = str(job.get("job_id", ""))
        job_type = job.get("job_type", "")
        attempt  = job.get("attempt_count", "?")

        logger.info(
            "job_id=%s type=%s attempt=%s status=dispatching worker=%s",
            job_id, job_type, attempt, self.worker_id,
        )

        handler = self.handlers.get(job_type)
        if handler is None:
            error = (
                f"no handler registered for job_type={job_type!r}; "
                "job failed without execution"
            )
            logger.error("job_id=%s %s", job_id, error)
            self._fail_job(job_id, error)
            return

        try:
            self._heartbeat(job_id)
            result = handler(
                job_id=job_id,
                payload=job.get("payload") or {},
                checkpoint=job.get("checkpoint") or {},
                attempt=attempt,
                heartbeat_fn=lambda cp=None: self._heartbeat(job_id, cp),
            )
            self._complete_job(job_id, result or {})
            logger.info(
                "job_id=%s type=%s attempt=%s status=completed worker=%s",
                job_id, job_type, attempt, self.worker_id,
            )
        except Exception as exc:  # noqa: BLE001
            error_message = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "job_id=%s type=%s attempt=%s status=failed worker=%s error=%r",
                job_id, job_type, attempt, self.worker_id, error_message,
            )
            self._fail_job(job_id, error_message)

    # ------------------------------------------------------------------
    # Run modes
    # ------------------------------------------------------------------

    def run_once(self) -> bool:
        """Claim and process one job.

        Returns True when a job was processed, False when the queue was empty.
        """
        job = self._claim_job()
        if job is None:
            logger.info(
                "no job available worker=%s job_types=%s",
                self.worker_id, self.job_types,
            )
            return False
        self._process_one(job)
        return True

    def run(self) -> None:
        """Poll indefinitely, sleeping between empty-queue checks.

        Exits cleanly when request_shutdown() is called (e.g. from a signal
        handler).  The current job always completes before the loop exits.
        """
        logger.info(
            "worker=%s starting poll loop sleep=%.1fs lease=%ds job_types=%s",
            self.worker_id, self.sleep_interval, self.lease_seconds, self.job_types,
        )
        while not self._shutdown_requested:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("unhandled error in worker loop: %s", exc)
            if not self._shutdown_requested:
                time.sleep(self.sleep_interval)
        logger.info("worker=%s stopped", self.worker_id)


# =============================================================================
# CLI entry point
# =============================================================================

def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="CertBound background job worker",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--worker-id",
        required=True,
        help="Unique identifier for this worker process",
    )
    parser.add_argument(
        "--job-types",
        default=None,
        help="Comma-separated list of job_type values to claim (default: all types)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Claim and process at most one job, then exit",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="Sleep duration between poll iterations",
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Lease duration in seconds (30–3600)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    job_types: Optional[List[str]] = None
    if args.job_types:
        job_types = [t.strip() for t in args.job_types.split(",") if t.strip()]

    from workers.job_handlers import build_handler_registry  # local import avoids circular

    client = build_supabase_client()

    worker = BackgroundWorker(
        worker_id=args.worker_id,
        client=client,
        handlers=build_handler_registry(client),
        job_types=job_types,
        lease_seconds=args.lease_seconds,
        sleep_interval=args.sleep,
    )

    def _on_signal(signum: int, _frame) -> None:
        logger.info("received signal %d; requesting clean shutdown", signum)
        worker.request_shutdown()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    if args.once:
        worker.run_once()
    else:
        worker.run()


if __name__ == "__main__":
    main()
