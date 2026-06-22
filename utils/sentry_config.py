"""Privacy-safe Sentry error monitoring for CertBound.

DSN loading order:
    1. SENTRY_DSN environment variable
    2. Streamlit secrets key SENTRY_DSN
    3. Neither present → skip initialization silently

Environment loading order:
    1. SENTRY_ENVIRONMENT environment variable
    2. Streamlit secrets key SENTRY_ENVIRONMENT
    3. Fallback: "development"

Release: APP_VERSION from utils.version (import-safe; falls back to None).

Privacy guarantees:
    - send_default_pii=False, include_local_variables=False
    - max_request_body_size="never"
    - No tracing, profiling, logging, or session replay
    - EventScrubber with extended deny list, recursive=True
    - before_send hook strips user context, headers, cookies, body,
      query string, and fr_session from request URLs
    - Exception type, stack trace, filename, line number, release,
      and environment are preserved for debugging
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

_SENTRY_INITIALIZED: bool = False

_EXTRA_DENYLIST = [
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "service_role",
    "api_key",
    "apikey",
    "session",
    "fr_session",
    "selected_answers",
    "answers",
    "question_text",
    "answer_options",
    "supabase_key",
]


def _get_dsn() -> Optional[str]:
    """Return SENTRY_DSN or None. Never raises."""
    dsn = os.environ.get("SENTRY_DSN", "")
    if dsn and dsn.strip():
        return dsn.strip()
    try:
        import streamlit as st  # noqa: PLC0415
        dsn = str(st.secrets.get("SENTRY_DSN", "") or "").strip()
        if dsn:
            return dsn
    except Exception:
        pass
    return None


def _get_environment() -> str:
    """Return SENTRY_ENVIRONMENT or 'development'. Never raises."""
    env = os.environ.get("SENTRY_ENVIRONMENT", "")
    if env and env.strip():
        return env.strip()
    try:
        import streamlit as st  # noqa: PLC0415
        env = str(st.secrets.get("SENTRY_ENVIRONMENT", "") or "").strip()
        if env:
            return env
    except Exception:
        pass
    return "development"


def _get_release() -> Optional[str]:
    """Return APP_VERSION string or None. Never raises."""
    try:
        from utils.version import APP_VERSION  # noqa: PLC0415
        return str(APP_VERSION) if APP_VERSION else None
    except Exception:
        return None


def _strip_fr_session_from_url(url: Optional[str]) -> Optional[str]:
    """Remove the fr_session query parameter from a URL. Never raises."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        params = parse_qs(parsed.query, keep_blank_values=True)
        params.pop("fr_session", None)
        clean_query = urlencode(params, doseq=True)
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            clean_query,
            parsed.fragment,
        ))
    except Exception:
        return url


def _before_send(
    event: Dict[str, Any], hint: Any
) -> Optional[Dict[str, Any]]:
    """Strip PII before the event is transmitted to Sentry.

    Removes: user context, request headers, cookies, body/data,
    query_string, and fr_session from the request URL.

    Retains: exception type, stack trace, source filename, line number,
    release, and environment.
    """
    event.pop("user", None)

    request = event.get("request")
    if isinstance(request, dict):
        request.pop("headers", None)
        request.pop("cookies", None)
        request.pop("data", None)
        request.pop("body", None)
        request.pop("query_string", None)
        if "url" in request:
            request["url"] = _strip_fr_session_from_url(request.get("url"))
        event["request"] = request

    return event


def _build_scrubber() -> Any:
    """Build an EventScrubber with the extended deny list. Returns None on failure."""
    try:
        from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber  # noqa: PLC0415
        combined = list(DEFAULT_DENYLIST) + _EXTRA_DENYLIST
        seen: set = set()
        deduped = []
        for key in combined:
            if key not in seen:
                seen.add(key)
                deduped.append(key)
        return EventScrubber(denylist=deduped, recursive=True)
    except Exception:
        try:
            from sentry_sdk.scrubber import EventScrubber  # noqa: PLC0415
            return EventScrubber(denylist=list(_EXTRA_DENYLIST), recursive=True)
        except Exception:
            return None


def init_sentry() -> None:
    """Initialize Sentry SDK once, idempotently.

    Safe to call on every Streamlit script rerun. Returns immediately if
    already initialized. Never raises: a missing DSN, unavailable SDK, or
    any init failure all fail silently so CertBound always starts normally.
    """
    global _SENTRY_INITIALIZED
    if _SENTRY_INITIALIZED:
        return

    try:
        dsn = _get_dsn()
    except Exception:
        return

    if not dsn:
        return

    try:
        from sentry_sdk import init as sentry_init  # noqa: PLC0415

        init_kwargs: Dict[str, Any] = {
            "dsn": dsn,
            "environment": _get_environment(),
            "release": _get_release(),
            "send_default_pii": False,
            "max_request_body_size": "never",
            "before_send": _before_send,
        }

        scrubber = _build_scrubber()
        if scrubber is not None:
            init_kwargs["event_scrubber"] = scrubber

        try:
            init_kwargs["include_local_variables"] = False
        except Exception:
            pass

        sentry_init(**init_kwargs)
        _SENTRY_INITIALIZED = True

    except ImportError:
        pass
    except Exception:
        pass
