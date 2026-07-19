# Scenario Simulator Persistence Architecture (SIM-PERSIST-01, amended by SIM-PERSIST-01A)

**Status:** Architecture and documentation only. No migrations, RPCs, RLS policies, or Python persistence code exist yet. Nothing in this document has been applied to the database.

**Amendment history:** SIM-PERSIST-01A (this revision) changed the scenario-content storage recommendation from repository-only (Option B) to hybrid repository+database (Option C, §7); resolved the reopening policy (§8), concurrent-attempt policy (§9.1), authentication/ownership direction (§11.2/§11.3), A/B-testing scope (§7.2/§19), and idempotency-key strategy (§12) from open decisions into firm V1 rules; corrected the certification-identity design from an unverified `certifications.id` FK to the actually-established `certifications.exam_name` natural key (§2, §6.1); and clarified the `service_role`-bypasses-RLS security model (§11). Renamed `active`/`activated_*` → `published`/`published_*` and `activate_scenario_version_v1` → `publish_scenario_version_v1` throughout for vocabulary consistency with the amendment.

**Scope:** Persistent storage for Scenario Simulator learner attempts, built on top of the completed local deterministic runtime (`utils/scenario_schema.py`, `utils/scenario_catalog.py`, `utils/scenario_engine.py`).

---

## 1. Executive recommendation

1. **Scenario content is hybrid-authoritative (Option C — amended from Option B in SIM-PERSIST-01).** Repository JSON files under `scenario_content/<certification_slug>/<simulation_id>/<version>/scenario.json` remain the **authoring, review, and source-control** source of truth — that is where a scenario is written and reviewed, exactly as today. But at **publication**, the exact validated document is inserted into `scenario_versions.content_snapshot` as an **immutable `jsonb` runtime snapshot**, and it is that database snapshot — not a live filesystem read — that runtime start/resume/replay actually loads. `scenario_versions` additionally retains `canonical_content_sha256`, the source repository path, the schema version, and the engine/execution-contract version the snapshot was validated under, plus a publication timestamp. See §7 for the full rationale and §2/§6.2 for the corrected table shape.
2. **Four new tables**, additive only: `public.scenarios`, `public.scenario_versions`, `public.scenario_attempts`, `public.scenario_decisions`. No existing table (`exam_attempts`, `question_attempts`, `certifications`, etc.) is modified or reused.
3. **All mutation goes through `SECURITY INVOKER` RPCs called by the existing service-role Python backend** — the same access model already used for every other write path in this codebase (`create_supabase_admin_client()` / `get_supabase_admin_client()` in `utils/access_control.py`). There is no Supabase-Auth-issued JWT anywhere in this application, so classic `auth.uid()`-scoped RLS policies are not the enforcement mechanism here (none exist in any of the 49 tracked migrations), and adopting one is explicitly **deferred** rather than designed here (§11). Ownership is **application-enforced, not end-user-RLS-enforced**: a trusted `user_email` is derived server-side from the signed session (`utils/access_control.py::get_current_user_email()`), never accepted from the browser as a raw parameter, then passed into the RPC and re-verified **inside the RPC body**, under a row lock, against the attempt row — the same identity string (`user_email`) already used as the ownership key for `exam_attempts` / `question_attempts` (`utils/question_selection.py::verify_exam_attempt_ownership`), but checked transactionally instead of only in the Python layer, and always trim+lowercase normalized before both persistence and comparison (§11).
4. **One RPC, `submit_scenario_decision_v1`, is the only way a decision is ever written.** It combines row-level locking (`FOR UPDATE`), expected-sequence/expected-scene verification, and idempotency-key handling in a single transaction — following the exact pattern already proven by `claim_background_job_v1` (`FOR UPDATE SKIP LOCKED`), `publish_question_version_v1` (`FOR UPDATE` + advisory lock + idempotent early return), and `apply_stripe_billing_event_v1` / `claim_billing_checkout_v1` (`UNIQUE` idempotency key + `ON CONFLICT DO NOTHING RETURNING`).
5. **Python (the engine) remains the only place scoring, state transitions, and ending evaluation happen.** The RPC never recomputes `state`, `flags`, `isCorrect`, `domainPerformance`, or the ending — it only validates *ordering and identity* of an already-engine-computed step and persists it. This mirrors the instruction "PostgreSQL should transactionally validate and record the expected transition, but Python remains the source of truth for calculating the transition" precisely, and is explained in full in §9. This holds under the amended hybrid content model too: publication moves the *content* into an immutable database snapshot, but no scoring, state-change, or ending-evaluation logic is ever added to PostgreSQL as a result — that logic stays exclusively in `utils/scenario_engine.py`.
6. **Reopening, concurrency, auth direction, A/B scope, and idempotency-key strategy are now resolved for V1** (amended from "open decisions" in the prior revision) — see §8, §9/§12, §11, §7.2/§19, and §12 respectively. Only the exact `certifications.id` type and the longer-term `auth.uid()` migration remain genuinely open, and both have documented, gated paths forward (§16, §11.3) rather than unresolved ambiguity.

---

## 2. Repository conventions discovered

All of the following were confirmed by reading migrations and Python source in this repository; nothing here is a generic Supabase best-practice assumption.

| Convention | Evidence |
|---|---|
| Migration filename shape `YYYYMMDDHHMMSS_vNN_description.sql`, monotonically increasing version tag (`v44`…`v65` currently tracked) | `supabase/migrations/*.sql` directory listing |
| Primary keys on every table added since v44 are `uuid PRIMARY KEY DEFAULT gen_random_uuid()` | `supabase/migrations/20260623000000_v44_question_version_foundation.sql:29`, `...20260624023300_v44_background_jobs_foundation.sql:34`, `...20260629120000_v46_free_mock_curation_foundation.sql:30`, `...20260625000000_v46_stripe_billing_foundation.sql:16` |
| `created_at`/`updated_at` are `timestamptz NOT NULL DEFAULT now()`; **there is no automatic `updated_at` trigger anywhere in this repository** — every RPC sets `updated_at = now()` explicitly in its own `UPDATE` statement | `supabase/migrations/20260629120000_v46_free_mock_curation_foundation.sql:657-659`, `...20260625120000_v46_stripe_checkout_claims.sql:159-161` |
| Named `CONSTRAINT <table>_<column>_<intent>` for every `CHECK`/`UNIQUE` (never anonymous constraints) | `supabase/migrations/20260624023300_v44_background_jobs_foundation.sql:103-204` |
| **`ENABLE ROW LEVEL SECURITY` is applied to every table, but no migration in this repository's tracked history contains a single `CREATE POLICY` statement.** RLS-enabled + zero policies means anon/authenticated get zero rows/writes by default; `service_role` bypasses RLS entirely per Postgres/Supabase semantics. | Confirmed by an exhaustive grep of `supabase/migrations/*.sql` for `CREATE POLICY` (no matches) and `ENABLE ROW LEVEL SECURITY` (13 files) |
| **No migration anywhere uses `auth.uid()`.** This application has no Supabase-Auth-issued JWT flow; authentication is a custom HMAC-signed session cookie/query-param carrying `user_email`, and the Python backend always connects to Supabase with the **service-role** key. | Exhaustive grep of `supabase/migrations/*.sql` for `auth.uid()` (no matches); `utils/access_control.py:59-67` (`create_supabase_admin_client`), `utils/access_control.py:526-527` (`get_supabase_client` is an alias for the admin/service-role client — "this is server-side Streamlit; service role is kept centralized") |
| Every RPC is `LANGUAGE plpgsql`, `SET search_path = public, pg_catalog`, and **`SECURITY INVOKER`** by deliberate, near-universal default (32 files use `SECURITY INVOKER`; exactly one file, `20260704000000_v54_restore_admin_import_questions_batch_v1.sql`, uses `SECURITY DEFINER`, as a documented one-off exception) | Grep counts across `supabase/migrations/*.sql` |
| Privilege hardening pattern: `REVOKE ALL ... FROM PUBLIC`, then explicit `REVOKE EXECUTE ... FROM anon, authenticated` (belt-and-suspenders even though the `PUBLIC` revoke already covers it), then `GRANT EXECUTE ... TO service_role` only | `supabase/migrations/20260624023700_v44_background_job_enqueue_claim_rpcs.sql:242-293`, `...20260625000000_v46_stripe_billing_foundation.sql:328-341` |
| `RAISE EXCEPTION ... USING ERRCODE = '<code>'`, with `invalid_parameter_value` and `no_data_found` as the two most common application error codes, and the real Postgres `23505` (`unique_violation`) reused for ownership/uniqueness conflicts | `supabase/migrations/20260624023700_v44_background_job_enqueue_claim_rpcs.sql:57-88`, `...20260625000000_v46_stripe_billing_foundation.sql:237-266` (`23505` for ownership conflicts) |
| Row-level concurrency: `SELECT ... FOR UPDATE` to serialize per-row writers; `FOR UPDATE SKIP LOCKED` specifically for multi-worker queue claiming; `pg_advisory_xact_lock(hashtext(...))` for serializing an operation keyed by something *other* than a single row's primary key (e.g. "publish this question" or "publish this exam+language's free-mock set") | `supabase/migrations/20260624240000_v45_question_version_publication_gate.sql:210,236-299` (row lock + advisory lock); `...20260624023700_v44_background_job_enqueue_claim_rpcs.sql:182-221` (`FOR UPDATE SKIP LOCKED`); `...20260629120000_v46_free_mock_curation_foundation.sql:744-746` (advisory lock keyed by exam+language) |
| Idempotency: a `UNIQUE` idempotency-key column, `INSERT ... ON CONFLICT (key) DO NOTHING RETURNING id INTO v_id`, branch on `v_id IS NULL` to detect "already exists," and — critically — **an idempotent early return with no further writes** when the underlying action already completed | `supabase/migrations/20260625000000_v46_stripe_billing_foundation.sql:189-222` (`apply_stripe_billing_event_v1`, keyed on `stripe_event_id`); `...20260625120000_v46_stripe_checkout_claims.sql:58-144` (`claim_billing_checkout_v1`, keyed on `idempotency_key`, distinguishes "reused" vs "created") |
| Immutability-after-publish is enforced with a `BEFORE UPDATE OR DELETE` trigger function that `RAISE EXCEPTION`s on any illegal transition, allowlisting only the one legal transition (e.g. `published → retired`) | `supabase/migrations/20260629120000_v46_free_mock_curation_foundation.sql:122-207` (`guard_free_mock_set_mutation_v1`) |
| Learner-owned attempt rows are keyed by **`user_email`** (case-insensitive/trim-normalized), not a Supabase-Auth user id. A *stored* attempt id (session state, URL) is never trusted directly — it is re-verified against the live row's `user_email`/`mode`/`exam_name`/`language_code` before reuse. | `utils/question_selection.py:527-599` (`verify_exam_attempt_ownership`, `_exam_attempt_row_matches_expected`) |
| Idempotent child-row persistence today relies on a `UNIQUE (parent_id, child_key)` constraint plus `upsert`, called directly from Python against the service-role client (no RPC) | `utils/question_selection.py:689-` (`persist_question_attempts`, upserts against the unique `(exam_attempt_id, question_id)` constraint) |
| Content-addressable versioning for large authored documents already exists as **repository files + a `catalog.json` index carrying `canonicalContentSha256` per version**, distinct from the `question_versions` JSONB-in-database pattern used for individually-edited MCQ items | `scenario_content/business_analyst/catalog.json`, `utils/scenario_catalog.py:35-49` |
| Certification identity: scenario content's `certificationExamName` is a display/lookup string that the ENGINEERING_BRIEF states **should eventually resolve to an authoritative `certifications.id` foreign key at publish/import time** — but **no table in this repository's tracked history actually stores or joins on `certifications.id` for anything.** `certification_domains` — the one existing table that *does* need to reference certification identity — joins to `certifications` by the **`exam_name` text natural key**, not by `id` (its own migration inserts/queries only ever filter `WHERE exam_name = ...`, never `certifications.id`). Elsewhere, a JSON metadata key literally named `certification_id` (`audit_runs.metadata->>'certification_id'`, and `workers/duplicate_question_detector.py`) is populated with the **exam-name string itself**, not a surrogate key — confirming `certification_id` is not an established id-FK convention anywhere in this codebase today. | `scenario_content/docs/ENGINEERING_BRIEF.md:29-36` (the aspiration); `supabase/migrations/20260713224500_v61_add_platform_app_builder_certification_catalog.sql:134-158` (certifications/certification_domains joined and inserted by `exam_name`, never `id`); `workers/duplicate_question_detector.py:117-120,212-214` (`"certification_id": certification_exam_name` — a text alias, not a surrogate key) |
| Verification-script convention for future migrations: a paired `supabase/tests/<change>_verification.sql` file, wrapped in `BEGIN; ... ROLLBACK;` (or committed only in a scratch/test project), using plain PL/pgSQL `ASSERT`, no pgTAP dependency, run as `service_role` | `supabase/tests/v44_background_job_lifecycle_verification.sql:1-39` |
| **No `CREATE TABLE public.certifications` (or `certification_domains`) exists anywhere in this repository — confirmed by an exhaustive repo-wide grep, not just a migrations-folder check.** The one migration that touches these tables says so explicitly in its own header comment, and ships a dedicated read-only `information_schema` preflight script specifically because the live schema has never been directly verified in-repo. `public.questions.id`, by contrast, **is** confirmed `integer` (`int4`) as of the v44 migration header. `certifications.id`'s type is therefore genuinely undeclared in this repository (see §16 for the required live preflight before any migration assumes it) | `supabase/migrations/20260713224500_v61_add_platform_app_builder_certification_catalog.sql:7-8` ("public.certifications and public.certification_domains predate this repository's migration history -- there is no CREATE TABLE migration for either"); `supabase/tests/v61_platform_app_builder_catalog_schema_preflight.sql:7-20` (the paired preflight script, checking columns/constraints/types via `information_schema` only, read-only); `supabase/migrations/20260623000000_v44_question_version_foundation.sql:18-21` (`questions.id` confirmed `int4`, for contrast) |

