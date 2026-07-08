-- =============================================================================
-- V60-EXPL-03: Enforce the deterministic explanation-missing finding in
-- complete_ai_quality_audit_run_v1
-- Created : 2026-07-08 22:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Fixes a defect where a question with an objectively empty/whitespace-only
-- explanation could complete an AI quality audit run as run_status=
-- 'completed', approved=true, with zero persisted findings and no human
-- review, whenever Pass C's NORMAL_DISPUTE resolution returned
-- resolution_status=RESOLVED with an empty confirmed_finding_refs list
-- (i.e. Pass C declined to confirm the reference). Because
-- build_confirmed_findings_for_completion (worker) only ever forwards
-- Pass-C-confirmed refs to this RPC, the deterministic
-- EXPLANATION_MISSING finding derived by the worker
-- (workers/ai_quality_audit_schemas.py::derive_explanation_finding,
-- V60-EXPL-03) could be silently dropped by a single probabilistic Pass C
-- decision -- an emptiness check that should never depend on model
-- judgment.
--
-- Detection source (worker-authoritative, RPC never re-evaluates text)
-- ----------------------------------------------------------------------
-- The worker is the sole authority for the emptiness predicate
-- ``(explanation or "").strip() == ""``; this migration never inspects
-- explanation text and never duplicates that predicate in SQL. It instead
-- scans the run's own persisted Pass B proposed_findings (``v_pass_b.
-- result_json -> 'proposed_findings'``, NOT p_confirmed_findings, which
-- would already have dropped an unconfirmed ref) for the provenance shape:
--
--   finding_code = 'EXPLANATION_MISSING'
--   AND materiality = 'blocking'
--   AND metadata.deterministic_explanation_check = true
--
-- Because this scan reads Pass B's own record directly, the finding can
-- never vanish merely because Pass C omitted its finding_ref from
-- confirmed_finding_refs -- exactly the property the pre-existing
-- correctness-abstention scan (V60-PASSC-03, migration 20260708210000)
-- already relies on p_confirmed_findings for (that scan is unaffected;
-- see "Coexistence" below).
--
-- Disposition behavior
-- ---------------------
--   * Pass C confirmed the ref (finding_ref present in Pass C's
--     confirmed_finding_refs): the worker will have included it in
--     p_confirmed_findings, so it flows through the existing generic
--     confirmed-findings validation/insert loop completely unchanged --
--     persisted finding_status=open, materiality=blocking,
--     dispute_resolution_status='RESOLVED_MODEL_CONFIRMED',
--     pass_c_confirmed=true, requires_human_review=false, and the run may
--     complete normally (run_status='completed') exactly like any other
--     Pass-C-confirmed blocking finding.
--   * Pass C did NOT confirm the ref (RESOLVED with an empty or
--     non-matching confirmed_finding_refs -- the qbv1-028 defect shape):
--     the finding is inserted directly from the Pass B record via a new
--     dedicated block, tagged dispute_resolution_status=
--     'DETERMINISTIC_DEFECT_ENFORCED', pass_c_confirmed=false,
--     requires_human_review=true, and the run is rerouted to
--     run_status='inconclusive' -- it can never silently complete.
--   * Pass C resolution_status=UNRESOLVED: unchanged. The pre-existing
--     UNRESOLVED branch (migration 20260707000000) already persists this
--     finding generically, because it is a blocking Pass B proposal
--     referenced by the active dispute trigger (every blocking Pass B
--     proposal is included in the trigger's finding_refs by the worker's
--     ``_maybe_persist_blocking_defect_trigger``) -- no change needed.
--
-- Coexistence / branch ordering (explicit, single unified flow)
-- ----------------------------------------------------------------------
-- The RESOLVED-path branch structure is restructured into ONE unified flow
-- with a single terminal decision, replacing the V60-PASSC-03 abstention
-- branch's own early RETURN. This guarantees neither trigger can cause the
-- other's finding to be lost:
--   1. Detect the confirmed correctness-abstention shape in
--      p_confirmed_findings (V60-PASSC-03 predicate, unchanged) and the
--      deterministic explanation finding in v_pass_b (this migration).
--   2. Mixed-confirmation safety rule (V60-PASSC-03, unchanged in intent):
--      an abstention confirmed alongside any OTHER confirmed finding still
--      raises -- but the deterministic explanation finding's own
--      finding_ref is explicitly excluded from that "other" tally (V60-
--      EXPL-03), because it is an independent, objective defect, not a
--      competing correctness judgment; confirming both together (or the
--      abstention alongside an unconfirmed-but-enforced explanation
--      finding) is valid and must not raise.
--   3. If an abstention is present, insert it (unchanged logic/metadata).
--   4. If the deterministic explanation finding is present, insert it
--      exactly once (new), independent of confirmation state.
--   5. Insert every remaining confirmed finding in p_confirmed_findings
--      (skipping the abstention and explanation refs already handled in
--      steps 3-4) through the existing, unmodified full validation logic --
--      so e.g. a Pass-C-confirmed WRONG_ANSWER_KEY persists alongside an
--      unconfirmed deterministic explanation finding with no partial loss.
--   6. Terminal disposition: run_status='inconclusive' if an abstention was
--      present OR the explanation finding was present-but-unconfirmed;
--      otherwise run_status='completed'. Exactly one UPDATE/RETURN QUERY
--      for the whole RESOLVED path (no more per-branch early returns).
--
-- Everything else is byte-for-byte unchanged:
--   * The pre-existing Pass-C-UNRESOLVED branch is untouched.
--   * A confirmed WRONG_ANSWER_KEY / UNSUPPORTED_ANSWER /
--     MULTIPLE_DEFENSIBLE_ANSWERS (or any other non-abstention,
--     non-deterministic-explanation finding) continues through the
--     existing validation/insert loop unchanged.
--   * A warning-materiality OTHER_REVIEW_NEEDED and a general-judge
--     EXPLANATION_MISSING duplicate never reach this RPC at all -- both are
--     resolved worker-side (materiality guard / merge dedupe respectively)
--     before p_confirmed_findings or Pass B's persisted proposed_findings
--     are ever built.
--   * Publication remains blocked identically:
--     count_blocking_findings_for_question_version_v1 counts
--     materiality='blocking' AND finding_status IN ('open','accepted')
--     regardless of the owning run's run_status.
--   * No non-blocking (warning/informational) Pass B proposal handling is
--     changed by this migration.
--
-- Design rules
-- ------------
--   * Purely a function replacement (CREATE OR REPLACE FUNCTION) with the
--     exact existing signature: complete_ai_quality_audit_run_v1(uuid,
--     jsonb, jsonb) -> TABLE(uuid, text, integer, integer). No parameter,
--     return-shape, or caller-visible contract change.
--   * No table, column, index, or CHECK-constraint changes.
--   * All four existing accepted completion shapes, the terminal-state
--     guards (already-completed idempotent no-op, already-inconclusive
--     raises), and the full pre-existing UNRESOLVED-branch logic are
--     copied verbatim and unchanged. The RESOLVED-path body is
--     restructured (see above) but every individual validation rule is
--     preserved.
--   * SECURITY INVOKER, search_path, grants, and revocations are
--     unchanged from the immediately preceding migration.
-- =============================================================================


