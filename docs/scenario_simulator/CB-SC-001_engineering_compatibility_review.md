# CB-SC-001 Engineering Compatibility Review

**Task ID:** SIM-ENG-COMPAT-01
**Type:** Review-only engineering compatibility assessment. No code, schema, migration, or runtime changes were made to produce this document.
**Repository:** `C:\Users\Abdel\Projects\salesforce-cert-mock-exam-latest`
**Creative Studio workspace:** `C:\Users\Abdel\Projects\CertBound Creative Studio`
**Author:** Engineering review agent (Sonnet, SIM-ENG-COMPAT-01)
**Date:** 2026-07-30

---

## 1. Executive verdict

**The approved CB-SC-001 content package is narratively and structurally sound, but it is not directly executable by the existing Scenario Simulator runtime as authored.** The existing runtime (`utils/scenario_schema.py`, `utils/scenario_catalog.py`, `utils/scenario_engine.py`, `utils/scenario_persistence.py`, `utils/scenario_learner_controller.py`, `pages/Scenario_Simulator.py`) is a deliberately generic, deterministic, replay-safe engine that already supports linear/branching scenes, convergence, numeric state with clamping, accumulating flags, stable option IDs, idempotent persistence, and full decision-history replay. It does **not** support four things CB-SC-001 requires as designed:

1. **Budget-conditioned dynamic routing** — the same option must route to a corrective scene only while a runtime corrective-budget counter is still available, and to the reconvergence scene directly once exhausted. The current engine's `nextScene` is a single fixed string per option; it cannot branch on accumulated state.
2. **Multi-tier option scoring** (`optimal`/`acceptable`/`suboptimal`/`high-risk`, 4/3/1/0 points) — the current schema has only a binary `isCorrect` boolean.
3. **A computed, multi-stage outcome classifier** (severe caps → moderate caps → weighted composite → strong guards → banding) that references **flags** and **derived aggregates** (a division-by-variable-decision-count composite score) — the current `endings[]` mechanism only supports an AND of `Min`/`Max`/`Equals` conditions against raw, purely-additive final **state variables**, evaluated in strict array order.
4. **Flag-dependent dialogue variants and multi-character, multi-exchange dialogue** — the current schema carries a single opaque `narrative` string and a single-speaker `characterContext`; it has no structured exchange list and no conditional-text mechanism.

None of these four gaps requires a database migration, a new persistence table, or any change to authentication/session/billing code. All four are **content-schema and deterministic-engine extensions**, additive to the existing `utils/scenario_engine.py`/`utils/scenario_schema.py` contract, versioned through the **schema-versioning mechanism the runtime already has built in** (`schemaVersion` is read per-document; a new `1.1.0` schema can be introduced without touching the already-published BA-201 `1.0.0` content or its tests).

**Recommended architecture:** a deterministic, offline, human-reviewed **build-time compiler** (Option C) that transforms the approved Creative Studio markdown into an **extended canonical JSON schema** (Option D, additive `1.1.0`), which is then validated, hashed, and published through the **existing, unmodified** `scenario_versions` catalog/publish pipeline. Neither raw markdown consumption at runtime (Option A) nor open-ended manual hand-authoring at scale (Option B) is recommended for production; Option B (or a thin, disposable spike compiler) is acceptable **only** for the smallest vertical slice described in §20.

**Engineering readiness verdict:** **Not ready for full-package implementation.** Ready for a narrowly-scoped, additive vertical slice once the extended schema (§16) is drafted and reviewed. See §25/§26 for the precise gating sequence.

---

## 2. Current runtime summary

The existing BA-201 Scenario Simulator runtime is layered as follows (all read-only inspected, none modified):

| Layer | File | Responsibility |
|---|---|---|
| Content schema + validation | `utils/scenario_schema.py` | JSON Schema (Draft 2020-12) validation, cross-reference validation (state-variable keys, choice IDs, transitions, domains, ending conditions), cycle detection, reachability, canonical SHA-256 hashing |
| Content schema document | `scenario_content/schemas/1.0.0/simulation.schema.json` | The literal JSON Schema for schema version `1.0.0` |
| Catalog / version resolution | `utils/scenario_catalog.py` | Discovers `scenario_content/<certification_slug>/catalog.json`, resolves an immutable `(certificationExamName, simulationId, version)` triple to a content file, verifies identity + optional content-hash pin |
| Deterministic engine | `utils/scenario_engine.py` | `start_scenario_run`, `apply_decision`, `evaluate_ending`, `replay_scenario_run`/`resume_scenario_run`, `serialize_run_snapshot`/`replay_serialized_run` — pure functions over `ScenarioContent` |
| Persistence adapter | `utils/scenario_persistence.py` | Python wrapper around 4 Postgres RPCs (`start_or_resume_scenario_attempt_v1`, `get_scenario_attempt_v1`, `submit_scenario_decision_v1`, `abandon_scenario_attempt_v1`); validates shapes, never recomputes scoring |
| Database | `supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql` (+ v66/v67) | `scenario_attempts` / `scenario_decisions` tables, immutability triggers, `service_role`-only RLS, no `auth.uid()` |
| Application controller | `utils/scenario_learner_controller.py` | Orchestrates load → resume → prepare-decision → submit-decision → completion-result, producing learner-safe view dataclasses |
| UI | `pages/Scenario_Simulator.py` | Streamlit page: intro/start gate, one scene + one decision per page (`st.radio` over `scene.options` in **authored array order**), retry-safe submission, completion results |
| Tests | `tests/test_scenario_schema.py`, `test_scenario_catalog.py`, `test_scenario_engine.py`, `test_scenario_persistence.py`, `test_scenario_learner_controller.py`, `test_scenario_simulator_page_access.py`, `test_scenario_decision_submission_page.py`, `test_scenario_completion_result_page.py` | Documented as 109 passed for the schema/catalog/engine trio as of `scenario_content/docs/SCENARIO_PERSISTENCE_ARCHITECTURE.md`; controller/page/persistence suites added since |

Key architectural facts, confirmed by direct code inspection:

- **Content publishing model is already hybrid and already versioned.** `scenario_content/docs/SCENARIO_PERSISTENCE_ARCHITECTURE.md` documents (and the schema code confirms) that repository JSON is the authoring source of truth, and at publish time an immutable snapshot is written to `scenario_versions.content_snapshot`; runtime never does a live filesystem read of production content. `schemaVersion` is read **per document** (`build_scenario_content(...)`, `load_schema(schema_version)`), so a new schema version is additive by construction — the existing BA-201 `1.0.0` content and its 109 passing tests are structurally isolated from any `1.1.0` change.
- **Persistence is 100% content-shape-agnostic.** `scenario_attempts`/`scenario_decisions` store opaque `jsonb` (`serialized_engine_state`, `state_before`, `state_after`, `terminal_result_snapshot`) plus a handful of identity/ordering scalars. Nothing in the SQL or the Python adapter inspects scene/option/flag semantics — this is why §11 concludes no persistence schema change is required for CB-SC-001, regardless of which integration architecture is chosen.
- **Replay trusts only `(sequenceNumber, sceneId, optionId)` triples**, per decision. Every other field (`state`, `flags`, `isCorrect`, `domainPerformance`, `currentSceneId`, `isComplete`, the ending) is always recomputed from content, never from a serialized value (`utils/scenario_engine.py:357-469`, `:602-625`). This is the single most important existing guarantee for CB-SC-001's replay requirement, and it must be preserved by any extension.
- **Flags only ever accumulate.** `ScenarioOption.set_flags` is appended (`run.flags + tuple(selected.set_flags)`); there is no clear/unset mechanism anywhere in `utils/scenario_engine.py`.
- **Routing is static per option.** `option.nextScene` is a single fixed string (or the literal sentinel `EVALUATE_ENDING`); nothing in `apply_decision` consults accumulated `state`/`flags` to choose between two possible next scenes for the same option.
- **Ending evaluation is a flat, ordered, AND-only comparison against final numeric `state`** (`Min`/`Max`/`Equals` suffixes only); it never reads `flags`, and it never computes a derived/weighted value — `ending.condition` keys must already exist as declared `stateVariables`.
- **The current completion view (`ScenarioCompletionResultView`) is summary-only**: `scenario_title, certification_exam_name, completion_heading, ending_title, ending_narrative, decisions_correct, decisions_total, accuracy_percentage, domain_breakdown` (per-domain accuracy only). There is no decision-by-decision view, no final per-dimension state dashboard, and no debrief-seed concept anywhere in the controller today.
- **`ScenarioSceneView`** (the learner-facing scene shown by the page) is deliberately minimal: `domain_label, narrative, decision_prompt, options` — no `scene_id`, no character/visual metadata field at all.

---

## 3. Creative Studio package summary