---

## 3. Current runtime contract

The local engine (read-only for this task) already provides everything a persistence layer needs to bind to:

- **Identity** — `ScenarioContent.simulation_id`, `.version`, `.canonical_content_sha256`, plus `ENGINE_VERSION = "SCENARIO_ENGINE_V1"` (`utils/scenario_engine.py:17`). `replay_serialized_run` rejects any payload whose `simulationId`/`version`/`canonicalContentSha256`/`engineVersion` don't match the supplied content (`utils/scenario_engine.py:573-625`).
- **Authoritative replay input** — `ScenarioDecisionInput(sequence_number, scene_id, option_id)` (`utils/scenario_engine.py:56-74`) is the *only* thing replay trusts. Everything else (`state`, `flags`, `isCorrect`, `domainPerformance`, `currentSceneId`, `isComplete`, `endingId`) is always recomputed from content + the ordered decisions, never taken from a serialized/persisted source.
- **General reconstruction primitive** — `replay_scenario_run(content, decisions) -> ScenarioRunSnapshot` (`utils/scenario_engine.py:412-454`) supports empty, partial, and complete histories and never itself enforces completion. `resume_scenario_run` is a documented thin alias (`utils/scenario_engine.py:457-468`).
- **Terminal enforcement** — `build_terminal_result(run) -> ScenarioTerminalResult` (`utils/scenario_engine.py:340-354`) is the only function that requires `run.is_complete`.
- **Strict deserialization** — `deserialize_decision_history(value) -> tuple[ScenarioDecisionInput, ...]` (`utils/scenario_engine.py:546-570`) rejects non-list payloads, non-object entries, missing/extra fields, non-integer/boolean/`<1` sequence numbers, blank ids, and any gap/duplicate/reorder in `sequenceNumber`.
- **Serialization contract** — `serialize_run_snapshot(run)` emits exactly `simulationId, version, canonicalContentSha256, engineVersion, currentSceneId, state, flags, decisionHistory[], isComplete, terminalResult` (`utils/scenario_engine.py:683-718`); `serialize_terminal_result(result)` emits `endingId, scoreBand, narrative, recommendedReview[], finalState, flags, decisions[], domainPerformance[], engineVersion, canonicalContentSha256` (`utils/scenario_engine.py:650-672`). Every value is a freshly built `dict`/`list`, so mutating a serialized payload can never mutate the engine's own immutable runtime objects.
- **Deep immutability** — all state/flag/decision-history mappings on `ScenarioRunSnapshot`/`ScenarioDecisionRecord`/`ScenarioTerminalResult` are produced through `_freeze_state` (`MappingProxyType` over a defensive copy) (`utils/scenario_engine.py:133-145`).

The isolated scenario suite (`test_scenario_schema.py`, `test_scenario_catalog.py`, `test_scenario_engine.py`) currently reports 109 passed. Nothing in this document requires changing any of the files above.

---

## 4. Recommended persistence boundaries

| Layer | Owns | Never does |
|---|---|---|
| **Repository files** (`scenario_content/**/scenario.json`, `catalog.json`) | The full authored scenario document; the only place scene/decision/ending content is edited | Store attempt or learner data |
| **`utils/scenario_schema.py` + `utils/scenario_catalog.py`** (unchanged) | Loading, JSON-Schema validation, graph analysis, canonical SHA-256 hashing of content | Any I/O beyond the local filesystem; any learner-specific state |
| **`utils/scenario_engine.py`** (unchanged) | Deterministic state transitions, ending evaluation, replay, serialization contracts | Any database or network I/O; any concept of "who" is playing |
| **New: `utils/scenario_persistence.py`** (Python application layer, not built in this task) | Bridges the engine to Supabase: loads content, calls the engine, calls the RPC, translates RPC errors into typed exceptions | Recompute anything the engine already computed; trust any client-submitted derived value |
| **New: Postgres tables + RPCs** (not built in this task) | Durable attempt/decision storage, ordering/idempotency/ownership enforcement, transactional advancement | Scoring, state-change arithmetic, ending evaluation, path/graph logic |
| **Future: Streamlit session state** | A short-lived UI cache of the current attempt's `ScenarioRunSnapshot` for the current page render | Ever be the only copy of attempt progress — a rerun, tab close, or device switch must be able to reconstruct identical state from the database via `replay_scenario_run` |

---

## 5. Proposed entity model

```
public.certifications (existing, unmodified)
        ▲
        │ certification_exam_name (text natural key — see §2/§6.1;
        │ NOT certifications.id, which no existing table references)
public.scenarios  ───────────────┐
        ▲                        │ 1:N
        │ scenario_id (FK)       ▼
public.scenario_versions  (immutable JSONB content snapshot once published)
        ▲
        │ scenario_version_id (FK)
public.scenario_attempts  (one row per learner attempt; at most one
        ▲                  in_progress per user+version, §8/§9)
        │ attempt_id (FK)
        │ 1:N, append-only
public.scenario_decisions
```

- `scenarios` — one row per `(certification_exam_name, simulation_id)`. Mirrors a `catalog.json` entry's identity, not its versions.
- `scenario_versions` — one row per `(scenario_id, version)`. Mirrors one `catalog.json` version entry's identity (`version`, `schemaVersion`, `relativePath`, `estimatedMinutes`) plus, as of this amendment, the immutable **`content_snapshot jsonb`** actually loaded at runtime, `canonical_content_sha256`, the engine/execution-contract version, and a publication timestamp — a DB-only publication lifecycle (§6.2, §7).
- `scenario_attempts` — one row per learner attempt, bound to exactly one immutable `scenario_version_id`, with at most one `in_progress` row per `(normalized user_email, scenario_version_id)` (§8/§9).
- `scenario_decisions` — one append-only row per submitted decision, ordered by `sequence_number`.

No table for endings, scenes, or options is introduced as a *separate* relational structure — that content is part of the single `content_snapshot jsonb` document on `scenario_versions`, loaded through `utils/scenario_schema.py`'s existing parsing logic (applied to the database snapshot instead of a filesystem read — see §7) at read/replay time, identified by the FK chain above.

---

## 6. Detailed table contracts

### 6.1 `public.scenarios`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | NO | `gen_random_uuid()` | PK |
| `certification_exam_name` | `text` | NO | — | **Amended from a `certification_id` FK (SIM-PERSIST-01A).** The natural-key text value matching `public.certifications.exam_name` — the same identity field `certification_domains.exam_name` actually joins on today (§2), and the field the ENGINEERING_BRIEF calls "the same real, authoritative lookup/validation identity used elsewhere in CertBound." Resolved once from the content's `certificationExamName` at ingestion time and never re-derived on every read. A real `FOREIGN KEY (certification_exam_name) REFERENCES public.certifications(exam_name)` is the target shape *if* a live `information_schema` check (§16) confirms `certifications.exam_name` actually carries a `UNIQUE`/`PRIMARY KEY` constraint capable of being an FK target — mirroring the working precedent already implied by `certification_domains.exam_name`. If that check finds no such constraint, this column still functions correctly as an application-validated (not database-enforced) natural key, exactly as `certification_domains.exam_name` already does today without necessarily having its own declared FK either (§2) |
| `certification_id` | *(type TBD — see §16)* | YES | `NULL` | **Deferred, optional, nullable secondary column** — not populated in V1. Reserved for the ENGINEERING_BRIEF's stated future direction of resolving to a `certifications.id` surrogate key, but only ever backfilled once a live `information_schema` preflight (§16) confirms `certifications.id`'s actual type and that some other part of the application has begun relying on it as a real FK target. Never required, never assumed, never blocking |
| `certification_slug` | `text` | NO | — | Mirrors the repository folder name (e.g. `business_analyst`); authoring/debugging convenience only, never used for lookups |
| `simulation_id` | `text` | NO | — | Matches `ScenarioContent.simulation_id` / `catalog.json`'s `simulationId` exactly, byte-for-byte |
| `exam_code` | `text` | NO | — | Display-only candidate-facing code (e.g. `BA-201`), matches `ScenarioContent.exam_code` |
| `title` | `text` | NO | — | Display title, matches the *current* catalog entry; **not** authoritative for any past attempt (see `scenario_attempts.title_snapshot`, §6.3) |
| `is_active` | `boolean` | NO | `true` | Whether learners may start **new** attempts against any version of this scenario. Does not affect existing attempts. |
| `created_at` | `timestamptz` | NO | `now()` | |
| `updated_at` | `timestamptz` | NO | `now()` | Set explicitly by every mutating RPC, per repository convention |

Constraints:
- `PRIMARY KEY (id)`
- `UNIQUE (certification_exam_name, simulation_id)` — one scenario identity per certification
- `CHECK (btrim(simulation_id) <> '')`, `CHECK (btrim(exam_code) <> '')`, `CHECK (btrim(title) <> '')`, `CHECK (btrim(certification_slug) <> '')`, `CHECK (btrim(certification_exam_name) <> '')`

Indexes: `(certification_exam_name, is_active)` for "list active scenarios for this certification."

Mutability: `simulation_id`, `certification_exam_name`, `certification_slug`, `exam_code` are immutable after creation (a new `simulation_id` is a new scenario, never a rename). `title` and `is_active` may be updated to track catalog edits. No `DELETE` — retire via `is_active = false`.

Authoritative vs. derived: every column here is authoritative identity/catalog metadata copied from `catalog.json` at ingestion time — nothing here is derived from attempts. `certification_id` is explicitly non-authoritative/unused placeholder metadata in V1 (see above).

RLS: `ENABLE ROW LEVEL SECURITY`, zero policies (deny-all for `anon`/`authenticated`), `REVOKE ALL FROM PUBLIC`, `GRANT SELECT, INSERT, UPDATE TO service_role` only (content-management table — same shape as `question_versions`, §6 of `20260623000000_v44_question_version_foundation.sql`). No `DELETE` grant, matching the "content tables are append/retire, not delete" convention already used for `free_mock_sets`.

Retention: rows are never deleted; a scenario with no remaining active versions is simply `is_active = false`.

### 6.2 `public.scenario_versions`