CREATE OR REPLACE FUNCTION public.complete_ai_quality_audit_run_v1(
    p_audit_run_id uuid,
    p_confirmed_findings jsonb DEFAULT '[]'::jsonb,
    p_metadata jsonb DEFAULT '{}'::jsonb
)
RETURNS TABLE (
    audit_run_id     uuid,
    run_status         text,
    finding_count        integer,
    evidence_count          integer
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    v_run_status        text;
    v_pass_a            public.audit_run_pass_results;
    v_pass_b            public.audit_run_pass_results;
    v_pass_c            public.audit_run_pass_results;
    v_trigger           public.audit_run_dispute_triggers;
    v_shape              text;
    v_resolution_status   text;
    v_proposed_refs        jsonb;
    v_confirmed_refs          jsonb;
    v_finding_count             integer := 0;
    v_evidence_count              integer := 0;

    v_fi          integer;
    v_finding     jsonb;
    v_finding_id  uuid;
    v_finding_code text;
    v_finding_type text;
    v_severity     text;
    v_materiality  text;
    v_title        text;
    v_description  text;
    v_confidence   numeric;
    v_finding_ref  text;

    v_ei        integer;
    v_evidence  jsonb;
    v_chunk_id  uuid;
    v_role      text;
    v_quote     text;
    v_relevance numeric;

    -- V60-PASSC-03: confirmed correctness-abstention detection.
    v_is_abstention           boolean;
    v_abstention_count        integer := 0;
    v_other_confirmed_count   integer := 0;

    -- V60-EXPL-03: deterministic explanation-missing detection, sourced
    -- from Pass B's own persisted proposed_findings (never re-derived in
    -- SQL, never dependent on p_confirmed_findings).
    v_det_expl_finding    jsonb;
    v_det_expl_ref        text;
    v_det_expl_confirmed  boolean := false;
    v_handled_refs        jsonb := '[]'::jsonb;
    v_needs_reroute        boolean := false;