Source: `06-dialogue/simulator-v1/CB-SC-001/` (17 files), `02-scenarios/specifications/CB-SC-001_*-v1.md` (4 specs), `09-review/CB-SC-001_*` (3 review records). Full structured extraction was produced by a dedicated read-only sub-review and is summarized here; see that extraction for exact per-scene tables (option text, per-dimension deltas, flags, routing) — it is not reproduced in full in this document to keep it navigable, but every classification below is grounded in it.

- **Status:** All 17 dialogue files are `Approved for Engineering Compatibility Review` (product approval report, `CB-SC-001_simulator-dialogue-product-approval-report.md`, CS-SIM-DLG-05). The approval **explicitly does not** authorize implementation, does not select an integration architecture ("direct-consumption vs compiled dialogue architecture" is explicitly named as *not* decided), and does not authorize any DB/auth/billing/deploy work. This review treats that boundary as binding.
- **Structure:** 12 core scenes (`SC001-C01`…`SC001-C12`, strict order) + 5 available corrective scenes (`SC001-R2A`, `R4A`, `R6A`, `R8A`, `R10A`), each attached after one core scene's suboptimal/high-risk options and reconverging into the very next core scene. Corrective scenes never re-branch (single decision, single fixed reconvergence target). Max 3 corrective scenes actually experienced per run out of 5 available; max 15 scored decisions (12 core + up to 3 corrective), min 12.
- **Options:** exactly 3 per scene (`A`/`B`/`C`), IDs of the form `opt-sc001-{scene}-{a|b|c}`, already scoped uniquely per scene — directly compatible with the existing per-scene-unique option-ID rule.
- **Scoring model:** seven hidden numeric dimensions (`customerConfidence`, `operationalRisk`, `dataQuality`, `scheduleImpact`, `complianceExposure`, `requirementsClarity`, `stakeholderAlignment`), initial values 68/42/44/38/32/48/52, clamped to `[0,100]`. Each option carries a **4-tier evaluation label** (`optimal`=4, `acceptable`=3, `suboptimal`=1, `high-risk`=0 points) in addition to its 7-dimension deltas. A `compositeScoreUnrounded = positiveHealth*0.55 + decisionQuality*0.45` drives outcome banding, where `decisionQuality` divides by the **run's actual scored-decision count** (12–15, itself variable based on which correctives were experienced). A documented 7-step deterministic evaluation order (severe caps → moderate caps → compute composite → strong guards → band selection → tie-break → round-for-display) governs the four outcomes.
- **Flags:** 13 canonical flags (e.g. `flag-unsupported-customer-date`, `flag-verbal-handoff-only`, `flag-conflicting-customer-messages`), used both to alter later dialogue text ("flag-dependent dialogue variants") and as direct inputs to severe/moderate scoring caps. Almost all are "sticky" (set once, never cleared) — but the review extraction confirms **at least one explicit exception**: `flag-agreement-index-pending` is specified to be **cleared** by `SC001-C11` option A.
- **Dialogue:** each scene is a genuine multi-character, multi-exchange script (`Speaker`, `Audience`, `Communication Type`, `Expression`, `Body Language`, `Tone`, `Dialogue Text` per exchange), with named characters (`CB-CH-001`…`CB-CH-005`) plus one role-only figure (`CB-RL-019`, no dialogue). No image/portrait asset IDs exist yet anywhere in the approved package — visual richness is currently prose description only (`Setting`, `Expression`, `Body Language`).
- **Debrief:** a 12-section, end-only debrief (outcome banner, seven-dimension final-state dashboard, decision-by-decision review with a `Debrief seed` block per option — `optionId, strongestOptionId, whyStronger, whyWeaker, immediateConsequence, laterConsequence, competencyImpact, stateImpact, capGuardEffect` — corrective path summary, competency roll-up, critical risks missed, decision path replay). Debrief structure is documented as READY; prose is authored per-scene as `Debrief seed:` lines in the dialogue files themselves.
- **Review records' own stated position on architecture:** the focused revalidation record states *"Engineering may proceed to compatibility review using dialogue files as the authoritative option-title source until SIS is updated"* and flags a real risk (REV-D-008) that binding option titles from the wrong source (SIS vs dialogue) produces a mismatched learner-facing label — evidence that **the dialogue files, not the interaction spec prose, must be the literal text source of truth** for anything rendered verbatim to the learner.

---

## 4. Contract inventory (current runtime)

| Contract / file | Purpose | Currently supported fields | Assumptions | Extension risk | Relevant tests |
|---|---|---|---|---|---|
| `simulation.schema.json` (v1.0.0) | JSON Schema for one scenario version | `simulationId, version, schemaVersion, certificationExamName, examCode, title, description, estimatedMinutes, domains[], stateVariables[], initialState, scenes[], startScene, endings[]`; scene: `id, domainId, isDetour, narrative, characterContext{speakingAs,role}, decision, explanation`; option: `id, text, isCorrect, feedback, stateChanges, setFlags, nextScene` | One flat narrative string per scene; binary correctness; static routing; endings are AND-only Min/Max/Equals over raw state | **Low** to add new optional fields (backward compatible); **Medium-High** to add new *semantics* (conditional routing, variant text, flag-aware conditions) since those change engine behavior, not just schema shape | `tests/test_scenario_schema.py` |
| `utils/scenario_catalog.py` | Certification-scoped scenario/version discovery | `catalogVersion, certificationSlug, certificationExamName, scenarios[].{simulationId,title,examCode,versions[]}`, version entry `{version, schemaVersion, relativePath, canonicalContentSha256, estimatedMinutes, isDefault}` | One catalog per certification slug directory; scenario identity = `(certificationExamName, simulationId)` | **Low** — a new scenario is just a new catalog entry; no code change needed | `tests/test_scenario_catalog.py` |
| `utils/scenario_engine.py` | Deterministic execution/replay | `apply_decision`, `evaluate_ending` (Min/Max/Equals only, no flags), `replay_scenario_run`/`resume_scenario_run`, `serialize_run_snapshot`/`replay_serialized_run`, `_freeze_state` deep-immutability | Routing and ending evaluation are pure functions of the *current option chosen* and *final raw state*, never of accumulated counters/flags together in a derived formula | **Medium** for conditional routing (new but bounded, deterministic, replay-safe if only fed already-recomputed state/flags); **Medium-High** for a computed multi-stage outcome classifier (new derived-metrics step) | `tests/test_scenario_engine.py` |
| `utils/scenario_persistence.py` + V68 RPCs | Durable attempt/decision storage | Fully generic over content shape; identity/ordering/idempotency/ownership only | Every `state_before`/`state_after` is a full `serialize_run_snapshot(...)` output; RPC never inspects scene/option semantics | **None** — no change needed for CB-SC-001 | `tests/test_scenario_persistence.py`, SQL migration itself |
| `utils/scenario_learner_controller.py` | Learner-facing orchestration + view construction | `ScenarioAttemptView`, `ScenarioSceneView` (no `scene_id`, no visuals), `ScenarioCompletionResultView` (summary + per-domain accuracy only, no decision-by-decision, no 7-dim dashboard) | View classes were deliberately built minimal for BA-201's binary-correctness model | **Medium** — additive new view dataclasses (decision review, dimension dashboard, corrective summary) are new code but do not change existing BA-201 behavior | `tests/test_scenario_learner_controller.py` |
| `pages/Scenario_Simulator.py` | UI: intro gate, one scene/one decision per page, retry-safe submit, completion render | `st.radio` over `scene.options` in **authored array order**; no character cards, no images, no progress-dimension display (by design — "no visible scoring during the run" is already satisfied) | Single scene, single decision-prompt, single option list per page render | **Low-Medium** — mostly additive UI components (intro screens, character cards, richer debrief) | `tests/test_scenario_simulator_page_access.py`, `test_scenario_decision_submission_page.py`, `test_scenario_completion_result_page.py` |

---

## 5. Field-by-field compatibility matrix

Classifications: **SUPPORTED**, **PARTIALLY SUPPORTED**, **UNSUPPORTED**, **CONFLICTING** (existing model actively contradicts the requirement, not just missing a field), **NOT REQUIRED AT RUNTIME**.

