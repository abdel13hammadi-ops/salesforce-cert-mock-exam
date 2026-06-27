"""Generate the BA multi-select batch repair migration from the verified manifest."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.fixtures.ba_multiselect_repair_manifest import (  # noqa: E402
    ACTOR,
    MIGRATION_NAME,
    QUESTION_IDS,
    REPAIR_MANIFEST,
)


def _sql_text(value: str) -> str:
    return f"$txt${value}$txt$"


def _sql_array_int(values: list[int]) -> str:
    return "ARRAY[" + ", ".join(str(v) for v in values) + "]::integer[]"


def _sql_array_text(values: list[str]) -> str:
    parts = []
    for value in values:
        escaped = value.replace("'", "''")
        parts.append(f"'{escaped}'")
    return "ARRAY[" + ", ".join(parts) + "]::text[]"


def _sql_array_bool(values: list[bool]) -> str:
    return "ARRAY[" + ", ".join("true" if v else "false" for v in values) + "]::boolean[]"


def _manifest_insert_rows() -> str:
    rows = []
    for entry in REPAIR_MANIFEST:
        repaired = entry["repaired_explanation"]
        repaired_sql = "NULL" if repaired is None else _sql_text(repaired)
        rows.append(
            "        (\n"
            f"            {entry['question_id']},\n"
            f"            {_sql_text(entry['stem'])},\n"
            f"            {_sql_text(entry['explanation'])},\n"
            f"            {repaired_sql},\n"
            f"            {entry['before_select_count']},\n"
            f"            {entry['after_select_count']},\n"
            f"            {_sql_array_int(entry['option_ids'])},\n"
            f"            {_sql_array_text(entry['option_labels'])},\n"
            f"            {_sql_array_int(entry['option_orders'])},\n"
            f"            {_sql_array_text(entry['option_texts'])},\n"
            f"            {_sql_array_bool(entry['before_correct'])},\n"
            f"            {_sql_array_int(entry['after_correct_ids'])},\n"
            f"            {_sql_array_text(entry['after_correct_labels'])}\n"
            "        )"
        )
    return ",\n".join(rows)


def generate() -> str:
    ids_sql = _sql_array_int(QUESTION_IDS)
    return f"""-- =============================================================================
-- V45 production-data repair: BA multi-select answer-key batch (10 questions)
-- Created : 2026-06-24 22:00:00 UTC
--
-- Purpose
-- -------
-- Repair verified corrupted live questions 1046, 1055, 1081, 1091, 1094, 1102,
-- 1107, 1116, 1125, and 1126 where select_count and/or answer keys contradict
-- the stem and explanation. Appends immutable version 2 for each; does not
-- mutate version 1 snapshots.
--
-- Safety
-- ------
--   * All-or-nothing: aborts unless every question matches verified production
--   * Idempotent when all ten are already repaired with valid version 2
--   * One-time helper function is dropped after invocation
-- =============================================================================

CREATE OR REPLACE FUNCTION public.repair_ba_multiselect_batch_v1()
RETURNS TABLE (
    question_id         integer,
    version_number      integer,
    question_version_id uuid,
    repair_status       text
)
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
DECLARE
    c_migration_name constant text := '{MIGRATION_NAME}';
    c_actor          constant text := '{ACTOR}';
    c_question_ids   constant integer[] := {ids_sql};

    v_entry record;
    v_q                     public.questions%ROWTYPE;
    v_v1_id                 uuid;
    v_v1_hash               text;
    v_v1_question_text      text;
    v_v1_explanation        text;
    v_v1_select_count       integer;
    v_v2_id                 uuid;
    v_v2_exists             boolean;
    v_live_correct_count    integer;
    v_live_correct_labels   text[];
    v_live_option_count     integer;
    v_v1_option_count       integer;
    v_content_hash          text;
    v_idx                   integer;
    v_present_count         integer;
    v_already_repaired      integer := 0;
    v_target_explanation    text;
