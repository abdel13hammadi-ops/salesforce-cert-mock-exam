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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from utils.scenario_catalog import resolve_default_scenario_version_path
from utils.scenario_engine import (
    ENGINE_VERSION,
    ScenarioEngineError,
    ScenarioRunSnapshot,
    get_current_scene,
    replay_serialized_run,
    serialize_run_snapshot,
    start_scenario_run,
)
from utils.scenario_persistence import (
    ScenarioPersistenceError,
    ScenarioVersionMismatchError,
    normalize_scenario_persistence_email,
    start_or_resume_attempt,
)
from utils.scenario_schema import ScenarioContentError, load_scenario_content

logger = logging.getLogger(__name__)

# Temporary BA-201 catalog identity (see module docstring). Not final/permanent
# creative content -- these are the existing catalog lookup keys already
# present in scenario_content/business_analyst/catalog.json.
BA201_CERTIFICATION_EXAM_NAME = "Salesforce Certified Business Analyst"
BA201_SIMULATION_ID = "ba201-sim-meridian-health-01"


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
    response, unexpected exception, etc.)."""


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
# Internal helpers
# ---------------------------------------------------------------------------


def _default_client() -> Any:
    from utils.access_control import get_supabase_admin_client  # noqa: PLC0415

    return get_supabase_admin_client()


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


# ---------------------------------------------------------------------------
# Public controller entry point
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

    is_complete = bool(run.is_complete) or result.status != "in_progress"
    current_scene_view = None if is_complete else _build_scene_view(run)
    progress_label = "Scenario complete" if is_complete else f"Decision {len(run.decisions) + 1}"

    return ScenarioAttemptView(
        attempt_id=result.attempt_id,
        is_new_attempt=result.created,
        is_complete=is_complete,
        scenario_title=content.title,
        certification_exam_name=content.certification_exam_name,
        progress_label=progress_label,
        current_scene=current_scene_view,
    )