| Field / concept | Classification | Notes |
|---|---|---|
| Scenario identity | SUPPORTED | `simulationId` is an arbitrary string; a new value (e.g. `cb-sc-001-onboarding-handoff-01`) is a normal new catalog entry. |
| Certification identity | SUPPORTED (mechanism) — value is an **open product decision** | `certificationExamName`/`examCode` exist; which certification CB-SC-001 belongs to is not stated in the inspected package (see §24). |
| Content version | PARTIALLY SUPPORTED | `version`/`schemaVersion`/`canonicalContentSha256` mechanism is fully supported and already designed for exactly this extension (per-document schema version); the **new schema version itself does not exist yet**. |
| Scene identity | SUPPORTED | Arbitrary scene IDs; `SC001-C01` etc. fit directly. |
| Scene type (core vs. corrective) | PARTIALLY SUPPORTED | `isDetour: boolean` exists but is explicitly documented as UI/analytics-only, carrying no routing or budget semantics — those must be new engine logic, not this flag. |
| Stage / phase | NOT REQUIRED AT RUNTIME | No distinct "stage" field exists or is needed; scene order is already fully captured by the graph (`nextScene` pointers), not by an independent stage label. |
| Characters (multi-speaker) | UNSUPPORTED for structure / PARTIALLY SUPPORTED as prose | `characterContext` is single-speaker, free-text only (`speakingAs`, `role`). CB-SC-001 needs a multi-exchange, multi-character script. Prose can be flattened into `narrative` today (lossy: no machine-readable per-line speaker/expression), or a new `exchanges[]` array can be added (schema extension). |
| Visual metadata | UNSUPPORTED | No image/expression/background asset fields exist in the schema, and the approved Creative Studio package itself has no image asset IDs yet either — this is a shared, currently-moot gap on both sides. |
| Dialogue exchanges (structured) | UNSUPPORTED | No structured multi-turn exchange list exists; only one flat `narrative` string per scene. |
| Dialogue variants (flag-conditioned text) | CONFLICTING | Current model: exactly one fixed narrative per scene, always. Required model: narrative/exchange text selected deterministically based on already-computed flags. Direct contradiction of current assumption, not just a missing field. |
| Option ID | SUPPORTED | Per-scene-unique letter IDs match exactly. |
| Learner response text | SUPPORTED | `option.text`. |
| State deltas (7 dimensions) | SUPPORTED | `stateChanges` is an open numeric dict, clamped per declared `stateVariables` — the seven dimensions are ordinary new state-variable declarations. |
| Flags set | SUPPORTED | `setFlags`. |
| Flags cleared | CONFLICTING | Engine only ever appends flags (`run.flags + tuple(...)`); there is no clear/unset mechanism. CB-SC-001 has a confirmed, non-hypothetical requirement for this (`flag-agreement-index-pending` cleared by `SC001-C11-A`). |
| Corrective trigger (which option triggers it) | PARTIALLY SUPPORTED | Choosing a corrective vs. non-corrective path based on **which option was picked** is ordinary static branching (fully supported today). |
| Next core scene | SUPPORTED | `nextScene`. |
| Reconvergence | SUPPORTED | Schema explicitly documents "multiple options may point to the same nextScene (convergence)"; this is a first-class, tested capability. |
| Corrective budget (the counter) | SUPPORTED (as an authored state variable) | A `correctiveScenesExperienced` counter, clamped `[0,3]` and incremented via `stateChanges` on corrective-scene options, fits the existing state-variable model with zero schema change. |
| Corrective budget (conditioning routing on the counter) | CONFLICTING | This is the actual gap: the **same option** must route to a corrective scene while budget remains and to the reconvergence scene once exhausted. Static per-option `nextScene` cannot express this. |
| Sequence number | SUPPORTED | Fully general in engine and DB (`CHECK sequence_number >= 1`, no upper bound); works for any content shape including up to 15 decisions. |
| Terminal state | SUPPORTED | `EVALUATE_ENDING` sentinel, `is_complete`, `terminal_result`. |
| Outcome caps (severe/moderate) | CONFLICTING | Requires referencing **flags** (unsupported by `ending.condition` today) and **derived counters** (`highRiskDecisionCount`, feasible as a state variable) inside a strict priority order that is richer than "first array-order match wins" over independent AND-only conditions. |
| Outcome guards (Strong disqualifiers) | CONFLICTING | Same root cause as caps, plus a genuinely **computed** composite score (weighted average, division by a *variable* decision count) that cannot be produced by pure per-decision additive `stateChanges` alone. |
| Final outcome (container) | PARTIALLY SUPPORTED | `endings[]` (id/condition/narrative/scoreBand/recommendedReview) is a fine data container for exactly 4 outcomes; the **classification logic** feeding it is the unsupported part above. |
| Debrief seed | UNSUPPORTED | No such field exists in the option schema, and no view dataclass in `utils/scenario_learner_controller.py` exposes anything decision-by-decision today. |
| Artifact references (`CB-DOC-*`/`CB-COM-*`) | NOT REQUIRED AT RUNTIME | Purely descriptive/presentational; safe to omit from the runtime schema entirely, or add as an optional descriptive array later with zero engine impact. |
| Accessibility metadata | NOT REQUIRED AT RUNTIME (as schema data) | This is a Streamlit component-implementation concern (semantic structure, ARIA-equivalent affordances), not a per-scene JSON field. |
| Mobile metadata | NOT REQUIRED AT RUNTIME (as schema data) | Same reasoning as accessibility — a UI/CSS concern, not a content-schema concern. |

---

## 6. Confirmed compatibility

The following CB-SC-001 requirements are **already fully supported today, with zero schema, engine, or persistence change**:

- 3 options per scene (schema allows 2–6).
- Per-scene-unique, stable option IDs used as the submission identity (never positional index) — `submit_prepared_ba201_decision`/`submit_scenario_decision_v1` already key everything on `selected_option_id`, never on display order.
- Branching and reconvergence (multiple options pointing at the same `nextScene`) — explicitly designed for and tested.
- Numeric state with per-variable clamping, applied after every single decision — a direct fit for the seven hidden dimensions.
- Flags as an accumulating list, used later for conditional logic — a direct fit for the ~12 of 13 canonical flags that are "sticky" and never need clearing.
- 12–15 decisions per run, arbitrary path length — the engine has no hardcoded scene count, min/max path length is computed generically from the graph, and the persistence layer has no upper bound on `sequence_number`.
- Deterministic replay from a decision-only history — `(sequenceNumber, sceneId, optionId)` triples are already the *only* trusted replay input; state/flags/scoring/ending are always recomputed, never trusted from storage. This is exactly the guarantee CB-SC-001's "deterministic replay" requirement needs.
- No visible scoring during the run — the current `ScenarioSceneView`/page rendering already never exposes state, flags, or scoring to the learner mid-run; this is a pre-existing, tested property.
- No unrestricted AI chat anywhere in the load/execute/persist/replay path — the entire pipeline is deterministic Python and SQL; there is no LLM call in the runtime critical path today, and nothing in this proposal introduces one.
- Content versioning and hybrid-authoritative publishing (repository JSON → immutable `scenario_versions` snapshot) — already the production pattern for BA-201, directly reusable for CB-SC-001 with a new catalog entry.
- Idempotent, retry-safe decision submission and ownership enforcement (`user_email`-scoped, row-locked, sequence/scene/state-conflict detection) — fully generic, needs no change.

---

## 7. Confirmed gaps

Grouped by category (a single requirement can span more than one category):

- **Content-format mismatch:** flat single-narrative scenes vs. CB-SC-001's structured multi-character, multi-exchange dialogue; no visual/image metadata on either side yet.
- **Schema mismatch:** binary `isCorrect` vs. 4-tier `optimal/acceptable/suboptimal/high-risk` scoring; no `clearFlags`; `ending.condition` cannot reference flags; no dialogue-variant construct; no debrief-seed fields.
- **Runtime-state mismatch:** flags are append-only (no clear); routing cannot condition on accumulated state (corrective-budget override); ending evaluation cannot use a computed/derived value (weighted composite requiring a variable decision-count divisor).
- **Persistence mismatch:** **none identified.** The existing `scenario_attempts`/`scenario_decisions` tables and RPCs are content-shape-agnostic `jsonb` containers and require no change (see §11).
- **UI mismatch:** no intro/briefing/character-card/expression UI; no seven-dimension debrief dashboard; no decision-by-decision review UI; no randomized-option-order rendering (`st.radio` currently iterates `scene.options` in fixed authored order).
- **Scoring mismatch:** the entire outcome-classification model (caps/guards/weighted composite) is new; current `endings[]` only supports flat AND-of-thresholds over raw state.
- **Debrief mismatch:** current `ScenarioCompletionResultView` is a 9-field summary; CB-SC-001 needs a 12-section, per-decision, per-dimension debrief.
- **Catalog/registry mismatch:** none identified — a new scenario is a normal new catalog entry under an existing or new certification slug; the only open item is *which* certification it belongs to (§24), not a technical incompatibility.
- **Test-coverage mismatch:** no existing test exercises conditional routing, flag clearing, tiered scoring, computed outcome classification, or a multi-section debrief — an entirely new test surface is required (§19), additive to the existing 109+-test schema/catalog/engine suite.

---

## 8. Data-integrity risks

Assessed against the existing, unmodified persistence/replay guarantees, and against what CB-SC-001 would add:

