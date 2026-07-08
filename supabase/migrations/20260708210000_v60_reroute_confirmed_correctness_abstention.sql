-- =============================================================================
-- V60-PASSC-03: Reroute a confirmed correctness-specialist abstention away
-- from ordinary completion
-- Created : 2026-07-08 21:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- Fixes a defect in complete_ai_quality_audit_run_v1 where a Pass C
-- resolution_status of RESOLVED that confirms a correctness-specialist
-- abstention finding (finding_code=OTHER_REVIEW_NEEDED,
-- materiality=blocking, metadata.correctness_detector_abstained=true) was
-- treated as an ordinary confirmed blocking defect: the run completed
-- normally (run_status='completed') with no human-review marker, even
-- though the confirmed finding is itself an admission that the
-- answer-correctness question could not be resolved from evidence.
-- OTHER_REVIEW_NEEDED is an abstention/control signal, not a specific
-- defect classification -- confirming the *reference* to that signal does
-- not resolve the underlying uncertainty it represents.
--
-- New behavior
-- ------------
-- Before validating/inserting any RESOLVED-path (or NORMAL_NO_DISPUTE)
-- confirmed findings, this function now scans the entire
-- p_confirmed_findings array for the correctness-abstention shape:
--
--   finding_code = 'OTHER_REVIEW_NEEDED'
--   AND materiality = 'blocking'
--   AND metadata.correctness_detector_abstained = true (safe boolean
--       extraction via COALESCE, defaulting to false)
--
-- Deliberately NOT keyed on finding_code alone (a general-judge fallback
-- OTHER_REVIEW_NEEDED can be warning-materiality and must never trigger
-- this branch) and NOT keyed on finding_type='correctness' alone (that
-- would also match WRONG_ANSWER_KEY / UNSUPPORTED_ANSWER /
-- MULTIPLE_DEFENSIBLE_ANSWERS, which must keep completing normally).
--
-- If any confirmed finding matches this predicate:
--
--   * Mixed-confirmation safety rule: if the same p_confirmed_findings
--     array also contains any OTHER confirmed finding (abstention or not),
--     the function raises before any insert or run_status update. Nothing
--     is partially persisted and nothing is silently discarded -- the
--     caller must resubmit with the abstention as the sole confirmed
--     finding, exactly as Pass C's own NORMAL_DISPUTE contract intends
--     (the abstention is the only finding_ref eligible for confirmation
--     in this shape; a genuine specific finding confirmed together with an
--     abstention is an invalid/unvalidated upstream shape).
--   * Otherwise, the run is rerouted through the same terminal semantics
--     as the existing Pass-C-UNRESOLVED branch (migration
--     20260707000000): run_status='inconclusive', completed_at=now(), the
--     finding is persisted finding_status='open', materiality='blocking'
--     (forced), tagged with:
--       pass_c_reference_confirmed = true   (Pass C did include this ref
--                                             in confirmed_finding_refs)
--       pass_c_semantic_resolution = false  (but did not resolve the
--                                             underlying uncertainty)
--       pass_c_confirmed           = false  (backward-compatible alias
--                                             for existing consumers keyed
--                                             on this exact key, e.g.
--                                             workers/quality_benchmark_
--                                             v48_orchestration.py's
--                                             _disputed_blocking_findings)
--       requires_human_review      = true
--       source_pass_code           = 'B'
--       dispute_resolution_status  = 'RESOLVED_REFERENCE_BUT_SEMANTICALLY_UNRESOLVED'
--       completion_shape           = <detected shape>
--     Evidence is inserted from the confirmed finding's own 'evidence'
--     array (resource_chunk_id / evidence_role / quote_text), matching the
--     shape produced by the worker's _proposed_to_confirmed_finding
--     (NOT the raw Pass B proposed_findings 'evidence_chunk_ids' shape
--     used by the pre-existing UNRESOLVED branch), validated against the
--     run's frozen audit_run_evidence_set exactly like the normal
--     RESOLVED-path evidence loop.
--
-- Everything else is byte-for-byte unchanged:
--   * The pre-existing Pass-C-UNRESOLVED branch (resolution_status =
--     'UNRESOLVED') is untouched.
--   * A confirmed WRONG_ANSWER_KEY / UNSUPPORTED_ANSWER /
--     MULTIPLE_DEFENSIBLE_ANSWERS (or any other non-abstention finding)
--     continues through the existing RESOLVED-path validation/insert loop
--     and completes normally.
--   * A warning-materiality OTHER_REVIEW_NEEDED (general-judge fallback
--     code, e.g. formatting/duplication/policy/other) never matches this
--     predicate (materiality != 'blocking') and completes normally.
--   * Publication remains blocked identically to the existing UNRESOLVED
--     branch: count_blocking_findings_for_question_version_v1 counts
--     materiality='blocking' AND finding_status IN ('open','accepted')
--     regardless of the owning run's run_status.
--   * No non-blocking (warning/informational) Pass B proposal handling is
--     changed by this migration -- the separate defect where dispute paths
--     can drop a valid non-blocking finding such as SOURCE_SUPPORT_WEAK is
--     explicitly out of scope and will be addressed in a later, isolated
--     change.
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
--     raises), and the full pre-existing UNRESOLVED-branch and RESOLVED-
--     path confirmed-findings validation are copied verbatim and
--     unchanged except for the new predicate/branch inserted immediately
--     after the p_confirmed_findings array-type check.
--   * Service-role / admin access only, matching the original function.
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
    -- -------------------------------------------------------------------------
    IF jsonb_typeof(COALESCE(p_confirmed_findings, '[]'::jsonb)) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'p_confirmed_findings must be a JSON array'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- -------------------------------------------------------------------------
    -- V60-PASSC-03: detect a confirmed correctness-specialist abstention
    -- BEFORE any validation or insert on the RESOLVED path. A finding only
    -- matches when ALL THREE hold (never finding_code alone, never
    -- finding_type='correctness' alone -- both would wrongly catch
    -- warning-materiality general-judge fallbacks or genuine specific
    -- correctness defects respectively):
    --   finding_code = 'OTHER_REVIEW_NEEDED'
    --   AND materiality = 'blocking'
    --   AND metadata.correctness_detector_abstained = true
    -- -------------------------------------------------------------------------
    v_abstention_count := 0;
    v_other_confirmed_count := 0;
    FOR v_fi IN 0 .. jsonb_array_length(COALESCE(p_confirmed_findings, '[]'::jsonb)) - 1 LOOP
        v_finding      := COALESCE(p_confirmed_findings, '[]'::jsonb) -> v_fi;
        v_finding_code := TRIM(v_finding ->> 'finding_code');
        v_materiality  := COALESCE(NULLIF(TRIM(v_finding ->> 'materiality'), ''), 'warning');
        v_is_abstention := (
            v_finding_code = 'OTHER_REVIEW_NEEDED'
            AND v_materiality = 'blocking'
            AND COALESCE((v_finding -> 'metadata' ->> 'correctness_detector_abstained')::boolean, false)
        );
        IF v_is_abstention THEN
            v_abstention_count := v_abstention_count + 1;
        ELSE
            v_other_confirmed_count := v_other_confirmed_count + 1;
        END IF;
    END LOOP;

    -- Mixed-confirmation safety rule: an abstention confirmed alongside any
    -- other confirmed finding is an invalid/unvalidated upstream shape.
    -- Detected and raised before any insert or run_status update so nothing
    -- is ever partially persisted and nothing is silently discarded.
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

    IF v_abstention_count > 0 THEN
        -- ---------------------------------------------------------------------
        -- Reroute through the same terminal semantics as the pre-existing
        -- UNRESOLVED branch above: the run terminates as 'inconclusive',
        -- never 'completed'. Pass C DID reference-confirm this finding_ref
        -- (unlike the UNRESOLVED branch, where Pass C never confirmed
        -- anything) but that confirmation did not resolve the underlying
        -- evidence uncertainty the abstention represents -- the metadata
        -- below distinguishes these two provenances explicitly rather than
        -- overwriting one state with the other.
        -- ---------------------------------------------------------------------
        FOR v_fi IN 0 .. jsonb_array_length(COALESCE(p_confirmed_findings, '[]'::jsonb)) - 1 LOOP
            v_finding      := COALESCE(p_confirmed_findings, '[]'::jsonb) -> v_fi;
            v_finding_ref  := TRIM(v_finding ->> 'finding_ref');
            v_finding_code := TRIM(v_finding ->> 'finding_code');
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

        UPDATE public.audit_runs AS ar
        SET    run_status = 'inconclusive', completed_at = now()
        WHERE  ar.id = p_audit_run_id;

        RETURN QUERY SELECT p_audit_run_id, 'inconclusive'::text, v_finding_count, v_evidence_count;
        RETURN;
    END IF;

    -- Proposed finding_ref universe: Pass B for normal shapes, Pass C for
    -- substitution shapes (Pass C did the full review itself in that case).
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

    FOR v_fi IN 0 .. jsonb_array_length(COALESCE(p_confirmed_findings, '[]'::jsonb)) - 1 LOOP
        v_finding      := COALESCE(p_confirmed_findings, '[]'::jsonb) -> v_fi;
        v_finding_ref  := TRIM(v_finding ->> 'finding_ref');
        v_finding_code := TRIM(v_finding ->> 'finding_code');
        v_finding_type := TRIM(v_finding ->> 'finding_type');
        v_severity     := TRIM(v_finding ->> 'severity');
        v_title        := TRIM(v_finding ->> 'title');
        v_description  := TRIM(v_finding ->> 'description');
        v_materiality  := COALESCE(NULLIF(TRIM(v_finding ->> 'materiality'), ''), 'warning');

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
unpersisted. On the RESOLVED (or NORMAL_NO_DISPUTE) path, p_confirmed_findings
is first scanned for a correctness-specialist abstention (finding_code=
OTHER_REVIEW_NEEDED, materiality=blocking, metadata.correctness_detector_
abstained=true): if present alongside any other confirmed finding, the call
raises before any insert or run_status update (mixed confirmation is
rejected atomically); if present alone, the run is rerouted to
run_status=inconclusive and the finding is persisted finding_status=open,
materiality=blocking, tagged with metadata (dispute_resolution_status=
RESOLVED_REFERENCE_BUT_SEMANTICALLY_UNRESOLVED, pass_c_reference_confirmed=
true, pass_c_semantic_resolution=false, pass_c_confirmed=false,
requires_human_review=true, source_pass_code=B) -- a confirmed correctness
abstention can never produce run_status=completed. Otherwise, inserts only
confirmed findings whose finding_ref is present upstream, whose evidence is
a subset of audit_run_evidence_set, forces SOURCE_SUPPORT_WEAK/
DOMAIN_MISALIGNMENT to warning materiality, and requires blocking findings
to be present in Pass C''s confirmed_finding_refs. Already-completed runs
are idempotent no-ops; inconclusive runs are terminal.
Execute permission: service_role only.';
