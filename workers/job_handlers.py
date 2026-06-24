"""
CertBound background job handler registry.

Every key in HANDLER_REGISTRY corresponds to a valid background_jobs.job_type
value.  All handlers are stubs that raise NotImplementedHandler — they signal
clearly that no real implementation exists yet.

Rules
-----
* No handler may mark success without performing real work.
* No handler may write to background_jobs directly.
* No handler may call LLMs, Supabase RPCs, or external services without
  explicit implementation.
* To implement a handler, replace the _stub(...) call for the relevant type
  with a real function that matches the handler signature.

Handler signature
-----------------
    def handle_my_type(
        job_id:      str,
        payload:     dict,
        checkpoint:  dict,
        attempt:     int,
        heartbeat_fn: Callable[[], None],
    ) -> dict:
        ...
        return {"key": "value"}   # stored in background_jobs.result

The handler should call heartbeat_fn() periodically for long-running work.
Raising any exception causes the worker to call fail_background_job_v1.
Returning a dict causes the worker to call complete_background_job_v1.
"""

from __future__ import annotations

from typing import Any, Callable, Dict


class NotImplementedHandler(Exception):
    """Raised by stub handlers to signal they have no real implementation."""


def _stub(job_type: str) -> Callable[..., Any]:
    """Return a named callable that always raises NotImplementedHandler."""

    def _handler(
        job_id: str,
        payload: dict,
        checkpoint: dict,
        attempt: int,
        heartbeat_fn: Callable[[], None],
    ) -> dict:
        raise NotImplementedHandler(
            f"handler for job_type={job_type!r} is not yet implemented. "
            "This stub must be replaced with a real implementation before "
            "enabling this job type in production."
        )

    _handler.__name__ = f"handle_{job_type}"
    _handler.__qualname__ = f"handle_{job_type}"
    return _handler


HANDLER_REGISTRY: Dict[str, Callable[..., Any]] = {
    "resource_ingestion":   _stub("resource_ingestion"),
    "deterministic_audit":  _stub("deterministic_audit"),
    "llm_audit":            _stub("llm_audit"),
    "hybrid_audit":         _stub("hybrid_audit"),
    "question_generation":  _stub("question_generation"),
    "candidate_promotion":  _stub("candidate_promotion"),
    "embedding_generation": _stub("embedding_generation"),
    "other":                _stub("other"),
}

__all__ = ["HANDLER_REGISTRY", "NotImplementedHandler"]