BEGIN
    -- -------------------------------------------------------------------------
    -- Lock the run; idempotent no-op for already-completed runs; reject
    -- transitions out of the terminal inconclusive state.
    -- -------------------------------------------------------------------------
    SELECT ar.run_status INTO v_run_status
    FROM   public.audit_runs ar
    WHERE  ar.id = p_audit_run_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_run not found: %', p_audit_run_id
            USING ERRCODE = 'no_data_found';
    END IF;

    IF v_run_status = 'completed' THEN
        SELECT COUNT(af.id)::integer, COUNT(afe.id)::integer
        INTO   v_finding_count, v_evidence_count
        FROM   public.audit_findings af
        LEFT JOIN public.audit_finding_evidence afe ON afe.finding_id = af.id
        WHERE  af.audit_run_id = p_audit_run_id;

        RETURN QUERY SELECT p_audit_run_id, 'completed'::text, v_finding_count, v_evidence_count;
        RETURN;
    END IF;

    IF v_run_status = 'inconclusive' THEN
        RAISE EXCEPTION
            'audit_run % is inconclusive (terminal) and cannot transition to completed',
            p_audit_run_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    IF v_run_status NOT IN ('pending', 'running') THEN
        RAISE EXCEPTION
            'audit_run % has status %; only pending or running ai_quality runs can be completed',
            p_audit_run_id, v_run_status
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT apr.*
    INTO   v_pass_a
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = p_audit_run_id
      AND  apr.pass_code = 'A';
    SELECT apr.*
    INTO   v_pass_b
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = p_audit_run_id
      AND  apr.pass_code = 'B';
    SELECT apr.*
    INTO   v_pass_c
    FROM   public.audit_run_pass_results AS apr
    WHERE  apr.audit_run_id = p_audit_run_id
      AND  apr.pass_code = 'C';
    SELECT art.*
    INTO   v_trigger
    FROM   public.audit_run_dispute_triggers AS art
    WHERE  art.audit_run_id = p_audit_run_id;

    IF v_pass_a.status = 'failed' OR v_pass_b.status = 'failed' OR v_pass_c.status = 'failed' THEN
        RAISE EXCEPTION 'audit_run % has a failed pass; cannot be completed', p_audit_run_id
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Shape detection: exactly the four accepted completion paths.
    -- -------------------------------------------------------------------------
    IF v_pass_a.status = 'completed' AND v_pass_b.status = 'completed'
       AND v_pass_c.status = 'skipped' AND v_trigger.audit_run_id IS NULL THEN
        v_shape := 'NORMAL_NO_DISPUTE';

    ELSIF v_pass_a.status = 'completed' AND v_pass_b.status = 'completed'
          AND v_pass_c.status = 'completed'
          AND v_trigger.audit_run_id IS NOT NULL
          AND v_trigger.reason_code IN ('BLIND_ANSWER_MISMATCH', 'BLOCKING_DEFECT_PROPOSED', 'AMBIGUITY_PROPOSED', 'EVIDENCE_STORED_ANSWER_CONFLICT')
          AND v_trigger.source_pass_code = 'B'
          AND v_pass_c.result_json ->> 'resolution_type' = 'NORMAL_DISPUTE'
          AND v_pass_c.result_json -> 'substituted_for_passes' = '[]'::jsonb THEN
        v_shape := 'NORMAL_DISPUTE';

    ELSIF v_pass_a.status = 'schema_invalid' AND v_pass_a.attempt_count = 2
          AND v_pass_b.audit_run_id IS NULL
          AND v_trigger.audit_run_id IS NOT NULL
          AND v_trigger.reason_code = 'PASS_A_SCHEMA_INVALID' AND v_trigger.source_pass_code = 'A'
          AND v_pass_c.status = 'completed'
          AND v_pass_c.result_json ->> 'resolution_type' = 'PASS_A_SUBSTITUTION'
          AND v_pass_c.result_json -> 'substituted_for_passes' = '["A","B"]'::jsonb THEN
        v_shape := 'PASS_A_SUBSTITUTION';

    ELSIF v_pass_a.status = 'completed'
          AND v_pass_b.status = 'schema_invalid' AND v_pass_b.attempt_count = 2
          AND v_trigger.audit_run_id IS NOT NULL
          AND v_trigger.reason_code = 'PASS_B_SCHEMA_INVALID' AND v_trigger.source_pass_code = 'B'
          AND v_pass_c.status = 'completed'
          AND v_pass_c.result_json ->> 'resolution_type' = 'PASS_B_SUBSTITUTION'
          AND v_pass_c.result_json -> 'substituted_for_passes' = '["B"]'::jsonb THEN
        v_shape := 'PASS_B_SUBSTITUTION';

    ELSE
        RAISE EXCEPTION
            'audit_run % pass-state combination is not an accepted completion path (A=%, B=%, C=%, trigger=%)',
            p_audit_run_id, v_pass_a.status, v_pass_b.status, v_pass_c.status,
            COALESCE(v_trigger.reason_code, 'none')
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- Resolve final run status from Pass C's resolution_status (NORMAL_NO_
    -- DISPUTE never produced a contentful Pass C, so it always completes).
    -- -------------------------------------------------------------------------
    IF v_shape = 'NORMAL_NO_DISPUTE' THEN
        v_resolution_status := 'RESOLVED';
    ELSE
        v_resolution_status := v_pass_c.result_json ->> 'resolution_status';
        IF v_resolution_status NOT IN ('RESOLVED', 'UNRESOLVED') THEN
            RAISE EXCEPTION
                'Pass C resolution_status must be RESOLVED or UNRESOLVED, got: %',
                v_resolution_status
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END IF;

    IF v_resolution_status = 'UNRESOLVED' THEN
        -- ---------------------------------------------------------------------
        -- V48 fix: preserve the disputed Pass B blocking proposal(s) instead
        -- of discarding them. Only the exact finding_ref(s) recorded on this
        -- run's active dispute trigger are eligible; every other Pass B
        -- proposal (including any non-blocking finding Pass C never reviewed)
        -- is left untouched by this branch. The run still terminates as
        -- 'inconclusive', never 'completed'; the persisted finding is never
        -- 'accepted'/'resolved'/'overridden' and is never represented as
        -- Pass C-confirmed or model consensus -- it remains 'open' and is
        -- explicitly tagged as requiring human review.
        --
        -- V60-EXPL-03 note: a deterministic explanation-missing finding is
        -- already covered here unchanged -- it is a blocking Pass B proposal
        -- referenced by the active dispute trigger like any other, so it is
        -- persisted by this same generic loop with no additional code.
        -- ---------------------------------------------------------------------
        FOR v_fi IN 0 .. jsonb_array_length(COALESCE(v_pass_b.result_json -> 'proposed_findings', '[]'::jsonb)) - 1 LOOP
            v_finding     := COALESCE(v_pass_b.result_json -> 'proposed_findings', '[]'::jsonb) -> v_fi;
            v_finding_ref := TRIM(v_finding ->> 'finding_ref');
            v_materiality := TRIM(v_finding ->> 'materiality');

            -- Only the exact blocking proposal(s) referenced by the active
            -- dispute trigger are disputed; skip everything else.
            CONTINUE WHEN v_materiality IS DISTINCT FROM 'blocking';
            CONTINUE WHEN NOT (COALESCE(v_trigger.finding_refs, '[]'::jsonb) @> to_jsonb(v_finding_ref));

            v_finding_code := TRIM(v_finding ->> 'finding_code');
            v_finding_type := TRIM(v_finding ->> 'finding_type');
            v_severity     := TRIM(v_finding ->> 'severity');
            v_title        := TRIM(v_finding ->> 'title');
            v_description  := TRIM(v_finding ->> 'description');

            IF COALESCE(v_finding_code, '') = '' OR COALESCE(v_finding_type, '') = ''
               OR COALESCE(v_severity, '') = '' OR COALESCE(v_title, '') = ''
               OR COALESCE(v_description, '') = '' THEN
                RAISE EXCEPTION
                    'disputed Pass B finding (finding_ref=%) for run % is missing a required field',
                    v_finding_ref, p_audit_run_id
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            v_finding_id := gen_random_uuid();

            INSERT INTO public.audit_findings (
                id, audit_run_id, finding_code, finding_type, severity, materiality,
                finding_status, title, description, detector_name, detector_version, metadata
            ) VALUES (
                v_finding_id, p_audit_run_id, v_finding_code, v_finding_type, v_severity, 'blocking',
                'open', v_title, v_description, 'ai_quality_audit', v_shape,
                COALESCE((v_finding -> 'metadata')::jsonb, '{}'::jsonb)
                    || jsonb_build_object(
                        'finding_ref', v_finding_ref,
                        'completion_shape', v_shape,
                        'dispute_resolution_status', 'UNRESOLVED',
                        'pass_c_confirmed', false,
                        'requires_human_review', true,
                        'source_pass_code', 'B'
                    )
            );

            v_finding_count := v_finding_count + 1;

            FOR v_ei IN 0 .. jsonb_array_length(COALESCE(v_finding -> 'evidence_chunk_ids', '[]'::jsonb)) - 1 LOOP
                BEGIN
                    v_chunk_id := (COALESCE(v_finding -> 'evidence_chunk_ids', '[]'::jsonb) ->> v_ei)::uuid;
                EXCEPTION WHEN others THEN
                    RAISE EXCEPTION
                        'disputed finding (finding_ref=%) evidence_chunk_ids[%] is not a valid uuid',
                        v_finding_ref, v_ei
                        USING ERRCODE = 'invalid_parameter_value';
                END;
                IF v_chunk_id IS NULL THEN
                    RAISE EXCEPTION
                        'disputed finding (finding_ref=%) evidence_chunk_ids[%] is null',
                        v_finding_ref, v_ei
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM public.audit_run_evidence_set ares
                    WHERE  ares.audit_run_id = p_audit_run_id AND ares.resource_chunk_id = v_chunk_id
                ) THEN
                    RAISE EXCEPTION
                        'disputed finding (finding_ref=%) evidence_chunk_ids[%] (resource_chunk_id=%) is outside the frozen evidence set for run %',
                        v_finding_ref, v_ei, v_chunk_id, p_audit_run_id
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;

                INSERT INTO public.audit_finding_evidence (
                    id, finding_id, resource_chunk_id, evidence_role, metadata
                ) VALUES (
                    gen_random_uuid(), v_finding_id, v_chunk_id, 'supporting', '{}'::jsonb
                );

                v_evidence_count := v_evidence_count + 1;
            END LOOP;
        END LOOP;

        UPDATE public.audit_runs AS ar
        SET    run_status = 'inconclusive', completed_at = now()
        WHERE  ar.id = p_audit_run_id;

        RETURN QUERY SELECT p_audit_run_id, 'inconclusive'::text, v_finding_count, v_evidence_count;
        RETURN;
    END IF;

    -- -------------------------------------------------------------------------
    -- RESOLVED (or NORMAL_NO_DISPUTE): validate and insert confirmed findings.
    -- V60-EXPL-03: restructured into one unified flow (see migration header)
    -- so the correctness-abstention reroute and the deterministic
    -- explanation-missing enforcement can never cause each other's finding
    -- to be lost via an early RETURN.
    -- -------------------------------------------------------------------------
    IF jsonb_typeof(COALESCE(p_confirmed_findings, '[]'::jsonb)) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'p_confirmed_findings must be a JSON array'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Proposed finding_ref universe: Pass B for normal shapes, Pass C for
    -- substitution shapes (Pass C did the full review itself in that case).
    -- (Moved up from its original later position so both the
    -- correctness-abstention detection and the deterministic
    -- explanation-missing detection below can use v_confirmed_refs.)
    IF v_shape IN ('PASS_A_SUBSTITUTION', 'PASS_B_SUBSTITUTION') THEN
        v_proposed_refs := COALESCE(
            (SELECT jsonb_agg(elem -> 'finding_ref')
             FROM   jsonb_array_elements(COALESCE(v_pass_c.result_json -> 'proposed_findings', '[]'::jsonb)) AS elem),
            '[]'::jsonb
        );
    ELSE
        v_proposed_refs := COALESCE(
            (SELECT jsonb_agg(elem -> 'finding_ref')
             FROM   jsonb_array_elements(COALESCE(v_pass_b.result_json -> 'proposed_findings', '[]'::jsonb)) AS elem),
            '[]'::jsonb
        );
    END IF;

    v_confirmed_refs := CASE
        WHEN v_shape = 'NORMAL_NO_DISPUTE' THEN '[]'::jsonb
        ELSE COALESCE(v_pass_c.result_json -> 'confirmed_finding_refs', '[]'::jsonb)
    END;

    -- -------------------------------------------------------------------------
    -- V60-EXPL-03: detect the deterministic explanation-missing finding
    -- directly from Pass B's own persisted proposed_findings -- NEVER from
    -- p_confirmed_findings, so Pass C omitting its finding_ref can never make
    -- it disappear. The emptiness predicate itself is never re-evaluated
    -- here; only the worker-attached provenance marker is inspected:
    --   finding_code = 'EXPLANATION_MISSING'
    --   AND materiality = 'blocking'
    --   AND metadata.deterministic_explanation_check = true
    -- Only Pass B ever runs the deterministic derivation, so this is scoped
    -- to shapes with a real v_pass_b.result_json (NORMAL_NO_DISPUTE,
    -- NORMAL_DISPUTE); the substitution shapes have no Pass B result and
    -- therefore never carry this finding, consistent with substitution
    -- being explicitly out of scope for this fix.
    -- -------------------------------------------------------------------------
    v_det_expl_finding := NULL;
    IF v_pass_b.result_json IS NOT NULL THEN
        SELECT elem INTO v_det_expl_finding
        FROM   jsonb_array_elements(COALESCE(v_pass_b.result_json -> 'proposed_findings', '[]'::jsonb)) AS elem
        WHERE  TRIM(elem ->> 'finding_code') = 'EXPLANATION_MISSING'
          AND  TRIM(elem ->> 'materiality') = 'blocking'
          AND  COALESCE((elem -> 'metadata' ->> 'deterministic_explanation_check')::boolean, false)
        LIMIT 1;
    END IF;
    v_det_expl_ref := NULLIF(TRIM(v_det_expl_finding ->> 'finding_ref'), '');
    v_det_expl_confirmed := (v_det_expl_ref IS NOT NULL) AND (v_confirmed_refs @> to_jsonb(v_det_expl_ref));

    -- -------------------------------------------------------------------------
    -- V60-PASSC-03: detect a confirmed correctness-specialist abstention in
    -- p_confirmed_findings BEFORE any validation or insert on the RESOLVED
    -- path. A finding only matches when ALL THREE hold (never finding_code
    -- alone, never finding_type='correctness' alone -- both would wrongly
    -- catch warning-materiality general-judge fallbacks or genuine specific
    -- correctness defects respectively):
    --   finding_code = 'OTHER_REVIEW_NEEDED'
    --   AND materiality = 'blocking'
    --   AND metadata.correctness_detector_abstained = true
    --
    -- V60-EXPL-03: the deterministic explanation finding's own finding_ref
    -- (if confirmed) is explicitly excluded from v_other_confirmed_count --
    -- it is an independent, objective defect, not a competing correctness
    -- judgment, and must never trip the mixed-confirmation safety rule
    -- below merely because it also happens to be present/confirmed.
    -- -------------------------------------------------------------------------
    v_abstention_count := 0;
    v_other_confirmed_count := 0;
    FOR v_fi IN 0 .. jsonb_array_length(COALESCE(p_confirmed_findings, '[]'::jsonb)) - 1 LOOP
        v_finding      := COALESCE(p_confirmed_findings, '[]'::jsonb) -> v_fi;
        v_finding_ref  := TRIM(v_finding ->> 'finding_ref');
        v_finding_code := TRIM(v_finding ->> 'finding_code');
        v_materiality  := COALESCE(NULLIF(TRIM(v_finding ->> 'materiality'), ''), 'warning');
        v_is_abstention := (
            v_finding_code = 'OTHER_REVIEW_NEEDED'
            AND v_materiality = 'blocking'
            AND COALESCE((v_finding -> 'metadata' ->> 'correctness_detector_abstained')::boolean, false)
        );
        IF v_is_abstention THEN
            v_abstention_count := v_abstention_count + 1;
        ELSIF v_det_expl_ref IS NOT NULL AND v_finding_ref = v_det_expl_ref THEN
            -- Handled by the dedicated deterministic-explanation insert
            -- block below, regardless of confirmation state; excluded here
            -- so it can never be double-counted or double-inserted.
            NULL;
        ELSE
            v_other_confirmed_count := v_other_confirmed_count + 1;
        END IF;
    END LOOP;

    -- Mixed-confirmation safety rule: an abstention confirmed alongside any
    -- OTHER confirmed finding (excluding the deterministic explanation
    -- finding, per V60-EXPL-03 above) is an invalid/unvalidated upstream
    -- shape. Detected and raised before any insert or run_status update so
    -- nothing is ever partially persisted and nothing is silently discarded.
    IF v_abstention_count > 0 AND v_other_confirmed_count > 0 THEN
        RAISE EXCEPTION
            'audit_run % p_confirmed_findings mixes % correctness-specialist '
            'abstention finding(s) (finding_code=OTHER_REVIEW_NEEDED, '
            'materiality=blocking, metadata.correctness_detector_abstained=true) '
            'with % other confirmed finding(s); an abstention must be the sole '
            'confirmed finding for the run',
            p_audit_run_id, v_abstention_count, v_other_confirmed_count
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Non-negotiable human-review triggers: either one forces the run to
    -- terminate as 'inconclusive' rather than 'completed', regardless of
    -- what else is confirmed.
    v_needs_reroute := (v_abstention_count > 0)
        OR (v_det_expl_ref IS NOT NULL AND NOT v_det_expl_confirmed);

    -- ---------------------------------------------------------------------
    -- Insert the confirmed correctness-abstention finding(s), if any.
    -- Identical logic/metadata to V60-PASSC-03; no longer returns early --
    -- falls through to the shared finalization at the end of this branch so
    -- a deterministic explanation finding (or any other confirmed finding)
    -- is never lost to an early RETURN.
    -- ---------------------------------------------------------------------
    IF v_abstention_count > 0 THEN
        FOR v_fi IN 0 .. jsonb_array_length(COALESCE(p_confirmed_findings, '[]'::jsonb)) - 1 LOOP
            v_finding      := COALESCE(p_confirmed_findings, '[]'::jsonb) -> v_fi;
            v_finding_ref  := TRIM(v_finding ->> 'finding_ref');
            v_finding_code := TRIM(v_finding ->> 'finding_code');
            v_materiality  := COALESCE(NULLIF(TRIM(v_finding ->> 'materiality'), ''), 'warning');
            v_is_abstention := (
                v_finding_code = 'OTHER_REVIEW_NEEDED'
                AND v_materiality = 'blocking'
                AND COALESCE((v_finding -> 'metadata' ->> 'correctness_detector_abstained')::boolean, false)
            );
            CONTINUE WHEN NOT v_is_abstention;

            v_finding_type := TRIM(v_finding ->> 'finding_type');
            v_severity     := TRIM(v_finding ->> 'severity');
            v_title        := TRIM(v_finding ->> 'title');
            v_description  := TRIM(v_finding ->> 'description');

            IF COALESCE(v_finding_ref, '') = '' THEN
                RAISE EXCEPTION 'finding % is missing finding_ref', v_fi
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF COALESCE(v_finding_code, '') = '' OR COALESCE(v_finding_type, '') = ''
               OR COALESCE(v_severity, '') = '' OR COALESCE(v_title, '') = ''
               OR COALESCE(v_description, '') = '' THEN
                RAISE EXCEPTION
                    'confirmed correctness-abstention finding (finding_ref=%) for run % is missing a required field',
                    v_finding_ref, p_audit_run_id
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            v_finding_id := gen_random_uuid();

            INSERT INTO public.audit_findings (
                id, audit_run_id, finding_code, finding_type, severity, materiality,
                finding_status, title, description, detector_name, detector_version, metadata
            ) VALUES (
                v_finding_id, p_audit_run_id, v_finding_code, v_finding_type, v_severity, 'blocking',
                'open', v_title, v_description, 'ai_quality_audit', v_shape,
                COALESCE((v_finding -> 'metadata')::jsonb, '{}'::jsonb)
                    || jsonb_build_object(
                        'finding_ref', v_finding_ref,
                        'completion_shape', v_shape,
                        'dispute_resolution_status', 'RESOLVED_REFERENCE_BUT_SEMANTICALLY_UNRESOLVED',
                        'pass_c_reference_confirmed', true,
                        'pass_c_semantic_resolution', false,
                        'pass_c_confirmed', false,
                        'requires_human_review', true,
                        'source_pass_code', 'B'
                    )
            );

            v_finding_count := v_finding_count + 1;
            v_handled_refs := v_handled_refs || to_jsonb(v_finding_ref);

            FOR v_ei IN 0 .. jsonb_array_length(COALESCE(v_finding -> 'evidence', '[]'::jsonb)) - 1 LOOP
                v_evidence := COALESCE(v_finding -> 'evidence', '[]'::jsonb) -> v_ei;

                BEGIN
                    v_chunk_id := (v_evidence ->> 'resource_chunk_id')::uuid;
                EXCEPTION WHEN others THEN
                    RAISE EXCEPTION
                        'confirmed correctness-abstention finding (finding_ref=%) evidence % has invalid resource_chunk_id',
                        v_finding_ref, v_ei
                        USING ERRCODE = 'invalid_parameter_value';
                END;
                IF v_chunk_id IS NULL THEN
                    RAISE EXCEPTION
                        'confirmed correctness-abstention finding (finding_ref=%) evidence % is missing resource_chunk_id',
                        v_finding_ref, v_ei
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM public.audit_run_evidence_set ares
                    WHERE  ares.audit_run_id = p_audit_run_id AND ares.resource_chunk_id = v_chunk_id
                ) THEN
                    RAISE EXCEPTION
                        'confirmed correctness-abstention finding (finding_ref=%) evidence % (resource_chunk_id=%) is outside the frozen evidence set for run %',
                        v_finding_ref, v_ei, v_chunk_id, p_audit_run_id
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;

                v_role := TRIM(v_evidence ->> 'evidence_role');
                IF v_role NOT IN ('supporting', 'contradicting', 'contextual') THEN
                    RAISE EXCEPTION
                        'confirmed correctness-abstention finding (finding_ref=%) evidence % has invalid evidence_role: %',
                        v_finding_ref, v_ei, v_role
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                v_quote := v_evidence ->> 'quote_text';

                INSERT INTO public.audit_finding_evidence (
                    id, finding_id, resource_chunk_id, evidence_role, quote_text, metadata
                ) VALUES (
                    gen_random_uuid(), v_finding_id, v_chunk_id, v_role, v_quote, '{}'::jsonb
                );

                v_evidence_count := v_evidence_count + 1;
            END LOOP;
        END LOOP;
    END IF;

    -- ---------------------------------------------------------------------
    -- V60-EXPL-03: insert the deterministic explanation-missing finding
    -- exactly once, sourced from Pass B's own record (v_det_expl_finding),
    -- regardless of whether Pass C confirmed its ref. requires_human_review
    -- and dispute_resolution_status distinguish the two sub-cases; both
    -- persist identically otherwise (finding_status=open,
    -- materiality=blocking forced).
    -- ---------------------------------------------------------------------
    IF v_det_expl_ref IS NOT NULL THEN
        v_finding_code := TRIM(v_det_expl_finding ->> 'finding_code');
        v_finding_type := TRIM(v_det_expl_finding ->> 'finding_type');
        v_severity     := TRIM(v_det_expl_finding ->> 'severity');
        v_title        := TRIM(v_det_expl_finding ->> 'title');
        v_description  := TRIM(v_det_expl_finding ->> 'description');

        IF COALESCE(v_finding_code, '') = '' OR COALESCE(v_finding_type, '') = ''
           OR COALESCE(v_severity, '') = '' OR COALESCE(v_title, '') = ''
           OR COALESCE(v_description, '') = '' THEN
            RAISE EXCEPTION
                'deterministic explanation finding (finding_ref=%) for run % is missing a required field',
                v_det_expl_ref, p_audit_run_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        v_finding_id := gen_random_uuid();

        INSERT INTO public.audit_findings (
            id, audit_run_id, finding_code, finding_type, severity, materiality,
            finding_status, title, description, detector_name, detector_version, metadata
        ) VALUES (
            v_finding_id, p_audit_run_id, v_finding_code, v_finding_type, v_severity, 'blocking',
            'open', v_title, v_description, 'ai_quality_audit', v_shape,
            COALESCE((v_det_expl_finding -> 'metadata')::jsonb, '{}'::jsonb)
                || jsonb_build_object(
                    'finding_ref', v_det_expl_ref,
                    'completion_shape', v_shape,
                    'pass_c_confirmed', v_det_expl_confirmed,
                    'requires_human_review', NOT v_det_expl_confirmed,
                    'source_pass_code', 'B',
                    'dispute_resolution_status',
                        CASE WHEN v_det_expl_confirmed
                             THEN 'RESOLVED_MODEL_CONFIRMED'
                             ELSE 'DETERMINISTIC_DEFECT_ENFORCED'
                        END
                )
        );

        v_finding_count := v_finding_count + 1;
        v_handled_refs := v_handled_refs || to_jsonb(v_det_expl_ref);

        FOR v_ei IN 0 .. jsonb_array_length(COALESCE(v_det_expl_finding -> 'evidence_chunk_ids', '[]'::jsonb)) - 1 LOOP
            BEGIN
                v_chunk_id := (COALESCE(v_det_expl_finding -> 'evidence_chunk_ids', '[]'::jsonb) ->> v_ei)::uuid;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION
                    'deterministic explanation finding (finding_ref=%) evidence_chunk_ids[%] is not a valid uuid',
                    v_det_expl_ref, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END;
            IF v_chunk_id IS NULL THEN
                RAISE EXCEPTION
                    'deterministic explanation finding (finding_ref=%) evidence_chunk_ids[%] is null',
                    v_det_expl_ref, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_run_evidence_set ares
                WHERE  ares.audit_run_id = p_audit_run_id AND ares.resource_chunk_id = v_chunk_id
            ) THEN
                RAISE EXCEPTION
                    'deterministic explanation finding (finding_ref=%) evidence_chunk_ids[%] (resource_chunk_id=%) is outside the frozen evidence set for run %',
                    v_det_expl_ref, v_ei, v_chunk_id, p_audit_run_id
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            INSERT INTO public.audit_finding_evidence (
                id, finding_id, resource_chunk_id, evidence_role, metadata
            ) VALUES (
                gen_random_uuid(), v_finding_id, v_chunk_id, 'supporting', '{}'::jsonb
            );

            v_evidence_count := v_evidence_count + 1;
        END LOOP;
    END IF;

    -- ---------------------------------------------------------------------
    -- Insert every remaining confirmed finding in p_confirmed_findings
    -- (skipping the abstention and deterministic-explanation refs already
    -- handled above) through the existing, unmodified full validation
    -- logic. This is unchanged from the pre-V60-EXPL-03 generic loop except
    -- for the new CONTINUE guard against v_handled_refs.
    -- ---------------------------------------------------------------------
    FOR v_fi IN 0 .. jsonb_array_length(COALESCE(p_confirmed_findings, '[]'::jsonb)) - 1 LOOP
        v_finding      := COALESCE(p_confirmed_findings, '[]'::jsonb) -> v_fi;
        v_finding_ref  := TRIM(v_finding ->> 'finding_ref');
        v_finding_code := TRIM(v_finding ->> 'finding_code');
        v_finding_type := TRIM(v_finding ->> 'finding_type');
        v_severity     := TRIM(v_finding ->> 'severity');
        v_title        := TRIM(v_finding ->> 'title');
        v_description  := TRIM(v_finding ->> 'description');
        v_materiality  := COALESCE(NULLIF(TRIM(v_finding ->> 'materiality'), ''), 'warning');

        CONTINUE WHEN v_handled_refs @> to_jsonb(v_finding_ref);

        IF COALESCE(v_finding_ref, '') = '' THEN
            RAISE EXCEPTION 'finding % is missing finding_ref', v_fi
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF NOT (v_proposed_refs @> to_jsonb(v_finding_ref)) THEN
            RAISE EXCEPTION
                'finding % (finding_ref=%) is not present in the upstream proposed_findings for run %',
                v_fi, v_finding_ref, p_audit_run_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_finding_code, '') = '' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) is missing finding_code', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_finding_type, '') = '' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) is missing finding_type', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_severity, '') = '' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) is missing severity', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_title, '') = '' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) is missing title', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF COALESCE(v_description, '') = '' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) is missing description', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_finding_type NOT IN (
            'correctness', 'ambiguity', 'duplication', 'outdated', 'formatting',
            'coverage', 'difficulty', 'cognitive_level', 'answer_quality',
            'explanation_quality', 'source_support', 'policy', 'other'
        ) THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) has invalid finding_type: %',
                v_fi, v_finding_ref, v_finding_type
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_severity NOT IN ('info', 'low', 'medium', 'high', 'critical') THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) has invalid severity: %',
                v_fi, v_finding_ref, v_severity
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
        IF v_materiality NOT IN ('blocking', 'warning', 'informational') THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) has invalid materiality: %',
                v_fi, v_finding_ref, v_materiality
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        -- SOURCE_SUPPORT_WEAK and DOMAIN_MISALIGNMENT can never be blocking.
        IF v_finding_code IN ('SOURCE_SUPPORT_WEAK', 'DOMAIN_MISALIGNMENT') THEN
            v_materiality := 'warning';
        END IF;

        -- Blocking findings require completed Pass C confirming this exact ref.
        IF v_materiality = 'blocking' THEN
            IF v_shape = 'NORMAL_NO_DISPUTE' THEN
                RAISE EXCEPTION
                    'finding % (finding_ref=%) cannot be blocking: Pass C did not run for run %',
                    v_fi, v_finding_ref, p_audit_run_id
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF NOT (v_confirmed_refs @> to_jsonb(v_finding_ref)) THEN
                RAISE EXCEPTION
                    'finding % (finding_ref=%) is materiality=blocking but is not present in Pass C''s confirmed_finding_refs for run %',
                    v_fi, v_finding_ref, p_audit_run_id
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
        END IF;

        -- Optional confidence.
        IF (v_finding ->> 'confidence') IS NOT NULL THEN
            BEGIN
                v_confidence := (v_finding ->> 'confidence')::numeric;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION 'finding % (finding_ref=%) has non-numeric confidence', v_fi, v_finding_ref
                    USING ERRCODE = 'invalid_parameter_value';
            END;
            IF v_confidence < 0 OR v_confidence > 1 THEN
                RAISE EXCEPTION
                    'finding % (finding_ref=%) confidence must be in [0,1], got: %',
                    v_fi, v_finding_ref, v_confidence
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
        END IF;

        -- Evidence must be a JSON array and a strict subset of the run's
        -- frozen audit_run_evidence_set.
        IF (v_finding -> 'evidence') IS NOT NULL
           AND jsonb_typeof(v_finding -> 'evidence') IS DISTINCT FROM 'array' THEN
            RAISE EXCEPTION 'finding % (finding_ref=%) evidence must be a JSON array', v_fi, v_finding_ref
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        FOR v_ei IN 0 .. jsonb_array_length(COALESCE(v_finding -> 'evidence', '[]'::jsonb)) - 1 LOOP
            v_evidence := COALESCE(v_finding -> 'evidence', '[]'::jsonb) -> v_ei;

            BEGIN
                v_chunk_id := (v_evidence ->> 'resource_chunk_id')::uuid;
            EXCEPTION WHEN others THEN
                RAISE EXCEPTION 'finding % evidence % has invalid resource_chunk_id', v_fi, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END;
            IF v_chunk_id IS NULL THEN
                RAISE EXCEPTION 'finding % evidence % is missing resource_chunk_id', v_fi, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM public.audit_run_evidence_set ares
                WHERE  ares.audit_run_id = p_audit_run_id AND ares.resource_chunk_id = v_chunk_id
            ) THEN
                RAISE EXCEPTION
                    'finding % evidence % (resource_chunk_id=%) is outside the frozen evidence set for run %',
                    v_fi, v_ei, v_chunk_id, p_audit_run_id
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            v_role := TRIM(v_evidence ->> 'evidence_role');
            IF v_role NOT IN ('supporting', 'contradicting', 'contextual') THEN
                RAISE EXCEPTION 'finding % evidence % has invalid evidence_role: %', v_fi, v_ei, v_role
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            v_quote := v_evidence ->> 'quote_text';
            IF v_quote IS NOT NULL AND TRIM(v_quote) = '' THEN
                RAISE EXCEPTION 'finding % evidence % has empty quote_text', v_fi, v_ei
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
            IF (v_evidence ->> 'relevance_score') IS NOT NULL THEN
                BEGIN
                    v_relevance := (v_evidence ->> 'relevance_score')::numeric;
                EXCEPTION WHEN others THEN
                    RAISE EXCEPTION 'finding % evidence % has non-numeric relevance_score', v_fi, v_ei
                        USING ERRCODE = 'invalid_parameter_value';
                END;
                IF v_relevance < 0 OR v_relevance > 1 THEN
                    RAISE EXCEPTION 'finding % evidence % relevance_score must be in [0,1]', v_fi, v_ei
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
            END IF;
        END LOOP;

        -- Zero-evidence SOURCE_SUPPORT_WEAK requires a complete
        -- source_support_context block in metadata.
        IF v_finding_code = 'SOURCE_SUPPORT_WEAK'
           AND jsonb_array_length(COALESCE(v_finding -> 'evidence', '[]'::jsonb)) = 0 THEN
            DECLARE
                v_ctx jsonb := v_finding -> 'metadata' -> 'source_support_context';
            BEGIN
                IF v_ctx IS NULL OR jsonb_typeof(v_ctx) IS DISTINCT FROM 'object' THEN
                    RAISE EXCEPTION
                        'finding % (finding_ref=%) is a zero-evidence SOURCE_SUPPORT_WEAK and requires metadata.source_support_context',
                        v_fi, v_finding_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF NOT (v_ctx ? 'attempted_retrieval')
                   OR jsonb_typeof(v_ctx -> 'attempted_retrieval') IS DISTINCT FROM 'number'
                   OR (v_ctx ->> 'attempted_retrieval')::numeric < 0
                   OR (v_ctx ->> 'attempted_retrieval')::numeric <> floor((v_ctx ->> 'attempted_retrieval')::numeric) THEN
                    RAISE EXCEPTION
                        'finding % (finding_ref=%) source_support_context.attempted_retrieval must be a nonnegative integer',
                        v_fi, v_finding_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF COALESCE(TRIM(v_ctx ->> 'evidence_limitation'), '') = '' THEN
                    RAISE EXCEPTION
                        'finding % (finding_ref=%) source_support_context.evidence_limitation must be non-empty',
                        v_fi, v_finding_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF COALESCE(TRIM(v_ctx ->> 'proposed_technical_claim'), '') = '' THEN
                    RAISE EXCEPTION
                        'finding % (finding_ref=%) source_support_context.proposed_technical_claim must be non-empty',
                        v_fi, v_finding_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
                IF COALESCE(TRIM(v_ctx ->> 'insufficiency_reason'), '') = '' THEN
                    RAISE EXCEPTION
                        'finding % (finding_ref=%) source_support_context.insufficiency_reason must be non-empty',
                        v_fi, v_finding_ref
                        USING ERRCODE = 'invalid_parameter_value';
                END IF;
            END;
        END IF;

        v_finding_id := gen_random_uuid();

        INSERT INTO public.audit_findings (
            id, audit_run_id, finding_code, finding_type, severity, materiality,
            title, description, field_path, confidence, detector_name, detector_version, metadata
        ) VALUES (
            v_finding_id, p_audit_run_id, v_finding_code, v_finding_type, v_severity, v_materiality,
            v_title, v_description, v_finding ->> 'field_path', v_confidence,
            'ai_quality_audit', v_shape,
            COALESCE((v_finding -> 'metadata')::jsonb, '{}'::jsonb)
                || jsonb_build_object('finding_ref', v_finding_ref, 'completion_shape', v_shape)
        );

        v_finding_count := v_finding_count + 1;

        FOR v_ei IN 0 .. jsonb_array_length(COALESCE(v_finding -> 'evidence', '[]'::jsonb)) - 1 LOOP
            v_evidence  := COALESCE(v_finding -> 'evidence', '[]'::jsonb) -> v_ei;
            v_chunk_id  := (v_evidence ->> 'resource_chunk_id')::uuid;
            v_role      := TRIM(v_evidence ->> 'evidence_role');
            v_quote     := v_evidence ->> 'quote_text';
            v_relevance := (v_evidence ->> 'relevance_score')::numeric;

            INSERT INTO public.audit_finding_evidence (
                id, finding_id, resource_chunk_id, evidence_role, quote_text, relevance_score, metadata
            ) VALUES (
                gen_random_uuid(), v_finding_id, v_chunk_id, v_role, v_quote, v_relevance,
                COALESCE((v_evidence -> 'metadata')::jsonb, '{}'::jsonb)
            );

            v_evidence_count := v_evidence_count + 1;
        END LOOP;
    END LOOP;

    -- -------------------------------------------------------------------------
    -- Single terminal decision for the whole RESOLVED path (V60-EXPL-03):
    -- 'inconclusive' if a correctness abstention was confirmed OR the
    -- deterministic explanation finding was present-but-unconfirmed;
    -- otherwise 'completed'.
    -- -------------------------------------------------------------------------
    IF v_needs_reroute THEN
        UPDATE public.audit_runs AS ar
        SET    run_status = 'inconclusive', completed_at = now()
        WHERE  ar.id = p_audit_run_id;

        RETURN QUERY SELECT p_audit_run_id, 'inconclusive'::text, v_finding_count, v_evidence_count;
        RETURN;
    END IF;

    UPDATE public.audit_runs AS ar
    SET    run_status = 'completed',
           started_at = COALESCE(ar.started_at, now()),
           completed_at = now(),
           metadata = ar.metadata || COALESCE(p_metadata, '{}'::jsonb)
    WHERE  ar.id = p_audit_run_id;

    RETURN QUERY SELECT p_audit_run_id, 'completed'::text, v_finding_count, v_evidence_count;