BEGIN
    CREATE TEMP TABLE _repair_manifest ON COMMIT DROP AS
    SELECT *
    FROM (
        VALUES
{_manifest_insert_rows()}
    ) AS m(
        question_id,
        expected_stem,
        expected_explanation,
        repaired_explanation,
        before_select_count,
        after_select_count,
        option_ids,
        option_labels,
        option_orders,
        option_texts,
        before_correct,
        after_correct_ids,
        after_correct_labels
    );

    SELECT COUNT(*)
    INTO   v_present_count
    FROM   public.questions AS q
    WHERE  q.id = ANY (c_question_ids);

    IF v_present_count = 0 THEN
        RAISE NOTICE 'repair_ba_multiselect_batch_v1 skipped: none of the batch questions are present';
        RETURN;
    END IF;

    IF v_present_count <> array_length(c_question_ids, 1) THEN
        RAISE EXCEPTION
            'repair precondition failed: batch requires all % questions, found %',
            array_length(c_question_ids, 1), v_present_count
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Phase 1: validate every question before any write.
    FOR v_entry IN
        SELECT *
        FROM   _repair_manifest
        ORDER  BY question_id
    LOOP
        SELECT q.*
        INTO   v_q
        FROM   public.questions AS q
        WHERE  q.id = v_entry.question_id
        FOR UPDATE;

        IF NOT FOUND THEN
            RAISE EXCEPTION 'repair precondition failed: question % not found', v_entry.question_id
                USING ERRCODE = 'no_data_found';
        END IF;

        SELECT qv.id,
               qv.content_hash,
               qv.question_text,
               qv.explanation,
               qv.select_count
        INTO   v_v1_id,
               v_v1_hash,
               v_v1_question_text,
               v_v1_explanation,
               v_v1_select_count
        FROM   public.question_versions AS qv
        WHERE  qv.question_id = v_entry.question_id
          AND  qv.version_number = 1;

        IF v_v1_id IS NULL THEN
            RAISE EXCEPTION 'repair precondition failed: question % is missing version 1', v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM   public.question_versions AS qv
            WHERE  qv.question_id = v_entry.question_id
              AND  qv.version_number > 2
        ) THEN
            RAISE EXCEPTION 'repair precondition failed: question % has unexpected version > 2', v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        SELECT EXISTS (
            SELECT 1
            FROM   public.question_versions AS qv
            WHERE  qv.question_id = v_entry.question_id
              AND  qv.version_number = 2
              AND  qv.select_count = v_entry.after_select_count
        )
        INTO v_v2_exists;

        SELECT COUNT(*)
        INTO   v_live_option_count
        FROM   public.answer_options AS ao
        WHERE  ao.question_id = v_entry.question_id;

        IF v_live_option_count <> array_length(v_entry.option_ids, 1) THEN
            RAISE EXCEPTION
                'repair precondition failed: question % must have exactly % live answer_options, found %',
                v_entry.question_id,
                array_length(v_entry.option_ids, 1),
                v_live_option_count
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM   public.answer_options AS ao
            WHERE  ao.question_id = v_entry.question_id
              AND  ao.id <> ALL (v_entry.option_ids)
        ) THEN
            RAISE EXCEPTION
                'repair precondition failed: question % live answer_options must be exactly ids %',
                v_entry.question_id, v_entry.option_ids
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        FOR v_idx IN 1 .. array_length(v_entry.option_ids, 1) LOOP
            IF NOT EXISTS (
                SELECT 1
                FROM   public.answer_options AS ao
                WHERE  ao.question_id = v_entry.question_id
                  AND  ao.id = v_entry.option_ids[v_idx]
                  AND  ao.option_label = v_entry.option_labels[v_idx]
                  AND  ao.option_text = v_entry.option_texts[v_idx]
                  AND  ao.display_order = v_entry.option_orders[v_idx]
                  AND  ao.is_correct IS NOT DISTINCT FROM v_entry.before_correct[v_idx]
            ) THEN
                RAISE EXCEPTION
                    'repair precondition failed: question % live option % (id %, label %, order %) must match verified text, order, and corrupted correctness',
                    v_entry.question_id,
                    v_idx,
                    v_entry.option_ids[v_idx],
                    v_entry.option_labels[v_idx],
                    v_entry.option_orders[v_idx]
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
        END LOOP;

        SELECT COUNT(*)
        INTO   v_v1_option_count
        FROM   public.question_option_versions AS qov
        WHERE  qov.question_version_id = v_v1_id;

        IF v_v1_option_count <> array_length(v_entry.option_ids, 1) THEN
            RAISE EXCEPTION
                'repair precondition failed: question % version 1 must contain exactly % option snapshots, found %',
                v_entry.question_id,
                array_length(v_entry.option_ids, 1),
                v_v1_option_count
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_v1_select_count <> v_entry.before_select_count THEN
            RAISE EXCEPTION
                'repair precondition failed: question % version 1 select_count must be %, got %',
                v_entry.question_id, v_entry.before_select_count, v_v1_select_count
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_q.question_text IS DISTINCT FROM v_entry.expected_stem THEN
            RAISE EXCEPTION
                'repair precondition failed: question % live stem must match verified production text',
                v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_q.explanation IS DISTINCT FROM v_entry.expected_explanation THEN
            RAISE EXCEPTION
                'repair precondition failed: question % live explanation must match verified production text',
                v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_v1_question_text IS DISTINCT FROM v_entry.expected_stem THEN
            RAISE EXCEPTION
                'repair precondition failed: question % version 1 stem must match verified production text',
                v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_v1_explanation IS DISTINCT FROM v_entry.expected_explanation THEN
            RAISE EXCEPTION
                'repair precondition failed: question % version 1 explanation must match verified production text',
                v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_v1_question_text IS DISTINCT FROM v_q.question_text THEN
            RAISE EXCEPTION
                'repair precondition failed: question % live stem must match immutable version 1 snapshot',
                v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_v1_explanation IS DISTINCT FROM v_q.explanation THEN
            RAISE EXCEPTION
                'repair precondition failed: question % live explanation must match immutable version 1 snapshot',
                v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        FOR v_idx IN 1 .. array_length(v_entry.option_ids, 1) LOOP
            IF NOT EXISTS (
                SELECT 1
                FROM   public.question_option_versions AS qov
                WHERE  qov.question_version_id = v_v1_id
                  AND  qov.option_label = v_entry.option_labels[v_idx]
                  AND  qov.option_text = v_entry.option_texts[v_idx]
                  AND  qov.display_order = v_entry.option_orders[v_idx]
                  AND  qov.is_correct IS NOT DISTINCT FROM v_entry.before_correct[v_idx]
            ) THEN
                RAISE EXCEPTION
                    'repair precondition failed: question % version 1 option % (label %, order %) must match verified corrupted snapshot',
                    v_entry.question_id,
                    v_idx,
                    v_entry.option_labels[v_idx],
                    v_entry.option_orders[v_idx]
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;
        END LOOP;

        SELECT COUNT(*) FILTER (WHERE ao.is_correct)
        INTO   v_live_correct_count
        FROM   public.answer_options AS ao
        WHERE  ao.question_id = v_entry.question_id;

        SELECT ARRAY_AGG(
                   ao.option_label::text
                   ORDER BY ao.display_order, ao.option_label
               )
        INTO   v_live_correct_labels
        FROM   public.answer_options AS ao
        WHERE  ao.question_id = v_entry.question_id
          AND  ao.is_correct;

        IF v_q.select_count = v_entry.after_select_count
           AND v_q.content_version = 2
           AND v_live_correct_count = v_entry.after_select_count
           AND v_live_correct_labels = v_entry.after_correct_labels THEN
            IF v_entry.repaired_explanation IS NOT NULL
               AND v_q.explanation IS DISTINCT FROM v_entry.repaired_explanation THEN
                RAISE EXCEPTION
                    'repair blocked: question % live explanation is not fully repaired',
                    v_entry.question_id
                    USING ERRCODE = 'invalid_parameter_value';
            END IF;

            IF v_v2_exists THEN
                v_already_repaired := v_already_repaired + 1;
                CONTINUE;
            END IF;

            RAISE EXCEPTION
                'repair blocked: live question % already corrected but immutable version 2 is missing',
                v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_q.question_type <> 'multiple' THEN
            RAISE EXCEPTION
                'repair precondition failed: question % question_type must be multiple, got %',
                v_entry.question_id, v_q.question_type
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF COALESCE(v_q.content_version, 0) <> 1 THEN
            RAISE EXCEPTION
                'repair precondition failed: question % content_version must be 1 before repair, got %',
                v_entry.question_id, v_q.content_version
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_q.select_count <> v_entry.before_select_count THEN
            RAISE EXCEPTION
                'repair precondition failed: question % select_count must be % before repair, got %',
                v_entry.question_id, v_entry.before_select_count, v_q.select_count
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF COALESCE(v_q.is_active, false) IS NOT TRUE THEN
            RAISE EXCEPTION
                'repair precondition failed: question % must remain active',
                v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF COALESCE(v_q.quality_status, '') <> 'approved' THEN
            RAISE EXCEPTION
                'repair precondition failed: question % quality_status must be approved, got %',
                v_entry.question_id, v_q.quality_status
                USING ERRCODE = 'invalid_parameter_value';
        END IF;

        IF v_v2_exists THEN
            RAISE EXCEPTION
                'repair blocked: question % already has version 2 but live rows remain corrupted',
                v_entry.question_id
                USING ERRCODE = 'invalid_parameter_value';
        END IF;
    END LOOP;

    IF v_already_repaired = array_length(c_question_ids, 1) THEN
        FOR v_entry IN
            SELECT *
            FROM   _repair_manifest
            ORDER  BY question_id
        LOOP
            SELECT qv.id
            INTO   v_v2_id
            FROM   public.question_versions AS qv
            WHERE  qv.question_id = v_entry.question_id
              AND  qv.version_number = 2;

            RETURN QUERY
            SELECT v_entry.question_id, 2, v_v2_id, 'already_repaired'::text;
        END LOOP;
        RETURN;
    END IF;

    IF v_already_repaired > 0 THEN
        RAISE EXCEPTION
            'repair blocked: batch contains partial repaired state (% of % already corrected)',
            v_already_repaired, array_length(c_question_ids, 1)
            USING ERRCODE = 'invalid_parameter_value';
    END IF;

    -- Phase 2: apply repairs for every question in one transaction.
    FOR v_entry IN
        SELECT *
        FROM   _repair_manifest
        ORDER  BY question_id
    LOOP
        SELECT qv.id, qv.content_hash
        INTO   v_v1_id, v_v1_hash
        FROM   public.question_versions AS qv
        WHERE  qv.question_id = v_entry.question_id
          AND  qv.version_number = 1;

        v_target_explanation := COALESCE(v_entry.repaired_explanation, v_entry.expected_explanation);

        UPDATE public.questions AS q
        SET    select_count    = v_entry.after_select_count,
               content_version = 2,
               explanation     = CASE
                   WHEN v_entry.repaired_explanation IS NOT NULL THEN v_entry.repaired_explanation
                   ELSE q.explanation
               END
        WHERE  q.id = v_entry.question_id;

        UPDATE public.answer_options AS ao
        SET    is_correct = CASE
                   WHEN ao.id = ANY (v_entry.after_correct_ids) THEN TRUE
                   ELSE FALSE
               END
        WHERE  ao.question_id = v_entry.question_id
          AND  ao.id = ANY (v_entry.option_ids);

        SELECT q.*
        INTO   v_q
        FROM   public.questions AS q
        WHERE  q.id = v_entry.question_id;

        SELECT md5(
            COALESCE(v_q.question_text,   '') || E'\\x01' ||
            COALESCE(v_q.explanation,     '') || E'\\x01' ||
            COALESCE(v_q.category,        '') || E'\\x01' ||
            COALESCE(v_q.difficulty,      '') || E'\\x01' ||
            COALESCE(v_q.question_type,   '') || E'\\x01' ||
            v_q.select_count::text            || E'\\x01' ||
            COALESCE(v_q.language_code,   '') || E'\\x01' ||
            COALESCE(v_q.cognitive_level, '') || E'\\x01' ||
            COALESCE(v_q.concept_key,     '') || E'\\x01' ||
            COALESCE(
                (
                    SELECT string_agg(
                        ao.option_label || E'\\x02' ||
                        ao.option_text  || E'\\x02' ||
                        ao.is_correct::text,
                        E'\\x03'
                        ORDER BY ao.display_order ASC, ao.option_label ASC
                    )
                    FROM public.answer_options AS ao
                    WHERE ao.question_id = v_entry.question_id
                ),
                ''
            )
        )
        INTO v_content_hash;

        v_v2_id := gen_random_uuid();

        INSERT INTO public.question_versions (
            id,
            question_id,
            version_number,
            question_text,
            explanation,
            category,
            difficulty,
            cognitive_level,
            concept_key,
            question_type,
            select_count,
            language_code,
            content_hash,
            source_type,
            created_by,
            supersedes_version_id,
            metadata
        ) VALUES (
            v_v2_id,
            v_entry.question_id,
            2,
            v_q.question_text,
            v_q.explanation,
            v_q.category,
            v_q.difficulty,
            v_q.cognitive_level,
            v_q.concept_key,
            v_q.question_type,
            v_entry.after_select_count,
            v_q.language_code,
            v_content_hash,
            'production_data_repair',
            c_actor,
            v_v1_id,
            jsonb_build_object(
                'repair_migration', c_migration_name,
                'prior_version_number', 1,
                'prior_version_id', v_v1_id,
                'prior_content_hash', v_v1_hash,
                'correct_option_labels', v_entry.after_correct_labels,
                'correct_option_ids', v_entry.after_correct_ids
            )
        );

        INSERT INTO public.question_option_versions (
            id,
            question_version_id,
            option_label,
            option_text,
            is_correct,
            display_order
        )
        SELECT
            gen_random_uuid(),
            v_v2_id,
            ao.option_label,
            ao.option_text,
            ao.is_correct,
            ao.display_order
        FROM public.answer_options AS ao
        WHERE ao.question_id = v_entry.question_id
        ORDER BY ao.display_order ASC, ao.option_label ASC;

        IF (
            SELECT COUNT(*)
            FROM   public.question_option_versions AS qov
            WHERE  qov.question_version_id = v_v2_id
        ) <> array_length(v_entry.option_ids, 1) THEN
            RAISE EXCEPTION 'repair failed: question % version 2 option snapshot count mismatch', v_entry.question_id
                USING ERRCODE = 'data_exception';
        END IF;

        IF (
            SELECT COUNT(*)
            FROM   public.question_option_versions AS qov
            WHERE  qov.question_version_id = v_v2_id
              AND  qov.is_correct
        ) <> v_entry.after_select_count THEN
            RAISE EXCEPTION 'repair failed: question % version 2 correct option count mismatch', v_entry.question_id
                USING ERRCODE = 'data_exception';
        END IF;

        INSERT INTO public.question_version_events (
            id, question_id, question_version_id, event_type, actor_email, reason, event_data
        )
        SELECT
            gen_random_uuid(),
            v_entry.question_id,
            v_v2_id,
            ev.event_type,
            ev.actor_email,
            ev.reason,
            ev.event_data
        FROM (
            VALUES
                (
                    'created'::text,
                    c_actor,
                    'Corrected immutable version 2 appended for select_count repair'::text,
                    jsonb_build_object('version_number', 2, 'source_type', 'production_data_repair')
                ),
                (
                    'approved'::text,
                    c_actor,
                    format('Approved corrected answer key for question %s', v_entry.question_id),
                    jsonb_build_object('version_number', 2, 'repair_reason', 'select_count_mismatch')
                ),
                (
                    'published'::text,
                    c_actor,
                    format('Published corrected answer key to live question %s', v_entry.question_id),
                    jsonb_build_object('version_number', 2, 'live_select_count', v_entry.after_select_count)
                ),
                (
                    'override_applied'::text,
                    c_actor,
                    'Production data repair applied for contradictory multi-select answer key'::text,
                    jsonb_build_object(
                        'migration', c_migration_name,
                        'previous_select_count', v_entry.before_select_count,
                        'new_select_count', v_entry.after_select_count,
                        'correct_option_labels', v_entry.after_correct_labels
                    )
                )
        ) AS ev(event_type, actor_email, reason, event_data)
        WHERE NOT EXISTS (
            SELECT 1
            FROM   public.question_version_events AS qve
            WHERE  qve.question_version_id = v_v2_id
              AND  qve.event_type = ev.event_type
              AND  qve.actor_email = ev.actor_email
        );

        IF NOT EXISTS (
            SELECT 1
            FROM   public.question_version_events AS qve
            WHERE  qve.question_version_id = v_v1_id
              AND  qve.event_type = 'superseded'
        ) THEN
            INSERT INTO public.question_version_events (
                id, question_id, question_version_id, event_type, actor_email, reason, event_data
            ) VALUES (
                gen_random_uuid(),
                v_entry.question_id,
                v_v1_id,
                'superseded',
                c_actor,
                'Superseded by corrected version 2',
                jsonb_build_object(
                    'superseded_by_version_id', v_v2_id,
                    'superseded_by_version_number', 2,
                    'repair_migration', c_migration_name
                )
            );
        END IF;

        IF (SELECT q.select_count FROM public.questions AS q WHERE q.id = v_entry.question_id) <> v_entry.after_select_count THEN
            RAISE EXCEPTION 'repair verification failed: question % live select_count mismatch', v_entry.question_id
                USING ERRCODE = 'data_exception';
        END IF;

        IF (
            SELECT ARRAY_AGG(
                       ao.option_label::text
                       ORDER BY ao.display_order, ao.option_label
                   )
            FROM   public.answer_options AS ao
            WHERE  ao.question_id = v_entry.question_id
              AND  ao.is_correct
        ) IS DISTINCT FROM v_entry.after_correct_labels THEN
            RAISE EXCEPTION 'repair verification failed: question % live correct options mismatch', v_entry.question_id
                USING ERRCODE = 'data_exception';
        END IF;

        IF v_entry.repaired_explanation IS NOT NULL
           AND (SELECT q.explanation FROM public.questions AS q WHERE q.id = v_entry.question_id)
               IS DISTINCT FROM v_entry.repaired_explanation THEN
            RAISE EXCEPTION 'repair verification failed: question % explanation not corrected', v_entry.question_id
                USING ERRCODE = 'data_exception';
        END IF;

        RETURN QUERY
        SELECT v_entry.question_id, 2, v_v2_id, 'repaired'::text;
    END LOOP;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM   public.questions AS q
        WHERE  q.id = ANY (ARRAY[{", ".join(str(i) for i in QUESTION_IDS)}])
    ) THEN
        RAISE NOTICE 'repair_ba_multiselect_batch_v1 skipped: batch questions not present in this database';
    ELSE
        PERFORM public.repair_ba_multiselect_batch_v1();
    END IF;
END;
$$;

DROP FUNCTION IF EXISTS public.repair_ba_multiselect_batch_v1();
"""


def main() -> None:
    output = REPO_ROOT / "supabase" / "migrations" / f"{MIGRATION_NAME}.sql"
    output.write_text(generate(), encoding="utf-8")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