**Amended (SIM-PERSIST-01A): content storage is now hybrid (Option C), not repository-only (Option B).** `content_snapshot` is new; everything else below is unchanged from SIM-PERSIST-01 except `active`/`activated_*` are renamed `published`/`published_*` to match the amendment's own vocabulary ("on publication", "published scenario version") — this is a naming clarification, not a behavior change; the lifecycle semantics (§8) are identical.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | NO | `gen_random_uuid()` | PK |
| `scenario_id` | `uuid` | NO | — | FK → `public.scenarios(id)` |
| `version` | `text` | NO | — | Matches `ScenarioContent.version` (semver-shaped string, e.g. `1.0.0`), byte-for-byte |
| `schema_version` | `text` | NO | — | Matches `ScenarioContent.schema_version` |
| `source_repository_path` | `text` | NO | — | Repository-relative path from `catalog.json`'s `relativePath` (e.g. `ba201-sim-meridian-health-01/1.0.0/scenario.json`); retained for authoring/provenance/audit ("which reviewed file did this snapshot come from") — **no longer read from at runtime** once a version is published (renamed from `relative_path` to make this provenance-only role explicit) |
| `content_snapshot` | `jsonb` | NO | — | **New.** The exact validated `ScenarioContent` document (the same JSON `utils/scenario_schema.py::load_scenario_content` parses from disk), inserted verbatim at publication time. This — not the repository file — is what runtime start/resume/replay actually loads once a version exists in the database. `NOT NULL` from row creation: a `draft` row is only ever created together with the snapshot it was validated against, never with a placeholder |
| `canonical_content_sha256` | `text` | NO | — | The SHA-256 of `content_snapshot`, computed by `utils/scenario_schema.py`'s existing canonical-hash routine over the *repository file* at publication time. This is the binding hash checked (a) once, at publication (`content_snapshot`'s hash must equal this column), and (b) on every subsequent load (the application recomputes the hash of the loaded `content_snapshot` and must get this same value back) — see §7 |
| `estimated_minutes` | `integer` | YES | `NULL` | Display only |
| `engine_version` | `text` | NO | — | The `ENGINE_VERSION` string this version was last verified to load and replay successfully under, at publication time (see §13) |
| `status` | `text` | NO | `'draft'` | `draft` \| `published` \| `retired` (see §8; renamed from `active` for terminology consistency with this amendment) |
| `created_at` | `timestamptz` | NO | `now()` | |
| `published_at` | `timestamptz` | YES | `NULL` | Renamed from `activated_at` |
| `published_by` | `text` | YES | `NULL` | Renamed from `activated_by`. Actor email, mirrors `published_by` on `free_mock_sets` |
| `retired_at` | `timestamptz` | YES | `NULL` | |

Constraints:
- `PRIMARY KEY (id)`
- `UNIQUE (scenario_id, version)`
- `CHECK (status IN ('draft', 'published', 'retired'))`
- `CHECK (status <> 'published' OR (published_at IS NOT NULL AND published_by IS NOT NULL AND btrim(published_by) <> ''))` — mirrors `free_mock_sets_publish_metadata`
- `CHECK (btrim(canonical_content_sha256) <> '')`, `CHECK (btrim(source_repository_path) <> '')`, `CHECK (btrim(version) <> '')`
- `CHECK (content_snapshot IS NOT NULL AND jsonb_typeof(content_snapshot) = 'object')` — guards against an accidental scalar/array/null insert

Indexes: `UNIQUE INDEX ... (scenario_id) WHERE status = 'published'` — **at most one published version per scenario at a time**, mirroring `idx_free_mock_sets_one_published`. This is the concrete enforcement of "BA-201 V1 uses one published scenario version at a time" (§7.2/§19 — A/B testing/multiple simultaneously-published versions is explicitly deferred, not designed).

Mutability: a `draft` row (and its `content_snapshot`) may be freely re-ingested (re-read from disk, re-hashed, snapshot replaced) until published. **Once `status = 'published'` or `'retired'`, the row — including `content_snapshot` — is immutable** except for the `draft → published → retired` status transitions themselves, enforced by a `BEFORE UPDATE` guard trigger identical in shape to `guard_free_mock_set_mutation_v1` (`supabase/migrations/20260629120000_v46_free_mock_curation_foundation.sql:122-165`), extended to also reject any attempted change to `content_snapshot`/`canonical_content_sha256`/`source_repository_path`/`schema_version`/`engine_version` once `published`. This is what "Scenario versions must be immutable after publication" (Core Architecture Principle 6, and this amendment's item 1) means concretely: **a content edit always creates a new `scenario_versions` row** (new `version` string, new `draft` row, re-publish) — it is never expressed as an `UPDATE` to a published row's snapshot.
  - **How publication prevents repository/database divergence:** `publish_scenario_version_v1` (renamed from `activate_scenario_version_v1`, §10) is the *only* write path that can move a row from `draft` to `published`, and it performs the hash check *before* flipping status: it re-loads the repository file at `source_repository_path` through `utils/scenario_schema.py`, recomputes the canonical hash, and requires that hash to equal both the value Python computed just now (`p_verified_content_sha256`) and the row's own `canonical_content_sha256`. If the repository file has drifted from what was drafted (someone edited it after `INSERT`, before publish), publication fails closed (`CONTENT_HASH_MISMATCH`) rather than publishing stale or mismatched content. Once published, the *database* snapshot is authoritative for runtime — the repository file may continue to exist, be reformatted, or even be deleted (not recommended, but not catastrophic) without affecting any already-published version, because runtime never reads the file again. Divergence can therefore only occur *before* publication (a normal, expected, review-time state — a `draft` row is not yet trusted) and is structurally impossible *after* publication, because the published `content_snapshot` is frozen and the only remaining copy anyone should treat as canonical.

Authoritative vs. derived: `version`, `schema_version`, `source_repository_path`, `estimated_minutes` are copied from `catalog.json`/`ScenarioContent` at ingestion for provenance. **`content_snapshot` and `canonical_content_sha256` are the runtime-authoritative content once published** — the repository file is authoritative only up to the moment of publication, after which the database snapshot is what every attempt actually binds to and replays against. `status`/`published_*`/`retired_at` are DB-native lifecycle state with no repository equivalent.

RLS/grants: identical shape to `scenarios` — service-role only, no `DELETE`.

Retention: never deleted. A version that must never be played again is `retired`, not removed — this preserves the ability to replay/audit any historical attempt bound to it.

### 6.3 `public.scenario_attempts`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | NO | `gen_random_uuid()` | PK |
| `user_email` | `text` | NO | — | Ownership key. **Stored already trim+lowercase normalized at insertion** (not just re-normalized at comparison time — amended in SIM-PERSIST-01A so the partial unique index below and every equality lookup can rely on simple `=` against a canonical form) and defensively re-normalized (`lower(btrim(...))`) again on every RPC-side comparison, in case a historical row was ever written before normalization was enforced. The value itself is always sourced server-side from the signed session (`utils/access_control.py::get_current_user_email()`), never taken verbatim from a browser-supplied parameter — see §11. Same identity used by `exam_attempts`/`question_attempts` (`verify_exam_attempt_ownership`) |
| `app_user_id` | `text` | YES | `NULL` | Optional secondary reference to `app_users.id` (cast to `text`, matching the existing `au.id::text` convention in `apply_stripe_billing_event_v1`); **not** the ownership key — `user_email` is |
| `scenario_version_id` | `uuid` | NO | — | FK → `public.scenario_versions(id)`; **immutable for the life of the attempt** |
| `simulation_id` | `text` | NO | — | Denormalized copy of `scenario_versions.scenario_id → scenarios.simulation_id` at creation time, so a query never needs to join three tables to know "which scenario is this" — a deliberate, small, intentional snapshot, in the same spirit as `background_jobs.payload` being described as "an immutable snapshot... not a reference to a mutable source record" |
| `canonical_content_sha256` | `text` | NO | — | Copied from `scenario_versions` at attempt-creation time. Redundant with the FK today (the version row is immutable), but pins the exact content hash the attempt was created against even if a future migration ever needs to relax version immutability — defense in depth, not decoration |
| `engine_version` | `text` | NO | — | The `ENGINE_VERSION` active when the attempt was created |
| `status` | `text` | NO | `'in_progress'` | `in_progress` \| `completed` \| `abandoned` (see §8 — no additional statuses are needed) |
| `next_sequence_number` | `integer` | NO | `1` | The only sequence number `submit_scenario_decision_v1` will currently accept; advances by exactly 1 per successfully applied decision |
| `current_scene_id` | `text` | YES | — | `NULL` only when `status = 'completed'`; otherwise the scene id the next decision must target |
| `decision_count` | `integer` | NO | `0` | `= next_sequence_number - 1`; stored redundantly for cheap listing/analytics queries without a `COUNT(*)` join |
| `terminal_ending_id` | `text` | YES | `NULL` | Set only when `status = 'completed'` |
| `title_snapshot` | `text` | NO | — | `scenarios.title` at attempt-creation time, so a later catalog title edit never rewrites a learner's history display |
| `started_at` | `timestamptz` | NO | `now()` | |
| `updated_at` | `timestamptz` | NO | `now()` | Set explicitly on every advancing write |
| `completed_at` | `timestamptz` | YES | `NULL` | |
| `abandoned_at` | `timestamptz` | YES | `NULL` | |

Constraints:
- `PRIMARY KEY (id)`
- `CHECK (status IN ('in_progress', 'completed', 'abandoned'))`
- `CHECK (next_sequence_number >= 1)`
- `CHECK (decision_count = next_sequence_number - 1)`
- `CHECK (status <> 'completed' OR (completed_at IS NOT NULL AND terminal_ending_id IS NOT NULL AND current_scene_id IS NULL))`
- `CHECK (status <> 'abandoned' OR abandoned_at IS NOT NULL)`
- `CHECK (status = 'in_progress' OR completed_at IS NOT NULL OR abandoned_at IS NOT NULL)` (a non-`in_progress` row always has the matching terminal timestamp)
- `CHECK (btrim(user_email) <> '')`
- `CHECK (user_email = lower(btrim(user_email)))` — **new in SIM-PERSIST-01A**: enforces the "stored already normalized" rule above at the database level, not just at the RPC's `INSERT` call site

Indexes:
- `(user_email, status, started_at DESC)` — "list this learner's in-progress/completed attempts"
- `(scenario_version_id)` for admin/audit queries
- **New (SIM-PERSIST-01A) — `UNIQUE INDEX scenario_attempts_one_in_progress_per_user_version ON (user_email, scenario_version_id) WHERE status = 'in_progress'`** — the concurrent-attempt-policy enforcement mechanism; see §9.1 for the full mechanism and rationale. (The index can key directly on `user_email` rather than `lower(btrim(user_email))` because the new `CHECK` constraint above guarantees the column is already normalized.)

Mutability: `user_email`, `scenario_version_id`, `simulation_id`, `canonical_content_sha256`, `engine_version`, `started_at` are immutable after creation. Every other column advances only through `submit_scenario_decision_v1` or an explicit abandon/complete RPC — never a direct client `UPDATE`.

Authoritative vs. derived: `next_sequence_number`, `current_scene_id`, `decision_count`, `status`, `terminal_ending_id` are the database's cached copy of what the engine has already computed and had verified into `scenario_decisions` — they are authoritative *for concurrency-control purposes* (the RPC's own row-lock check), but a client must never treat them as trusted final scoring output the way, say, `exam_attempts.score` is; the true authority for "is this ending correct" is always a fresh `replay_scenario_run` over `scenario_decisions`, exactly as `replay_matches_run` already demonstrates in the engine (`utils/scenario_engine.py:471-481`).

RLS: `ENABLE ROW LEVEL SECURITY`, zero policies. See §11 for why direct client access is not offered even though this is learner-owned data.

Retention: no hard delete path in v1. Abandoned/completed attempts are retained indefinitely for progress history and audit, matching how `exam_attempts` is never deleted today. A future data-retention policy (e.g. account deletion) is out of scope for this task and is listed in §19.

### 6.4 `public.scenario_decisions`

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | `uuid` | NO | `gen_random_uuid()` | PK |
| `attempt_id` | `uuid` | NO | — | FK → `public.scenario_attempts(id) ON DELETE CASCADE` (cascades only because an attempt is never actually deleted in practice — this is a safety net, not an expected code path) |
| `sequence_number` | `integer` | NO | — | 1-based, matches `ScenarioDecisionInput.sequence_number` exactly |
| `scene_id` | `text` | NO | — | Matches `ScenarioDecisionInput.scene_id` — **authoritative replay input** |
| `option_id` | `text` | NO | — | Matches `ScenarioDecisionInput.option_id` — **authoritative replay input** |
| `idempotency_key` | `text` | NO | — | Client-supplied request-dedup key (see §12) |
| `domain_id` | `text` | YES | — | **Evidence/audit only** — copied from the engine's `ScenarioDecisionRecord.domain_id` for analytics; never read back by replay |
| `is_correct` | `boolean` | YES | — | **Evidence/audit only** |
| `next_scene_id` | `text` | YES | — | **Evidence/audit only** |
| `state_after` | `jsonb` | YES | — | **Evidence/audit only** — a debugging/analytics snapshot of `ScenarioDecisionRecord.state_after` |
| `created_at` | `timestamptz` | NO | `now()` | |

