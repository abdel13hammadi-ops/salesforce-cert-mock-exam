-- =============================================================================
-- V45 corrective: loader uses latest version_number, not published events
-- Created : 2026-06-24 18:00:00 UTC
--
-- Purpose
-- -------
-- Live CertBound banks have created-only question_version_events.  Replace the
-- loader RPC so it selects the highest version_number per active question
-- without requiring a published event.
-- =============================================================================

CREATE OR REPLACE FUNCTION public.list_certification_current_question_versions_v1(
    p_certification_exam_name text
)
RETURNS TABLE (
    question_version_id  uuid,
    question_id          integer,
    certification_exam_name text,
    question_text        text,
    category             text,
    version_number       integer
)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = public, pg_catalog
AS $$
    SELECT
        current_qv.id,
        q.id,
        q.exam_name,
        current_qv.question_text,
        current_qv.category,
        current_qv.version_number
    FROM   public.questions AS q
    JOIN LATERAL (
        SELECT qv.id,
               qv.question_text,
               qv.category,
               qv.version_number
        FROM   public.question_versions AS qv
        WHERE  qv.question_id = q.id
        ORDER  BY qv.version_number DESC,
                 qv.created_at DESC,
                 qv.id DESC
        LIMIT  1
    ) AS current_qv ON TRUE
    WHERE  TRIM(q.exam_name) = TRIM(p_certification_exam_name)
      AND  q.is_active = TRUE;
$$;

REVOKE ALL ON FUNCTION public.list_certification_current_question_versions_v1(text) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.list_certification_current_question_versions_v1(text) FROM anon;
REVOKE EXECUTE ON FUNCTION public.list_certification_current_question_versions_v1(text) FROM authenticated;
GRANT EXECUTE ON FUNCTION public.list_certification_current_question_versions_v1(text) TO service_role;

COMMENT ON FUNCTION public.list_certification_current_question_versions_v1(text) IS
'Returns the latest question_version row (highest version_number) for each active
live question in one certification (exam_name). Tie-breakers: created_at, id.
Does not require question_version_events.';