| Risk | Current mitigation | CB-SC-001-specific consideration |
|---|---|---|
| Stable IDs / duplicate IDs | `_validate_choice_ids` (per-scene option uniqueness), `_scene_by_id` (global scene-id uniqueness) already enforced at load time | CB-SC-001's `opt-sc001-{scene}-{a,b,c}` pattern is naturally collision-free; no new risk |
| Content version pinning | Attempt row permanently pins `scenario_version_id` + `scenario_content_sha256` + `engine_version`; `replay_serialized_run` rejects any mismatch before replaying a single decision | Unchanged; a new schema version is just a new pinned identity tuple |
| Replay against changed content | Identity check happens *before* any decision is replayed (`_verify_serialized_identity`) | If a future engine extension changes derived-metric formulas (composite score, cap/guard order) without bumping `ENGINE_VERSION`, an old completed attempt's terminal result could theoretically be "re-explained" differently on a hypothetical re-derivation — mitigated today because the terminal result is computed and stored **once**, at completion, never recomputed for display; any future "recompute debrief text at read-time from stored raw state" design must also pin an explicit debrief/engine version, not just content version |
| Missing flags / invalid routes / unreachable scenes | `_validate_transitions`, `_detect_cycle`, "all scenes must be reachable" enforced at load time | A conditional-routing extension must still be statically analyzable for reachability — i.e., every possible resolved target (both the "budget available" and "budget exhausted" branches) must itself be a validated, reachable scene id, or graph-metadata/reachability analysis silently becomes incomplete |
| Branch cycles | `_detect_cycle` (DAG-only enforcement) already rejects any cycle | CB-SC-001's design (correctives always move forward to a *later* core scene, never backward) is naturally acyclic; no new risk, provided the conditional-routing extension does not accidentally introduce a path back to an earlier scene |
| Corrective-budget enforcement | N/A today | New requirement — must be enforced **deterministically from already-recomputed state**, never from a separately-persisted "did we skip this due to budget" boolean (that would violate the "never trust a persisted derived value" principle already core to `replay_serialized_run`) |
| State clamping / cap-guard order | `_clamp_value` applied after every single state-changing update | The 7-step cap/guard/band evaluation order is a **strict priority order that must never be reordered** — this needs the exact same "array order is semantically load-bearing, not a display convenience" discipline the current `endings[]` docstring already enforces for its own simpler model; must be encoded as code/tests, not left as prose |
| Terminal-state correctness | `scenario_attempts_completed_requires_terminal_result` / `..._in_progress_has_no_terminal_result` DB constraints; engine only marks terminal exactly once, at `apply_decision` | Unchanged and sufficient; no new terminal-state risk from CB-SC-001 |
| Partial-write risk | Single-RPC, single-transaction write per decision (`submit_scenario_decision_v1`), row-locked | Unchanged; sufficient |
| Duplicate submission | UUIDv4 idempotency key, `UNIQUE(attempt_id, idempotency_key)`, request-fingerprint cross-check | Unchanged; sufficient |
| Resume after interrupted write | `get_scenario_attempt_v1` + `replay_scenario_run` reconstruct purely from persisted decision history | Unchanged; sufficient, **provided** any new derived runtime fields (corrective-budget counter, tier counts) are represented as ordinary state variables (persisted, replay-safe) and never as ephemeral Python-only bookkeeping that would not survive a resume |
| Stale-client submission | `expected_sequence_number` / `expected_scene_id` / `state_before` conflict checks | Unchanged; sufficient |
| Mismatch between displayed content and stored decision | Submission is always keyed by `option_id`, never by display position | Already correctly satisfied — but this must be preserved when option-order randomization is introduced (§ "randomized option display" below); the randomization must happen strictly in the rendering layer, never leak into what is submitted or persisted |

---

## 9. Replay and versioning assessment

**Can the current engine reconstruct everything CB-SC-001 needs, using only stored decisions and versioned content?**

- **Branches, state, flags:** yes, in principle, once the engine gains flag-clear and conditional-routing support — both remain **pure functions of (content, ordered decision history)**, which is exactly what `replay_scenario_run` already guarantees for the existing model. No new persisted field is needed to make either replay-safe, because both derive entirely from already-stored `(sequenceNumber, sceneId, optionId)` triples plus the (extended) content document.
- **Corrective-budget behavior:** yes, if and only if the budget counter is modeled as an ordinary numeric state variable that is recomputed by replay like every other state variable — never as a separately-persisted "this decision was budget-gated" boolean. This preserves the existing "never trust a persisted derived value" invariant.
- **Conditional dialogue:** yes, if the variant-selection function is a pure, deterministic function of (content version, already-recomputed flags/state at the moment the scene is entered) — i.e., dialogue variant selection must be **computed at render/debrief time**, never stored as "which variant was shown."
- **Final outcome:** yes for the *classification decision itself* (it can be a pure function of final state + flags + counters), but **the composite-score *formula* itself must be versioned alongside the engine** (a new `ENGINE_VERSION`, since `_verify_serialized_identity` already keys replay rejection on engine version) — this is the one place where a future formula change is genuinely a breaking change to already-completed attempts' potential *re-derivation*, though not to their already-stored terminal result (which is computed once and never recomputed for display, per current design).
- **Debrief explanations (`Debrief seed` text):** these are **static content**, not derived from runtime state — they are selected at debrief-render time based on which option was chosen at which scene (already fully knowable from decision history) and require no new persistence.

**Flag for reliance on mutable/non-deterministic inputs:** none found that would make replay nondeterministic, provided the option-display-order randomization (next section) is implemented as pure rendering-layer behavior and never persisted as, or substituted for, decision identity.

**Randomized option display order (Q7):**

- Submission is already ID-based, never position-based (`selected_option_id`) — the contract is already correct for randomization.
- Current rendering (`st.radio` over `scene.options`) is in **fixed authored array order** — randomization does not exist today; this is a real, additive UI gap, not a regression risk.
- Display order does **not** need to be persisted for replay or debrief correctness — the debrief only needs to know "which option ID was chosen" and "what the strongest option ID was," never "what position was it shown at."
- Recommendation: a deterministic, seeded shuffle (e.g. seeded by `hash(attempt_id, scene_id)`) computed purely client/render-side, so that a mid-scene browser refresh (before submission) does not visually reshuffle options the learner was already looking at, without persisting anything new.
- Accessibility/analytics implication: screen-reader announcement order should follow the (randomized) visual order, not the authored order, and any future analytics on "which position wins more often" must bucket by option ID, not raw list index, to remain meaningful across randomized renders.

---

## 10. UI compatibility assessment

Exact gaps against the product contract's UI list (implementation is explicitly out of scope for this review):