Constraints:
- `PRIMARY KEY (id)`
- `UNIQUE (attempt_id, sequence_number)` — the core ordering/dedup guarantee; matches the engine's own gap-free/duplicate-free invariant
- `UNIQUE (attempt_id, idempotency_key)` — the request-level dedup guarantee (§12)
- `CHECK (sequence_number >= 1)`
- `CHECK (btrim(scene_id) <> '')`, `CHECK (btrim(option_id) <> '')`, `CHECK (btrim(idempotency_key) <> '')`

Indexes: `(attempt_id, sequence_number)` (also serves as the natural "get full ordered history" query; the `UNIQUE` constraint above already creates this index).

Mutability: **fully immutable, append-only, no `UPDATE`, no `DELETE`** (other than the `ON DELETE CASCADE` safety net above, which never fires under normal operation since attempts are never deleted). No admin correction path exists or should exist — a wrong decision is a new decision made later within the same run, exactly as a real exam works; the audit trail must never be edited.

Authoritative vs. derived: `sequence_number`, `scene_id`, `option_id` are authoritative replay input — this table, read in `sequence_number` order and fed through `deserialize_decision_history` + `replay_scenario_run`, is the *entire* durable state of an attempt. `domain_id`, `is_correct`, `next_scene_id`, `state_after` are stored **only** as evidence/audit convenience (fast analytics without replaying every attempt) and are explicitly documented as never trusted by replay, matching Core Architecture Principle 4 and the engine's own `ScenarioDecisionInput` docstring (`utils/scenario_engine.py:56-70`).

RLS: `ENABLE ROW LEVEL SECURITY`, zero policies, service-role only, no `DELETE`, no `UPDATE` grant at all (only `SELECT, INSERT`) — enforced at the grant level, not just by convention, since there is genuinely no legitimate `UPDATE` path for this table.

Retention: never deleted in the normal product; retained as the permanent, replayable record of what a learner did.

---

## 7. Scenario-content storage decision

**Amended recommendation (SIM-PERSIST-01A): Option C — hybrid. Repository files remain the authoring/review source of truth; the database stores an immutable JSONB runtime snapshot per published version, and runtime always reads the database, never the filesystem, once a version is published.** This supersedes the SIM-PERSIST-01 recommendation of repository-only Option B.

### 7.1 The responsibility split

| Concern | Owner |
|---|---|
| Authoring, editing, PR review, source control history | Repository file (`scenario_content/<slug>/<simulation_id>/<version>/scenario.json`) |
| Schema validation, graph analysis (reachability/path-length/cycle detection), canonical SHA-256 computation | `utils/scenario_schema.py`, run against the repository file at ingestion/publication time |
| The exact content every attempt actually starts, resumes, and replays against | `scenario_versions.content_snapshot` (immutable `jsonb`), from the moment of publication onward |
| Provenance: "which reviewed file did this snapshot come from" | `scenario_versions.source_repository_path` (kept, but read-only/informational post-publication — §6.2) |
| Binding/verification hash | `scenario_versions.canonical_content_sha256`, checked at publication and re-checked by the application against every loaded snapshot |
| Schema/engine compatibility metadata | `scenario_versions.schema_version` / `engine_version`, both captured at publication time |
| Publication timestamp | `scenario_versions.published_at` (§6.2) |

### 7.2 Why this is the production approach, weighed against the task's explicit criteria

- **Immutable replay** — strengthened versus repository-only Option B: replay now binds to a database row that cannot be edited (enforced by a `BEFORE UPDATE` trigger, §6.2), rather than to a repository file that — while conventionally protected by PR review — is still, in principle, a mutable filesystem path a future deploy could alter without any database-level guardrail noticing before replay time. Under Option C, that class of drift is caught at publication (fail closed) rather than discovered later at replay.
- **Deployment consistency / avoiding divergence** — this is the primary reason for the amendment. Under repository-only Option B, the database can only *detect* drift (a recomputed hash mismatch) after it has already happened; it cannot *prevent* the repository file from silently changing underneath an already-published version between one deploy and the next. Under Option C, **publication is the one moment divergence is checked, and after that moment there is nothing left to diverge** — the runtime source of truth (`content_snapshot`) is copied into the database and frozen; the repository file's later fate (edited, reformatted, moved, even deleted) cannot affect any attempt already bound to a published snapshot, because runtime never reads the file again post-publication. Concretely: `publish_scenario_version_v1` (§10) loads the repository file, computes its hash, and requires that hash to equal both the value Python just computed and the value already recorded on the `draft` row — if the file changed after the `draft` row was created but before publication, this fails closed (`CONTENT_HASH_MISMATCH`) and nothing is published. Once past that gate, the database and the repository can never disagree about what a *published* version's content is, because only one of them (the database) is consulted again.
- **Rollback** — reverting a bad *draft* is still a normal git revert (nothing has been published yet, so this is unchanged from Option B). Reverting a bad *published* version is a new operation under Option C: publish a new corrected version and retire the bad one (§6.2's `UNIQUE ... WHERE status = 'published'` index enforces at most one live version), or simply retire the bad version with no replacement if the scenario should be temporarily unavailable. This is no worse than Option B, where "rollback" of an already-played-against file was already this delicate for exactly the same reason (existing attempts are pinned to a hash, so even an Option-B git revert wouldn't retroactively change what those attempts replay against).
- **Content review** — unchanged from Option B: scenario authoring still goes through normal PR review as a JSON file. Publication is a distinct, later step from authoring/merging, so review quality is unaffected by this amendment.
- **Hash verification** — strengthened: `canonical_content_sha256` now verifies two things instead of one — (a) at publication, that the database snapshot about to be frozen matches the reviewed repository file; and (b) on every subsequent load, that the snapshot read back out of the database still matches its own recorded hash (a defense against an unexpected direct database mutation bypassing the immutability trigger, e.g. a manual superuser `UPDATE`).
- **Operational simplicity / Supabase read cost** — this is the one criterion where Option C costs more than Option B: a `jsonb` column read replaces a free filesystem read on every attempt creation/resume/replay. This is an accepted, explicit trade-off — a single-row `jsonb` fetch by primary key is a cheap, well-understood Postgres/Supabase read pattern (no different in kind from reading any other row), and BA-201-scale scenario documents (dozens of scenes) are well within normal `jsonb` payload sizes. The divergence-prevention guarantee above is judged worth this cost.
- **Repository-based content authoring** — unchanged: `utils/scenario_catalog.py` and `scenario_content/**/catalog.json` remain exactly as they are today for authoring and local/offline development; Option C only changes what *runtime* (attempt creation/resume/replay against a *published* version) reads from, not how content is written or reviewed.

`question_versions` (JSONB-in-database) remains a distinct, separately-justified pattern for individually-authored MCQ items, edited one field at a time through an admin review workflow with per-field audit trails (`question_version_events`) — that is a different granularity of edit than a whole-document scenario publish, and this amendment does not blur that distinction: scenario documents are still authored and reviewed as whole files with graph-level validation, and only the already-fully-validated whole document is ever copied into `content_snapshot` — there is no field-level database editing workflow for scenario content.

### 7.3 What does not change

No scoring, state-change, or ending-evaluation logic moves into PostgreSQL as a result of this amendment. `content_snapshot` is loaded, parsed, and validated in Python via the existing `utils/scenario_schema.py` dataclasses (just from a `jsonb` payload already decoded by the Supabase client instead of `json.load()` on a file handle) — the engine (`utils/scenario_engine.py`) is entirely unaware of where the `ScenarioContent` it receives came from.

---

## 8. Attempt state machine

Three statuses only — `in_progress`, `completed`, `abandoned` — no more are needed for the BA-201 vertical slice:

```
          create_scenario_attempt_v1
                    │
                    ▼
             ┌─────────────┐
             │ in_progress │◄──────────┐
             └─────┬───────┘           │
                    │ submit_scenario_decision_v1     submit_scenario_decision_v1
                    │ (not yet EVALUATE_ENDING)        (retry with an already-applied
                    └───────────────────────────────────  idempotency_key/sequence_number)
                    │
                    │ submit_scenario_decision_v1
                    │ (reaches EVALUATE_ENDING)
                    ▼
             ┌─────────────┐
             │  completed  │  (terminal — no further decisions accepted)
             └─────────────┘

  in_progress ──abandon_scenario_attempt_v1──► abandoned  (terminal — no further decisions accepted)
```

- **Creation** — `create_scenario_attempt_v1(p_user_email, p_scenario_id)` resolves the currently-`published` `scenario_versions` row for that scenario, loads its `content_snapshot` (hash-reverified against `canonical_content_sha256` — §7/§13) via `utils/scenario_schema.py` in Python, calls `start_scenario_run`, and inserts one `scenario_attempts` row with `status='in_progress'`, `next_sequence_number=1`, `current_scene_id = content.start_scene`. No `scenario_decisions` rows yet. **Amended concurrent-attempt behavior (SIM-PERSIST-01A):** before inserting, the RPC first checks for an existing `in_progress` row for `(lower(btrim(p_user_email)), scenario_version_id)`; if one exists, it is returned unchanged (`created = false`) instead of creating a second one — see §9.1 for the full mechanism.
- **Resume** — reading an existing `in_progress` attempt requires no special RPC: fetch the row (ownership-checked), fetch its `scenario_decisions` in `sequence_number` order, and call `replay_scenario_run` in Python. This is exactly `replay_scenario_run`'s designed purpose (`utils/scenario_engine.py:412-454`) — partial history in, live resumable snapshot out. **Only `in_progress` attempts may resume** (see reopening policy immediately below).
- **Decision submission** — the only state-advancing write; see §9.
- **Completion** — happens automatically, inside `submit_scenario_decision_v1`, the moment the engine (in Python, before calling the RPC) determines the decision reaches `EVALUATE_ENDING`; there is no separate "complete" call for the happy path.
- **Abandonment** — `abandon_scenario_attempt_v1(p_attempt_id, p_user_email)` transitions `in_progress → abandoned` with `abandoned_at = now()`. Ownership-checked, idempotent (calling it twice on an already-abandoned attempt is a no-op success, not an error), and rejected on a `completed` attempt (`ERRCODE = 'invalid_parameter_value'`, matching the "only draft sets can be edited" style rejection in `replace_free_mock_draft_items_v1`).
- **Reopening policy — resolved, not supported in v1 (amended from an open decision to a firm rule in SIM-PERSIST-01A).** `completed` attempts are immutable and never reopened. `abandoned` attempts are never reopened. **Only `in_progress` attempts may resume** — resume is simply "read + replay," not a status transition, so there is nothing to "reopen" for an `in_progress` row in the first place. A learner who wants another attempt always starts a brand-new `scenario_attempts` row via `create_scenario_attempt_v1` — a retake is unconditionally a new attempt, never a mutation of a terminal one. This is enforced structurally, not just by convention: no RPC in §10 accepts a `completed`/`abandoned` attempt as a target for any state-advancing operation, and `scenario_decisions` has no legitimate `UPDATE` path at all (§6.4).
- **Concurrent-attempt policy — resolved for V1 (amended from an open decision, SIM-PERSIST-01A).** Unlimited `completed` attempts are allowed per learner per scenario (retaking is unrestricted once a prior attempt is terminal). At most **one `in_progress` attempt per `(normalized user_email, scenario_version_id)`** — enforced by a partial unique index (§6.3, §9.1), not just application discipline. Calling `create_scenario_attempt_v1` again while one is already `in_progress` for that exact version returns the existing row (`created = false`) rather than erroring or creating a second one. This does **not** introduce cross-version A/B-testing infrastructure — it is scoped to one specific immutable `scenario_version_id`, not "one attempt per scenario across all versions"; see §7.2/§19 for why multiple simultaneously-`published` versions remain out of scope regardless.
- **Version compatibility failure** — if `create_scenario_attempt_v1` finds no `published` version for the requested scenario (e.g. the only version was retired without a replacement being published), it fails with a typed `NO_PUBLISHED_VERSION` error; the Python layer surfaces this as "this scenario is temporarily unavailable," never as a silent fallback to a different version.
- **Deleted or unavailable scenario versions** — cannot happen for an *existing* attempt's content, because `content_snapshot` lives in the database row itself (not the filesystem) and `scenario_versions` rows are never deleted (§6.2 retention) and are immutable once published. The only remaining failure mode is a *repository* file being removed after publication, which is now purely a provenance/audit concern (`source_repository_path` becoming unresolvable) and **no longer affects runtime replay at all**, since runtime never reads that path post-publication (§7) — a meaningful robustness improvement over the SIM-PERSIST-01 repository-only design, where a missing file was a hard replay failure (§14 still documents the pre-amendment failure mode for historical/comparison context, since a `draft` row's ingestion still depends on the file existing).