END;
$$;

REVOKE ALL ON FUNCTION public.complete_ai_quality_audit_run_v1(uuid, jsonb, jsonb) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.complete_ai_quality_audit_run_v1(uuid, jsonb, jsonb) FROM anon;
REVOKE EXECUTE ON FUNCTION public.complete_ai_quality_audit_run_v1(uuid, jsonb, jsonb) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.complete_ai_quality_audit_run_v1(uuid, jsonb, jsonb) TO service_role;

COMMENT ON FUNCTION public.complete_ai_quality_audit_run_v1(uuid, jsonb, jsonb) IS
'Validates the run against exactly four accepted completion shapes (normal
no-dispute, normal dispute, Pass-A substitution, Pass-B substitution),
rejecting every other pass/trigger/discriminator combination. An UNRESOLVED
Pass C marks the run inconclusive and persists exactly the disputed Pass B
blocking proposal(s) referenced by the active dispute trigger as
finding_status=open, materiality=blocking, tagged with metadata
(dispute_resolution_status=UNRESOLVED, pass_c_confirmed=false,
requires_human_review=true, source_pass_code=B) -- never accepted, resolved,
or represented as Pass C-confirmed; all other Pass B proposals are left
unpersisted. On the RESOLVED (or NORMAL_NO_DISPUTE) path: (1) a confirmed
correctness-specialist abstention (finding_code=OTHER_REVIEW_NEEDED,
materiality=blocking, metadata.correctness_detector_abstained=true) found in
p_confirmed_findings alongside any other confirmed finding (excluding the
deterministic explanation finding) raises before any insert; if present
alone it is persisted finding_status=open, materiality=blocking, tagged
dispute_resolution_status=RESOLVED_REFERENCE_BUT_SEMANTICALLY_UNRESOLVED,
pass_c_reference_confirmed=true, pass_c_semantic_resolution=false,
pass_c_confirmed=false, requires_human_review=true; (2) a deterministic
explanation-missing finding (finding_code=EXPLANATION_MISSING,
materiality=blocking, metadata.deterministic_explanation_check=true) is
independently detected from Pass B''s own persisted proposed_findings
(never from p_confirmed_findings) and always persisted finding_status=open,
materiality=blocking exactly once, tagged pass_c_confirmed=true/
dispute_resolution_status=RESOLVED_MODEL_CONFIRMED when Pass C confirmed its
ref, or pass_c_confirmed=false/requires_human_review=true/
dispute_resolution_status=DETERMINISTIC_DEFECT_ENFORCED when Pass C did not
-- a confirmed abstention or an unconfirmed deterministic explanation
finding can never produce run_status=completed, and the two triggers cannot
suppress each other''s finding. Every other confirmed finding is inserted
via full validation (finding_ref present upstream, evidence a subset of
audit_run_evidence_set, SOURCE_SUPPORT_WEAK/DOMAIN_MISALIGNMENT forced to
warning materiality, blocking findings present in Pass C''s
confirmed_finding_refs). Already-completed runs are idempotent no-ops;
inconclusive runs are terminal. Execute permission: service_role only.';
