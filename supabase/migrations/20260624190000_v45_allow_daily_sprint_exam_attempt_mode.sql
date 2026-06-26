-- =============================================================================
-- V45: Allow Daily Sprint in exam_attempts.mode check constraint
-- Created : 2026-06-24 19:00:00 UTC
--
-- Purpose
-- -------
--   Daily Sprint sessions persist mode = 'Daily Sprint', but production
--   chk_exam_attempts_mode did not include that value. Replace the constraint
--   in place, preserving the verified production definition exactly and adding
--   only Daily Sprint.
-- =============================================================================

ALTER TABLE public.exam_attempts
    DROP CONSTRAINT IF EXISTS chk_exam_attempts_mode;

ALTER TABLE public.exam_attempts
    ADD CONSTRAINT chk_exam_attempts_mode
        CHECK (
            mode IS NULL
            OR mode::text = ANY (
                ARRAY[
                    'Free Mock Exam',
                    'Paid Mock Exam',
                    'Timed Mock Exam',
                    'Practice by Category',
                    'Weak Areas Practice',
                    'Daily Sprint'
                ]::text[]
            )
        );

COMMENT ON CONSTRAINT chk_exam_attempts_mode ON public.exam_attempts IS
'Allowed attempt modes for CertBound exam_attempts rows. Daily Sprint added in V45.';
