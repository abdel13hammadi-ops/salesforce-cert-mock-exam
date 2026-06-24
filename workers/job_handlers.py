"""
CertBound background job handler registry.

Handler signature
-----------------
    def handle_my_type(
        job_id:       str,
        payload:      dict,
        checkpoint:   dict,
        attempt:      int,
        heartbeat_fn: Callable[[], None],
    ) -> dict:
        ...
        return {"key": "value"}   # stored in background_jobs.result

Rules
-----
* No handler may mark success without performing real work.
* No handler may write to background_jobs directly.
* No handler may call LLMs, Supabase RPCs, or external services without
  explicit implementation.
* Raising any exception causes the worker to call fail_background_job_v1.
* Returning a dict causes the worker to call complete_background_job_v1.

Implemented handlers (Phase 8B / 8C)
--------------------------------------
  resource_ingestion   → calls ingest_resource_version_v1 RPC
  candidate_promotion  → calls promote_question_candidate_v1 RPC

All other handlers are stubs that raise NotImplementedHandler.
Use build_handler_registry(client) to build the registry with real handlers
injected.  HANDLER_REGISTRY contains only stubs (useful for testing and as a
fallback default).
"""

from __future__ import annotations

from typing import Any, Callable, Dict


# ===========================================================================
# Exceptions
# ===========================================================================

class NotImplementedHandler(Exception):
    """Raised by stub handlers to signal they have no real implementation."""


class HandlerPayloadError(ValueError):
    """Raised when a job payload is malformed or missing required fields."""


# ===========================================================================
# Private helpers
# ===========================================================================

def _require(payload: dict, field: str) -> Any:
    """Return payload[field], raising HandlerPayloadError when absent or blank."""
    value = payload.get(field)
    if value is None:
        raise HandlerPayloadError(
            f"missing required payload field: {field!r}"
        )
    if isinstance(value, str) and not value.strip():
        raise HandlerPayloadError(
            f"payload field {field!r} must not be empty"
        )
    return value


def _call_rpc(client, name: str, params: dict) -> dict:
    """Invoke a Supabase RPC and return the first result row.

    Raises RuntimeError when the response carries an error or when no rows
    are returned.  The RuntimeError propagates to the worker's failure path.
    """
    result = client.rpc(name, params).execute()
    if getattr(result, "error", None):
        raise RuntimeError(f"RPC {name!r} failed: {result.error}")
    rows = result.data or []
    if not rows:
        raise RuntimeError(f"RPC {name!r} returned no rows")
    return rows[0]


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


# ===========================================================================
# Implemented handler factories (Phase 8B / 8C)
# ===========================================================================

def make_resource_ingestion_handler(client) -> Callable[..., dict]:
    """Return the resource_ingestion handler bound to the given Supabase client.

    Expected payload fields
    -----------------------
    Required:
      resource_id   str  — UUID of the official_resources row
      content_text  str  — full text of the resource version
      content_hash  str  — deterministic hash of the content
      created_by    str  — actor identifier (e.g. email or service name)

    Optional:
      source_url                str   — canonical source URL
      source_external_version   str   — upstream version string (e.g. "v2024-Q3")
      effective_at              str   — ISO-8601 timestamp; when the version takes effect
      metadata                  dict  — arbitrary extra metadata
      chunks                    list  — array of chunk dicts for resource_chunks

    Returns
    -------
      resource_version_id  str
      resource_id          str
      version_number       int
      chunk_count          int

    Only calls: ingest_resource_version_v1
    """

    def handle(
        job_id: str,
        payload: dict,
        checkpoint: dict,
        attempt: int,
        heartbeat_fn: Callable[[], None],
    ) -> dict:
        resource_id  = _require(payload, "resource_id")
        content_text = _require(payload, "content_text")
        content_hash = _require(payload, "content_hash")
        created_by   = _require(payload, "created_by")

        params = {
            "p_resource_id":             resource_id,
            "p_source_url":              payload.get("source_url"),
            "p_source_external_version": payload.get("source_external_version"),
            "p_content_text":            content_text,
            "p_content_hash":            content_hash,
            "p_effective_at":            payload.get("effective_at"),
            "p_created_by":              created_by,
            "p_metadata":                payload.get("metadata") or {},
            "p_chunks":                  payload.get("chunks") or [],
        }

        row = _call_rpc(client, "ingest_resource_version_v1", params)

        return {
            "resource_version_id": str(row.get("resource_version_id", "")),
            "resource_id":         str(row.get("resource_id", "")),
            "version_number":      row.get("version_number"),
            "chunk_count":         row.get("chunk_count"),
        }

    handle.__name__ = "handle_resource_ingestion"
    handle.__qualname__ = "handle_resource_ingestion"
    return handle


def make_candidate_promotion_handler(client) -> Callable[..., dict]:
    """Return the candidate_promotion handler bound to the given Supabase client.

    Expected payload fields
    -----------------------
    Required:
      candidate_id  str  — UUID of the question_candidates row
      actor_email   str  — email of the human or service triggering promotion
      reason        str  — human-readable promotion rationale

    Optional:
      event_data    dict — extra data merged into the question_candidate_events row

    Returns
    -------
      candidate_id        str
      question_version_id str
      question_id         int
      version_number      int

    Only calls: promote_question_candidate_v1
    """

    def handle(
        job_id: str,
        payload: dict,
        checkpoint: dict,
        attempt: int,
        heartbeat_fn: Callable[[], None],
    ) -> dict:
        candidate_id = _require(payload, "candidate_id")
        actor_email  = _require(payload, "actor_email")
        reason       = _require(payload, "reason")

        params = {
            "p_candidate_id": candidate_id,
            "p_actor_email":  actor_email,
            "p_reason":       reason,
            "p_event_data":   payload.get("event_data") or {},
        }

        row = _call_rpc(client, "promote_question_candidate_v1", params)

        return {
            "candidate_id":        str(row.get("candidate_id", "")),
            "question_version_id": str(row.get("question_version_id", "")),
            "question_id":         row.get("question_id"),
            "version_number":      row.get("version_number"),
        }

    handle.__name__ = "handle_candidate_promotion"
    handle.__qualname__ = "handle_candidate_promotion"
    return handle


# ===========================================================================
# Stub-only registry (backwards-compatible default)
# ===========================================================================

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


def build_handler_registry(client) -> Dict[str, Callable[..., Any]]:
    """Build a complete handler registry with real handlers injected.

    Returns a new dict that overrides resource_ingestion and
    candidate_promotion with real implementations bound to *client*, leaving
    all other types as stubs.

    Parameters
    ----------
    client:
        Any object exposing ``.rpc(name, params).execute()`` — in production,
        the Supabase Python client; in tests, a FakeSupabase mock.
    """
    return {
        **HANDLER_REGISTRY,
        "resource_ingestion":  make_resource_ingestion_handler(client),
        "candidate_promotion": make_candidate_promotion_handler(client),
    }


__all__ = [
    "HANDLER_REGISTRY",
    "NotImplementedHandler",
    "HandlerPayloadError",
    "make_resource_ingestion_handler",
    "make_candidate_promotion_handler",
    "build_handler_registry",
]
