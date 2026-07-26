"""SIM-VSLICE-01 / SIM-VSLICE-01D: BA-201 learner start/resume application
controller.

This module is the single application-layer bridge between:

- the verified-session learner identity (`utils.access_control`),
- the read-only scenario catalog and schema validation
  (`utils.scenario_catalog` / `utils.scenario_schema`),
- the deterministic scenario runtime (`utils.scenario_engine`), and
- the V68 attempt-persistence adapter (`utils.scenario_persistence`).

It exists so `pages/Scenario_Simulator.py` never has to import
`utils.scenario_persistence` directly, never has to know the shape of a
serialized engine snapshot, and never has to duplicate any of the
validation those modules already perform. This module performs NO scoring,
scene-transition, or persistence VALIDATION logic of its own -- it only
sequences existing, already-hardened building blocks and translates their
results (and their failures) into one small, learner-safe view model
(`ScenarioAttemptView`) and one small, focused exception hierarchy.

Creative content note
----------------------
The only scenario wired up by this controller is the existing temporary
BA-201 catalog entry (`BA201_CERTIFICATION_EXAM_NAME` /
`BA201_SIMULATION_ID`) already present in
`scenario_content/business_analyst/catalog.json`. These two constants are
catalog LOOKUP KEYS, not creative content -- this module never hard-codes a
character name, dialogue line, image path, or any other Use.AI/Northstar
asset; all narrative text rendered by the learner page comes from the
already-validated scenario JSON via `utils.scenario_schema.ScenarioContent`.

Rerun safety
------------
`start_or_resume_ba201_attempt(...)` is safe to call on every single
Streamlit script rerun. `utils.scenario_persistence.start_or_resume_attempt`
is itself idempotent for an existing `in_progress` attempt (one row per
exact `(user_email, scenario_version_id)` pair, enforced by V68's partial
unique index) -- calling it again simply returns that same attempt's own
persisted state. This module therefore never needs, and never uses,
Streamlit session state as an authoritative source of attempt identity or
state; a caller MAY cache the returned `ScenarioAttemptView` in session
state for display purposes between reruns, but must never treat that cache
as a substitute for calling this function again to get the current
persisted truth.

Scenario-version identity resolution (SIM-VSLICE-01D)
------------------------------------------------------
`start_or_resume_attempt(...)` requires a `scenario_version_id` (a
`scenario_versions.id` UUID) that V66/V67 -- not this module, not V68 --
own. V66 makes `scenarios.current_published_version_id` the single
selection authority for "the version currently offered to NEW learners":
publishing a newer version only repoints it -- older published
`scenario_versions` rows remain permanently published side by side. This
module therefore resolves the id to pass to V68 with a small, read-only
`client.table(...).select(...)` lookup that follows ONLY that pointer:

1. `scenarios` by `simulation_id`, requiring `is_active` and a non-null
   `current_published_version_id`;
2. `scenario_versions` by `id = scenarios.current_published_version_id`
   AND `scenario_id = scenarios.id`, requiring its `version` to exactly
   match the already-validated repository content's `version`.

It deliberately never selects a `scenario_versions` row merely because its
`(scenario_id, version)` matches the local catalog -- an older row could
also match that pair and must never be chosen over the current pointer.
It also deliberately does NOT check `scenario_versions.lifecycle_status`,
`engine_version`, or `canonical_content_sha256` itself:
`start_or_resume_scenario_attempt_v1` already re-validates publication
status, engine version, and content hash server-side for whatever id it is
given (see that function's own comment: it never resolves "the current
version" for a simulation itself), raising `scenario_version_not_published:`
/ `engine_version_mismatch:` / etc. (mapped by the adapter to
`ScenarioVersionMismatchError`, which this module re-wraps as
`ScenarioLearnerVersionUnavailableError`) -- duplicating any of those checks
here would only create a second place that could drift out of sync with the
single source of truth.

Deferred (explicitly out of scope for this task): cross-version resume
policy -- e.g. what happens to a learner's still-`in_progress` attempt on an
older version after a newer version becomes current -- is a separate design
decision required before a second scenario version ever ships. This module
currently only resolves the CURRENT pointer for a fresh
`start_or_resume_attempt(...)` call; it does not special-case an existing
attempt pinned to a version that is no longer current.

Decision submission (SIM-VSLICE-02 / SIM-VSLICE-02A / SIM-VSLICE-02B)
------------------------------------------------------------------------
Decision submission is split into two explicit stages so an uncertain
persistence/backend result can be retried with the EXACT ORIGINAL V68
request, even after the underlying attempt has already advanced or
completed on a prior call whose response was lost:

1. `prepare_ba201_decision(...)` -- does all of the "what is true right
   now" work EXACTLY ONCE per intentional learner decision: verifies
   identity, loads content, resolves the CURRENT scenario version, fetches
   the authoritative persisted attempt (never trusting a caller-supplied
   belief about its sequence/scene), replays it, and applies the selected
   option through `utils.scenario_engine.apply_decision(...)` -- the ONLY
   place scoring, option validity, and scene transition are computed. It
   returns an immutable `PreparedScenarioDecision` and never calls
   `utils.scenario_persistence.submit_decision(...)` itself.

2. `submit_prepared_ba201_decision(...)` -- sends the EXACT fields captured
   by stage 1 to `utils.scenario_persistence.submit_decision(...)`,
   unchanged, on every call, and returns only a small, immutable
   `ScenarioDecisionPersistenceOutcome` -- it deliberately does NOT load
   scenario content, call `get_attempt(...)`, re-resolve the current
   scenario-version pointer, or call
   `utils.scenario_engine.apply_decision(...)` again (SIM-VSLICE-02B). A
   retry of a `PreparedScenarioDecision` is resolution of an
   ALREADY-DECIDED request, not a new decision, so nothing about "what
   should happen" is recomputed, and nothing about "can this retry even
   reach V68" depends on the local scenario-content file still being
   loadable. This is what lets V68's own idempotent replay return the
   ORIGINAL stable result even when the persisted attempt the caller would
   otherwise have looked up is now already advanced past (or completed at)
   the point this request was prepared against -- re-deriving
   `expected_sequence_number`/`expected_scene_id`/`state_before` from a
   freshly-fetched attempt on a retry would otherwise turn a genuine
   lost-response retry into a spurious `sequence_mismatch:` /
   `state_before_mismatch:` conflict, or (for a terminal decision) an
   `attempt_not_in_progress:` rejection, instead of V68's own stable
   `idempotent_replay=true` result; and a transient local content-load
   failure would otherwise be able to block a retry from ever reaching V68
   at all.

   `submit_prepared_ba201_decision(...)` also validates the persisted
   response's identity/lifecycle/state fields against the prepared request
   itself (never against reloaded scenario content) -- a successful V68
   call whose response does not actually match what was submitted is
   classified as an UNCERTAIN integrity outcome (`ScenarioLearnerBackendError`),
   never as an ordinary conclusive rejection, so the caller never discards
   recovery state after a write that may have partially or ambiguously
   succeeded.

View reconstruction (rendering the resulting scene/completion state) is
DELIBERATELY separated from persistence confirmation (SIM-VSLICE-02B): a
caller that needs the learner-facing scene must call
`start_or_resume_ba201_attempt(...)` again (which legitimately reloads
content) once persistence is confirmed -- `submit_prepared_ba201_decision(...)`
itself never does this, so a step that only ever needs to reach V68 can
never be blocked by an unrelated content-loading problem.

`submit_ba201_decision(...)` remains available as a `prepare` ->
`submit prepared` -> `rebuild a ScenarioAttemptView` convenience wrapper for
callers that do not need cross-call retry safety (e.g. tests, one-shot
scripts). It rebuilds the returned view directly from the prepared
request's own (already response-validated) `state_after` payload -- never
via a second `get_attempt(...)` call -- so it still never needs to trust
anything beyond what `submit_prepared_ba201_decision(...)` already
confirmed. `pages/Scenario_Simulator.py` MUST use the explicit two-stage
API directly (never this wrapper) so it can persist the returned
`PreparedScenarioDecision` in `st.session_state` BEFORE persistence is
attempted, and so it can defer view reconstruction to a fresh
`start_or_resume_ba201_attempt(...)` call on the NEXT page pass -- see that
module's own docstring.

Idempotency-key lifecycle is entirely the CALLER's responsibility:
`idempotency_key` is a required parameter to `prepare_ba201_decision(...)`,
never generated internally, so a caller that needs to safely retry an
uncertain submission passes the exact same key (bound inside the returned
`PreparedScenarioDecision`) on every retry. Whether a given failure is safe
to retry, or must be treated as conclusively rejected, is communicated
entirely through which `ScenarioLearnerError` subclass is raised -- see each
exception class's own docstring below.

Deep immutability (SIM-VSLICE-02B)
------------------------------------
`PreparedScenarioDecision` binds every JSON request payload
(`state_before`, `state_after`, `terminal_result_snapshot`) as an already-
canonicalized JSON **string** (`state_before_json` / `state_after_json` /
`terminal_result_snapshot_json`), never as a `dict`/`list`/`MappingProxyType`.
A Python `str` is immutable at every character, so nothing nested inside a
`PreparedScenarioDecision` -- not the top-level mapping, not a nested
`flags` list, not a `decisionHistory` entry, not a nested `terminalResult`
object -- can ever be mutated after preparation, by this module, by
`pages/Scenario_Simulator.py`, or by anything else holding a reference to
it. Every field of `PreparedScenarioDecision` is therefore also a plain
`str`/`int`/`bool`/`Optional[str]` scalar, which makes the whole object
trivially `pickle`-serializable and safe to place directly in Streamlit
`st.session_state` (a `MappingProxyType`, used in the prior SIM-VSLICE-02A
revision, is neither deeply immutable below its own top level nor
`pickle`-serializable). The corresponding plain `dict`/`list` values are
reconstructed via `json.loads(...)` fresh, on demand, only immediately
before calling `utils.scenario_persistence.submit_decision(...)` -- two
separate calls to reconstruct the same field always produce
value-equivalent but independently-owned objects, never the same mutable
instance.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from utils.scenario_catalog import load_resolved_scenario_content, resolve_default_scenario_version_path
from utils.scenario_engine import (
    ENGINE_VERSION,
    ScenarioEngineError,
    ScenarioRunSnapshot,
    ScenarioRunStateError,
    apply_decision,
    get_current_scene,
    replay_serialized_run,
    serialize_run_snapshot,
    serialize_terminal_result,
    start_scenario_run,
)
from utils.scenario_persistence import (
    ScenarioAttemptNotFoundError,
    ScenarioAttemptNotInProgressError,
    ScenarioIdempotencyConflictError,
    ScenarioPersistenceError,
    ScenarioSceneConflictError,
    ScenarioSequenceConflictError,
    ScenarioStateConflictError,
    ScenarioVersionMismatchError,
    get_attempt,
    normalize_scenario_persistence_email,
    start_or_resume_attempt,
    submit_decision,
)
from utils.scenario_schema import ScenarioContentError, load_scenario_content

logger = logging.getLogger(__name__)

# Temporary BA-201 catalog identity (see module docstring). Not final/permanent
# creative content -- these are the existing catalog lookup keys already
# present in scenario_content/business_analyst/catalog.json.
BA201_CERTIFICATION_EXAM_NAME = "Salesforce Certified Business Analyst"
BA201_SIMULATION_ID = "ba201-sim-meridian-health-01"


# ---------------------------------------------------------------------------
# SIM-RUNTIME-03A: opt-in, environment-gated decision-submission diagnostics
# ---------------------------------------------------------------------------
#
# These markers exist ONLY to let a disposable smoke-test run distinguish
# which stage of the two-stage prepare/submit decision pipeline a live
# failure actually happened in, without ever being able to leak a learner
# email, selected option, scenario text, session token, Supabase URL/key,
# idempotency key, attempt/version UUID, serialized engine state, raw RPC
# request/response, or exception message. Every event name and every field
# name/value is checked against the fixed allowlist below before anything is
# written -- there is no code path that accepts an arbitrary caller-supplied
# value. Completely disabled unless CERTBOUND_SCENARIO_SMOKE_DIAGNOSTICS=1.
# Mirrors the identical design already established for auth bootstrap
# diagnostics in `utils.access_control._auth_smoke_trace(...)` (SIM-SMOKE-02H
# / SIM-SMOKE-02I) -- kept as an independent implementation here rather than
# a cross-module import so this module's diagnostics never depend on
# `utils.access_control`'s own allowlist changing shape.
_SCENARIO_SMOKE_DIAGNOSTICS_ENV_VAR = "CERTBOUND_SCENARIO_SMOKE_DIAGNOSTICS"

_SAFE_CLASS_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# Fixed enum of WHICH field category `_persisted_response_matches_prepared(...)`
# rejected -- never the actual (potentially free-form) value on either side.
# `_classify_scenario_response_mismatch(...)` below is the only producer of
# this enum; it never affects the real (unchanged) matching result.
_SCENARIO_MISMATCH_FIELD_VALUES = (
    "attempt_id",
    "sequence_number",
    "scene_id",
    "lifecycle_status",
    "terminal_status",
    "current_scene",
    "engine_state",
    "malformed_response",
    "unknown",
)

# Maps each allowed event name to its allowed fields. Each field spec is one
# of: the literal type `bool` (True/False only), the literal string
# `"SMALL_INT"` (a non-bool int in the closed range [0, 9999] -- ample
# headroom for any real BA-201 sequence number, while still refusing to
# print an arbitrarily large/crafted integer), the literal string
# `"SAFE_CLASS_NAME"` (a bare exception class name -- e.g.
# `type(exc).__name__` -- matching `_SAFE_CLASS_NAME_PATTERN`; this can
# never carry an exception *message*, only its class name), or a tuple of
# the exact fixed enum strings accepted.
_SCENARIO_SMOKE_EVENT_FIELDS: Dict[str, Dict[str, Any]] = {
    "scenario_decision_prepare_started": {},
    "scenario_decision_submit_started": {
        "expected_sequence_number": "SMALL_INT",
        "expected_terminal": bool,
    },
    # Covers every backend-raised failure for the one submit_decision(...)
    # call, including a malformed/no-row RPC response -- `scenario_
    # persistence.py`'s `_require_row(...)`/`_require_field(...)` family
    # raises the same `ScenarioPersistenceBackendError` class for both "RPC
    # returned no row" and "returned row failed validation", so the two are
    # necessarily reported as the one exception_class value here rather than
    # as separate event names; every OTHER backend rejection (sequence/
    # scene/state conflict, idempotency conflict, attempt not found/not in
    # progress, version mismatch) has its own distinct exception_class.
    "scenario_decision_submit_rpc_exception": {"exception_class": "SAFE_CLASS_NAME"},
    "scenario_decision_submit_response_mismatch": {"mismatch_field": _SCENARIO_MISMATCH_FIELD_VALUES},
    "scenario_decision_submit_confirmed_nonterminal": {
        "expected_sequence_number": "SMALL_INT",
        "returned_sequence_number": "SMALL_INT",
    },
    "scenario_decision_submit_confirmed_terminal": {
        "expected_sequence_number": "SMALL_INT",
        "returned_sequence_number": "SMALL_INT",
    },
    "scenario_decision_submit_idempotent_replay": {"returned_terminal": bool},
    "scenario_decision_submit_pending_retry_retained": {"expected_terminal": bool},
}


def _scenario_smoke_diagnostics_enabled() -> bool:
    return os.environ.get(_SCENARIO_SMOKE_DIAGNOSTICS_ENV_VAR) == "1"


def _scenario_smoke_value_allowed(spec: Any, value: Any) -> bool:
    if spec is bool:
        return isinstance(value, bool)
    if spec == "SMALL_INT":
        # bool is a subclass of int in Python (True == 1) -- an accidental
        # bool must never be accepted by an int field spec.
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9999
    if spec == "SAFE_CLASS_NAME":
        return isinstance(value, str) and bool(_SAFE_CLASS_NAME_PATTERN.fullmatch(value))
    if isinstance(spec, tuple):
        if isinstance(value, bool):
            return False
        return value in spec
    return False


def _scenario_smoke_trace(event: str, **safe_fields: Any) -> None:
    """Emit one fixed, allowlisted BA-201 decision-submission diagnostic
    marker to stderr.

    A no-op unless `CERTBOUND_SCENARIO_SMOKE_DIAGNOSTICS=1`. `event` must be
    a key of `_SCENARIO_SMOKE_EVENT_FIELDS`; any field name not declared for
    that event, or any value that does not exactly match that field's fixed
    spec, is silently dropped rather than printed -- there is no fallback
    path that stringifies or logs an unrecognized value. Never accepts (and
    therefore can never leak) a learner email, selected option, scenario
    text, session token, Supabase URL/key, idempotency key, attempt/version
    UUID, serialized engine state, raw RPC request/response, or exception
    message. Never raises."""
    if not _scenario_smoke_diagnostics_enabled():
        return
    try:
        allowed_fields = _SCENARIO_SMOKE_EVENT_FIELDS.get(event)
        if allowed_fields is None:
            return
        parts = [f"event={event}"]
        for name in sorted(allowed_fields):
            if name not in safe_fields:
                continue
            value = safe_fields[name]
            if not _scenario_smoke_value_allowed(allowed_fields[name], value):
                continue
            parts.append(f"{name}={value}")
        sys.stderr.write("[certbound_scenario_smoke] " + " ".join(parts) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScenarioLearnerError(Exception):
    """Base error for the BA-201 learner start/resume controller."""


class ScenarioLearnerAccessError(ScenarioLearnerError):
    """Raised when no verified learner email is available. Callers must
    obtain the email from the existing session layer
    (`utils.access_control.get_current_user_email()` /
    `utils.access_control.require_login()`) -- this module never accepts an
    email from any other source."""


class ScenarioLearnerContentError(ScenarioLearnerError):
    """Raised when the scenario catalog entry, or its underlying content
    JSON, cannot be resolved, loaded, or schema-validated."""


class ScenarioLearnerVersionUnavailableError(ScenarioLearnerError):
    """Raised when the scenario's current-published-version pointer cannot
    be resolved locally (no `scenarios` row, scenario not active, no
    current pointer, pointed `scenario_versions` row missing/mismatched),
    or the V68 RPC itself reports the target `scenario_versions` row does
    not exist, is not published, or does not match the loaded content's
    engine version / content hash."""


class ScenarioLearnerStateError(ScenarioLearnerError):
    """Raised when a persisted engine-state snapshot returned by V68 cannot
    be restored by the deterministic runtime (`utils.scenario_engine`) --
    e.g. its decision history fails replay validation."""


class ScenarioLearnerBackendError(ScenarioLearnerError):
    """Raised for any other V68 persistence-backend failure (malformed RPC
    response, unexpected exception, etc.).

    SIM-VSLICE-02: this is the ONE exception in this hierarchy that
    represents an UNCERTAIN outcome -- the caller (a Streamlit page) must
    NOT clear a pending idempotency key merely because this was raised; the
    same key should be retried once the learner explicitly asks to retry.
    Every other exception below represents a CONCLUSIVE rejection, safe to
    clear pending submission state for."""


class ScenarioLearnerAttemptNotFoundError(ScenarioLearnerError):
    """SIM-VSLICE-02: raised when the attempt id supplied to
    `submit_ba201_decision(...)` does not exist, or exists but is not owned
    by the verified learner email -- deliberately never distinguishable to a
    caller, matching `utils.scenario_persistence.get_attempt`'s /
    `ScenarioAttemptNotFoundError`'s own documented behavior. A conclusive
    rejection: never safe to retry with the same pending state."""


class ScenarioLearnerAttemptNotActiveError(ScenarioLearnerError):
    """SIM-VSLICE-02: raised when a decision is submitted against an attempt
    that is already `completed` or `abandoned` (checked locally against the
    freshly-fetched persisted attempt, and again defensively if the RPC
    itself reports `attempt_not_in_progress:`). A conclusive rejection."""


class ScenarioLearnerAttemptNotCompletedError(ScenarioLearnerError):
    """SIM-VSLICE-03: raised by `load_ba201_completion_result(...)` when the
    requested attempt's persisted status is `in_progress` or `abandoned` --
    i.e. NOT `completed`. Deliberately the mirror image of
    `ScenarioLearnerAttemptNotActiveError` above (which rejects a NEW
    decision against an attempt that has already ENDED): this rejects a
    RESULTS lookup against an attempt that has NOT yet ended. A conclusive
    outcome from `pages/Scenario_Simulator.py`'s perspective -- a stored
    completion marker that turns out to reference an in-progress or
    abandoned attempt is simply wrong, and is cleared rather than retried
    (see that page's own docstring)."""


class ScenarioLearnerInvalidOptionError(ScenarioLearnerError):
    """SIM-VSLICE-02: raised when the selected option is not one of the
    options available on the attempt's actual persisted current scene --
    either because `utils.scenario_engine.apply_decision(...)` rejects it
    outright, or (defensively) because the persistence RPC itself reports a
    scene/state conflict for the freshly-replayed state. A conclusive
    rejection: the learner's selection no longer applies to the attempt's
    actual current scene, so retrying the identical submission can never
    succeed."""


class ScenarioLearnerConflictError(ScenarioLearnerError):
    """SIM-VSLICE-02: raised when the persistence RPC reports a genuine
    conflict against a submission this module already built from
    freshly-fetched persisted state -- a concurrent decision or retry raced
    ahead of this exact call (`sequence_mismatch:` / `scene_mismatch:` /
    `state_before_mismatch:`), or the supplied idempotency key was reused
    for a request whose inputs actually differ
    (`idempotency_key_conflict:`). A conclusive rejection for THIS specific
    request: the caller must discard the pending idempotency key and obtain
    fresh persisted state (e.g. by calling `start_or_resume_ba201_attempt`
    again) rather than blindly resubmitting the same key/inputs."""


# ---------------------------------------------------------------------------
# Learner-safe view model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioOptionView:
    """One selectable-but-not-yet-submittable option label."""

    option_id: str
    label: str


@dataclass(frozen=True)
class ScenarioSceneView:
    """The learner-facing content of exactly one scene."""

    domain_label: str
    narrative: str
    decision_prompt: str
    options: Tuple[ScenarioOptionView, ...]


@dataclass(frozen=True)
class ScenarioAttemptView:
    """A learner-safe, presentation-ready view of one BA-201 attempt.

    `attempt_id` is retained only for internal application use (e.g. a
    future decision-submission call in a later task) --
    `pages/Scenario_Simulator.py` must never render `attempt_id`, or any
    other backend identifier, directly to the learner.
    """

    attempt_id: str
    is_new_attempt: bool
    is_complete: bool
    scenario_title: str
    certification_exam_name: str
    progress_label: str
    current_scene: Optional[ScenarioSceneView]


# ---------------------------------------------------------------------------
# Prepared decision request (SIM-VSLICE-02A)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedScenarioDecision:
    """An immutable, fully-formed V68 `submit_scenario_decision_v1` request,
    captured once by `prepare_ba201_decision(...)`.

    SIM-VSLICE-02B: every field is a plain `str` / `int` / `bool` /
    `Optional[str]` scalar -- in particular, every JSON request payload
    (`state_before_json`, `state_after_json`, `terminal_result_snapshot_json`)
    is stored as an already-canonicalized JSON **string**, never as a
    `dict`/`list`/`MappingProxyType`. A Python string is immutable at every
    character, so nothing nested inside one of these payloads -- not the
    top-level object, not a nested `flags` list, not a `decisionHistory`
    entry, not a nested `terminalResult` object -- can ever be mutated
    after preparation. This also makes the object trivially
    `pickle`-serializable and safe to place directly in Streamlit
    `st.session_state`, unlike the shallow `MappingProxyType` representation
    used by the prior SIM-VSLICE-02A revision (which left nested dicts/lists
    mutable and is not `pickle`-serializable). Use
    `reconstruct_state_before(...)` / `reconstruct_state_after(...)` /
    `reconstruct_terminal_result_snapshot(...)` below to obtain a fresh,
    independently-owned `dict` immediately before calling
    `utils.scenario_persistence.submit_decision(...)` -- two separate calls
    always return value-equivalent but distinct objects.

    Also binds `scenario_version`, `canonical_content_sha256`, and
    `engine_version` -- the exact content identity that was true at
    preparation time -- purely for provenance; `submit_prepared_ba201_decision(...)`
    itself validates the persisted RESPONSE against this request's own
    fields directly and deliberately never reloads scenario content to do
    so (see module docstring).

    `submit_prepared_ba201_decision(...)` sends every request field to
    `utils.scenario_persistence.submit_decision(...)` UNCHANGED -- it never
    re-derives any of them from a freshly-fetched attempt. This is what
    allows a retry to reach V68's own stable idempotent-replay path even
    after the underlying attempt has since advanced past (or completed at)
    the state this request was originally prepared against.

    Never rendered to the learner, and never inspected field-by-field by
    `pages/Scenario_Simulator.py` -- a caller only ever stores this object
    opaquely (e.g. in `st.session_state`) and passes it back into
    `submit_prepared_ba201_decision(...)` unchanged.
    """

    normalized_email: str
    certification_exam_name: str
    simulation_id: str
    scenario_version_id: str
    scenario_version: str
    canonical_content_sha256: str
    engine_version: str
    attempt_id: str
    selected_option_id: str
    idempotency_key: str
    expected_sequence_number: int
    expected_scene_id: str
    state_before_json: str
    state_after_json: str
    resulting_scene_id: Optional[str]
    is_terminal: bool
    terminal_ending_id: Optional[str]
    terminal_result_snapshot_json: Optional[str]

    def reconstruct_state_before(self) -> Dict[str, Any]:
        """A fresh `dict`, parsed from `state_before_json` -- independently
        owned by the caller; mutating it never affects this
        `PreparedScenarioDecision` or any other reconstruction."""
        return _parse_canonical_json(self.state_before_json, field="state_before")

    def reconstruct_state_after(self) -> Dict[str, Any]:
        """A fresh `dict`, parsed from `state_after_json` -- see
        `reconstruct_state_before(...)`."""
        return _parse_canonical_json(self.state_after_json, field="state_after")

    def reconstruct_terminal_result_snapshot(self) -> Optional[Dict[str, Any]]:
        """A fresh `dict`, parsed from `terminal_result_snapshot_json`, or
        `None` for a nonterminal decision -- see
        `reconstruct_state_before(...)`."""
        if self.terminal_result_snapshot_json is None:
            return None
        return _parse_canonical_json(self.terminal_result_snapshot_json, field="terminal_result_snapshot")


@dataclass(frozen=True)
class ScenarioAttemptCompletionMarker:
    """SIM-VSLICE-02B: a minimal, immutable, EMAIL-BOUND record that one
    BA-201 attempt reached a confirmed terminal outcome this Streamlit
    session -- deliberately NOT a `ScenarioAttemptView` (rendering a full
    scene/title would require reloading scenario content, which persistence
    confirmation deliberately never does; see
    `submit_prepared_ba201_decision(...)`'s own docstring). Every field is a
    plain `str`, so this is trivially `pickle`-serializable for
    `st.session_state`.

    Defined HERE (in this always-normally-imported module) rather than
    inside `pages/Scenario_Simulator.py` itself so its class identity stays
    stable across every Streamlit rerun of that page -- a type defined
    directly inside a Streamlit PAGE module can, depending on how that page
    module happens to be loaded/re-executed, end up with a different class
    object on each execution, which would silently break an `isinstance(...)`
    check against a value stored in `st.session_state` on a PRIOR execution.

    `normalized_email` binds this marker to the exact learner identity that
    earned it (`PreparedScenarioDecision.normalized_email`) -- the page
    discards a marker that does not match the CURRENT verified learner
    email before ever showing it.

    SIM-VSLICE-03: this marker remains transient navigation/session
    coordination ONLY -- it identifies WHICH `attempt_id` to load a full
    result for, it is never itself the result authority, and it is never
    read for any display field (title, narrative, score, etc.). The page
    passes `attempt_id` (plus the current verified learner email) to
    `load_ba201_completion_result(...)`, which re-fetches and re-validates
    the persisted attempt from V68 independently of anything this marker
    claims. A marker whose `attempt_id` turns out not to be a completed
    attempt owned by the current learner is simply wrong and is cleared;
    see `pages/Scenario_Simulator.py`'s own docstring for the full
    marker-clearing-vs-preserving decision table.
    """

    normalized_email: str
    attempt_id: str
    status: str


@dataclass(frozen=True)
class ScenarioDecisionPersistenceOutcome:
    """SIM-VSLICE-02B: a small, immutable, learner-safe summary of exactly
    what `utils.scenario_persistence.submit_decision(...)` persisted --
    returned by `submit_prepared_ba201_decision(...)` INSTEAD OF a full
    `ScenarioAttemptView`, since building that view requires reloading
    scenario content, which the persistence-confirmation step deliberately
    never does (see module docstring).

    Never rendered to the learner directly -- a caller uses `is_complete`
    only to decide whether to render a completion marker or to rerun into
    `start_or_resume_ba201_attempt(...)` for the advanced in-progress
    attempt.
    """

    attempt_id: str
    attempt_status: str
    is_complete: bool
    current_scene_id: Optional[str]
    idempotent_replay: bool


# ---------------------------------------------------------------------------
# Completion results (SIM-VSLICE-03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioDomainResultView:
    """SIM-VSLICE-03: one domain's persisted performance.

    Every field comes directly from one entry of the persisted
    `terminal_result_snapshot.domainPerformance` array -- cross-validated
    against a fresh, independent engine replay before this view is ever
    built (see `load_ba201_completion_result(...)`'s own docstring) -- never
    recomputed, estimated, or classified ("strength"/"weakness") beyond
    what the engine itself already defines via
    `utils.scenario_engine.DomainPerformanceSnapshot`.

    `accuracy_percentage` is `None` only when `total_count` is `0` (this
    domain was never actually visited during the attempt) -- a percentage
    is mathematically undefined there, and is never displayed as `0%`.
    """

    domain_label: str
    correct_count: int
    total_count: int
    accuracy_percentage: Optional[float]


@dataclass(frozen=True)
class ScenarioCompletionResultView:
    """SIM-VSLICE-03: a learner-safe, presentation-ready view of one
    COMPLETED BA-201 attempt's persisted terminal result, built by
    `load_ba201_completion_result(...)`.

    Every field here is sourced from exactly one of:

    - the validated scenario content for the attempt's own PINNED
      `scenario_version_id` (`scenario_title`, `certification_exam_name`);
    - the persisted `terminal_result_snapshot`, cross-validated against an
      independent fresh engine replay of the same persisted decision
      history (`ending_title`, `ending_narrative`, `decisions_correct`,
      `decisions_total`, `accuracy_percentage`, `domain_breakdown`,
      `recommended_review_domains`);
    - a fixed, content-independent presentation constant
      (`completion_heading`).

    Deliberately excludes (see `load_ba201_completion_result(...)`'s own
    docstring for the full rationale): attempt/scenario-version UUIDs,
    idempotency keys, sequence numbers, raw engine `state`/`flags`, the raw
    `terminal_result_snapshot` payload itself, canonical content hashes,
    and any other backend/database field. Nothing here is ever recomputed
    from browser/session assumptions -- the persisted attempt is the sole
    authority, and this view is only ever a safe projection of it.

    `ending_title` is exactly `ScenarioEnding.score_band` -- the BA-201
    content schema (`utils.scenario_schema.ScenarioEnding`) has no separate,
    structured "title" field distinct from its authored `scoreBand` string
    (e.g. `"Pass with Distinction"`); this is the one authored string that
    plays that role for this content. A `"strengths"` field is deliberately
    NOT part of this view: nothing in the persisted result or the BA-201
    content contract labels any domain a "strength" -- inventing such a
    label from raw domain-accuracy numbers would be exactly the kind of
    unsupported classification this task forbids. `recommended_review_domains`
    is the one remediation-shaped field the content DOES author explicitly
    (`ScenarioEnding.recommended_review`, a list of domain ids) -- it is
    always present (never `None`), but is often an empty tuple for a strong
    ending; the page renders that section only when it is non-empty.
    """

    scenario_title: str
    certification_exam_name: str
    completion_heading: str
    ending_title: str
    ending_narrative: str
    decisions_correct: Optional[int]
    decisions_total: Optional[int]
    accuracy_percentage: Optional[float]
    domain_breakdown: Tuple[ScenarioDomainResultView, ...]
    recommended_review_domains: Tuple[str, ...]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _canonical_json(value: Mapping[str, Any]) -> str:
    """The one canonicalization this module ever uses for a JSON request
    payload -- `sort_keys=True` makes nested key order irrelevant, so this
    is deterministic across process restarts and across a Streamlit
    session-state pickle round-trip; the exact same input dict always
    produces the exact same string. Matches the style (though not
    necessarily byte-for-byte, since that is never required here)
    `utils.scenario_persistence.compute_request_fingerprint(...)` itself
    documents using for its own canonical encoding."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _parse_canonical_json(text: str, *, field: str) -> Dict[str, Any]:
    """The inverse of `_canonical_json(...)`. Only ever called on a string
    this module itself produced via `_canonical_json(...)` -- a failure
    here indicates local corruption (e.g. a hand-crafted/corrupted
    `PreparedScenarioDecision`), never a normal, expected outcome, so it is
    mapped to `ScenarioLearnerStateError` exactly like any other
    "persisted/prepared state could not be restored" failure."""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ScenarioLearnerStateError(
            f"The prepared {field} payload could not be restored."
        ) from exc
    if not isinstance(parsed, dict):
        raise ScenarioLearnerStateError(
            f"The prepared {field} payload could not be restored."
        )
    return parsed


def _default_client() -> Any:
    """Obtain the default Supabase admin client, or raise
    `ScenarioLearnerBackendError` -- NEVER a raw exception -- if
    construction itself fails (e.g. missing/invalid service-role
    configuration).

    SIM-VSLICE-02C: this is the ONE place all three public entry points
    (`start_or_resume_ba201_attempt`, `prepare_ba201_decision`,
    `submit_prepared_ba201_decision`) obtain a default client, so wrapping
    it here consistently maps a client-initialization failure for all three
    without broadening any OTHER exception boundary. Each caller already
    gives `ScenarioLearnerBackendError` the correct retry semantics for its
    own stage:

    - `start_or_resume_ba201_attempt`: a plain read/write failure -- the
      page shows its existing safe "unavailable" state. This call happens
      BEFORE `_resolve_current_scenario_version_id(...)`, so a client
      failure here can never be mistaken for, or obscure, a later
      `ScenarioLearnerVersionUnavailableError`.
    - `prepare_ba201_decision`: a preparation/READ failure -- no V68 write
      was ever attempted, so a caller must never retain a pending prepared
      request for this (see that function's own docstring).
    - `submit_prepared_ba201_decision`: an UNCERTAIN submit/replay outcome
      -- the caller (`pages/Scenario_Simulator.py`) preserves the exact
      pending `PreparedScenarioDecision` for retry, exactly as it already
      does for any other `ScenarioLearnerBackendError` raised there. This
      call happens AFTER the prepared JSON payloads are reconstructed
      locally (no catalog/content/attempt/pointer work of any kind), so a
      client failure here still never performs any of that extra work.
    """
    from utils.access_control import get_supabase_admin_client  # noqa: PLC0415

    try:
        return get_supabase_admin_client()
    except Exception as exc:  # noqa: BLE001 - client construction/config failure, never surfaced raw
        logger.exception("BA-201 controller could not obtain the default Supabase admin client")
        raise ScenarioLearnerBackendError(
            "The scenario service is temporarily unavailable."
        ) from exc


def _resolve_current_scenario_version_id(client: Any, *, simulation_id: str, version: str) -> str:
    """Resolve the exact `scenario_versions.id` UUID that
    `scenarios.current_published_version_id` currently points at for one
    `simulation_id`, and confirm that row's `version` matches the loaded
    repository content's `version`.

    ONLY `scenarios.current_published_version_id` -- never an arbitrary
    `(scenario_id, version)` string match against `scenario_versions` --
    determines which version this controller offers to a NEW learner.
    Multiple published `scenario_versions` rows may permanently coexist for
    the same scenario (V66: publishing a newer version only repoints
    `current_published_version_id`, it never edits or retires older
    published versions); an older row must never be selected merely
    because its `version` string happens to equal the local repository
    content's `version`.

    Raises `ScenarioLearnerVersionUnavailableError` when:
    - no `scenarios` row exists for `simulation_id`;
    - `scenarios.is_active` is false;
    - `scenarios.current_published_version_id` is null/empty;
    - the pointed `scenario_versions` row does not exist, or does not
      belong to this exact scenario;
    - its `version` does not exactly match `version`.

    Raises `ScenarioLearnerBackendError` for any unexpected client/network
    failure.

    Deliberately does NOT check `scenario_versions.lifecycle_status`,
    `engine_version`, or `canonical_content_sha256` -- see the module
    docstring's "Scenario-version identity resolution" section:
    `start_or_resume_scenario_attempt_v1` (V68) already re-validates all of
    those server-side for whatever id it is given, and remains the single
    source of truth for them.
    """
    try:
        scenario_rows = (
            client.table("scenarios")
            .select("id,is_active,current_published_version_id")
            .eq("simulation_id", simulation_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001 - backend/network failure, not a validation failure
        raise ScenarioLearnerBackendError(
            f"Unable to resolve scenario row for simulation_id {simulation_id!r}"
        ) from exc

    if not scenario_rows or not scenario_rows[0].get("id"):
        raise ScenarioLearnerVersionUnavailableError(
            f"No scenario is registered for simulation_id {simulation_id!r}"
        )
    scenario_row = scenario_rows[0]

    if not scenario_row.get("is_active"):
        raise ScenarioLearnerVersionUnavailableError(
            f"Scenario {simulation_id!r} is not active"
        )

    current_published_version_id = scenario_row.get("current_published_version_id")
    if not current_published_version_id:
        raise ScenarioLearnerVersionUnavailableError(
            f"Scenario {simulation_id!r} has no current published version"
        )

    scenario_id = scenario_row["id"]

    try:
        version_rows = (
            client.table("scenario_versions")
            .select("id,version")
            .eq("id", current_published_version_id)
            .eq("scenario_id", scenario_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        raise ScenarioLearnerBackendError(
            f"Unable to resolve current scenario_versions row for simulation_id {simulation_id!r}"
        ) from exc

    if not version_rows or not version_rows[0].get("id"):
        # Missing row AND "belongs to a different scenario" both surface
        # here identically, since the query above filters on
        # (id, scenario_id) together -- a pointer that resolves to a row
        # owned by a different scenario simply will not be found.
        raise ScenarioLearnerVersionUnavailableError(
            f"Scenario {simulation_id!r}'s current published version could not be resolved"
        )

    if version_rows[0].get("version") != version:
        raise ScenarioLearnerVersionUnavailableError(
            f"Scenario {simulation_id!r}'s current published version does not match the loaded scenario content"
        )

    # The value passed onward is always the pointer itself, never a
    # re-derived id from the scenario_versions row -- they are required to
    # be equal by the query's own (id = ...) filter, but this makes the
    # contract ("exactly scenarios.current_published_version_id") explicit
    # rather than incidental.
    return str(current_published_version_id)


def _load_default_scenario_content(*, certification_exam_name: str, simulation_id: str):
    """Load and schema-validate the catalog's default (or sole) version for
    one scenario, using only the existing catalog-resolution and
    schema-validation modules -- this function performs no validation of
    its own."""
    try:
        content_path = resolve_default_scenario_version_path(
            certification_exam_name=certification_exam_name,
            simulation_id=simulation_id,
        )
        return load_scenario_content(content_path)
    except ScenarioContentError as exc:
        logger.exception(
            "Scenario content could not be loaded for certification_exam_name=%r simulation_id=%r",
            certification_exam_name,
            simulation_id,
        )
        raise ScenarioLearnerContentError("The scenario could not be loaded right now.") from exc


def _build_scene_view(run: ScenarioRunSnapshot) -> ScenarioSceneView:
    scene = get_current_scene(run)
    domain_labels = {domain.id: domain.label for domain in run.content.domains}
    options = tuple(
        ScenarioOptionView(option_id=option.id, label=option.text) for option in scene.decision.options
    )
    return ScenarioSceneView(
        domain_label=domain_labels.get(scene.domain_id, scene.domain_id),
        narrative=scene.narrative,
        decision_prompt=scene.decision.prompt,
        options=options,
    )


def _build_attempt_view(
    *,
    run: ScenarioRunSnapshot,
    attempt_id: str,
    is_new_attempt: bool,
    lifecycle_status: str,
) -> ScenarioAttemptView:
    """Build the one shared learner-safe view model from a just-replayed
    `ScenarioRunSnapshot`, used identically by
    `start_or_resume_ba201_attempt(...)` and `submit_ba201_decision(...)` so
    "is this attempt complete" / "what does the current scene look like" is
    decided in exactly one place regardless of which of those two functions
    produced the run.

    `lifecycle_status` is the persisted attempt's own `status` /
    `attempt_status` field (`"in_progress"` / `"completed"` /
    `"abandoned"`) -- a run can be `is_complete=False` while the persisted
    attempt is `"abandoned"` (an abandoned attempt is never replayed as
    complete by the engine itself), so both signals are combined here,
    exactly as the pre-existing start/resume logic already did.
    """
    is_complete = bool(run.is_complete) or lifecycle_status != "in_progress"
    current_scene_view = None if is_complete else _build_scene_view(run)
    progress_label = "Scenario complete" if is_complete else f"Decision {len(run.decisions) + 1}"
    return ScenarioAttemptView(
        attempt_id=attempt_id,
        is_new_attempt=is_new_attempt,
        is_complete=is_complete,
        scenario_title=run.content.title,
        certification_exam_name=run.content.certification_exam_name,
        progress_label=progress_label,
        current_scene=current_scene_view,
    )


# ---------------------------------------------------------------------------
# Public controller entry points
# ---------------------------------------------------------------------------


def start_or_resume_ba201_attempt(
    user_email: Optional[str],
    *,
    client: Any = None,
    certification_exam_name: str = BA201_CERTIFICATION_EXAM_NAME,
    simulation_id: str = BA201_SIMULATION_ID,
) -> ScenarioAttemptView:
    """Start or resume the caller's BA-201 attempt and return a
    presentation-ready view model for `pages/Scenario_Simulator.py`.

    `user_email` must already be the verified learner email obtained from
    `utils.access_control.get_current_user_email()` /
    `utils.access_control.require_login()`. This function never trusts, and
    never accepts, an email from a query parameter, form field, or arbitrary
    session value -- an unauthenticated or missing email is rejected with
    `ScenarioLearnerAccessError` before any catalog, engine, or persistence
    call is made.

    Safe to call on every Streamlit rerun (see module docstring).
    """
    if not user_email or "@" not in str(user_email):
        raise ScenarioLearnerAccessError(
            "A verified learner email is required to start or resume a scenario attempt."
        )
    normalized_email = normalize_scenario_persistence_email(user_email)

    content = _load_default_scenario_content(
        certification_exam_name=certification_exam_name,
        simulation_id=simulation_id,
    )

    resolved_client = client if client is not None else _default_client()

    version_id = _resolve_current_scenario_version_id(
        resolved_client,
        simulation_id=content.simulation_id,
        version=content.version,
    )

    # Cheap, pure, and required by start_or_resume_attempt's signature
    # regardless of whether an attempt already exists -- the RPC itself
    # uses these values ONLY when it actually creates a brand-new attempt,
    # and ignores them entirely when resuming (see
    # utils.scenario_persistence.start_or_resume_attempt's own docstring).
    initial_run = start_scenario_run(content)
    initial_serialized_state = serialize_run_snapshot(initial_run)

    try:
        result = start_or_resume_attempt(
            resolved_client,
            user_email=normalized_email,
            scenario_version_id=version_id,
            initial_current_scene_id=content.start_scene,
            initial_serialized_state=initial_serialized_state,
            engine_version=ENGINE_VERSION,
            scenario_content_sha256=content.canonical_content_sha256,
        )
    except ScenarioVersionMismatchError as exc:
        logger.exception("BA-201 scenario version is unavailable for start/resume")
        raise ScenarioLearnerVersionUnavailableError(
            "This scenario version is not currently available."
        ) from exc
    except ScenarioPersistenceError as exc:
        logger.exception("BA-201 start/resume attempt persistence call failed")
        raise ScenarioLearnerBackendError(
            "The scenario attempt could not be started or resumed right now."
        ) from exc

    # ALWAYS restore the runtime from the RPC's own returned
    # serialized_engine_state -- never from the initial_run built above --
    # so a resumed attempt's actual persisted progress is what gets
    # rendered, never a freshly-built initial snapshot.
    try:
        run = replay_serialized_run(content, result.serialized_engine_state)
    except ScenarioEngineError as exc:
        logger.exception("BA-201 persisted engine state failed replay validation")
        raise ScenarioLearnerStateError(
            "The saved progress for this scenario could not be restored."
        ) from exc

    return _build_attempt_view(
        run=run,
        attempt_id=result.attempt_id,
        is_new_attempt=result.created,
        lifecycle_status=result.status,
    )


def prepare_ba201_decision(
    user_email: Optional[str],
    *,
    attempt_id: str,
    selected_option_id: str,
    idempotency_key: str,
    client: Any = None,
    certification_exam_name: str = BA201_CERTIFICATION_EXAM_NAME,
    simulation_id: str = BA201_SIMULATION_ID,
) -> PreparedScenarioDecision:
    """Stage A (SIM-VSLICE-02A): resolve "what is true right now" for a
    NEW, intentional learner decision, and return an immutable
    `PreparedScenarioDecision` -- WITHOUT calling
    `utils.scenario_persistence.submit_decision(...)`.

    `user_email` must already be the verified learner email (see
    `start_or_resume_ba201_attempt`'s own docstring; the same rule applies
    here unchanged). `attempt_id` must be an attempt already owned by that
    email (checked via `utils.scenario_persistence.get_attempt`, never by
    trusting a caller's own belief). `idempotency_key` must already be a
    UUIDv4 string the caller generated once for this specific intentional
    submission -- it is bound into the returned object unchanged, never
    regenerated, so every retry of the resulting `PreparedScenarioDecision`
    (via `submit_prepared_ba201_decision(...)`) uses the identical key.

    Sequencing, in order:

    1. Verify `user_email` (`ScenarioLearnerAccessError`).
    2. Load and validate the canonical BA-201 content
       (`ScenarioLearnerContentError`).
    3. Resolve the scenario's CURRENT published version id, exactly like
       `start_or_resume_ba201_attempt` (`ScenarioLearnerVersionUnavailableError`)
       -- current-version enforcement belongs HERE, to preparing a NEW
       decision, never to replaying an already-prepared one.
    4. Fetch the attempt's actual persisted state via
       `utils.scenario_persistence.get_attempt(...)`
       (`ScenarioLearnerAttemptNotFoundError` /
       `ScenarioLearnerBackendError`) -- this is the ONLY source of the
       `expected_sequence_number` / `expected_scene_id` bound into the
       returned request; a caller-supplied belief is never used.
    5. Reject an attempt that is not `in_progress`
       (`ScenarioLearnerAttemptNotActiveError`).
    6. Reject an attempt pinned to a `scenario_version_id` other than the
       version resolved in step 3 (`ScenarioLearnerVersionUnavailableError`)
       -- the deferred cross-version resume policy (see module docstring)
       means this attempt can no longer safely accept a new decision
       through this controller.
    7. Replay the attempt's persisted engine state
       (`ScenarioLearnerStateError` on a corrupt/foreign snapshot).
    8. Apply `selected_option_id` through
       `utils.scenario_engine.apply_decision(...)` -- the ONLY place
       scoring, option validity, and scene transition are computed
       (`ScenarioLearnerInvalidOptionError` if the option does not exist on
       the attempt's actual current scene).
    9. Serialize the resulting state/terminal fields, canonicalize each
       JSON payload into an immutable string (`_canonical_json(...)`), and
       bind every field, unchanged, into the returned
       `PreparedScenarioDecision` (SIM-VSLICE-02B: never as a mutable
       `dict`/`list`/`MappingProxyType`).

    None of these failures ever attempt a V68 write -- a caller (see
    `pages/Scenario_Simulator.py`) must never retain a pending
    idempotency-key/request record for a `prepare_ba201_decision(...)`
    failure, since no write was ever attempted.
    """
    _scenario_smoke_trace("scenario_decision_prepare_started")
    if not user_email or "@" not in str(user_email):
        raise ScenarioLearnerAccessError(
            "A verified learner email is required to submit a scenario decision."
        )
    normalized_email = normalize_scenario_persistence_email(user_email)

    content = _load_default_scenario_content(
        certification_exam_name=certification_exam_name,
        simulation_id=simulation_id,
    )

    resolved_client = client if client is not None else _default_client()

    version_id = _resolve_current_scenario_version_id(
        resolved_client,
        simulation_id=content.simulation_id,
        version=content.version,
    )

    try:
        attempt = get_attempt(
            resolved_client,
            user_email=normalized_email,
            attempt_id=attempt_id,
        )
    except ScenarioAttemptNotFoundError as exc:
        raise ScenarioLearnerAttemptNotFoundError(
            "This scenario attempt could not be found."
        ) from exc
    except ScenarioPersistenceError as exc:
        logger.exception("BA-201 decision-preparation attempt lookup failed")
        raise ScenarioLearnerBackendError(
            "This scenario attempt could not be loaded right now."
        ) from exc

    if attempt.status != "in_progress":
        raise ScenarioLearnerAttemptNotActiveError(
            "This scenario attempt has already ended and cannot accept another decision."
        )

    if attempt.scenario_version_id != version_id:
        raise ScenarioLearnerVersionUnavailableError(
            "This scenario attempt is pinned to a version that is no longer current."
        )

    try:
        run = replay_serialized_run(content, attempt.serialized_engine_state)
    except ScenarioEngineError as exc:
        logger.exception("BA-201 persisted engine state failed replay validation before a decision")
        raise ScenarioLearnerStateError(
            "The saved progress for this scenario could not be restored."
        ) from exc

    try:
        next_run = apply_decision(run, selected_option_id)
    except ScenarioRunStateError as exc:
        raise ScenarioLearnerInvalidOptionError(
            "That option is no longer available for the current scene."
        ) from exc

    state_before_json = _canonical_json(dict(attempt.serialized_engine_state))
    state_after_json = _canonical_json(serialize_run_snapshot(next_run))

    if next_run.is_complete:
        terminal_result = next_run.terminal_result
        assert terminal_result is not None  # guaranteed by apply_decision when is_complete is True
        resulting_scene_id: Optional[str] = None
        is_terminal = True
        terminal_ending_id: Optional[str] = terminal_result.ending_id
        terminal_result_snapshot_json: Optional[str] = _canonical_json(
            serialize_terminal_result(terminal_result)
        )
    else:
        resulting_scene_id = next_run.current_scene_id
        is_terminal = False
        terminal_ending_id = None
        terminal_result_snapshot_json = None

    return PreparedScenarioDecision(
        normalized_email=normalized_email,
        certification_exam_name=certification_exam_name,
        simulation_id=simulation_id,
        scenario_version_id=version_id,
        scenario_version=content.version,
        canonical_content_sha256=content.canonical_content_sha256,
        engine_version=ENGINE_VERSION,
        attempt_id=attempt.attempt_id,
        selected_option_id=selected_option_id,
        idempotency_key=idempotency_key,
        expected_sequence_number=attempt.next_sequence_number,
        expected_scene_id=attempt.current_scene_id,
        state_before_json=state_before_json,
        state_after_json=state_after_json,
        resulting_scene_id=resulting_scene_id,
        is_terminal=is_terminal,
        terminal_ending_id=terminal_ending_id,
        terminal_result_snapshot_json=terminal_result_snapshot_json,
    )


def _persisted_response_matches_prepared(
    prepared: PreparedScenarioDecision,
    submission: Any,
    *,
    state_after: Mapping[str, Any],
    terminal_result_snapshot: Optional[Mapping[str, Any]],
) -> bool:
    """Pure equality/shape check -- never a recomputation of scoring,
    transitions, or endings -- proving the persisted RESPONSE actually
    reflects the EXACT request `prepared` describes, using ONLY the
    request's own already-known fields (never scenario content, which
    `submit_prepared_ba201_decision(...)` deliberately never reloads).

    SIM-VSLICE-02C: requires an EXACT lifecycle-status match rather than
    merely `!= "in_progress"` -- a nonterminal `prepared` request requires
    the response to be exactly `"in_progress"`, and a terminal one requires
    exactly `"completed"`. This means `"abandoned"` (a third, legitimate
    lifecycle status this module itself never causes, but which V68 could
    in principle return for some other reason) is NEVER treated as a valid
    match for either case -- it always falls through to `return False`
    below, exactly like any other mismatch, preserving pending state rather
    than being silently accepted as a completion. `terminal_result_snapshot`
    is compared value-for-value against the exact reconstructed prepared
    snapshot, never merely checked for `None`-ness.
    """
    if submission.attempt_id != prepared.attempt_id:
        return False

    # Only checked when the result type actually exposes both fields --
    # some test doubles/response contracts may not.
    if hasattr(submission, "sequence_number") and hasattr(submission, "next_sequence_number"):
        if submission.sequence_number != prepared.expected_sequence_number:
            return False
        if submission.next_sequence_number != prepared.expected_sequence_number + 1:
            return False

    if prepared.is_terminal:
        if submission.attempt_status != "completed":
            return False
        if submission.current_scene_id is not None:
            return False
        if submission.terminal_ending_id != prepared.terminal_ending_id:
            return False
        if dict(submission.terminal_result_snapshot or {}) != dict(terminal_result_snapshot or {}):
            return False
    else:
        if submission.attempt_status != "in_progress":
            return False
        if submission.current_scene_id != prepared.resulting_scene_id:
            return False
        if submission.terminal_ending_id is not None:
            return False
        if submission.terminal_result_snapshot is not None:
            return False

    if submission.serialized_engine_state != dict(state_after):
        return False
    return True


def _classify_scenario_response_mismatch(
    prepared: PreparedScenarioDecision,
    submission: Any,
    *,
    state_after: Mapping[str, Any],
    terminal_result_snapshot: Optional[Mapping[str, Any]],
) -> str:
    """SIM-RUNTIME-03A diagnostics-only helper: reclassify WHY
    `_persisted_response_matches_prepared(...)` already returned `False` as
    exactly one of `_SCENARIO_MISMATCH_FIELD_VALUES`, for
    `_scenario_smoke_trace(...)` to report -- NEVER the actual field values
    on either side, and NEVER used to decide the real (unchanged) matching
    result. Mirrors `_persisted_response_matches_prepared(...)`'s own checks,
    in the same order, purely as a read-only re-walk; it is intentionally a
    separate function so `_persisted_response_matches_prepared(...)` itself
    is never touched by this diagnostics feature. Never raises: an
    unexpected shape on `submission` is reported as `"malformed_response"`,
    and any other unexpected failure during classification is reported as
    `"unknown"` rather than propagating."""
    try:
        if submission.attempt_id != prepared.attempt_id:
            return "attempt_id"

        if hasattr(submission, "sequence_number") and hasattr(submission, "next_sequence_number"):
            if submission.sequence_number != prepared.expected_sequence_number:
                return "sequence_number"
            if submission.next_sequence_number != prepared.expected_sequence_number + 1:
                return "sequence_number"

        if prepared.is_terminal:
            if submission.attempt_status != "completed":
                return "lifecycle_status"
            if submission.current_scene_id is not None:
                return "current_scene"
            if submission.terminal_ending_id != prepared.terminal_ending_id:
                return "terminal_status"
            if dict(submission.terminal_result_snapshot or {}) != dict(terminal_result_snapshot or {}):
                return "terminal_status"
        else:
            if submission.attempt_status != "in_progress":
                return "lifecycle_status"
            if submission.current_scene_id != prepared.resulting_scene_id:
                return "current_scene"
            if submission.terminal_ending_id is not None:
                return "terminal_status"
            if submission.terminal_result_snapshot is not None:
                return "terminal_status"

        if submission.serialized_engine_state != dict(state_after):
            return "engine_state"

        # Every known check above passed, yet the caller observed
        # `_persisted_response_matches_prepared(...)` return False -- this
        # should be unreachable in practice, but is reported honestly as
        # "unknown" rather than silently defaulting to any specific field.
        return "unknown"
    except AttributeError:
        return "malformed_response"
    except Exception:
        return "unknown"


def submit_prepared_ba201_decision(
    user_email: Optional[str],
    prepared: PreparedScenarioDecision,
    *,
    client: Any = None,
) -> ScenarioDecisionPersistenceOutcome:
    """Stage B (SIM-VSLICE-02B): submit (or safely replay) an ALREADY
    `prepare_ba201_decision(...)`-prepared request, unchanged, and return
    only a small, immutable `ScenarioDecisionPersistenceOutcome` --
    deliberately NOT a full `ScenarioAttemptView`.

    This is the ONLY function that calls
    `utils.scenario_persistence.submit_decision(...)`. Every field of
    `prepared` is sent exactly as reconstructed from its immutable JSON
    string payloads -- this function never loads scenario content, never
    calls `utils.scenario_persistence.get_attempt(...)`, never re-resolves
    the current scenario-version pointer, and never calls
    `utils.scenario_engine.apply_decision(...)` again (SIM-VSLICE-02B: NONE
    of those can transiently fail and block a retry from reaching V68 at
    all). A retry of the exact same `prepared` object (same
    `idempotency_key`, same everything else) is therefore resolution of an
    ALREADY-DECIDED request, not a new decision -- so V68's own
    idempotent-replay path can return the original stable result even if
    the persisted attempt has, in the meantime, already advanced past (a
    committed but unacknowledged nonterminal decision) or completed at (a
    committed but unacknowledged terminal decision) the exact state this
    request was prepared against.

    `user_email` must be the CURRENT verified learner email, and must
    match `prepared.normalized_email` -- a prepared request can never be
    submitted under a different learner's session
    (`ScenarioLearnerAccessError`).

    After a successful V68 call, the persisted response is validated
    against `prepared`'s own fields (`_persisted_response_matches_prepared`)
    -- NOT against reloaded scenario content. A mismatch is treated as an
    UNCERTAIN integrity outcome (`ScenarioLearnerBackendError`), never as an
    ordinary conclusive rejection: a write that appears to have succeeded
    but whose response cannot be confirmed to be exactly what was requested
    must never cause a caller to discard its recovery state.

    Every OTHER raised exception has the exact same meaning (and the exact
    same safe-to-retry-with-the-same-pending-state semantics) as the
    corresponding one raised by the pre-SIM-VSLICE-02A single-call
    `submit_ba201_decision(...)`.
    """
    if not user_email or "@" not in str(user_email):
        raise ScenarioLearnerAccessError(
            "A verified learner email is required to submit a scenario decision."
        )
    normalized_email = normalize_scenario_persistence_email(user_email)
    if normalized_email != prepared.normalized_email:
        raise ScenarioLearnerAccessError(
            "This scenario decision was prepared for a different learner session."
        )

    # SIM-VSLICE-02B: reconstructed fresh from the immutable canonical JSON
    # strings -- no scenario content, no catalog access, no additional RPC
    # of any kind happens before the one submit_decision(...) call below.
    state_before = prepared.reconstruct_state_before()
    state_after = prepared.reconstruct_state_after()
    terminal_result_snapshot = prepared.reconstruct_terminal_result_snapshot()

    resolved_client = client if client is not None else _default_client()

    submit_kwargs: dict[str, Any] = {
        "resulting_scene_id": prepared.resulting_scene_id,
        "is_terminal": prepared.is_terminal,
        "terminal_ending_id": prepared.terminal_ending_id,
        "terminal_result_snapshot": terminal_result_snapshot,
    }

    _scenario_smoke_trace(
        "scenario_decision_submit_started",
        expected_sequence_number=prepared.expected_sequence_number,
        expected_terminal=prepared.is_terminal,
    )
    try:
        submission = submit_decision(
            resolved_client,
            user_email=prepared.normalized_email,
            attempt_id=prepared.attempt_id,
            expected_sequence_number=prepared.expected_sequence_number,
            expected_scene_id=prepared.expected_scene_id,
            selected_option_id=prepared.selected_option_id,
            state_before=state_before,
            state_after=state_after,
            idempotency_key=prepared.idempotency_key,
            **submit_kwargs,
        )
    except ScenarioAttemptNotFoundError as exc:
        _scenario_smoke_trace("scenario_decision_submit_rpc_exception", exception_class=type(exc).__name__)
        raise ScenarioLearnerAttemptNotFoundError(
            "This scenario attempt could not be found."
        ) from exc
    except ScenarioAttemptNotInProgressError as exc:
        _scenario_smoke_trace("scenario_decision_submit_rpc_exception", exception_class=type(exc).__name__)
        raise ScenarioLearnerAttemptNotActiveError(
            "This scenario attempt has already ended and cannot accept another decision."
        ) from exc
    except ScenarioVersionMismatchError as exc:
        _scenario_smoke_trace("scenario_decision_submit_rpc_exception", exception_class=type(exc).__name__)
        raise ScenarioLearnerVersionUnavailableError(
            "This scenario version is not currently available."
        ) from exc
    except (
        ScenarioSequenceConflictError,
        ScenarioSceneConflictError,
        ScenarioStateConflictError,
        ScenarioIdempotencyConflictError,
    ) as exc:
        _scenario_smoke_trace("scenario_decision_submit_rpc_exception", exception_class=type(exc).__name__)
        logger.warning("BA-201 decision submission hit a safe conflict: %s", exc)
        raise ScenarioLearnerConflictError(
            "This scenario has moved on since it was last loaded. Please try again."
        ) from exc
    except ScenarioPersistenceError as exc:
        _scenario_smoke_trace("scenario_decision_submit_rpc_exception", exception_class=type(exc).__name__)
        logger.exception("BA-201 decision-submission persistence call failed")
        raise ScenarioLearnerBackendError(
            "This decision could not be submitted right now."
        ) from exc

    # SIM-VSLICE-02B: a successful RPC call whose response does not
    # actually match this exact request is an UNCERTAIN integrity outcome,
    # never an ordinary conclusive rejection -- see this function's own
    # docstring and the module docstring's "Decision submission" section.
    if not _persisted_response_matches_prepared(
        prepared,
        submission,
        state_after=state_after,
        terminal_result_snapshot=terminal_result_snapshot,
    ):
        logger.error(
            "BA-201 decision submission returned a response that does not match the prepared "
            "request for attempt_id=%r idempotency_key=%r",
            prepared.attempt_id,
            prepared.idempotency_key,
        )
        _scenario_smoke_trace(
            "scenario_decision_submit_response_mismatch",
            mismatch_field=_classify_scenario_response_mismatch(
                prepared, submission, state_after=state_after, terminal_result_snapshot=terminal_result_snapshot
            ),
        )
        raise ScenarioLearnerBackendError(
            "This decision's confirmation could not be verified. Please try again."
        )

    if submission.idempotent_replay:
        _scenario_smoke_trace(
            "scenario_decision_submit_idempotent_replay",
            returned_terminal=submission.attempt_status == "completed",
        )
    elif prepared.is_terminal:
        _scenario_smoke_trace(
            "scenario_decision_submit_confirmed_terminal",
            expected_sequence_number=prepared.expected_sequence_number,
            returned_sequence_number=getattr(submission, "sequence_number", prepared.expected_sequence_number),
        )
    else:
        _scenario_smoke_trace(
            "scenario_decision_submit_confirmed_nonterminal",
            expected_sequence_number=prepared.expected_sequence_number,
            returned_sequence_number=getattr(submission, "sequence_number", prepared.expected_sequence_number),
        )

    return ScenarioDecisionPersistenceOutcome(
        attempt_id=submission.attempt_id,
        attempt_status=submission.attempt_status,
        is_complete=submission.attempt_status != "in_progress",
        current_scene_id=submission.current_scene_id,
        idempotent_replay=submission.idempotent_replay,
    )


def submit_ba201_decision(
    user_email: Optional[str],
    *,
    attempt_id: str,
    selected_option_id: str,
    idempotency_key: str,
    client: Any = None,
    certification_exam_name: str = BA201_CERTIFICATION_EXAM_NAME,
    simulation_id: str = BA201_SIMULATION_ID,
) -> ScenarioAttemptView:
    """Convenience wrapper: `prepare_ba201_decision(...)` ->
    `submit_prepared_ba201_decision(...)` -> rebuild a `ScenarioAttemptView`,
    in one call.

    SIM-VSLICE-02A/02B: a caller that needs to safely retry an uncertain
    result across multiple separate calls (i.e.
    `pages/Scenario_Simulator.py`) must NOT use this wrapper -- it must call
    `prepare_ba201_decision(...)` once, persist the returned
    `PreparedScenarioDecision` BEFORE persistence is attempted, and call
    `submit_prepared_ba201_decision(...)` with that exact stored object on
    every retry, deferring view reconstruction to a fresh
    `start_or_resume_ba201_attempt(...)` call on the NEXT page pass. This
    wrapper exists only for callers (e.g. tests, one-shot scripts) that do
    not need cross-call retry safety and want one call that still returns a
    renderable view.

    The rebuilt view is replayed from `prepared`'s OWN `state_after`
    payload -- never via a second `get_attempt(...)` call -- because
    `submit_prepared_ba201_decision(...)` has already confirmed the
    persisted response's `serialized_engine_state` is value-equal to it
    (see `_persisted_response_matches_prepared(...)`); a second RPC round
    trip would add nothing but latency and an extra failure point.
    """
    prepared = prepare_ba201_decision(
        user_email,
        attempt_id=attempt_id,
        selected_option_id=selected_option_id,
        idempotency_key=idempotency_key,
        client=client,
        certification_exam_name=certification_exam_name,
        simulation_id=simulation_id,
    )
    outcome = submit_prepared_ba201_decision(user_email, prepared, client=client)

    content = _load_default_scenario_content(
        certification_exam_name=prepared.certification_exam_name,
        simulation_id=prepared.simulation_id,
    )
    try:
        persisted_run = replay_serialized_run(content, prepared.reconstruct_state_after())
    except ScenarioEngineError as exc:
        logger.exception("BA-201 persisted post-decision engine state failed replay validation")
        raise ScenarioLearnerStateError(
            "The saved progress for this scenario could not be restored."
        ) from exc

    return _build_attempt_view(
        run=persisted_run,
        attempt_id=outcome.attempt_id,
        is_new_attempt=False,
        lifecycle_status=outcome.attempt_status,
    )


def _resolve_pinned_scenario_version(
    client: Any,
    *,
    scenario_version_id: str,
    expected_scenario_id: str,
    expected_simulation_id: str,
    expected_engine_version: str,
    expected_content_sha256: str,
) -> str:
    """SIM-VSLICE-03A: resolve the exact `version` STRING for one already-
    pinned `scenario_versions.id`, with the expected identity coming from
    the COMPLETED ATTEMPT ITSELF -- deliberately the mirror image of
    `_resolve_current_scenario_version_id(...)` above.

    Completion results belong to the attempt's OWN pinned
    `scenario_version_id`, never to whichever version happens to be
    `scenarios.current_published_version_id` at VIEW time -- a learner must
    remain able to view a historical completed result after a future
    version becomes current, the scenario is deactivated, or the current
    pointer moves on. This function therefore never touches
    `current_published_version_id`, `is_active`, or `lifecycle_status` at
    all.

    V68's own composite foreign key
    (`scenario_attempts (scenario_id, scenario_version_id) REFERENCES
    scenario_versions (scenario_id, id)`) and immutability triggers already
    guarantee, at the database level, that a real `scenario_attempts` row's
    `scenario_id`/`scenario_version_id`/`engine_version`/
    `scenario_content_sha256` are mutually consistent with the
    `scenario_versions` row they were pinned to at creation time, and that
    a published `scenario_versions` row's own `engine_version`/
    `canonical_content_sha256` can never change afterward (see
    `supabase/migrations/20260718170000_v66_scenario_definition_
    persistence_foundation.sql` and `supabase/migrations/20260719130000_
    v68_scenario_attempt_persistence_foundation.sql`). This function
    independently RE-VERIFIES that same identity chain against whatever
    the backend actually returns right now -- a completion-results
    codepath must fail closed on an inconsistent response rather than rely
    solely on database constraints it cannot see or introspect at this
    layer.

    Query 1 -- `scenario_versions` filtered by BOTH `id = scenario_version_id`
    AND `scenario_id = expected_scenario_id` together (never `id` alone):
    a row belonging to a DIFFERENT scenario than the attempt's own
    `scenario_id` must never be treated as a match, even if its `id`
    happens to equal `scenario_version_id` (which, given V68's composite FK,
    should never actually diverge for a genuine attempt -- but this
    function never assumes that invariant holds in the response it was
    given).

    Query 2 -- `scenarios` filtered by `id = expected_scenario_id`, then
    its `simulation_id` is compared against `expected_simulation_id`.

    Requires ALL of:
    1. the `scenario_versions` row (matching both filters) exists;
    2. its `id` equals `scenario_version_id` and its `scenario_id` equals
       `expected_scenario_id` (defense in depth on top of the query
       filters themselves);
    3. the owning `scenarios` row (`id = expected_scenario_id`) exists;
    4. that scenario's `simulation_id` equals `expected_simulation_id`;
    5. the version row's `engine_version` exactly equals
       `expected_engine_version`;
    6. the version row's `canonical_content_sha256` exactly equals
       `expected_content_sha256`;
    7. the version row's `version` string is non-empty.

    Any violation raises `ScenarioLearnerVersionUnavailableError`. Never
    falls back to a different scenario/version row on any mismatch --
    the function returns exactly once, on the single fully-verified row,
    or not at all.

    Raises `ScenarioLearnerBackendError` for any unexpected client/network
    failure.
    """
    try:
        version_rows = (
            client.table("scenario_versions")
            .select("id,scenario_id,version,engine_version,canonical_content_sha256")
            .eq("id", scenario_version_id)
            .eq("scenario_id", expected_scenario_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001 - backend/network failure, not a validation failure
        raise ScenarioLearnerBackendError(
            f"Unable to resolve pinned scenario_versions row {scenario_version_id!r}"
        ) from exc

    if not version_rows or not version_rows[0].get("id"):
        # Covers both "no such row" and "a row with this id exists but
        # belongs to a different scenario_id" -- the query's own combined
        # filter makes the two indistinguishable, exactly like
        # `get_attempt(...)`'s own "never distinguish not-found from
        # not-owned" contract.
        raise ScenarioLearnerVersionUnavailableError(
            f"The pinned scenario version {scenario_version_id!r} could not be resolved for scenario "
            f"{expected_scenario_id!r}"
        )
    version_row = version_rows[0]

    # Defense in depth on top of the query filters themselves -- never
    # trust a response merely because the request that produced it looked
    # right.
    if version_row.get("id") != scenario_version_id or version_row.get("scenario_id") != expected_scenario_id:
        raise ScenarioLearnerVersionUnavailableError(
            f"The pinned scenario version {scenario_version_id!r} did not resolve to itself for scenario "
            f"{expected_scenario_id!r}"
        )

    version_string = str(version_row.get("version") or "").strip()
    if not version_string:
        raise ScenarioLearnerVersionUnavailableError(
            f"The pinned scenario version {scenario_version_id!r} is missing required fields"
        )

    try:
        scenario_rows = (
            client.table("scenarios")
            .select("id,simulation_id")
            .eq("id", expected_scenario_id)
            .limit(1)
            .execute()
        ).data or []
    except Exception as exc:  # noqa: BLE001
        raise ScenarioLearnerBackendError(
            f"Unable to resolve owning scenarios row for pinned version {scenario_version_id!r}"
        ) from exc

    if not scenario_rows or not scenario_rows[0].get("id"):
        raise ScenarioLearnerVersionUnavailableError(
            f"The pinned scenario version {scenario_version_id!r} has no owning scenario row"
        )
    if scenario_rows[0].get("simulation_id") != expected_simulation_id:
        # The pinned version row exists and belongs to the expected
        # scenario_id, but that scenario's OWN simulation_id does not match
        # the one this controller only ever serves results for -- never
        # trusted, and never satisfied merely because SOME other scenario
        # row happens to carry the expected simulation_id.
        raise ScenarioLearnerVersionUnavailableError(
            f"The pinned scenario version {scenario_version_id!r} does not belong to simulation_id "
            f"{expected_simulation_id!r}"
        )

    if version_row.get("engine_version") != expected_engine_version:
        raise ScenarioLearnerVersionUnavailableError(
            f"The pinned scenario version {scenario_version_id!r} engine_version does not match "
            "the completed attempt's own engine_version"
        )
    if version_row.get("canonical_content_sha256") != expected_content_sha256:
        raise ScenarioLearnerVersionUnavailableError(
            f"The pinned scenario version {scenario_version_id!r} canonical_content_sha256 does not match "
            "the completed attempt's own scenario_content_sha256"
        )

    return version_string


def load_ba201_completion_result(
    user_email: Optional[str],
    *,
    attempt_id: str,
    client: Any = None,
    certification_exam_name: str = BA201_CERTIFICATION_EXAM_NAME,
    simulation_id: str = BA201_SIMULATION_ID,
) -> ScenarioCompletionResultView:
    """SIM-VSLICE-03: load the persisted result of exactly one COMPLETED
    BA-201 attempt, and return a small, immutable, learner-safe
    `ScenarioCompletionResultView` -- never recomputing or overwriting the
    persisted result, only VALIDATING it.

    The completed attempt persisted by V68 (`utils.scenario_persistence`)
    is the sole authority. This function never calculates a second result
    from browser/session assumptions, and never falls back to whichever
    scenario version happens to be current -- it resolves content ONLY via
    the attempt's own pinned `scenario_version_id`
    (`_resolve_pinned_scenario_version(...)`), so a historical completed
    result remains viewable even after a future scenario version becomes
    the current published one.

    Sequencing, in order:

    1. Verify `user_email` (`ScenarioLearnerAccessError`).
    2. Fetch the attempt via `utils.scenario_persistence.get_attempt(...)`
       -- this is ALSO the ownership check: an attempt that does not exist,
       or exists but is owned by a different learner, both raise
       `ScenarioLearnerAttemptNotFoundError` (matching `get_attempt`'s own
       "never distinguish the two" contract).
    3. Require `attempt.status == "completed"` exactly -- an `in_progress`
       or `abandoned` attempt raises `ScenarioLearnerAttemptNotCompletedError`.
    4. Require `attempt.current_scene_id is None`, `attempt.terminal_ending_id`
       to be a non-empty string, and `attempt.terminal_result_snapshot` to
       be present -- any violation raises `ScenarioLearnerStateError`
       (the persisted attempt claims to be completed but is not internally
       self-consistent).
    5. Resolve the attempt's PINNED scenario version string
       (`_resolve_pinned_scenario_version(...)`), then load and validate
       the EXACT matching scenario content version -- including a
       cross-check against the attempt's own persisted
       `scenario_content_sha256` -- via the existing
       `utils.scenario_catalog.load_resolved_scenario_content(...)`.
       Any resolution/load/validation failure raises
       `ScenarioLearnerVersionUnavailableError` (the pinned database
       version is not available/trustworthy locally right now) -- this is
       deliberately a DIFFERENT mapping than `_load_default_scenario_content(...)`'s
       `ScenarioLearnerContentError`, because a missing/mismatched EXACT
       historical version is a version-availability problem, not a
       "current scenario is broken" problem.
    6. Confirm `attempt.terminal_ending_id` actually exists in the loaded
       content's `endings` (`ScenarioLearnerStateError` if not).
    7. Replay the persisted engine state via
       `utils.scenario_engine.replay_serialized_run(...)` -- this
       INDEPENDENTLY recomputes the run (and, since it reaches
       `EVALUATE_ENDING`, an independent terminal result) purely from the
       persisted `decisionHistory` and the freshly-loaded content, never
       trusting any other field of the serialized payload (see that
       function's own docstring). Replay-identity mismatches and any other
       engine failure raise `ScenarioLearnerStateError`.
    8. Cross-validate the REPLAYED terminal result against the PERSISTED
       one: `run.is_complete`, `run.current_scene_id is None`,
       `run.terminal_result.ending_id == attempt.terminal_ending_id`, and
       `utils.scenario_engine.serialize_terminal_result(run.terminal_result)
       == dict(attempt.terminal_result_snapshot)` (byte-for-byte value
       equality). ANY mismatch raises `ScenarioLearnerStateError` -- this
       function NEVER silently substitutes the replayed result for the
       persisted one, and never displays a persisted result it could not
       independently reproduce from the exact same decision history.
    9. Build and return `ScenarioCompletionResultView` from the
       (now-validated) `run.terminal_result` and the loaded content's
       `domains` (for label resolution only) -- the persisted values
       themselves, never a second, independently-recomputed set of
       display numbers.

    Never exposes: the attempt id, scenario-version id, idempotency keys,
    sequence numbers, raw engine `state`/`flags`, the raw
    `terminal_result_snapshot` payload, canonical content hashes, or any
    other backend/database field -- see `ScenarioCompletionResultView`'s
    own docstring for the complete, deliberately small field list.
    """
    if not user_email or "@" not in str(user_email):
        raise ScenarioLearnerAccessError(
            "A verified learner email is required to view a scenario result."
        )
    normalized_email = normalize_scenario_persistence_email(user_email)

    resolved_client = client if client is not None else _default_client()

    try:
        attempt = get_attempt(
            resolved_client,
            user_email=normalized_email,
            attempt_id=attempt_id,
        )
    except ScenarioAttemptNotFoundError as exc:
        raise ScenarioLearnerAttemptNotFoundError(
            "This scenario attempt could not be found."
        ) from exc
    except ScenarioPersistenceError as exc:
        logger.exception("BA-201 completion-result attempt lookup failed")
        raise ScenarioLearnerBackendError(
            "This scenario result could not be loaded right now."
        ) from exc

    if attempt.status != "completed":
        raise ScenarioLearnerAttemptNotCompletedError(
            "This scenario attempt has not been completed yet."
        )
    if attempt.current_scene_id is not None:
        raise ScenarioLearnerStateError(
            "This scenario attempt's persisted result could not be verified."
        )
    if not attempt.terminal_ending_id or not str(attempt.terminal_ending_id).strip():
        raise ScenarioLearnerStateError(
            "This scenario attempt's persisted result is missing its outcome."
        )
    if attempt.terminal_result_snapshot is None:
        raise ScenarioLearnerStateError(
            "This scenario attempt's persisted result is missing its outcome."
        )

    # SIM-VSLICE-03A: fail closed, before any I/O, if the attempt's own
    # pinned `engine_version` is not the one engine version this codebase
    # actually knows how to replay. `ENGINE_VERSION` is the one
    # authoritative value `utils.scenario_engine` exposes for this
    # comparison -- `replay_serialized_run(...)` below would already reject
    # a serialized payload whose OWN embedded `engineVersion` field
    # mismatches `ENGINE_VERSION`, but checking the attempt's top-level
    # `engine_version` column here as well closes the gap for a response
    # where that column and the embedded payload have somehow diverged, and
    # produces the correct version-unavailable mapping (rather than an
    # engine/state error) either way. This is a strict equality check
    # against the current engine's own version constant -- it does not
    # invent any cross-version compatibility policy.
    if attempt.engine_version != ENGINE_VERSION:
        raise ScenarioLearnerVersionUnavailableError(
            "This scenario result was recorded under an engine version that is no longer available for replay."
        )

    version_string = _resolve_pinned_scenario_version(
        resolved_client,
        scenario_version_id=attempt.scenario_version_id,
        expected_scenario_id=attempt.scenario_id,
        expected_simulation_id=simulation_id,
        expected_engine_version=attempt.engine_version,
        expected_content_sha256=attempt.scenario_content_sha256,
    )

    try:
        content = load_resolved_scenario_content(
            certification_exam_name=certification_exam_name,
            simulation_id=simulation_id,
            version=version_string,
            expected_canonical_content_sha256=attempt.scenario_content_sha256,
        )
    except ScenarioContentError as exc:
        logger.exception(
            "BA-201 completion result could not load pinned scenario version %r", version_string
        )
        raise ScenarioLearnerVersionUnavailableError(
            "This scenario result's version is not available right now."
        ) from exc

    endings_by_id = {ending.id: ending for ending in content.endings}
    if attempt.terminal_ending_id not in endings_by_id:
        raise ScenarioLearnerStateError(
            "This scenario attempt's persisted outcome could not be verified."
        )

    try:
        run = replay_serialized_run(content, attempt.serialized_engine_state)
    except ScenarioEngineError as exc:
        logger.exception("BA-201 completion result failed engine replay validation")
        raise ScenarioLearnerStateError(
            "This scenario attempt's persisted result could not be restored."
        ) from exc

    if not run.is_complete or run.terminal_result is None or run.current_scene_id is not None:
        raise ScenarioLearnerStateError(
            "This scenario attempt's persisted result could not be verified."
        )
    if run.terminal_result.ending_id != attempt.terminal_ending_id:
        raise ScenarioLearnerStateError(
            "This scenario attempt's persisted outcome could not be verified."
        )
    if serialize_terminal_result(run.terminal_result) != dict(attempt.terminal_result_snapshot):
        # SIM-VSLICE-03: a genuine mismatch between the persisted result and
        # what this exact decision history independently replays to --
        # never silently trusted, and never silently substituted.
        logger.error(
            "BA-201 completion result mismatch between persisted terminal_result_snapshot and "
            "independently-replayed terminal result for attempt_id=%r",
            attempt.attempt_id,
        )
        raise ScenarioLearnerStateError(
            "This scenario attempt's persisted result could not be verified."
        )

    terminal_result = run.terminal_result
    domain_labels = {domain.id: domain.label for domain in content.domains}

    domain_breakdown = tuple(
        ScenarioDomainResultView(
            domain_label=domain_labels.get(snapshot.domain_id, snapshot.domain_id),
            correct_count=snapshot.correct_count,
            total_count=snapshot.total_count,
            accuracy_percentage=(
                round(snapshot.accuracy * 100.0, 1) if snapshot.total_count > 0 else None
            ),
        )
        for snapshot in terminal_result.domain_performance
    )

    if domain_breakdown:
        decisions_total = sum(entry.total_count for entry in domain_breakdown)
        decisions_correct = sum(entry.correct_count for entry in domain_breakdown)
        accuracy_percentage = (
            round(decisions_correct / decisions_total * 100.0, 1) if decisions_total > 0 else None
        )
    else:
        decisions_total = None
        decisions_correct = None
        accuracy_percentage = None

    recommended_review_domains = tuple(
        domain_labels.get(domain_id, domain_id) for domain_id in terminal_result.recommended_review
    )

    return ScenarioCompletionResultView(
        scenario_title=content.title,
        certification_exam_name=content.certification_exam_name,
        completion_heading="Scenario complete",
        ending_title=terminal_result.score_band,
        ending_narrative=terminal_result.narrative,
        decisions_correct=decisions_correct,
        decisions_total=decisions_total,
        accuracy_percentage=accuracy_percentage,
        domain_breakdown=domain_breakdown,
        recommended_review_domains=recommended_review_domains,
    )
