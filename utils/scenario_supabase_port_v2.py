"""Supabase-client-backed implementation of ``ScenarioOrchestrationV2PersistencePort``.

This module contains the ONLY concrete adapter that lets
``utils.scenario_orchestration_v2`` talk to a real Supabase/PostgREST
backend. It never instantiates a client itself, never reads environment
variables, and never falls back to a default/global/admin client -- a
caller must inject an already-authenticated client explicitly.

Trust boundary (read this before wiring this port into anything)
------------------------------------------------------------------
``supabase/migrations/20260719130000_v68_scenario_attempt_persistence_
foundation.sql`` deliberately:

- ``REVOKE ALL`` on ``public.scenario_attempts`` and
  ``public.scenario_decisions`` from ``PUBLIC``, ``anon``, ``authenticated``,
  AND ``service_role`` first, then re-grants only the minimum needed
  privilege set (``SELECT``/``INSERT``/``UPDATE`` as applicable) back to
  ``service_role`` alone;
- enables Row Level Security on both tables with **zero** policies (no
  ``auth.uid()``-based policy exists for either table, by design -- see that
  migration's own "Ownership and RLS design" section);
- grants ``EXECUTE`` on every one of the four scenario-attempt RPCs
  (``start_or_resume_scenario_attempt_v1``, ``get_scenario_attempt_v1``,
  ``submit_scenario_decision_v1``, ``abandon_scenario_attempt_v1``) to
  ``service_role`` only, revoking it from ``anon``/``authenticated``.

In other words: this schema is **not** a per-session-RLS design. It is a
**trusted-server-backend** design. Per-learner ownership is enforced
entirely INSIDE each RPC's own ``p_user_email`` equality check against
``scenario_attempts.user_email`` -- never by Postgres deciding which rows a
given database session may see. Consequently:

- this port requires an already-authenticated ``service_role`` Supabase
  client to be injected by a trusted server-side caller (e.g. a Streamlit
  backend process holding the service-role key out of band -- never a
  browser-supplied session);
- ``user_email`` MUST be derived by that same trusted server-side caller from
  its own authenticated application identity/session state (e.g. whatever
  server-side mechanism already establishes "which learner is this request
  for" before any orchestration call is made) -- it must NEVER be taken
  directly from an untrusted browser field, URL/query parameter, form input,
  or any other client-controlled payload that a learner could edit. This
  port forwards ``user_email`` to the RPCs verbatim (after whatever
  normalization the caller already applied) as the SOLE mechanism by which
  the database enforces "only your own attempt" -- the RPCs enforce row
  ownership using exactly the email string they are given, under the trust
  assumption that the trusted server boundary already verified it. This
  port itself never treats a caller-supplied email as proof of identity, and
  never adds a second, client-side ownership check that could disagree with
  the RPC's own -- it forwards ``user_email``, it does not authorize it. A
  future Engine V2 controller is responsible for deriving this value from
  the authenticated server-side user/session context before ever calling
  this port -- that responsibility is explicitly out of scope for this
  module and is NOT implemented here;
- this port never uses ``service_role`` table access to bypass that RPC
  boundary -- every operation below is a ``client.rpc(...)`` call, never a
  ``client.table(...)`` call, exactly mirroring the discipline
  ``utils/scenario_persistence.py`` already documents and enforces for
  Engine V1.

Design notes
------------
``call_start_or_resume_scenario_attempt_v1(...)`` and
``call_submit_scenario_decision_v1(...)`` are thin, faithful RPC
invocations: they pass the caller's params through unchanged (deep-copied
outbound so the caller's own mapping is never mutated) and return the raw,
deep-copied ``.data`` payload. They deliberately do NOT pre-validate the
response shape (list vs. mapping vs. empty vs. multi-row) -- that shape
validation already lives in
``utils.scenario_persistence_v2.parse_start_or_resume_rpc_response_v2`` /
``parse_submit_decision_rpc_response_v2``, and duplicating it here would
only create a second place that could silently drift out of sync.

``load_attempt_snapshot(...)`` is different: the orchestration protocol it
implements requires a single ``Mapping`` back (not a raw RPC list), so this
port resolves ``get_scenario_attempt_v1``'s response down to exactly one
attempt row itself (rejecting zero or multiple rows as malformed), and
additionally *projects* both the attempt row and every decision element
down to an explicit, minimal, approved key set before returning -- even
though the RPC itself already excludes internal-only columns
(``idempotency_key``, ``request_fingerprint``, ``user_email``, ...), this
extra projection is a second, independent guarantee that no unapproved
column can ever reach orchestration through this port, even if the RPC's
own return shape were ever accidentally widened in the future.

Error translation is a fail-closed ALLOWLIST, not a blocklist: a raw
exception/``.error`` message is preserved verbatim ONLY when it exactly
(case-sensitively, from position 0) matches one of the closed set of
business-error prefixes this repository's own V68/V69 RPCs are documented to
raise (``_APPROVED_BUSINESS_ERROR_PREFIXES`` below -- kept in sync with
``utils.scenario_orchestration_v2._RPC_ERROR_PREFIX_MAP``), so that
orchestration's own prefix-based classification (e.g. ``sequence_mismatch:``/
``attempt_not_found:``) continues to work unchanged. Every other outcome --
recognized transport/permission/authentication signals AND any genuinely
unrecognized error (an unexpected PostgREST API error such as a ``PGRST202``
schema-cache miss, a stack-trace-shaped message, a malformed/empty message,
etc.) -- receives this port's own fixed, generic, sanitized text and NEVER
the raw exception's message, so a connection string, JWT, schema/relation/
function name, or other internal detail can never reach learner-facing code
through an error message. This module previously (see
``SCENARIO_ENGINE_V2_SUPABASE_PORT_FOCUSED_REVIEW.md``, finding HIGH-01)
treated "does not match a known-bad marker" as sufficient reason to preserve
a message verbatim, which is not a safe default -- it has since been
corrected to treat "does not match a known-safe prefix" as sufficient reason
to sanitize instead. No raw Supabase/PostgREST exception object, and no raw
``KeyError``/``TypeError``/``AttributeError`` from this module's own
parsing, ever escapes a public method here.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple

__all__ = (
    "ScenarioSupabasePortV2Error",
    "ScenarioSupabasePortV2RpcError",
    "ScenarioSupabasePortV2TransportError",
    "ScenarioSupabasePortV2PermissionError",
    "ScenarioSupabasePortV2AuthenticationError",
    "ScenarioSupabasePortV2MalformedResponseError",
    "ScenarioSupabasePortV2NoAttemptRowError",
    "ScenarioSupabasePortV2MultipleAttemptRowsError",
    "ScenarioSupabasePortV2UnknownError",
    "SupabaseRpcClientProtocol",
    "SupabaseScenarioOrchestrationV2Port",
)


# ---------------------------------------------------------------------------
# RPC names (must match supabase/migrations/20260719130000_..._v68_....sql
# and 20260719140000_..._v69_....sql exactly)
# ---------------------------------------------------------------------------

_START_RPC_NAME = "start_or_resume_scenario_attempt_v1"
_SUBMIT_RPC_NAME = "submit_scenario_decision_v1"
_GET_ATTEMPT_RPC_NAME = "get_scenario_attempt_v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScenarioSupabasePortV2Error(Exception):
    """Base error for the Supabase-client-backed V2 persistence port."""


class ScenarioSupabasePortV2RpcError(ScenarioSupabasePortV2Error):
    """The RPC actually reached the database and Postgres raised a
    business-logic rejection this port POSITIVELY recognizes (its message
    starts with one of the closed ``_APPROVED_BUSINESS_ERROR_PREFIXES``
    entries -- e.g. ``sequence_mismatch:`` or ``attempt_not_found:``). The
    message text is preserved EXACTLY as extracted from the SDK/PostgREST
    error -- never reformatted, truncated, or reordered -- so a caller's own
    prefix-based classification (e.g.
    ``utils.scenario_orchestration_v2``'s internal RPC error-prefix map)
    continues to apply unchanged. Every one of these messages originates
    from this repository's own ``RAISE EXCEPTION '...'`` text inside the
    committed migrations, so no externally-supplied secret can appear in
    it. An error that does NOT match the approved-prefix allowlist is never
    raised as this type -- see ``ScenarioSupabasePortV2UnknownError``."""


class ScenarioSupabasePortV2TransportError(ScenarioSupabasePortV2Error):
    """A network/transport failure or timeout occurred before any response
    (success or database error) was ever obtained from PostgREST. The
    message is always this port's own generic, sanitized text -- never the
    raw SDK exception text, which could otherwise embed a connection
    string, host, or other transport-layer detail."""


class ScenarioSupabasePortV2PermissionError(ScenarioSupabasePortV2Error):
    """PostgREST/Postgres reported the request was denied for a
    permission/grant/RLS reason distinct from an RPC business-logic
    rejection (e.g. the injected client is not actually authenticated as
    ``service_role``). The message is always this port's own generic,
    sanitized text."""


class ScenarioSupabasePortV2AuthenticationError(ScenarioSupabasePortV2Error):
    """PostgREST reported a session/authentication failure (expired,
    missing, or invalid credentials) rather than executing the RPC at all.
    The message is always this port's own generic, sanitized text -- never
    the raw SDK exception text, which could otherwise embed a token or API
    key fragment."""


class ScenarioSupabasePortV2MalformedResponseError(ScenarioSupabasePortV2Error):
    """The SDK returned a response shape this port cannot safely interpret
    at the ``load_attempt_snapshot`` boundary (not a list, not a mapping,
    or a row that is not itself a mapping)."""


class ScenarioSupabasePortV2NoAttemptRowError(ScenarioSupabasePortV2MalformedResponseError):
    """``get_scenario_attempt_v1`` unexpectedly returned zero rows without
    itself raising its own ``attempt_not_found:`` exception. In real usage
    this RPC always either raises ``attempt_not_found:`` or returns exactly
    one row -- this exception exists purely as a defensive, fail-closed
    reaction to an SDK/response shape this port did not expect, and is
    deliberately worded so it can never be mistaken for that RPC's own
    ``attempt_not_found:`` business rejection."""


class ScenarioSupabasePortV2MultipleAttemptRowsError(ScenarioSupabasePortV2MalformedResponseError):
    """``get_scenario_attempt_v1`` unexpectedly returned more than one row.
    Ambiguous; never guessed at or silently resolved to the first row."""


class ScenarioSupabasePortV2UnknownError(ScenarioSupabasePortV2Error):
    """A raised exception or ``.error``-bearing response did not match the
    approved business-error prefix allowlist, and did not match any
    recognized authentication/permission/timeout/connection signal either.

    This is the fail-closed sanitized default for anything this port cannot
    positively identify as safe -- an unrecognized PostgREST API-level error
    (e.g. a ``PGRST202`` schema-cache/function-lookup failure), a malformed
    or unexpected SDK exception, a stack-trace-shaped message, or any other
    error shape this module's author did not anticipate. The message is
    always this port's own fixed, generic text -- it NEVER includes the raw
    exception's message/details/hint, because (unlike the approved-prefix
    bucket below) there is no positive guarantee that text is free of
    schema/relation/function names, connection details, or other internal
    information. See ``SCENARIO_ENGINE_V2_SUPABASE_PORT_FOCUSED_REVIEW.md``
    (HIGH-01) and ``SCENARIO_ENGINE_V2_SUPABASE_PORT_CORRECTION_REPORT.md``
    for the incident this class closes."""


# ---------------------------------------------------------------------------
# Error classification (never lets a raw SDK exception escape)
#
# Classification order (deterministic, checked in exactly this sequence):
#   1. Approved business-error prefix (exact, case-sensitive `str.startswith`
#      match against a closed allowlist) -- verbatim preserved.
#   2. Authentication signal (SQLSTATE/PostgREST code or text marker) --
#      sanitized.
#   3. Permission signal (SQLSTATE or text marker) -- sanitized.
#   4. Timeout signal (text marker) -- sanitized.
#   5. Connection/transport signal (text marker) -- sanitized.
#   6. (Malformed *response shape* -- as opposed to a raised exception -- is
#      classified separately by `_extract_single_attempt_row`, not here.)
#   7. Generic unknown persistence failure -- sanitized fail-closed default
#      for anything not positively matched above.
#
# Step 1 is checked FIRST and is the only step allowed to reproduce the raw
# exception's message text, precisely because it is the only step backed by
# a positive, closed-set guarantee (this repository's own committed RPC
# business-error contract) rather than a fuzzy substring heuristic. Every
# other step -- including the final fallback -- must never leak `message`.
# ---------------------------------------------------------------------------


def _extract_message(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    return str(exc).strip()


def _extract_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if code is None:
        return ""
    return str(code).strip()


# Closed allowlist of business-error prefixes this port trusts enough to
# preserve verbatim. This MUST stay exactly in sync with
# ``utils.scenario_orchestration_v2._RPC_ERROR_PREFIX_MAP`` (the actual,
# authoritative set of prefixes the orchestration layer classifies by) --
# every prefix `start_or_resume_scenario_attempt_v1`/`get_scenario_attempt_v1`/
# `submit_scenario_decision_v1` can genuinely raise (per their own
# `RAISE EXCEPTION '<prefix>: ...'` statements in
# ``supabase/migrations/20260719130000_..._v68_....sql`` and
# ``supabase/migrations/20260719140000_..._v69_....sql``) appears in
# orchestration's map, so this tuple is deliberately a plain copy of that
# map's prefixes rather than a re-derivation -- kept as a hardcoded literal
# (not an import) to preserve this port's documented zero-coupling design
# (neither module imports the other; only the `Protocol` connects them).
# `tests/test_scenario_supabase_port_v2.py::TestApprovedPrefixSetSync`
# structurally asserts these two lists never drift apart.
#
# Two additional migration-only prefixes -- `attempt_insert_guard_violation:`
# and `decision_insert_guard_violation:` -- are deliberately EXCLUDED. Both
# originate from trigger-level guards against direct, non-RPC table mutation
# (this port never calls `client.table(...)`, only `client.rpc(...)`, so
# these should never fire through it in normal operation) and orchestration
# itself does not recognize either prefix for classification purposes, so
# treating them as "approved" would provide no classification benefit while
# needlessly risking exposure of internal trigger/table implementation
# details if one were ever (erroneously) raised.
_APPROVED_BUSINESS_ERROR_PREFIXES: Tuple[str, ...] = (
    "invalid_user_email:",
    "invalid_attempt_id:",
    "invalid_scenario_version_id:",
    "invalid_idempotency_key:",
    "invalid_sequence_number:",
    "invalid_expected_scene_id:",
    "invalid_selected_option_id:",
    "invalid_request_fingerprint:",
    "invalid_state_before:",
    "invalid_state_after:",
    "invalid_is_terminal:",
    "invalid_resulting_scene_id:",
    "invalid_terminal_ending_id:",
    "invalid_terminal_result_snapshot:",
    "invalid_terminal_fields:",
    "invalid_initial_scene:",
    "invalid_initial_state:",
    "invalid_initial_state_identity:",
    "invalid_initial_state_lifecycle:",
    "scenario_version_not_found:",
    "scenario_version_not_published:",
    "engine_version_mismatch:",
    "content_hash_mismatch:",
    "attempt_not_found:",
    "attempt_not_in_progress:",
    "idempotency_key_conflict:",
    "sequence_mismatch:",
    "scene_mismatch:",
    "state_before_mismatch:",
    "state_identity_mismatch:",
    "state_lifecycle_mismatch:",
    "terminal_result_mismatch:",
    "terminal_ending_mismatch:",
    "attempt_id_conflict:",
    "attempt_id_collision:",
    "start_or_resume_failed:",
)


def _is_approved_business_error(message: str) -> bool:
    """Exact, case-sensitive prefix match only -- a near-match (wrong case,
    missing/extra character, prefix embedded mid-message rather than at
    position 0) is deliberately NOT accepted; this is the fail-closed half
    of the allowlist and must never be loosened to a substring/`in` check."""
    return any(message.startswith(prefix) for prefix in _APPROVED_BUSINESS_ERROR_PREFIXES)


_TIMEOUT_MARKERS: Tuple[str, ...] = ("timeout", "timed out")
_CONNECTION_MARKERS: Tuple[str, ...] = (
    "connection refused",
    "could not connect",
    "network is unreachable",
    "name resolution",
    "connection reset",
    "connection error",
    "connection aborted",
    "failed to establish a new connection",
)
_PERMISSION_CODES = frozenset({"42501"})
_PERMISSION_MARKERS: Tuple[str, ...] = (
    "permission denied",
    "insufficient_privilege",
    "insufficient privilege",
)
_AUTHENTICATION_CODES = frozenset({"28000", "28p01", "pgrst301", "pgrst302", "401"})
_AUTHENTICATION_MARKERS: Tuple[str, ...] = (
    "jwt",
    "unauthorized",
    "invalid api key",
    "invalid_token",
    "authentication failed",
    "no api key found",
)


class _RpcErrorCarrier(Exception):
    """Wraps a Supabase response's ``.error`` attribute (already-returned
    data, e.g. a dict/string, rather than a raised exception) so it can flow
    through the exact same classification path as an exception actually
    raised by ``.execute()``. Never mutates the wrapped ``error`` value."""

    def __init__(self, error: Any) -> None:
        message: Optional[str]
        code: Any
        if isinstance(error, Mapping):
            raw_message = error.get("message")
            message = raw_message if isinstance(raw_message, str) else None
            code = error.get("code")
        else:
            raw_message = getattr(error, "message", None)
            message = raw_message if isinstance(raw_message, str) else None
            code = getattr(error, "code", None)
        if not message or not message.strip():
            message = str(error)
        self.message = message.strip()
        self.code = code
        super().__init__(self.message)


def _classify_rpc_exception(rpc_name: str, exc: BaseException) -> ScenarioSupabasePortV2Error:
    """Translate any exception raised while calling ``rpc_name`` into one of
    this module's own typed errors, following the fixed, deterministic order
    documented above. Never returns/re-raises ``exc`` itself, and never lets
    ``exc``'s own message escape except through step 1's closed allowlist."""
    if isinstance(exc, ScenarioSupabasePortV2Error):
        return exc

    message = _extract_message(exc)
    code = _extract_code(exc).lower()
    haystack = f"{type(exc).__name__} {message} {code}".lower()

    # 1. Approved business-error prefix -- verbatim, checked first so a
    #    genuine business message can never be shadowed by one of the
    #    fuzzy substring heuristics below.
    if _is_approved_business_error(message):
        return ScenarioSupabasePortV2RpcError(message)

    # 2. Authentication.
    if code in _AUTHENTICATION_CODES or any(marker in haystack for marker in _AUTHENTICATION_MARKERS):
        return ScenarioSupabasePortV2AuthenticationError(
            f"authentication_failed: RPC {rpc_name!r} session/authentication failed."
        )

    # 3. Permission.
    if code in _PERMISSION_CODES or any(marker in haystack for marker in _PERMISSION_MARKERS):
        return ScenarioSupabasePortV2PermissionError(
            f"permission_denied: RPC {rpc_name!r} was denied by the database."
        )

    # 4. Timeout.
    if any(marker in haystack for marker in _TIMEOUT_MARKERS):
        return ScenarioSupabasePortV2TransportError(f"timeout: RPC {rpc_name!r} timed out before completing.")

    # 5. Connection/transport.
    if any(marker in haystack for marker in _CONNECTION_MARKERS):
        return ScenarioSupabasePortV2TransportError(f"transport_error: RPC {rpc_name!r} could not reach the database.")

    # 7. Generic unknown persistence failure -- the fail-closed sanitized
    #    default. Deliberately never includes `message`/`code`/`haystack`:
    #    anything that reaches this branch has NOT been positively verified
    #    to be free of schema/relation/function/connection/credential detail
    #    (this is exactly the HIGH-01 gap this correction closes -- see
    #    SCENARIO_ENGINE_V2_SUPABASE_PORT_CORRECTION_REPORT.md). Covers,
    #    among others: unrecognized PostgREST API errors (e.g. `PGRST202`
    #    schema-cache misses), stack-trace-shaped messages, empty messages,
    #    and non-string `.message`/`.error` payloads (already normalized to
    #    a string by `_extract_message`/`_RpcErrorCarrier` before reaching
    #    here, so this branch never itself raises on odd input shapes).
    return ScenarioSupabasePortV2UnknownError(f"persistence_error: RPC {rpc_name!r} failed unexpectedly.")


# ---------------------------------------------------------------------------
# Minimal injected-client protocol (structural typing only; this module
# never imports the `supabase`/`postgrest` packages, keeping it usable with
# any client -- real or a deterministic test fake -- that merely shapes up
# to this narrow surface)
# ---------------------------------------------------------------------------


class SupabaseRpcResponseProtocol(Protocol):
    data: Any
    error: Any


class SupabaseRpcRequestProtocol(Protocol):
    def execute(self) -> SupabaseRpcResponseProtocol: ...  # noqa: E704


class SupabaseRpcClientProtocol(Protocol):
    def rpc(self, fn: str, params: Mapping[str, Any]) -> SupabaseRpcRequestProtocol: ...  # noqa: E704


# ---------------------------------------------------------------------------
# Response projection (load_attempt_snapshot only -- the two RPC-call
# methods intentionally do not project; see module docstring)
# ---------------------------------------------------------------------------

_APPROVED_ATTEMPT_ROW_KEYS: Tuple[str, ...] = (
    "attempt_id",
    "scenario_id",
    "scenario_version_id",
    "status",
    "current_scene_id",
    "next_sequence_number",
    "serialized_engine_state",
    "engine_version",
    "scenario_content_sha256",
)

# Matches the exact jsonb_build_object(...) keys get_scenario_attempt_v1
# emits per decision -- idempotency_key/request_fingerprint are already
# excluded by the RPC itself; stateBefore/stateAfter/resultingSceneId/
# isTerminal/terminalEndingId/createdAt are additionally dropped here
# because orchestration's canonical-decision loader only ever needs the
# replay triple (sequenceNumber, expectedSceneId, selectedOptionId).
_APPROVED_DECISION_ROW_KEYS: Tuple[str, ...] = ("sequenceNumber", "expectedSceneId", "selectedOptionId")


def _project_decision_rows(decisions_raw: Any) -> Any:
    """Project each decision element down to the approved key set.

    A non-list ``decisions_raw`` or a non-mapping element is preserved
    as-is (deep-copied, never coerced or dropped) so that
    ``utils.scenario_orchestration_v2``'s own malformed-decision validation
    can still reject it with its own typed, fail-closed error -- this
    function must never mask a malformed shape by silently substituting an
    empty/partial value.
    """
    if not isinstance(decisions_raw, list):
        return copy.deepcopy(decisions_raw)
    projected: List[Any] = []
    for item in decisions_raw:
        if isinstance(item, Mapping):
            projected.append({key: copy.deepcopy(item[key]) for key in _APPROVED_DECISION_ROW_KEYS if key in item})
        else:
            projected.append(copy.deepcopy(item))
    return projected


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------


class SupabaseScenarioOrchestrationV2Port:
    """Concrete ``ScenarioOrchestrationV2PersistencePort`` backed by an
    injected Supabase (or narrowly Supabase-shaped) client.

    See the module docstring for the full trust-boundary explanation. In
    short: pass this class an already-authenticated ``service_role``
    Supabase client that YOU (the caller) obtained through your own trusted
    server-side credential source -- this class never reads environment
    variables, never creates a client, and never falls back to one.
    """

    def __init__(self, client: SupabaseRpcClientProtocol) -> None:
        if client is None:
            raise ValueError(
                "client must not be None -- this port never creates, resolves, or falls back to a "
                "default/global Supabase client; callers must inject an already-authenticated client"
            )
        self._client = client

    # -- Protocol: RPC invocation -----------------------------------------

    def call_start_or_resume_scenario_attempt_v1(self, params: Mapping[str, Any]) -> Any:
        """Invoke ``start_or_resume_scenario_attempt_v1`` with exactly the
        seven named params supplied. Returns the raw, deep-copied
        ``.data`` payload -- shape validation is the caller's
        responsibility (``utils.scenario_persistence_v2.
        parse_start_or_resume_rpc_response_v2``)."""
        return self._call_rpc(_START_RPC_NAME, params)

    def call_submit_scenario_decision_v1(self, params: Mapping[str, Any]) -> Any:
        """Invoke ``submit_scenario_decision_v1`` with exactly the thirteen
        named params supplied. Returns the raw, deep-copied ``.data``
        payload -- shape validation is the caller's responsibility
        (``utils.scenario_persistence_v2.
        parse_submit_decision_rpc_response_v2``). Never retried
        automatically."""
        return self._call_rpc(_SUBMIT_RPC_NAME, params)

    # -- Protocol: trusted attempt/decision loading ------------------------

    def load_attempt_snapshot(self, *, user_email: str, attempt_id: str) -> Dict[str, Any]:
        """Load exactly one trusted attempt row (plus its ordered,
        key-projected decision history) via the read-only
        ``get_scenario_attempt_v1`` RPC.

        Ownership is enforced entirely by that RPC's own ``p_user_email``
        equality check against the persisted
        ``scenario_attempts.user_email`` column (see this module's
        docstring) -- ``user_email`` is forwarded here verbatim as that
        RPC's trusted parameter, never evaluated by this port itself as an
        authorization decision. The caller MUST supply a ``user_email``
        already derived from its own trusted, authenticated server-side
        session/application identity -- never a raw, unauthenticated value
        taken directly from a browser field, query parameter, or other
        client-controlled payload (see the module docstring's "Trust
        boundary" section).
        """
        params = {"p_user_email": user_email, "p_attempt_id": attempt_id}
        data = self._call_rpc(_GET_ATTEMPT_RPC_NAME, params)
        row = self._extract_single_attempt_row(data)
        return self._project_attempt_row(row)

    # -- Internal helpers ---------------------------------------------------

    def _call_rpc(self, rpc_name: str, params: Mapping[str, Any]) -> Any:
        # Deep-copy outbound so the caller's own params mapping (and every
        # nested envelope/state object inside it) can never be mutated by
        # this port or by whatever the injected client does with it.
        outbound_params = copy.deepcopy(dict(params))
        try:
            response = self._client.rpc(rpc_name, outbound_params).execute()
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed port error below.
            raise _classify_rpc_exception(rpc_name, exc) from exc

        error = getattr(response, "error", None)
        if error:
            carrier = _RpcErrorCarrier(error)
            raise _classify_rpc_exception(rpc_name, carrier) from carrier

        data = getattr(response, "data", None)
        # Deep-copied so the returned value shares no mutable structure
        # with the raw SDK response object -- neither side can affect the
        # other after this call returns.
        return copy.deepcopy(data)

    def _extract_single_attempt_row(self, data: Any) -> Mapping[str, Any]:
        if isinstance(data, list):
            if len(data) == 0:
                raise ScenarioSupabasePortV2NoAttemptRowError(
                    f"malformed_response: {_GET_ATTEMPT_RPC_NAME!r} returned zero rows"
                )
            if len(data) > 1:
                raise ScenarioSupabasePortV2MultipleAttemptRowsError(
                    f"malformed_response: {_GET_ATTEMPT_RPC_NAME!r} returned {len(data)} rows, "
                    "expected exactly 1"
                )
            row = data[0]
        elif isinstance(data, Mapping):
            row = data
        else:
            raise ScenarioSupabasePortV2MalformedResponseError(
                f"malformed_response: {_GET_ATTEMPT_RPC_NAME!r} returned an unrecognized response "
                f"shape ({type(data).__name__})"
            )
        if not isinstance(row, Mapping):
            raise ScenarioSupabasePortV2MalformedResponseError(
                f"malformed_response: {_GET_ATTEMPT_RPC_NAME!r} row must be a JSON object, "
                f"got {type(row).__name__}"
            )
        return row

    def _project_attempt_row(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        projected: Dict[str, Any] = {
            key: copy.deepcopy(row[key]) for key in _APPROVED_ATTEMPT_ROW_KEYS if key in row
        }
        projected["decisions"] = _project_decision_rows(row.get("decisions"))
        return projected