| UI surface | Current state | Gap |
|---|---|---|
| Company/character introduction | Does not exist | New screen(s) needed |
| Character cards | Does not exist (`characterContext` is plain text, not rendered as a card anywhere found) | New component needed |
| Project/learner-role briefing | Does not exist as a distinct pre-scenario screen | New screen needed |
| Start Scenario gate | Exists conceptually (`_render_scenario_landing`-style flow referenced in `pages/Scenario_Simulator.py`'s docstring) but not styled/structured for a briefing+gate sequence | Needs restructuring, not ground-up build |
| Realistic scene visual / dialogue panel / expressions | Does not exist; no image asset pipeline on either side yet | New component, blocked on Creative Studio producing actual visual assets (currently prose-only) |
| Three response cards | Exists as `st.radio`, functionally equivalent but not visually a "card" pattern | Restyling, not a new capability |
| Consequence reaction / scene transition | Partially exists (`option.feedback` is already rendered as immediate feedback in the existing BA-201 flow, per the `explanation`/`feedback` fields) | Extend, don't rebuild |
| Progress indication | Exists (`progress_label`, `domain_label` already rendered) | Compatible, may need relabeling for CB-SC-001's stage model |
| Resume | Fully exists (`start_or_resume_ba201_attempt` pattern is directly reusable for any scenario) | No gap |
| Final outcome / full debrief | Exists only as a 9-field summary | Substantial new UI + new view dataclasses (§18) |
| Mobile behavior | No CB-SC-001-specific mobile gap found beyond general Streamlit responsive-layout work already implied by existing pages | Standard responsive work, not scenario-specific |

---

## 11. Persistence assessment

**Verdict: the current persistence model can safely support CB-SC-001 with zero migrations, zero new tables, and zero RPC changes.**

Evidence:

- `scenario_attempts.serialized_engine_state` and `scenario_decisions.state_before`/`state_after` are unconstrained `jsonb` objects (`CHECK jsonb_typeof(...) = 'object'` only) — they already store whatever `serialize_run_snapshot(...)` produces, and that function already emits `state` (open dict — the seven dimensions plus any counters are just more keys) and `flags` (open list — 13 canonical flags are just more strings) with no schema-level limit on key count or names.
- `scenario_decisions` is one row per decision, already storing `sequence_number`, `selected_option_id`, `resulting_scene_id`, `is_terminal`, `terminal_ending_id`, full `state_before`/`state_after` — this is already sufficient raw data to power a full decision-by-decision debrief (§18); the debrief gap is a controller/view-layer gap, not a persistence gap.
- `scenario_attempts`/`scenario_decisions` reference `scenario_id`/`scenario_version_id` generically — a brand-new scenario (new `simulationId`, new catalog entry, new `scenario_versions` row) requires no DDL change, only new *data*.
- Ownership, idempotency, sequence/scene/state-conflict enforcement are all generic and already sufficient for a 15-decision run (no hardcoded decision-count ceiling anywhere in the SQL or Python).
- Content version identity: fully supported today (`scenario_version_id` + `scenario_content_sha256` + `engine_version` pinned once at attempt creation, immutable thereafter via `guard_scenario_attempt_mutation_v1`).

What is **not** currently modeled, and does not need to be, because it derives cleanly from what already is persisted:
- Corrective-scene count, branch history, skipped-due-to-budget events, dialogue-variant history — all fully reconstructable from the existing `decisionHistory` + extended content, exactly like every other derived value in this engine's design philosophy. No new column or table should be added for any of these; doing so would violate the existing "derived values are never independently trusted" invariant and create a new class of drift risk between the stored derived value and the true replay result.

**Backwards-compatibility risk:** none identified for BA-201. A new `1.1.0` schema version is additive; `load_schema`/`_validate_and_compute_graph_metadata` already select validation rules per-document `schemaVersion`, so BA-201's `1.0.0` content, its catalog entry, and its 109+ passing tests are structurally unaffected by anything CB-SC-001 requires.

---

## 12. Architecture alternatives

| # | Architecture | Correctness | Data integrity | Maintainability | Authoring burden | Validation complexity | Debugging difficulty | Versioning | Future reuse | Op. complexity | Migration risk | CS↔runtime coupling | Silent semantic-loss risk |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **A. Direct consumption of Creative Studio markdown at runtime** | Poor — markdown is prose, not a typed contract; no schema validation possible before serving to learners | Low — no cross-reference/reachability/cycle validation exists for markdown | Poor — every future scenario needs bespoke runtime parsing | Low authoring burden but high **engineering** burden shifted to fragile runtime parsing | Very high (must validate at request time, in the hot path) | Very high (failures surface live, to learners) | None | Effectively impossible to pin/hash a "compiled" version distinctly from raw prose | Poor — every future certification re-implements ad hoc parsing | High (parsing becomes a runtime dependency) | N/A (no schema, so no migration, but also no integrity) | Very high | Very high |
| **B. Manual, one-off transformation to existing schema (hand-authored JSON)** | Good for a small slice; degrades at 17-scene scale (four-way parallel deltas × 3 options × 17 scenes = 51+ hand-typed delta sets, high transcription-error surface) | Good (existing validator still runs), but only as good as the human transcription | Poor at scale — no repeatable tooling, every future scenario repeats the same manual, error-prone work | Very high at scale | Same as today (existing validator) | Moderate — errors are transcription errors, hard to diff against source markdown | Straightforward (existing versioning) | Poor for reuse — no reusable artifact/tooling produced | Low | None (uses existing schema) | Low (once written, fully decoupled) | Moderate-high (nothing catches a hand-transcription mistake except the schema's structural checks, not its semantic fidelity to the source) |
| **C. Deterministic build-time compiler/normalizer** | High — a single, testable, versioned program is far less error-prone than repeated hand-transcription, and its output is validated identically to hand-authored content | High — compiler itself can assert 1:1 coverage (every dialogue file → exactly one scene, every option → exactly one compiled option) as a build-time check | High — one program to maintain, reused for every future scenario | Low ongoing burden after initial build; content authors keep writing markdown exactly as they do today | Moderate (schema validation + compiler-specific coverage assertions) | Low-moderate (compiler failures point at a specific source file/line; a human reviews the generated JSON diff before publish) | Straightforward, same versioning as today, plus the compiler's own version | High — directly reusable for every future Creative Studio scenario package | Low (an offline script, not a runtime dependency) | None if targeting an additive schema version | Low (compiler is a well-defined, testable boundary; generated JSON is reviewed before publish) | Low, **provided** a human reviews the generated JSON diff (never fully unattended, since scoring/cap logic is safety-critical) |
| **D. Extend the runtime schema/engine to consume a richer canonical package** | This is not an alternative to A/B/C — it is a **prerequisite** for any of them producing a correct result, since none of A/B/C can manufacture engine capabilities (conditional routing, flag-clear, computed outcome classification) that do not exist in `utils/scenario_engine.py` today | — | — | — | — | — | — | — | — | — | — | — |
| **E. Other** | No other evidence-supported architecture was found; content-authoring assistance via an LLM at *author time* (offline, human-reviewed, never in the live learner path) is compatible with C but is a tooling choice within C, not a distinct architecture | | | | | | | | | | | | |

**Why A is rejected:** it contradicts the explicit product requirement "deterministic operation without unrestricted AI chat" only if markdown parsing were done live/heuristically; more fundamentally, it discards every existing validation guarantee (`_validate_transitions`, `_detect_cycle`, reachability, canonical hashing) that the current runtime relies on for correctness and replay-safety, and the review records themselves treat "direct-consumption vs compiled" as an undecided, non-trivial engineering question — not a foregone conclusion.

**Why B alone is rejected for production, but accepted for the vertical slice:** at 2-3 scenes it is fast and safe (the existing schema already validates it); at 17 scenes with 7-dimension deltas × 3 options it is unmaintainable and produces no reusable tooling for future certifications, which the task explicitly asks to weigh ("future certification reuse").

**Why C is recommended, combined with D:** the compiler can only be as capable as its *target* schema. Since the target schema/engine must be extended regardless (conditional routing, flag-clear, tiered scoring, and computed outcome classification do not exist in any current schema version), the correct framing is **"C, targeting an additive D"** — not "C vs. D" as mutually exclusive options.

---

## 13. Recommended architecture (Architecture Decision Record)

- **Decision:** Adopt a deterministic, offline, human-reviewed build-time compiler (Architecture C) that consumes the approved Creative Studio markdown/frontmatter and emits documents conforming to a new, additive JSON Schema version `scenario_content/schemas/1.1.0/simulation.schema.json` (Architecture D), published through the existing, unmodified `scenario_versions` catalog/publish pipeline.
- **Context:** CB-SC-001 requires four runtime capabilities (conditional routing, flag-clear, tiered/computed scoring, dialogue variants) that do not exist in schema/engine version `1.0.0`. Persistence requires no change regardless of the chosen content-integration path (§11). The approved package is prose/markdown, not machine-typed JSON, and review records explicitly leave the integration architecture undecided.
- **Alternatives rejected:** direct runtime markdown consumption (A) — discards existing validation/determinism guarantees; unbounded manual hand-authoring at full 17-scene scale (B) — unmaintainable, error-prone, produces no reusable tooling.
- **Rationale:** the compiler is a single, testable, versioned artifact that can assert full source-to-target coverage at build time, is reused for every future Creative Studio scenario package (directly addressing "future certification reuse"), and never runs in the learner-facing runtime path (preserving 100% of the existing security/determinism model: Streamlit still only ever loads a validated, hashed, catalog-resolved JSON document, exactly as it does for BA-201 today).
- **Source-of-truth boundary:** see §14.
- **Generated-artifact policy:** see §15.
- **Validation boundary:** the compiler's output must pass the same `utils.scenario_schema.validate_scenario_document`-style validation as any hand-authored content, **plus** new compiler-specific coverage assertions (every dialogue file maps to exactly one scene; every option in every dialogue file maps to exactly one compiled option; every referenced flag is in the 13-flag canonical registry; every `nextScene`/corrective/reconvergence target resolves to an authored scene) run before the generated JSON is ever proposed for publish.
- **Runtime boundary:** the compiler never runs inside `pages/Scenario_Simulator.py` or any request-serving path; it is a build/CI-time or manually-invoked offline script only, identical in spirit to how the existing `catalog.json`/`canonicalContentSha256` pipeline already works for BA-201.
- **Versioning model:** new scenario schema version `1.1.0` (additive superset of `1.0.0`); new `simulationId` for CB-SC-001 (separate scenario identity, never a new version of BA-201); engine capability additions must bump `ENGINE_VERSION` if and only if they change what a stored decision's replay recomputes (conditional routing and flag-clear do; purely additive optional fields do not).
- **Rollback strategy:** because publishing is additive (new scenario, new schema version, new catalog entry), rollback is simply "do not add the new catalog entry as the default/visible scenario" — no existing BA-201 attempt, content, or test is touched, so no destructive rollback of existing state is ever required.
- **Future-scenario reuse:** the compiler, once built against the `1.1.0` schema, is the reusable integration point for any future Creative Studio scenario package that needs the same capabilities; a future package needing yet more capability would extend to `1.2.0` following the same additive discipline.
- **Known tradeoffs:** the compiler itself is new engineering surface that must be tested as rigorously as the engine it feeds (§19); a human review step on every generated-JSON diff is a deliberate, permanent process cost (not a one-time migration cost), justified by the safety-critical nature of scoring/cap/guard logic and by the review records' own explicit non-authorization of an unattended pipeline.

---

## 14. Source-of-truth model

| Artifact | Authoritative for | Who/what edits it |
|---|---|---|
| Creative Studio markdown (`06-dialogue/**`, `02-scenarios/specifications/**`) | Narrative prose, character dialogue lines, per-option debrief-seed prose, canon (character IDs, flags, artifact references) | **Humans** (Creative Studio content authors), unchanged from today |
| `scenario_content/schemas/1.1.0/simulation.schema.json` | The machine-checkable contract every compiled/authored document must satisfy | **Humans** (engineers), hand-written and reviewed like any other schema change |
| Compiler source code (new, not yet built) | The deterministic mapping from markdown structure → schema-conformant JSON | **Humans** (engineers), version-controlled, tested |
| Compiled `scenario.json` per version (e.g. `scenario_content/<cert-slug>/cb-sc-001-.../1.0.0/scenario.json`) | The exact runtime-loadable document for one immutable content version | **Machine-generated** by the compiler; **reviewed and committed by a human** (never auto-published without review, per §13) |
| `scenario_versions.content_snapshot` (database) | The exact immutable snapshot the live runtime actually loads at request time | **Machine-generated** at publish time from the reviewed, committed compiled JSON — unchanged existing publish flow |
| `canonicalContentSha256` | Tamper-evidence / identity pinning for a compiled document | **Machine-computed**, exactly as today (`compute_canonical_content_sha256`) |
| Outcome/scoring rules (caps, guards, composite formula) | The deterministic classification logic | **Split**: the *thresholds/coefficients* are authored data (could live in the compiled JSON as declarative condition data, to the extent the extended `ending.condition` language can express them); the *evaluation algorithm* (the 7-step priority order itself, and the composite-score formula) is **engine code**, versioned via `ENGINE_VERSION`, not re-authorable per scenario without an engineering change |
| Dialogue-variant selection logic | Which stored variant text is shown for a given flag/state combination | **Data-driven** (variant options + their trigger conditions are authored/compiled content); the **selection function** (deterministic "pick the matching variant") is **engine code** |
| Debrief seeds | Per-decision "why stronger/weaker" prose | **Authored content** (compiled from the `Debrief seed:` lines already present in the dialogue files) |
| Character/image references | Which character/visual asset a scene uses | **Authored content** (compiled from Creative Studio canon), **descriptive only** — never required by the engine for scoring/routing correctness |

**Explicit human-edits-vs-machine-generates boundary:** humans edit markdown and schema/engine/compiler source code. Machines generate: compiled scenario JSON (subject to mandatory human review before commit), the database snapshot at publish time, and the content hash. Nothing is ever hand-edited *inside* a compiled JSON file — a discrepancy there must be fixed upstream (in markdown or in the compiler) and recompiled, never patched in place, to keep the compiler the single source of transformation logic.

---

## 15. Generated-artifact policy

- Compiled `scenario.json` files are **committed to the repository**, not built ephemerally at deploy/runtime — this matches the existing BA-201 pattern exactly (`scenario_content/business_analyst/ba201-sim-meridian-health-01/1.0.0/scenario.json` is itself a committed file, not a runtime artifact) and keeps `git diff` a meaningful review surface for exactly what changed in learner-facing content.
- Every compiled file must carry (in a compiler-emitted, human-readable sidecar or code comment convention consistent with the repository's existing style — exact mechanism is an implementation detail, not decided by this review) a traceable link back to the exact Creative Studio source files and their versions/revision IDs (e.g. `sourceDialogueRef`, `revisionNotes` fields already present in the dialogue frontmatter) it was compiled from, so a future content change can be traced end-to-end.
- The compiler must be **idempotent and deterministic**: recompiling identical source markdown must byte-for-byte reproduce the same JSON (same key ordering assumptions aside — `compute_canonical_content_sha256` already sorts keys, so ordering is not itself a correctness risk, but *value* determinism is).
- A generated file is never published (i.e., never inserted into `scenario_versions.content_snapshot`) without passing both (a) the existing/extended `utils.scenario_schema.validate_scenario_document`-style validation and (b) a human review of the compiled JSON diff.
- No generated file is ever mutated in place after publish — exactly the existing "an already-published version's content must never be mutated in place" rule the `1.0.0` schema already documents; a content fix ships as a new `version` string, recompiled from corrected markdown.

---

## 16. Required schema/runtime changes (not performed in this task)

All of the following are **additive** to schema version `1.0.0` / `ENGINE_VERSION = "SCENARIO_ENGINE_V1"`, targeting a new `1.1.0` schema and a corresponding new engine version:

1. **Option evaluation tier:** add `evaluationTier: enum(optimal|acceptable|suboptimal|high-risk)` to the option schema (keep `isCorrect` derivable/optional for backward compatibility with `1.0.0` content, or leave `1.0.0` content on its existing binary model entirely unaffected).
2. **Flag clearing:** add `option.clearFlags: string[]`; extend `apply_decision` to compute `updated_flags = (run.flags_as_set - clear) | set(new)` deterministically (order-independent, since flags are semantically a set even though currently stored as a tuple).
3. **Conditional/dynamic routing:** add a bounded, declarative routing extension to `option` — e.g. an optional `nextSceneWhen: [{ conditions: {...}, nextScene: "..." }]` list evaluated in order against already-computed state/flags at `apply_decision` time, falling back to the existing plain `nextScene` when no conditional entry matches. Must remain a pure function of already-recomputed state/flags (never of anything not already replay-derivable) to preserve determinism.
4. **Dialogue structure + variants:** add an optional structured `exchanges: [{ speaker, expression, bodyLanguage, tone, text }]` array to scene, and an optional `variants` mechanism (either per-exchange conditional text or a small ordered list of `{ conditions, text }` overrides) evaluated deterministically against already-computed flags/state, exactly mirroring the routing extension's determinism discipline.
5. **Ending conditions over flags and derived aggregates:** extend `ending.condition` (and/or `evaluate_ending`) to support flag-presence conditions and a small, explicitly-versioned set of engine-computed derived aggregates (e.g. `positiveHealth`, `decisionQuality`, `compositeScoreUnrounded`, `highRiskDecisionCount`, `optimalDecisionCount`, `correctiveScenesExperienced`) computed once, deterministically, from final state + full decision history, before ending evaluation runs. This computed-metrics step is new engine logic, not new schema-declared state.
6. **Multi-stage evaluation order:** implement the 7-step priority order (severe caps → moderate caps → compute composite → strong guards → band selection → tie-break → round) as engine code operating over the new derived aggregates plus `endings[]`/flags, replacing (for `1.1.0`-schema content only) the current flat "first array-order match wins" semantics; `1.0.0` content's existing simpler semantics must remain byte-for-byte unchanged.
7. **Debrief-seed fields:** add optional `debriefSeed: { strongestOptionId, whyStronger, whyWeaker, immediateConsequence, laterConsequence, competencyImpact, capGuardEffect }` to option schema.
8. **Corrective-scene authoring/validation discipline:** add an optional stricter content-validation rule (not a schema field) asserting that any scene reachable only via a corrective-trigger edge has all of its options converge to exactly one single `nextScene` (enforces "no corrective re-branching" at validation time, catching an authoring mistake before publish rather than relying on convention alone).

None of the above requires changing `utils/scenario_persistence.py`, any SQL migration, or any RLS policy.

---

## 17. Required persistence changes

**None.** See §11 for the full justification. No new table, no new column, no new RPC, no new migration, no RLS change is required to support CB-SC-001, including its corrective-budget counters, flag history, and full decision-by-decision debrief data — all of it is already representable inside the existing generic `jsonb` snapshot/audit-trail columns.

---

## 18. Required UI changes

(Enumeration only — no implementation performed or recommended in this task.)

- New pre-scenario sequence: company/character introduction → learner-role briefing → Start Scenario gate (extending, not replacing, the existing landing/gate flow already referenced in `pages/Scenario_Simulator.py`).
- New character-card component (name, role, portrait placeholder until real assets exist, expression state).
- New dialogue-panel component capable of rendering a multi-exchange script per scene (speaker, tone/body-language cues, sequential reveal or single-block rendering — a UX decision, not decided here).
- Response-option cards: restyle existing `st.radio`-based rendering into a 3-card layout; add deterministic seeded shuffle for display order (§9) while keeping submission keyed on option ID.
- Consequence/feedback rendering: extend existing `option.feedback`/scene `explanation` rendering pattern; no new capability, more content.
- Scene-transition treatment for corrective vs. core scenes (visual differentiation, reusing/extending the existing `isDetour`-driven styling hook).
- Progress indication: extend existing `progress_label`/`domain_label` rendering; verify labeling still reads sensibly for CB-SC-001's stage/domain framing.
- New full-debrief screen: outcome banner (no numeric score shown by default, matching the "no visible scoring, detailed end-only debrief" requirement — this is already a philosophy the current page follows for the in-run experience, just not yet for the *end* debrief's richness), 7-dimension final-state dashboard, decision-by-decision review list, corrective-path summary (conditional on whether any corrective was experienced), competency roll-up, critical-risks-missed list, decision-path replay (read-only, non-scoring).
- Mobile responsive-layout verification for all of the above net-new components (standard responsive work, not a scenario-specific architecture concern).

---

## 19. Testing strategy (minimum required before implementation is considered safe)

Mirrors the existing test-module boundaries (`tests/test_scenario_schema.py`, `test_scenario_engine.py`, `test_scenario_persistence.py`, `test_scenario_learner_controller.py`, page tests) so new tests are additive, not a parallel/duplicate suite:

- **Schema tests** (extend `test_scenario_schema.py`): `1.1.0` schema accepts a minimal valid document with every new optional field; rejects malformed `evaluationTier`/`clearFlags`/conditional-routing/variant/ending-flag-condition shapes; confirms `1.0.0` documents remain valid and unaffected under the new schema-version-selection code path.
- **Content compilation/normalization tests** (new test module for the compiler): full source-to-target coverage assertions (every dialogue file → exactly one scene; every option → exactly one compiled option; every referenced flag is in the canonical 13-flag registry; deterministic/idempotent recompilation of identical input); golden-file tests comparing compiler output against a hand-verified expected JSON for at least the vertical-slice scenes.
- **Routing tests** (extend `test_scenario_engine.py`): conditional `nextSceneWhen` resolves correctly when the gating condition is true/false; falls back to plain `nextScene` when no conditional entry applies; reachability/cycle analysis correctly accounts for *every* possible resolved target of a conditional option, not just the first-declared one.
- **Flag tests:** `clearFlags` removes exactly the specified flags and leaves others untouched, order-independent of `setFlags` in the same option; a flag cleared then re-set later in the same run is fully differentiable from "never cleared."
- **State-delta tests:** seven-dimension deltas clamp correctly at both bounds; a counter-as-state-variable (e.g. corrective-budget) clamps and accumulates correctly across a full run.
- **Corrective-budget tests:** contract-level test (using a small synthetic fixture scenario, not the full 17-scene content) proving that once the budget counter reaches its cap, a subsequently-triggered corrective option resolves to the reconvergence scene instead of the corrective scene — this is the one mechanic explicitly permitted to be validated "at contract level" rather than through the live 17-scene content, per the task's own instructions, since the minimal vertical slice cannot reach budget exhaustion using only real content.
- **Replay tests:** a full run exercising every new mechanic (conditional routing taken, a flag both set and later cleared, at least one dialogue variant selected) replays byte-for-byte identically from `decisionHistory` alone, exactly like the existing `replay_matches_run`-style tests.
- **Persistence tests** (extend `test_scenario_persistence.py`): confirm zero-change assumption holds — a `serialized_engine_state`/`state_before`/`state_after` payload containing the new fields (extra state keys, a fuller flags list) round-trips through the existing validation helpers with no rejection, since they only validate shape, not content.
- **Idempotency tests:** unchanged existing tests remain valid; add one covering a retried submission of a decision whose resolution involves conditional routing, confirming idempotent replay still returns the identical resolved scene.
- **Stale-sequence tests:** unchanged existing coverage is sufficient; no new stale-sequence semantics are introduced.
- **Outcome tests:** each of the 4 outcomes reachable via at least one constructed path; each cap (`CAP-F01`–`F04`, `CAP-P01`–`P05`) and each guard (`GRD-S01`–`S05`) individually triggerable and correctly overriding a would-be-higher band; the 7-step evaluation order tie-break behavior explicitly tested (not just the common-path outcome).
- **UI contract tests** (extend the page test modules): new debrief screen renders from a `ScenarioCompletionResultView`-successor view without exposing any raw backend identifier (continuing the existing page-test discipline already enforced for BA-201); option-order randomization never changes what gets submitted for a given learner click.
- **Vertical-slice smoke test:** one end-to-end test (schema load → engine run → persistence round-trip → controller view) covering the exact vertical slice in §20, run in addition to (not instead of) the unit-level tests above.

Per the task's own instruction, the full repository test suite was **not** run for this review; only targeted reading of existing test modules was performed to establish current coverage boundaries.

---

## 20. Smallest safe vertical slice

**The default candidate in the task instructions is confirmed as the correct choice, with one addition.**

**Scope:** pre-scenario introduction (minimal) → `SC001-C01` → `SC001-C02` → conditionally `SC001-R2A` → convergence boundary at `SC001-C03` (render `C03`'s entry state to prove flag-dependent-variant read-through, but the slice does not need to go beyond it). No final outcome/debrief calculation — matches the task's own instruction ("no final production outcome calculation unless needed to validate the architecture").

**Why this slice is sufficient and well-chosen (mapped to the task's required minimum coverage):**

| Required coverage | Satisfied by |
|---|---|
| One normal transition | `C01` (any option) → `C02` |
| One corrective trigger | `C02` option B or C → `R2A` |
| One no-corrective path | `C02` option A → `C03` directly |
| One convergence | `R2A` (any option) → `C03`, converging with `C02`-A's direct path into the same scene |
| One flag-dependent dialogue variation | `C03` reads **two** independent upstream flags (`flag-verbal-handoff-only` from `C01`-C, `flag-sales-reengaged` from `R2A`) — a strong test case for variant-selection correctness |
| State mutation | All options across `C01`/`C02`/`R2A` apply real seven-dimension deltas from the approved spec |
| Persistence | Reuses the existing, unmodified V68 RPCs/tables end-to-end |
| Resume | Reuses the existing, unmodified `start_or_resume_ba201_attempt`-style flow |
| Replay | New replay test exercising the corrective-triggered path specifically |
| Stable option IDs | `opt-sc001-c0{1,2}-{a,b,c}` / `opt-sc001-r2a-{a,b,c}` used throughout |
| Maximum-corrective-budget mechanics at contract level | **Cannot** be exercised live within this 2-3 scene slice (budget=3, only one corrective is reachable) — satisfied instead by a small **synthetic fixture scenario** (not real content) that specifically unit-tests the new conditional-routing engine primitive at the budget cap, per §19's "corrective budget tests," which the task explicitly permits ("if practical," and the slice cannot practically reach exhaustion with only 2-3 real scenes) |

**Addition to the default candidate:** the slice's engineering scope should explicitly include building and testing the **`evaluationTier`** field and the **flag-clear** primitive even though this particular 2-3 scene span does not itself trigger a `clearFlags` case (that only happens at `C11`) — both are small, low-risk, and validating them now (via the same synthetic fixture used for the budget test) avoids discovering a schema gap late, after 14 more scenes are already compiled. The **computed-metrics/outcome-classification** engine work (§16 items 5-6) and the **full debrief UI** (§18) are correctly deferred past this slice, since nothing in `C01`-`C03` requires reaching a terminal scene.

**Not recommended as an alternative:** starting the slice later in the graph (e.g. `C08`-`C09`, which has richer flag interactions) — it would not exercise the *first* corrective trigger or the introduction/gate flow, and would not be meaningfully smaller in engineering effort, since the underlying primitives (routing, flag-clear, variants) are identical regardless of which pair of scenes is chosen. `C01`-`C03` is also the only span that additionally validates the introduction/briefing/Start-Scenario-gate UI, which every later span would skip.

---

## 21. Implementation sequence

Strictly sequential; each step's tests must pass before the next step begins. (Sequencing only — no step is performed by this review task.)

1. Draft and circulate the extended schema `1.1.0` specification (schema file + a short capability spec covering conditional routing, flag-clear, tiered scoring, dialogue variants, debrief seeds) for engineering + product sign-off. **Schema-authoring only, zero runtime code.**
2. Implement and unit-test the four engine primitives (flag-clear, conditional routing, computed-metrics step, multi-stage outcome evaluation) against synthetic fixture content only — **no CB-SC-001 content compiled yet**. Confirm `1.0.0`/BA-201 behavior is provably unchanged (full existing suite still green).
3. Build the deterministic compiler for the vertical-slice scenes only (`C01`, `C02`, `R2A`) plus its coverage-assertion tests; human-review the compiled JSON against the source markdown line-by-line.
4. Publish the vertical-slice content as a new catalog entry/scenario version (non-default/non-visible to learners, e.g. behind the existing feature-flag pattern already used for the Scenario Simulator) and run the full vertical-slice smoke test (§20) end-to-end, including resume and replay.
5. Build the minimal introduction/briefing/gate/character-card UI needed to present the slice; confirm no backend identifiers leak, matching existing page-test discipline.
6. Checkpoint: reassess scope/risk/timeline based on what steps 1-5 actually revealed before extending the compiler to the remaining 14 scenes.
7. Extend the compiler and content to all 17 scenes; extend engine tests to cover every cap/guard/outcome combination reachable in the full graph.
8. Build the full debrief UI and its supporting controller view classes.
9. Only after 1-8 are complete and reviewed: consider default-visibility/production launch decisions (explicitly out of scope for engineering to decide unilaterally — a product decision, §24).

---

## 22. Risk classification

**LOW RISK:**
- This review document itself.
- Drafting the `1.1.0` schema specification (documentation/schema-authoring, not runtime code).
- The compiler's coverage-assertion and golden-file tests.
- Non-persistent UI scaffolding (character cards, intro screens) built against static/mock data before wiring to real controller output.
- Any new unit test added to the existing `tests/test_scenario_*.py` modules.

**MEDIUM RISK:**
- Engine extension: conditional routing, flag-clear (changes `apply_decision`'s core semantics, must be proven not to affect `1.0.0` content).
- Computed-metrics step + multi-stage outcome evaluation (new derived-value computation, must be exhaustively tested for the 7-step priority order and tie-breaks).
- Dialogue-variant selection logic (new deterministic content-dependent branching in rendering/debrief).
- Content-version contracts (new `schemaVersion`/`ENGINE_VERSION` interplay must be verified not to break `replay_serialized_run`'s identity checks for existing BA-201 attempts).
- The compiler itself, as a new piece of engineering infrastructure with its own bug surface.

**HIGH RISK (explicitly out of scope for this task and for the recommended near-term implementation sequence):**
- Any database migration or persistence schema change — **not required**, per §11/§17.
- Any RLS policy change — **not required**.
- Any auth/session integration change — **not required**; CB-SC-001 reuses the existing `user_email`-scoped ownership model unmodified.
- Any production data mutation, staging, commit, push, or deploy — **not performed by this task**, per its own constraints.
- Marking any CB-SC-001 asset "Integrated," "Released," or "Production Approved" — **explicitly not authorized** by the product approval report itself, independent of this review's own scope.

**No high-risk work is proposed, required, or was performed by this task.**

---

## 23. Explicit non-goals

- This task did not implement, prototype, or scaffold any code, schema, or migration.
- This task did not select a final compiler implementation language/toolchain — only the architectural category (deterministic, offline, human-reviewed build-time transformation) is decided.
- This task did not determine exact numeric/threshold values beyond what the Creative Studio specs already state (those values are content-authoring facts, not engineering decisions).
- This task did not decide the exact wire-format of the conditional-routing or dialogue-variant schema extensions (e.g. exact JSON shape of `nextSceneWhen`) — only that such an extension is required and must remain a pure, deterministic function of already-recomputed state/flags.
- This task did not evaluate non-Salesforce-certification reuse, unrelated question-generation/benchmarking/audit-engine code, billing, or deployment infrastructure.
- This task did not inspect any protected path (`.local/`, `local_only/`, the named v58 policy-evaluator files, or any git-staging-all command) and did not need to, to reach its conclusions.
- This task did not run the full repository test suite (per its own instructions), only targeted reads of the specific test modules named in §4/§19.

---

## 24. Open product decisions

The following require a **product**, not an engineering, decision before implementation can proceed past the vertical slice:

1. **Certification identity:** which `certificationExamName`/`examCode` does CB-SC-001 belong to — a new scenario under the existing "Salesforce Certified Business Analyst" catalog (consistent with its Embedded-Business-Analyst framing), or a new certification track entirely? Not stated anywhere in the inspected Creative Studio package.
2. **Visual asset production:** the approved dialogue package has zero image/portrait asset IDs today (confirmed by the structured extraction). Product must decide whether the first production release ships with real character art/expressions, or with a text-first UI (prose `Setting`/`Expression`/`Body Language` rendered as styled text) as an interim state.
3. **Default/learner visibility timing:** when (if ever, pending successful vertical-slice validation) CB-SC-001 becomes visible to real learners — this is explicitly a product/launch decision, not something this review or the recommended implementation sequence resolves.
4. **Debrief-seed prose completeness:** the debrief-review record notes debrief-seed structure is READY but prose content status should be reconfirmed against the *current* (post-approval) dialogue files before the full debrief UI (§18, sequence step 8) is built, to avoid building UI against incomplete seed data.
5. **REV-D-004/REV-D-008-style residual documentation inconsistencies** (e.g. `SC001-C11`'s frontmatter `sourceDialogueRef: CB-DLG-006` vs. its own body §7 still citing `CB-DLG-005`) noted as non-blocking in the product approval report — should be resolved by Creative Studio before those specific scenes are compiled, to avoid the compiler needing to guess which reference is authoritative.

---

## 25. Engineering readiness verdict

- **Not ready** to compile and publish the full 17-scene CB-SC-001 package today, because required engine/schema capabilities (§16) do not exist.
- **Ready** to begin §21 step 1 (draft the `1.1.0` schema specification) immediately — this is documentation/schema-authoring work with no runtime impact and no dependency on any open product decision in §24.
- **Ready**, once step 1 is reviewed, to begin the vertical slice (§20) — it depends on no unresolved product decision (it uses real, approved `C01`/`C02`/`R2A` content and does not require a final certification-identity or visual-asset decision, since it needs neither a public certification listing nor real character art to prove the architecture).
- **Not ready** to build the full debrief UI or compile the remaining 14 scenes until the vertical slice (§20) has passed its full test suite (§19) and the checkpoint in §21 step 6 has been explicitly re-assessed.

---

## 26. Exact recommended next task

**SIM-ENG-COMPAT-02 (proposed): Draft the extended `scenario_content` schema version `1.1.0` specification.**

Scope: a JSON Schema file (`scenario_content/schemas/1.1.0/simulation.schema.json`, additive superset of `1.0.0`) plus a short companion engineering-capability specification covering exactly the six items in §16 (evaluation tier, flag-clear, conditional routing, dialogue structure/variants, flag/derived-aggregate ending conditions, multi-stage evaluation order) and the corrective-scene validation-discipline item. **Schema-authoring and specification-writing only — zero runtime code changes, zero engine changes, zero migrations, zero UI work.** This is the smallest possible next step that has no dependency on any open product decision (§24) and directly unblocks §21 step 2 (the synthetic-fixture engine-primitive implementation) without yet touching any CB-SC-001 content or the BA-201 production path.

---

## Completion report

1. **Task status:** Complete. Review-only; no files other than this document were created or modified.
2. **Review file created:** `docs/scenario_simulator/CB-SC-001_engineering_compatibility_review.md` (this file). The `docs/scenario_simulator/` directory did not previously exist and was created to hold it.
3. **Repository branch:** `main` (tracking `origin/main`, 17 commits ahead at the time of this review).
4. **Starting git status summary:** `## main...origin/main [ahead 17]`, plus pre-existing untracked, out-of-scope items: `.local/`, `local_only/`, `scripts/v58_run_combined_policy_evaluation.py`, `structural_audit_state.json`, `tests/test_combined_policy_evaluator.py`, `v68_corrected_review_bundle/`, `v68_final_review_bundle/`, `v68_review_bundle/`, `workers/combined_policy_evaluator.py`. No modified tracked files — the runtime baseline used for this review was clean and reliable. All protected paths listed above were left uninspected.
5. **Ending git status summary:** identical to the starting summary, plus exactly one new untracked file: `docs/scenario_simulator/CB-SC-001_engineering_compatibility_review.md`. Nothing was staged, committed, or pushed.
6. **Runtime files inspected:** `utils/scenario_schema.py`, `utils/scenario_catalog.py`, `utils/scenario_engine.py`, `utils/scenario_persistence.py` (contract/docstring sections), `utils/scenario_learner_controller.py` (view dataclasses and function signatures), `pages/Scenario_Simulator.py` (module docstring + option-rendering code), `scenario_content/schemas/1.0.0/simulation.schema.json`, `scenario_content/business_analyst/ba201-sim-meridian-health-01/1.0.0/scenario.json` (excerpt), `scenario_content/docs/SCENARIO_PERSISTENCE_ARCHITECTURE.md` (excerpt), `supabase/migrations/20260719130000_v68_scenario_attempt_persistence_foundation.sql` (table DDL + `submit_scenario_decision_v1`/`start_or_resume_scenario_attempt_v1` signatures), and the existing scenario test module list (`tests/test_scenario_*.py`) for coverage-boundary purposes only (no test execution).
7. **Creative Studio files inspected:** all four `CB-SC-001_*-v1.md` specifications, all 17 dialogue files under `06-dialogue/simulator-v1/CB-SC-001/`, and all three `09-review/CB-SC-001_*` review records, via a dedicated read-only structured-extraction pass; no other Creative Studio canon path was opened (none was needed to resolve any ambiguity).
8. **Protected paths:** none inspected, referenced, or modified.
9. **Stop conditions encountered:** none. Runtime contracts, Creative Studio files, and persistence behavior were all locatable and unambiguous from tracked, non-protected sources; the repository's tracked-file baseline was clean; no unapproved migration was required to complete the assessment.
