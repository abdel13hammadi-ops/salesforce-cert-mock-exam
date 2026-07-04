-- =============================================================================
-- V48 corrective migration: retrieval_shadow_evaluations privilege correction
-- Created : 2026-07-03 20:00:00 UTC
-- Author  : CertBound automated migration
--
-- Purpose
-- -------
-- The foundation migration
-- (20260702230000_v48_retrieval_shadow_evaluations_foundation.sql, already
-- applied) issued:
--
--     GRANT SELECT, INSERT, DELETE ON TABLE public.retrieval_shadow_evaluations
--         TO service_role;
--
-- GRANT is additive: it adds the listed privileges but never removes any
-- privilege the role already held. Live inspection of the applied database
-- shows service_role holds direct ACL grants (from postgres, the table
-- owner) for SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER,
-- and MAINTAIN on this table -- broader than the intended SELECT/INSERT/
-- DELETE-only surface. This migration does not modify the foundation
-- migration (migrations are immutable once applied); it adds one small,
-- additive corrective step: REVOKE ALL from service_role, then re-GRANT
-- only the three intended privileges.
--
-- Order of operations (must run in this exact order)
-- ----------------------------------------------------
--   1. REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM
--      service_role  -- clears every existing direct ACL grant to
--      service_role, including UPDATE/TRUNCATE/REFERENCES/TRIGGER/MAINTAIN.
--   2. GRANT SELECT, INSERT, DELETE ON TABLE
--      public.retrieval_shadow_evaluations TO service_role -- restores only
--      the intended read/write/rollback surface (no UPDATE: rows are
--      deterministic, write-once Stage 1 results and are never mutated in
--      place).
--
-- What this migration does NOT do
-- --------------------------------
--   * Does not revoke anything from postgres, the table owner. Table
--     ownership and the owner's implicit full privileges are untouched.
--   * Does not touch RLS (already enabled by the foundation migration).
--   * Does not add, change, or remove any anon/authenticated grant or
--     policy (there were none, and none are added here).
--   * Does not add, modify, or drop any RPC/function.
--   * Does not alter the table's columns, constraints, or indexes.
-- =============================================================================

REVOKE ALL ON TABLE public.retrieval_shadow_evaluations FROM service_role;

GRANT SELECT, INSERT, DELETE ON TABLE public.retrieval_shadow_evaluations TO service_role;

COMMENT ON TABLE public.retrieval_shadow_evaluations IS
'Deterministic Stage 1 hybrid_question_match_v2 shadow-classification result
per (evaluation_run_id, question_version_id, proposed_retrieval_method,
schema_version), produced offline by
workers/ai_quality_audit_shadow.py::classify_question_shadow_from_replay_record.
Structurally isolated from live audit execution: no column or foreign key
references audit_runs, audit_run_dedup_keys, or audit_run_evidence_set, so
shadow sweeps can be inserted, replayed, or deleted independently without
affecting Pass A/B/C or any live evidence_set_hash. Stage 1 only: no
semantic similarity, embedding, provider-error, qualified_v2, or L3/L4
fields are present; those belong to a later additive slice.
Service-role / admin access only: no anon or authenticated RLS policies, and
table privileges are explicitly revoked from PUBLIC, anon, and authenticated.
service_role holds exactly SELECT, INSERT, DELETE (corrected by
20260703200000_v48_retrieval_shadow_evaluations_privilege_correction.sql,
which first REVOKEs ALL from service_role to clear pre-existing direct ACL
grants for UPDATE/TRUNCATE/REFERENCES/TRIGGER/MAINTAIN before re-granting
only the intended three privileges). Table owner (postgres) privileges are
unaffected.';