Authoritative fields on `scenario_attempts`: `status`, `next_sequence_number`, `current_scene_id`, `decision_count`, `terminal_ending_id`, `completed_at`/`abandoned_at`/`started_at`/`updated_at`, `scenario_version_id`, `canonical_content_sha256`, `engine_version`, `user_email`. None of these is ever accepted as input from a client for anything other than `p_user_email` (used purely for the ownership check, never written back unverified) — every other value is either server-generated or computed by the engine and then verified transactionally by the RPC, per §9.

---

## 9. Transactional decision-submission flow

### 9.1 Concurrent-attempt enforcement (new in SIM-PERSIST-01A)

Resolved for V1: **at most one `in_progress` attempt per `(normalized user_email, scenario_version_id)`.**

- Enforced with a `UNIQUE` **partial** index on `scenario_attempts`:

  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS scenario_attempts_one_in_progress_per_user_version
      ON public.scenario_attempts (lower(btrim(user_email)), scenario_version_id)
      WHERE status = 'in_progress';
  ```

  This is a database-enforced invariant, not just an RPC-level check — even a hypothetical future direct-insert bug or a race between two `create_scenario_attempt_v1` calls that both pass an application-level pre-check cannot produce two simultaneously-`in_progress` rows for the same learner and version, because the second `INSERT` fails the index outright.
- `create_scenario_attempt_v1` handles this cooperatively rather than treating the index violation as an error path: it first `SELECT`s for an existing `in_progress` row matching `(lower(btrim(p_user_email)), scenario_version_id)`; if found, it returns that row's identity with `created = false` (§10) instead of attempting an `INSERT` that would violate the index. The index remains the authoritative backstop for the race between two concurrent first-time creations (both miss the `SELECT`, both attempt `INSERT`, the index allows exactly one to succeed) — in that specific race, the loser's `INSERT` raises `23505`, which `create_scenario_attempt_v1` catches and converts into the same "fetch and return the winner's row" behavior, so the *caller* never sees a raw constraint-violation error for this case.
- **Scope is deliberately per-`scenario_version_id`, not per-`scenario_id`.** A learner cannot have two simultaneous `in_progress` attempts against the *same published version*, but nothing here prevents (nor needs to prevent, since only one version is ever `published` at a time per scenario, §6.2) multiple historical `completed`/`abandoned` attempts, or an `in_progress` attempt that started under a now-retired version continuing to resume against that exact version it is pinned to (§7.2) while a different, newer version is `published` for new attempts. This is intentionally **not** cross-version A/B-testing infrastructure — it is a single-version concurrency guard, and the `UNIQUE ... WHERE status = 'published'` index on `scenario_versions` (§6.2) is what actually forecloses A/B testing, not this index.
- Unlimited `completed` attempts remain allowed — the partial index's `WHERE status = 'in_progress'` clause means completed/abandoned rows never participate in the uniqueness check at all.

### 9.2 Decision submission

This is the one place genuine concurrency risk exists once an attempt is already underway: two requests (a Streamlit double-click, a client retry racing a slow first response, two browser tabs) attempting to advance the *same* attempt at nearly the same time. A `UNIQUE (attempt_id, sequence_number)` constraint alone is not sufficient — it only rejects a duplicate *after* both requests have already run the engine and formed conflicting opinions about what "the next state" should be; it does not stop the second request from wastefully computing a step that will simply fail to insert, and it does not by itself guarantee the *decision count* (`next_sequence_number`) advances by exactly 1 per accepted decision. A single locking, all-or-nothing transaction is required.

**Request flow (Streamlit/client → Python → engine → Supabase RPC):**

1. **Streamlit/client** already has (or fetches) the current `ScenarioRunSnapshot` for the attempt — either freshly created, or reconstructed via `replay_scenario_run` over the attempt's persisted `scenario_decisions`. It renders the current scene and collects the learner's chosen `option_id`.
2. **Python application layer** (`utils/scenario_persistence.py`, not built in this task) calls `apply_decision(run, option_id)` from `utils/scenario_engine.py`. This is where **all** scoring happens: state changes, clamping, flag updates, correctness, and — if the decision reaches `EVALUATE_ENDING` — the ending and terminal result. The engine call is pure and offline; nothing has been persisted yet.
3. Python builds the RPC call with:
   - `p_attempt_id`, `p_user_email` (ownership)
   - `p_expected_sequence_number` = the sequence number Python *believes* is next (i.e., `run`'s pre-decision decision count + 1) and `p_expected_scene_id` = the scene id Python believes the attempt is currently sitting at (i.e., `run.current_scene_id` before `apply_decision`) — this is the "assumed current state" the RPC will re-verify
   - `p_scene_id`, `p_option_id` — the untrusted-but-now-engine-validated inputs that produced the new state (identical to what will be persisted as `ScenarioDecisionInput`)
   - `p_idempotency_key` — **resolved in SIM-PERSIST-01A (was an open decision):** Python generates a fresh **UUIDv4** the first time it is about to submit a given decision, and Streamlit session state retains that exact pending key across reruns/retries until the submission succeeds (a Streamlit rerun must reuse the same key it already generated for "the decision currently being submitted," never mint a new one for what is actually a retry of the same logical request). Once the RPC returns success (either `'applied'` or `'duplicate_applied'`), the key is discarded/cleared from session state so the *next* decision gets its own fresh UUIDv4 — a key is never reused across two different logical decisions
   - Audit-only fields computed by the engine for storage convenience: `p_domain_id`, `p_is_correct`, `p_next_scene_id`, `p_state_after` (as `jsonb`)
   - Only when the engine determined completion: `p_is_complete = true`, `p_terminal_ending_id`
4. **`submit_scenario_decision_v1`** runs as a single transaction:
   a. `SELECT ... FROM scenario_attempts WHERE id = p_attempt_id FOR UPDATE` — serializes every concurrent submission against this one attempt.
   b. Verify ownership: `lower(btrim(user_email)) = lower(btrim(p_user_email))`; mismatch or missing row → `RAISE EXCEPTION ... USING ERRCODE = 'no_data_found'` (the RPC never reveals whether an attempt id exists for a *different* owner — same "missing or mismatched ⇒ treated as absent" posture as `verify_exam_attempt_ownership`).
   c. **Idempotency check first** (before any state check): `SELECT ... FROM scenario_decisions WHERE attempt_id = p_attempt_id AND idempotency_key = p_idempotency_key`.
      - If found and its `sequence_number`/`scene_id`/`option_id` match the incoming request exactly → return the previously recorded outcome, no new write, `outcome = 'duplicate_applied'` (mirrors `apply_stripe_billing_event_v1`'s `'duplicate_processed'` branch).
      - If found but any of `sequence_number`/`scene_id`/`option_id` differ → `RAISE EXCEPTION ... USING ERRCODE = '23505'` (`IDEMPOTENCY_KEY_REUSE_MISMATCH`) — the same key must never silently apply two different decisions.
   d. Verify attempt status: `status = 'in_progress'`, else `RAISE EXCEPTION` (`ATTEMPT_NOT_IN_PROGRESS`) — no decisions accepted on `completed`/`abandoned` attempts.
   e. Verify expected sequence: `p_expected_sequence_number = next_sequence_number`, else `RAISE EXCEPTION` (`SEQUENCE_CONFLICT`) — this is what catches the race: the loser of a concurrent pair acquires the lock second, sees `next_sequence_number` already advanced by the winner, and fails cleanly instead of double-applying.
   f. Verify expected scene: `p_expected_scene_id = current_scene_id`, else `RAISE EXCEPTION` (`SCENE_CONFLICT`) — belt-and-suspenders with (e); catches a client that computed against stale scene state for some other reason.
   g. `INSERT INTO scenario_decisions (...)` with the authoritative triple plus the audit-only columns.
   h. `UPDATE scenario_attempts SET next_sequence_number = next_sequence_number + 1, current_scene_id = CASE WHEN p_is_complete THEN NULL ELSE p_next_scene_id END, decision_count = decision_count + 1, status = CASE WHEN p_is_complete THEN 'completed' ELSE status END, terminal_ending_id = p_terminal_ending_id, completed_at = CASE WHEN p_is_complete THEN now() ELSE NULL END, updated_at = now() WHERE id = p_attempt_id`.
   i. Return the new `sequence_number`, `next_sequence_number`, `current_scene_id`/`isComplete`/`terminalEndingId`, `outcome = 'applied'`.
5. **On `SEQUENCE_CONFLICT`/`SCENE_CONFLICT`** (another request won the race), Python re-fetches the attempt's current `scenario_decisions`, replays via `replay_scenario_run` to get the *true* current snapshot, and either discovers the learner's intended decision was already applied by the winner (nothing to do) or re-renders the current scene so the learner can re-choose against reality. The client never silently retries the exact same (now-stale) RPC call.

**Why scoring logic never lives in SQL:** step 4 never calls `apply_decision`-equivalent arithmetic in PL/pgSQL — it only compares the *already-computed* `p_scene_id`/`p_option_id`/`p_next_scene_id`/`p_is_complete`/`p_terminal_ending_id` against the attempt's own locked row state for ordering/identity consistency. If a caller supplied an invalid `option_id` (one that isn't valid for the current scene), that would already have raised a `ScenarioRunStateError` in step 2, in Python, before the RPC is ever called — the RPC does not and cannot re-derive "is this a legal choice," because it has no access to the scenario graph. This is precisely the requested split: **Postgres validates the transition is the one that was expected to happen next; Python calculates what that transition is.**

---

## 10. Proposed RPC contract

All RPCs: `LANGUAGE plpgsql`, `SET search_path = public, pg_catalog`, `SECURITY INVOKER` (consistent with the near-universal repository default — see §2; there is no reason for a `SECURITY DEFINER` exception here, since the caller is always `service_role` already). `REVOKE ALL FROM PUBLIC`; `REVOKE EXECUTE FROM anon, authenticated`; `GRANT EXECUTE TO service_role` only. Why this is safe: the Python backend is the only caller of any Supabase RPC in this entire application (`get_supabase_client()` is always the service-role client), so `SECURITY INVOKER` executing as `service_role` is exactly as privileged as every other RPC already in production, and RLS's deny-all-for-anon/authenticated posture is an unconditional second layer even if the anon/publishable key were ever mistakenly exposed to a browser.

```
create_scenario_attempt_v1(
    p_user_email          text,
    p_scenario_id         uuid,
    p_app_user_id         text    DEFAULT NULL
) RETURNS TABLE (
    attempt_id            uuid,
    scenario_version_id   uuid,
    current_scene_id      text,
    next_sequence_number  integer,
    created               boolean   -- false if an identical in_progress attempt already existed for
                                     -- (user_email, scenario_version_id) and was reused instead
)
```
Error categories: `INVALID_PARAMETER` (empty email, unknown `p_scenario_id`), `NO_PUBLISHED_VERSION` (§8, renamed from `NO_ACTIVE_VERSION`). `p_user_email` is always the server-derived, signed-session value from `utils/access_control.py::get_current_user_email()` — never a raw client-supplied field (§11) — and is trim+lowercase normalized by the RPC before use, matching the storage-time normalization on `scenario_attempts.user_email` (§6.3). **Concurrent-attempt behavior (§9.1):** if an `in_progress` row already exists for `(normalized p_user_email, scenario_version_id)`, that row is returned with `created = false` instead of a new row being inserted.

```
submit_scenario_decision_v1(
    p_attempt_id              uuid,
    p_user_email              text,
    p_idempotency_key         text,
    p_expected_sequence_number integer,
    p_expected_scene_id       text,
    p_scene_id                text,
    p_option_id               text,
    p_next_scene_id           text,    -- NULL when p_is_complete
    p_is_complete             boolean,
    p_terminal_ending_id      text     DEFAULT NULL,
    p_domain_id               text     DEFAULT NULL,
    p_is_correct              boolean  DEFAULT NULL,
    p_state_after             jsonb    DEFAULT NULL
) RETURNS TABLE (
    decision_id           uuid,
    sequence_number        integer,
    next_sequence_number    integer,
    current_scene_id       text,
    is_complete             boolean,
    terminal_ending_id      text,
    outcome                 text     -- 'applied' | 'duplicate_applied'
)
```
Error categories: `OWNERSHIP_MISMATCH` (→ `no_data_found`, indistinguishable from "not found"), `ATTEMPT_NOT_IN_PROGRESS` (→ `invalid_parameter_value`), `SEQUENCE_CONFLICT` (→ `invalid_parameter_value`, with `HINT` carrying the true current `next_sequence_number`), `SCENE_CONFLICT` (→ `invalid_parameter_value`), `IDEMPOTENCY_KEY_REUSE_MISMATCH` (→ `23505`).

```
abandon_scenario_attempt_v1(
    p_attempt_id  uuid,
    p_user_email  text
) RETURNS TABLE (
    attempt_id    uuid,
    status        text  -- 'abandoned' (idempotent: also returned if already abandoned)
)
```
Error categories: `OWNERSHIP_MISMATCH`, `ATTEMPT_ALREADY_COMPLETED` (abandoning a `completed` attempt is rejected, not silently ignored — completion is a stronger terminal state than abandonment).

```
publish_scenario_version_v1(
    p_scenario_version_id  uuid,
    p_actor_email          text,
    p_content_snapshot        jsonb,  -- the exact validated ScenarioContent document Python just
                                       -- re-loaded and re-validated from the repository file at
                                       -- p_source_repository_path; becomes scenario_versions.content_snapshot
    p_verified_content_sha256 text    -- the hash Python just computed over that same reload; the
                                      -- RPC rejects publication if this doesn't match the row's own
                                      -- recorded canonical_content_sha256 (drafted-but-drifted file),
                                      -- catching drift at the last possible moment before an attempt
                                      -- could ever be created against this version (§7.2)
) RETURNS TABLE (
    scenario_version_id  uuid,
    status               text,
    retired_version_id   uuid   -- the previously-published version for this scenario, if any
)
```
Error categories: `INVALID_PARAMETER`, `CONTENT_HASH_MISMATCH`, `ALREADY_RETIRED`. Mirrors `publish_free_mock_draft_v1`'s "validate, retire the previous published row, publish the new one, all in one transaction under `pg_advisory_xact_lock`" shape (§6 of `20260629120000_v46_free_mock_curation_foundation.sql`) — renamed from `activate_scenario_version_v1` in SIM-PERSIST-01A for vocabulary consistency with the amended hybrid-content model (§7).

---

## 11. RLS design

### 11.1 Grant matrix

Repeating the central, repository-grounded finding from §2: this application has **no** `auth.uid()`-bearing Postgres session anywhere. Every read and write from the Streamlit app goes through `service_role`, which bypasses RLS unconditionally. Writing `CREATE POLICY` rules keyed on `auth.uid() = user_id` would therefore be **decorative** — they would never actually run, because no request ever reaches Postgres as `authenticated` in the first place. This repository's own convention (RLS enabled, zero policies, service-role-only grants) already reflects this reality everywhere else, and scenario persistence should not invent a different, inconsistent story.

| Table | SELECT | INSERT | UPDATE | DELETE |
|---|---|---|---|---|
| `scenarios` | `service_role` only | `service_role` only | `service_role` only | not granted |
| `scenario_versions` | `service_role` only | `service_role` only | `service_role` only (status transitions via RPC/trigger guard) | not granted |
| `scenario_attempts` | `service_role` only | `service_role` only | `service_role` only (in practice, only via `submit_scenario_decision_v1`/`abandon_scenario_attempt_v1`) | not granted |
| `scenario_decisions` | `service_role` only | `service_role` only (in practice, only via `submit_scenario_decision_v1`) | **not granted at all** (§6.4 — no legitimate path exists) | not granted (`ON DELETE CASCADE` from the FK is a dangling-row safety net, not a supported operation) |

- **`anon`**: no access to any of the four tables, in any operation, ever — **no direct grants, full stop.** Anonymous/unauthenticated learners cannot start or view attempts (matches existing app behavior — Scenario Simulator requires `require_login()`/`require_paid_access()`, same gate as every other premium feature in `utils/access_control.py`).
- **`authenticated`**: also **no direct grants**, for the same reason `anon` has none — no request in this application ever reaches Postgres carrying the `authenticated` role in the first place, so a grant to it would be dead weight, and denying it costs nothing today while closing off an entire class of "what if the anon/publishable key leaks" concerns for free.
- **Service-role/administrative access**: `scenarios`/`scenario_versions` are content-management tables, written only by an ingestion/publication workflow run by an admin (through Python, through the same service-role client, calling `publish_scenario_version_v1`) — never by learner-facing code paths. **`service_role` credentials remain server-only** — never shipped to a browser, never present in any client-side bundle or Streamlit session-state value, exactly as `create_supabase_admin_client()`/`get_supabase_admin_client()` already centralize today (`utils/access_control.py:59-67`).
- **Direct client inserts/updates — explicitly not allowed, anywhere, for `scenario_attempts` or `scenario_decisions`.** All four mutation paths for these two tables (`create_scenario_attempt_v1`, `submit_scenario_decision_v1`, `abandon_scenario_attempt_v1`, and version publication cascading into `is_active`) are RPC-only. This is a deliberate strengthening over the *existing* `exam_attempts`/`question_attempts` convention (direct `.table(...).insert()/.upsert()` calls from Python against the service-role client, ownership-checked only in the Python layer beforehand) — appropriate here specifically because Core Architecture Principle 5 calls out that "a unique constraint alone is not sufficient" for concurrency safety, which the existing simpler upsert convention does not provide and does not need to, since `exam_attempts`/`question_attempts` do not have a race-prone "advance by exactly one step, in order" contract the way scenario decisions do.

### 11.2 Ownership model — application-enforced, not end-user-RLS-enforced (resolved in SIM-PERSIST-01A)

The authentication direction was an open question in SIM-PERSIST-01; it is resolved here for V1: **defer `auth.uid()` adoption entirely and build on the current server-side, service-role application architecture**, exactly as every other learner-owned table in this codebase already does.

- Because there is no `auth.uid()`-bearing session, and adopting one is explicitly deferred (§11.3), "ownership" here is never a database policy evaluated against a Postgres session identity — it is enforced entirely at the **application layer**, in two reinforcing places:
  1. **Python only ever fetches/operates on attempts belonging to the currently authenticated user.** The trusted `user_email` is always the value returned by `utils/access_control.py::get_current_user_email()` (the signed-session-derived identity) — **never** a value read from a request body, query string, hidden form field, or any other browser-controlled input. The browser never gets to say "act as this user"; it can only act as whoever the server's own signed session already says it is.
  2. **Authoritatively and transactionally, inside `submit_scenario_decision_v1`/`abandon_scenario_attempt_v1`/`create_scenario_attempt_v1` themselves**, via the explicit `p_user_email` parameter (itself always sourced from step 1, never independently re-derived from anything the browser sent) compared against the locked row (§9.2 step 4b) — closing the TOCTOU gap that a Python-only check (as used today for `exam_attempts`) cannot close by itself.
- **Normalization**: `user_email` is trim+lowercase normalized **before both persistence and lookup** — stored pre-normalized on `scenario_attempts.user_email` (enforced by a `CHECK` constraint, §6.3) and re-normalized defensively by every RPC before any comparison, so a historical row written before normalization was enforced (should one ever exist) still compares correctly.
- **Why "application-enforced" is the precise, not merely stylistic, term**: `service_role` bypasses RLS unconditionally (§2), so **every** RLS policy this document could write for these four tables would never actually evaluate against a real request — the genuine enforcement boundary is the explicit `p_user_email` comparison inside each RPC body, not any `CREATE POLICY` statement. Calling this "RLS-enforced" would misdescribe where the real security boundary lives and could mislead a future reader into thinking a policy change alone could strengthen or weaken it.
- **No browser ever holds an anon/publishable key path to these tables**: combined with §11.1's zero grants to `anon`/`authenticated`, even a hypothetical future mistake that exposed a publishable key client-side would still hit a hard deny at the database layer — the application-layer ownership check is the primary boundary, and the grant model is an unconditional second layer, not a substitute for it.

### 11.3 Future migration path to `auth.uid()` (documented, not implemented)

This amendment documents the shape of a future move to real Supabase-Auth-issued JWTs and `auth.uid()`-scoped RLS, without building any part of it now:

1. **Adopt Supabase Auth for end-user sessions** (a separate, cross-cutting project, not scoped to Scenario Simulator alone) — this would give every request a real `auth.uid()` inside Postgres for the first time anywhere in this codebase.
2. **Add a stable auth-user-UUID column** to `scenario_attempts` (e.g. `auth_user_id uuid`) alongside — not replacing — `user_email`, populated by resolving the signed-session email to its Supabase Auth user id at attempt-creation time.
3. **Introduce real `CREATE POLICY` statements** scoped to `auth.uid() = auth_user_id` for `SELECT` on `scenario_attempts`/`scenario_decisions` (via a join/subquery to the parent attempt) — only once the application's request path has actually changed to present Postgres with an `authenticated` session bearing that `auth.uid()`, since a policy alone changes nothing while every request still arrives as `service_role`.
4. **Keep RPC-only mutation regardless** — even after adopting `auth.uid()`, the concurrency/idempotency/ordering guarantees in §9 still require a locked, transactional RPC; `auth.uid()`-scoped RLS would add a *second, independent* read-side guarantee (defense in depth for `SELECT`), not replace the RPC-based write path.
5. **Migrate incrementally, table by table, application-wide** — this is explicitly not a Scenario-Simulator-only migration; `exam_attempts`/`question_attempts` would need the identical treatment for the two ownership models to remain consistent, which is why this is documented as a future path here rather than designed and built now.

This path is deferred, not designed in detail — it exists so a future task has a starting point, not because any part of it is scheduled.

---

## 12. Idempotency rules

**Resolved in SIM-PERSIST-01A** (was partly an open decision — see former §20 item 6):

- **Key generation**: Python generates a fresh **UUIDv4** for each new logical decision submission (`uuid.uuid4()`, no deterministic/derived-key scheme). Streamlit session state retains that exact pending key across reruns and client-side retries of the *same* in-flight submission — a rerun that re-executes top-to-bottom while a submission is still pending must find and reuse the cached key, never mint a second one for what is the same logical request. The key is cleared from session state only after the RPC returns a terminal outcome (`'applied'` or `'duplicate_applied'`), so the next decision always starts from a fresh UUIDv4.
- **Key scope**: `idempotency_key` is unique **per attempt** (`UNIQUE (attempt_id, idempotency_key)` on `scenario_decisions`), not globally. A decision submission is inherently scoped to one attempt; attempt-scoped uniqueness avoids any risk of two unrelated learners' independently-generated keys colliding, and matches the natural grain of the operation being deduplicated. (Global uniqueness would also work for a random UUIDv4 in practice, but attempt-scoping is the more defensible invariant and matches the existing repository precedent below.)
- **Retried identical requests**: same `attempt_id` + same `idempotency_key` + identical `sequence_number`/`scene_id`/`option_id` → the RPC returns the original outcome (`outcome = 'duplicate_applied'`) with **no new row and no state advancement**. This is checked *inside the locked transaction, before the status/sequence/scene checks* (§9.2 step 4c precedes 4d/4e/4f) — an idempotent retry must succeed even if, by the time it arrives, the attempt has since moved on (see "Expired or abandoned attempts" below). Safe for Streamlit reruns (which can re-execute the same script top-to-bottom) and network-retry clients.
- **Same key, different payload**: same `attempt_id` + same `idempotency_key` but a different `sequence_number`/`scene_id`/`option_id` → rejected with `IDEMPOTENCY_KEY_REUSE_MISMATCH` (`23505`). Because keys are now random UUIDv4s rather than derived, this case should only arise from a genuine client defect (e.g. a session-state bug that reuses a stale cached key for a new decision) — it is treated as a client bug to be fixed, not a case to silently paper over.
- **Concurrent duplicate requests**: two requests with the same key arriving at nearly the same instant both attempt the `FOR UPDATE` lock on the attempt row (§9.2 step 4a); one blocks until the other's transaction commits or rolls back, then proceeds and hits the idempotency lookup (4c) against the now-committed row — there is no window where both can succeed as `'applied'`. This is exactly why the idempotency lookup must run *inside* the same locked transaction as the sequence/scene checks, immediately after the row lock and ownership check, and *before* the status/sequence/scene rejections — never as a separate pre-check outside the lock.
- **Global vs. per-attempt**: deliberately per-attempt, not global — see "Key scope" above. (Contrast with `billing_checkout_claims.idempotency_key`, which *is* globally unique because a checkout session is a single global Stripe-facing action, not scoped to a narrower parent row the way a decision is scoped to its attempt.)
- **How successful prior results are returned**: the full original `RETURNS TABLE` row is re-derived from the already-inserted `scenario_decisions` row plus the (unchanged) `scenario_attempts` row and returned exactly as if the write had just happened — the caller cannot distinguish "first success" from "idempotent replay" except via the `outcome` field, which is intentional (callers that don't care can ignore it).
- **Expired or abandoned attempts**: an idempotency-key lookup happens *before* the status check (4c before 4d), so a retried *already-applied* decision on a since-completed/abandoned attempt still correctly returns `'duplicate_applied'` rather than erroring, but any genuinely *new* decision submission against a terminal attempt correctly hits `ATTEMPT_NOT_IN_PROGRESS` (4d). There is no separate expiry/TTL concept for decision idempotency keys (unlike `billing_checkout_claims.expires_at`) — a decision's idempotency window is simply "for the life of this attempt," since decisions, unlike checkout sessions, are never abandoned and retried under a fresh flow.

---

## 13. Versioning and replay integrity

- **Identity fields checked**: `simulationId`, `version`, `canonicalContentSha256`, `engineVersion` — exactly the four fields `replay_serialized_run`/`_verify_serialized_identity` already check (`utils/scenario_engine.py:573-625`). The persistence layer's job is only to make sure the *content* it hands to the engine for replay is the exact content whose hash is pinned on `scenario_attempts.canonical_content_sha256`/`scenario_versions.canonical_content_sha256` — the engine itself remains the sole authority for verifying the four-field identity contract once that content is loaded.
- **Ordered decisions**: fetched from `scenario_decisions` `ORDER BY sequence_number ASC`, mapped to `ScenarioDecisionInput(sequence_number, scene_id, option_id)` (discarding the audit-only columns), and passed to `replay_scenario_run`/`deserialize_decision_history` — the `UNIQUE (attempt_id, sequence_number)` constraint plus the RPC's own gap-free enforcement (§9 step 4e) means the database can never actually hand the engine a gapped/duplicated/reordered history in the first place; `deserialize_decision_history`'s strictness is a second, independent line of defense against a bug elsewhere (e.g. a manual `psql` correction) rather than the primary guarantee.
- **Repository no longer contains a referenced scenario version**: **materially de-risked by the Option C amendment (§7).** Because runtime loads `scenario_versions.content_snapshot` from the database, not the repository file, a missing/moved/deleted repository file has **no effect on any already-published version's replay** — replay never touches the filesystem again post-publication. The only remaining exposure is a *draft*, unpublished row: if its `source_repository_path` file disappears before `publish_scenario_version_v1` is ever called, publication simply cannot proceed (the RPC's re-load step fails), which is the correct, safe outcome — nothing was ever runtime-authoritative yet. `utils/scenario_schema.py` raising `ScenarioContentError` for a missing draft file is a publication-time failure, not a replay-time one; the persistence layer must not catch and hide it, but it also never surfaces to a learner, only to whoever is attempting to publish.
- **Content hash differs** (file on disk doesn't match the recorded `canonical_content_sha256` at publication time): detected the moment `utils/scenario_schema.py::load_scenario_content` recomputes the hash inside `publish_scenario_version_v1`'s Python-side pre-check; publication fails closed (`CONTENT_HASH_MISMATCH`) and nothing is written. Post-publication, a hash mismatch can only mean the *stored* `content_snapshot` no longer matches its own recorded `canonical_content_sha256` — since the row is immutable (guard trigger, §6.2), this should be structurally impossible short of a manual superuser bypass; the application still re-verifies the hash on every load as a defense-in-depth check against exactly that scenario, per §7.2.
- **Engine version has changed**: `ENGINE_VERSION` is a single hardcoded string today (`"SCENARIO_ENGINE_V1"`). A *new* attempt always records the currently-running `ENGINE_VERSION`. An *existing* attempt's replay is attempted with whatever engine version is currently deployed; if the engine's behavior for that content ever changes in a way that would alter historical results, that must be shipped as a new `ENGINE_VERSION` string precisely so `replay_serialized_run`'s identity check trips — the recommended operational response to an engine-version bump is **not** automatic mass re-validation of every historical attempt, but a one-time, explicitly-scheduled batch job (out of scope for this task) that re-replays completed attempts and alerts on any terminal-result mismatch, mirroring what `replay_matches_run` already does for a single run.
- **A decision references a scene or option absent from the immutable version**: cannot happen for a decision that was actually accepted by `submit_scenario_decision_v1`, because Python's `apply_decision` call (§9 step 2) already validates this before the RPC is ever invoked, against the exact immutable content the attempt is bound to. It *can* theoretically happen only if the underlying content file were edited in place without a version bump (an operational violation of "versions are immutable," not a data-model gap) — replay would then raise `ScenarioRunStateError` from `apply_decision`, surfaced the same way as any other replay failure below.
- **A persisted history fails deterministic replay**: treated as a hard failure, not "silently repair corrupted data" (per the task's explicit instruction). The attempt is flagged (a `replay_verified_at`/`replay_error` pair of columns is a reasonable future addition, listed in §19) and surfaced to admins; the learner-facing behavior is "this attempt could not be loaded, contact support," never a best-effort partial reconstruction.

---

## 14. Failure and corruption handling

Summary table of the operational postures already described above, gathered in one place:

| Failure | Detection point | Response |
|---|---|---|
| Missing/renamed scenario content file | `utils/scenario_catalog.py`/`utils/scenario_schema.py` load | Hard error, no attempt creation or replay; loud logging |
| Content hash drift vs. `scenario_versions.canonical_content_sha256` | Content load, compared by the persistence layer | Hard error; never silently re-pin the hash |
| Two concurrent decision submissions on one attempt | `submit_scenario_decision_v1` row lock + sequence/scene check | Loser gets `SEQUENCE_CONFLICT`/`SCENE_CONFLICT`; client refetches and retries against true state |
| Retried request (client-side double-submit) | Idempotency-key lookup inside the same transaction | Idempotent `'duplicate_applied'` return, zero side effects |
| Reused idempotency key with different payload | Idempotency-key lookup | `23505` rejection; treated as a client defect, not repaired |
| Attempt bound to a version that is later found hash-mismatched | Replay-time content load | Hard failure surfaced to admin/support; never a best-effort partial replay |
| Engine-version bump changes historical scoring | `replay_serialized_run` identity check on next replay attempt | Rejected at replay time; scheduled, explicit, auditable batch re-validation — not automatic |
| Decision references a scene/option missing from immutable content | `apply_decision` inside the engine (before the RPC is ever called) | `ScenarioRunStateError`, decision never persisted |
| Attempt row for another learner requested | Ownership comparison in the RPC | Treated identically to "not found" (`no_data_found`) — no existence leakage |

---

## 15. Creative Studio extension points

The four tables above are intentionally narrow. Stable, low-risk extension points for the separately-designed Creative Studio work (fictional companies, characters, relationships, dialogue, emotional state, visual assets):

- **Anchor to `scenario_versions.id`, not to individual scenes/options.** Creative Studio's authored assets (a character's portrait, a company's name/logo, a dialogue line) are properties of a specific immutable content version, exactly like scenes and options already are — they belong in the repository-authoritative content document (or a sibling repository-authoritative asset manifest keyed by the same `simulation_id`/`version`), not as new foreign keys bolted onto `scenario_attempts`/`scenario_decisions`. This preserves "immutable authored assets vs. attempt-scoped mutable learner state" as a clean split.
- **If Creative Studio ever needs its own database tables** (e.g. a reusable company/character library shared across multiple scenarios, rather than embedded per-scenario-file), the natural join key is `scenarios.id` or `scenario_versions.id` — both are already stable, already-immutable-after-publication identifiers requiring no schema change here to become referenceable.
- **Attempt-scoped mutable learner state that Creative Studio might eventually want** (e.g. "which dialogue variant did this learner see," "what was the character's simulated emotional state at each decision") fits naturally as additional **audit-only** columns on `scenario_decisions` (following the existing `domain_id`/`state_after` precedent, §6.4) or as a new, separate `scenario_decision_creative_events` child table keyed by `scenario_decisions.id` — never as a required column that would block the BA-201 vertical slice from shipping without Creative Studio existing yet.
- **No speculative foreign keys are added now.** `scenarios`/`scenario_versions`/`scenario_attempts`/`scenario_decisions` reference only `certifications` and each other — nothing here assumes Creative Studio's schema shape, and nothing in Creative Studio's future schema needs any of today's four tables changed to reference it later (only additive new FKs pointing *at* today's stable ids).

---

## 16. Migration sequencing proposal

Described as future steps only — **no migration files are created by this task.**

0. **Required live preflight, run before Migration 1 is even authored (amended, SIM-PERSIST-01A — item 7):** a read-only `supabase/tests/vNN_scenario_certifications_schema_preflight.sql` script, in the exact shape of `supabase/tests/v61_platform_app_builder_catalog_schema_preflight.sql` (`information_schema` only; no writes; `RAISE EXCEPTION` naming exactly what is wrong rather than guessing). It must confirm, against the real live database: (a) `public.certifications.exam_name`'s actual type and whether it carries a `UNIQUE`/`PRIMARY KEY` constraint capable of being a real FK target for `scenarios.certification_exam_name` (§6.1) — this is the working pattern `certification_domains` already relies on, but no in-repo migration ever declared it, so it must be verified live, not assumed; and (b) `public.certifications.id`'s actual type, for the record, even though V1 does not use it as an FK target (§6.1's optional, nullable `certification_id` column). **No table in this repository's tracked migration history declares either fact today** (`supabase/migrations/20260713224500_v61_add_platform_app_builder_certification_catalog.sql:7-8`) — this preflight is how Migration 1 avoids repeating that gap for a new FK.
1. `2026071500000X_v66_scenario_persistence_foundation.sql` — `scenarios`, `scenario_versions` tables (the latter including `content_snapshot jsonb`, per the Option C amendment, §6.2/§7), constraints, indexes, RLS enable, grants. The `FOREIGN KEY (certification_exam_name) REFERENCES public.certifications(exam_name)` (or an application-validated equivalent if step 0 finds no such constraint exists live) is added only after step 0's preflight passes, exactly the way `V63`'s `information_schema` pre-check (`supabase/migrations/20260714100000_v63_widen_certification_domain_weight_to_numeric.sql:68-96`) gates its own `ALTER` on a live-verified column type rather than an assumption.
2. `..._v66_scenario_attempts_decisions_foundation.sql` — `scenario_attempts` (including the new `scenario_attempts_one_in_progress_per_user_version` partial unique index, §9.1), `scenario_decisions` tables, constraints, indexes, RLS enable, grants (additive only; no other table touched).
3. `..._v66_scenario_version_publication_rpcs.sql` — `publish_scenario_version_v1` (renamed from `activate_scenario_version_v1`) + the `BEFORE UPDATE` immutability-guard trigger for `scenario_versions`, now also guarding `content_snapshot` (§6.2).
4. `..._v66_scenario_attempt_rpcs.sql` — `create_scenario_attempt_v1` (including the existing-in-progress-attempt reuse behavior, §9.1), `abandon_scenario_attempt_v1`.
5. `..._v66_scenario_decision_submission_rpc.sql` — `submit_scenario_decision_v1` (the highest-risk migration; ships alone so it can be reviewed/tested in isolation).
6. `..._v67_scenario_ingestion_backfill.sql` *(data migration, not schema)* — inserts one `scenarios`/`scenario_versions` row for the existing BA-201 content (`business_analyst` / `ba201-sim-meridian-health-01` / `1.0.0`), setting `certification_exam_name` directly (an exact string match against exactly one `certifications.exam_name` row, fail-closed on ambiguity — no `certifications.id` resolution needed for this column per the amended §6.1 design), copying the validated document into `content_snapshot`, and publishing that version. This is the only migration that writes catalog *data* rather than schema.

Each migration follows the existing template: a header comment block (Purpose / Design rules / Safety guarantees), additive-only DDL, named constraints, explicit grants, and a paired `supabase/tests/vNN_..._verification.sql` script using the `BEGIN ... ASSERT ... ROLLBACK` convention (§17).

---

## 17. Test strategy for the future implementation

Two independent layers, matching two conventions already proven in this repository:

1. **Python unit tests** (`tests/test_scenario_persistence.py`, future) — test the *Python* bridge layer (`utils/scenario_persistence.py`) against a fake/mocked Supabase client, exactly like `tests/test_scenario_engine.py` tests the engine with pure Python objects and no network. Focus: correct RPC parameter construction from an `ApplyDecision` result, correct translation of each RPC error category into a typed Python exception, correct re-fetch-and-replay behavior on `SEQUENCE_CONFLICT`/`SCENE_CONFLICT`.
2. **SQL verification scripts** (`supabase/tests/vNN_scenario_*_verification.sql`, future) — one per migration, run as `service_role`, wrapped in `BEGIN; ... ; ROLLBACK;`, using plain PL/pgSQL `ASSERT` (no pgTAP), following `supabase/tests/v44_background_job_lifecycle_verification.sql` exactly. Required coverage for the decision-submission RPC specifically:
   - T1 create attempt → `in_progress`, `next_sequence_number = 1`
   - T2 submit decision 1 with correct expected sequence/scene → `applied`, `next_sequence_number = 2`
   - T3 retry T2's exact call (same idempotency key) → `duplicate_applied`, no new row, `next_sequence_number` unchanged
   - T4 same idempotency key, different `option_id` → rejected `23505`
   - T5 submit with stale `p_expected_sequence_number` (simulating the race loser) → `SEQUENCE_CONFLICT`
   - T6 submit with stale `p_expected_scene_id` → `SCENE_CONFLICT`
   - T7 submit against another `user_email`'s attempt → treated as not found
   - T8 submit that reaches `EVALUATE_ENDING` → attempt transitions to `completed`, `current_scene_id` becomes `NULL`, `terminal_ending_id` set
   - T9 submit on a `completed` attempt → `ATTEMPT_NOT_IN_PROGRESS`
   - T10 abandon an `in_progress` attempt, then re-call abandon → idempotent success both times
   - T11 abandon a `completed` attempt → `ATTEMPT_ALREADY_COMPLETED`
   - T12 publish a version whose `p_verified_content_sha256` doesn't match the stored hash → `CONTENT_HASH_MISMATCH`, no publication, `content_snapshot` untouched
   - T13 publish a second version for the same scenario → the first is atomically retired, the unique-published-per-scenario index never sees two published rows simultaneously
   - T14 *(new, SIM-PERSIST-01A)* attempt `create_scenario_attempt_v1` twice in a row for the same `(user_email, scenario_version_id)` with no decisions submitted in between → second call returns the first call's `attempt_id` with `created = false`, no second row
   - T15 *(new, SIM-PERSIST-01A)* two concurrent `create_scenario_attempt_v1` calls for the same `(user_email, scenario_version_id)` (simulated via two sessions racing before either commits) → exactly one row is inserted; the losing transaction's `INSERT` hits the partial unique index and the RPC returns the winner's row instead of raising to the caller
   - T16 *(new, SIM-PERSIST-01A)* attempt a decision submission with a reused `idempotency_key` from a different, unrelated attempt → succeeds independently (keys are scoped per-`attempt_id`, §12), confirming key scope is not accidentally global
3. **Cross-layer replay parity test** (Python, future) — for a fully-played attempt, assert that replaying its persisted `scenario_decisions` via `replay_scenario_run` reproduces exactly the `terminal_result` that was computed and stored as audit data at submission time, generalizing `utils/scenario_engine.py`'s own `replay_matches_run` from "one in-memory run" to "one persisted attempt."

---

## 18. Security risks and mitigations

| Risk | Mitigation |
|---|---|
| Client submits a forged/replayed decision claiming a favorable outcome | The RPC never accepts `state`/`isCorrect`/`ending` as trusted scoring input for anything other than audit storage; the true outcome is always re-derivable via `replay_scenario_run` over `scenario_decisions`, which only trusts `sequence_number`/`scene_id`/`option_id` |
| Two concurrent requests double-advance one attempt | `FOR UPDATE` row lock + expected-sequence/expected-scene verification inside one transaction (§9); no window exists between "check" and "write" |
| A learner reads or advances another learner's attempt via a guessed/leaked `attempt_id` | Every RPC re-verifies `user_email` against the locked row before any read of decision content or any write; mismatch is indistinguishable from "not found," preventing existence-leakage via error-message timing/shape |
| Idempotency key reused to smuggle a different decision under a previously-accepted request's identity | `23505` rejection on payload mismatch for a reused key (§12) |
| Anon/publishable Supabase key exposed client-side is used to read/write attempts directly | RLS enabled with zero policies denies `anon`/`authenticated` entirely; only `service_role` (never shipped to a browser) can touch any of the four tables |
| A scenario version is edited post-publication, silently changing historical scoring | `scenario_versions` immutability trigger blocks any content-relevant `UPDATE` (including `content_snapshot`) once `published`/`retired`; `canonical_content_sha256` pinned per attempt makes drift detectable even if the trigger were ever bypassed by a manual superuser action |
| An engine-behavior change silently reinterprets old attempts differently | `ENGINE_VERSION` is part of the identity contract `replay_serialized_run` already enforces; a version bump makes old-vs-new replay mismatches loud, not silent |
| Ingestion resolves the wrong certification for a scenario's `certificationExamName` | `publish_scenario_version_v1`'s hash re-verification is a content check, not a certification check — the ingestion script (future, out of scope) must fail closed if `certificationExamName` does not exactly match exactly one `certifications.exam_name` row, never fuzzy-match. Using `exam_name` (not `certifications.id`, §6.1) as the join value means this check is a simple, auditable string-equality query, not a resolved-and-cached surrogate key that could go stale |
| `service_role` bypasses RLS, so a compromised server (not just a leaked anon key) could read/write any learner's attempt | Accepted residual risk, identical to every other `service_role`-only table in this codebase today (§2) — not something this document can uniquely solve for Scenario Simulator without also solving it application-wide; mitigated only by keeping `service_role` credentials out of any client-reachable surface (§11.1) and by the same operational security practices already relied on for `exam_attempts`/`billing_events`/etc. |

---

## 19. Explicitly deferred work

- Any migration file, RPC implementation, RLS policy statement, or Python persistence module (`utils/scenario_persistence.py`) — this task, and this amendment, remain architecture/documentation only.
- Streamlit UI/pages for the Scenario Simulator.
- A `replay_verified_at`/`replay_error` audit-status pair of columns on `scenario_attempts` for tracking scheduled re-validation runs after an `ENGINE_VERSION` bump (mentioned in §13 as a reasonable future addition, not designed in detail here).
- A data-retention/account-deletion policy for `scenario_attempts`/`scenario_decisions` (mentioned in §6.3 retention notes).
- Any admin UI for scenario ingestion/publication (the `publish_scenario_version_v1` RPC contract is specified; the workflow that calls it — CLI script vs. admin page — is not designed here).
- Analytics/reporting views over `scenario_decisions`' audit-only columns (e.g. per-domain accuracy dashboards) — the columns exist to make this possible later, but no view/report is designed in this task.
- Any Creative Studio schema itself (§15 only identifies extension points).
- Batch re-validation tooling for engine-version bumps (§13).
- **Adoption of `auth.uid()`-scoped RLS / Supabase-Auth-issued JWTs** — explicitly and deliberately deferred (§11.2/§11.3); the future migration path is documented, not implemented, and is application-wide in scope (not specific to Scenario Simulator), so it is out of scope for this feature to build alone.
- **A/B testing / experiments / variants / cohorts / split testing — explicitly deferred (resolved scope, SIM-PERSIST-01A).** BA-201 V1 uses exactly one `published` scenario version at a time (§6.2's `UNIQUE ... WHERE status = 'published'` index enforces this structurally). Any `in_progress` attempt remains pinned to the exact `scenario_version_id` it was created against (immutable FK, §6.3) even if a *different* version is later published for new attempts — so "the version in flight for an existing learner" and "the version currently offered to new learners" can briefly differ across a publish event without that being A/B testing; it is simply in-flight attempts finishing out their original version, the same way any versioned system handles an in-progress session across a deploy. Relaxing the one-`published`-version-per-scenario index to support true concurrent variants is a one-line future change, not a redesign, but is not designed further here.
- **Populating the optional `scenarios.certification_id` column** (§6.1) — deferred until/unless a live `information_schema` preflight (§16 step 0) confirms `certifications.id`'s type and some other part of the application begins relying on it as a real FK target; not needed for V1, which uses `certification_exam_name` exclusively.

---

## 20. Open decisions requiring Abdel's approval

Several items that were open decisions in the SIM-PERSIST-01 revision are now **resolved** by this amendment and removed from this list (reopening policy, concurrent-attempt policy, authentication direction, A/B-testing scope, and idempotency-key generation strategy — see §8, §9.1, §11.2/§11.3, §7.2/§19, and §12 respectively). Two items remain genuinely open:

1. **`certifications.exam_name`/`certifications.id` exact live schema shape.** This amendment's research confirms `certifications`/`certification_domains` predate this repository's tracked migration history (no `CREATE TABLE` anywhere in-repo) and that **no existing table actually uses `certifications.id` as an FK target** — the one working precedent (`certification_domains`) joins by the `exam_name` text natural key (§2, §6.1). This document accordingly recommends keying `scenarios.certification_exam_name` off `exam_name`, with `certification_id` kept as an optional, unpopulated, nullable column for a possible future surrogate-key migration. **Approval needed**: confirm this natural-key-first approach (rather than blocking on resolving `certifications.id`'s type before any scenario work can proceed) is acceptable, and confirm the required live `information_schema` preflight (§16 step 0) — not a guess — is run before Migration 1 is authored, to verify `certifications.exam_name` actually carries a constraint an FK can target.
2. **Whether/when to adopt real Supabase-Auth-issued JWTs + `auth.uid()`-scoped RLS** as a defense-in-depth layer for learner-owned tables generally (not just scenario tables) — the direction for V1 is now resolved (defer, use service-role/application-enforced ownership, §11.2), and a future migration path is documented (§11.3), but the *timing* of that broader, application-wide migration remains a product/engineering-priority decision outside this document's scope.

---

## Verification performed

- Every file path cited in this amendment was confirmed to exist by direct `Read`/`Grep` inspection during this task, including the newly-cited `supabase/migrations/20260713224500_v61_add_platform_app_builder_certification_catalog.sql` (header comment lines 7-8, and its `exam_name`-keyed `INSERT`/`SELECT` logic), `supabase/tests/v61_platform_app_builder_catalog_schema_preflight.sql`, and `workers/duplicate_question_detector.py` (lines 117-120, 212-214, confirming `certification_id` is used elsewhere in this codebase as a text alias for the exam-name string, not a surrogate key) — in addition to every path already cited in the SIM-PERSIST-01 revision (`utils/scenario_schema.py`, `utils/scenario_catalog.py`, `utils/scenario_engine.py`, `utils/question_selection.py`, `utils/access_control.py`, `scenario_content/business_analyst/catalog.json`, `scenario_content/docs/ENGINEERING_BRIEF.md`, and the specific `supabase/migrations/*.sql` / `supabase/tests/*.sql` files listed in §2).
- Reviewed the full amended document end-to-end for internal consistency, specifically checking that every reference to the renamed `active`/`activated_*` → `published`/`published_*` vocabulary and `activate_scenario_version_v1` → `publish_scenario_version_v1` was updated consistently throughout (§1, §2, §5, §6.2, §6.3, §7, §8, §9, §10, §11, §13, §14, §15, §16, §17, §18, §19), and that the `certification_id` → `certification_exam_name` change was propagated the same way (§1 diagram, §2, §5 diagram, §6.1, §16, §18, §20).
- No RPC, migration, RLS policy, or Python persistence code was written — this amendment, like SIM-PERSIST-01, remains documentation-only.
- Confirmed only `scenario_content/docs/SCENARIO_PERSISTENCE_ARCHITECTURE.md` was modified by this task (see the completion report for the full `git status --short --branch` output and its interpretation).
- No `git add`/staging was performed at any point in this task.
