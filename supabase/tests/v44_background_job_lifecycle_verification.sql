-- =============================================================================
-- V44 Phase 7D — Background Job Lifecycle Verification
-- =============================================================================
--
-- Purpose
-- -------
-- Validates the end-to-end behavior of the six background-job RPCs:
--
--   enqueue_background_job_v1
--   claim_background_job_v1
--   heartbeat_background_job_v1
--   complete_background_job_v1
--   fail_background_job_v1
--   recover_expired_background_jobs_v1
--
-- Test coverage
-- -------------
--   T1  enqueue creates a pending job
--   T2  claim transitions to leased and increments attempt_count
--   T3  heartbeat changes leased → running and extends lease
--   T4  complete transitions running → completed and clears lease fields
--   T5  complete is idempotent (re-calling returns current state, no write)
--   T6  fail with retries remaining → pending, available_at set
--   T7  fail at max_attempts → dead_letter, completed_at set
--   T8  recover expired lease with retries remaining → pending
--   T9  recover expired lease at max_attempts → dead_letter
--   T10 claim on an empty queue returns zero rows
--
-- Design notes
-- ------------
-- * All test state is created and modified within a single BEGIN block so
--   that ROLLBACK leaves no persistent rows.
-- * No pgTAP dependency.  Assertions use PL/pgSQL ASSERT (PostgreSQL ≥ 9.6).
-- * The script must be executed as service_role because the RPCs revoke
--   EXECUTE from anon and authenticated.
-- * To simulate an expired lease in T8/T9, the test script issues a direct
--   UPDATE after the RPC claim.  This is acceptable in a verification script
--   run by a DBA/service-role context and does NOT represent worker behavior.
-- =============================================================================

BEGIN;

DO $$
DECLARE
    -- Job IDs for each test scenario.
    v_job1           uuid;    -- used for T1–T5 (complete lifecycle)
    v_job2           uuid;    -- used for T6–T7 (retry then dead-letter)
    v_job3           uuid;    -- used for T8–T9 (expired-lease recovery)

    -- Scratch variables.
    v_status         text;
    v_recovered      integer;
    v_dead_letter    integer;

    -- Generic record for multi-column RPC results.
    r                record;
BEGIN
    RAISE NOTICE '== CertBound V44 Phase 7D: background job lifecycle verification ==';

    -- =========================================================================
    -- T1: enqueue_background_job_v1 creates a job in pending status
    -- =========================================================================
    RAISE NOTICE 'T1: enqueue creates pending job';

    SELECT job_id, job_status
    INTO   v_job1, v_status
    FROM   public.enqueue_background_job_v1(
        p_job_type    => 'other',
        p_created_by  => 'v44_verification_script',
        p_max_attempts => 3
    );

    ASSERT v_job1  IS NOT NULL,    'T1: job_id must be non-null';
    ASSERT v_status = 'pending',   'T1: enqueued job must have status=pending';

    -- Confirm the row exists in the table.
    SELECT job_status INTO v_status
    FROM   public.background_jobs
    WHERE  id = v_job1;

    ASSERT v_status = 'pending', 'T1: background_jobs table must reflect pending';

    -- =========================================================================
    -- T2: claim_background_job_v1 transitions to leased, increments attempt
    -- =========================================================================
    RAISE NOTICE 'T2: claim transitions to leased';

    SELECT * INTO r
    FROM   public.claim_background_job_v1(
        p_worker_id     => 'worker-verify-A',
        p_lease_seconds => 300
    );

    ASSERT r.job_id         = v_job1, 'T2: claimed job_id must equal enqueued job_id';
    ASSERT r.attempt_count  = 1,      'T2: attempt_count must be 1 after first claim';
    ASSERT r.lease_expires_at > now(), 'T2: lease_expires_at must be in the future';

    SELECT job_status INTO v_status FROM public.background_jobs WHERE id = v_job1;
    ASSERT v_status = 'leased', 'T2: table must show leased after claim';

    -- =========================================================================
    -- T3: heartbeat_background_job_v1 transitions leased → running
    -- =========================================================================
    RAISE NOTICE 'T3: heartbeat changes leased to running';

    SELECT * INTO r
    FROM   public.heartbeat_background_job_v1(
        p_job_id        => v_job1,
        p_worker_id     => 'worker-verify-A',
        p_lease_seconds => 300,
        p_checkpoint    => '{"step": 1}'::jsonb
    );

    ASSERT r.job_status       = 'running', 'T3: heartbeat must set status=running';
    ASSERT r.lease_expires_at >  now(),    'T3: extended lease must be in the future';
    ASSERT r.heartbeat_at     IS NOT NULL, 'T3: heartbeat_at must be set';

    SELECT job_status INTO v_status FROM public.background_jobs WHERE id = v_job1;
    ASSERT v_status = 'running', 'T3: table must reflect running after heartbeat';

    -- =========================================================================
    -- T4: complete_background_job_v1 transitions running → completed
    -- =========================================================================
    RAISE NOTICE 'T4: complete transitions to completed and clears lease fields';

    SELECT * INTO r
    FROM   public.complete_background_job_v1(
        p_job_id    => v_job1,
        p_worker_id => 'worker-verify-A',
        p_result    => '{"output": "verified"}'::jsonb
    );

    ASSERT r.job_status  = 'completed',    'T4: complete must set status=completed';
    ASSERT r.completed_at IS NOT NULL,     'T4: completed_at must be set';

    SELECT job_status, lease_owner, lease_expires_at, heartbeat_at
    INTO   r
    FROM   public.background_jobs
    WHERE  id = v_job1;

    ASSERT r.job_status       = 'completed', 'T4: table must reflect completed';
    ASSERT r.lease_owner      IS NULL,       'T4: lease_owner must be cleared';
    ASSERT r.lease_expires_at IS NULL,       'T4: lease_expires_at must be cleared';
    ASSERT r.heartbeat_at     IS NULL,       'T4: heartbeat_at must be cleared';

    -- =========================================================================
    -- T5: complete_background_job_v1 is idempotent
    -- =========================================================================
    RAISE NOTICE 'T5: complete is idempotent';

    SELECT job_status INTO v_status
    FROM   public.complete_background_job_v1(
        p_job_id    => v_job1,
        p_worker_id => 'worker-verify-A'
    );

    ASSERT v_status = 'completed',
        'T5: re-completing an already-completed job must return completed without error';

    -- =========================================================================
    -- T6: fail_background_job_v1 with retries remaining → pending
    -- =========================================================================
    RAISE NOTICE 'T6: fail with retries remaining returns pending';

    SELECT job_id INTO v_job2
    FROM   public.enqueue_background_job_v1(
        p_job_type    => 'other',
        p_created_by  => 'v44_verification_script',
        p_max_attempts => 3
    );

    -- Claim: attempt_count → 1; max_attempts = 3 → retry budget remains.
    PERFORM public.claim_background_job_v1('worker-verify-B', 300, NULL);

    SELECT * INTO r
    FROM   public.fail_background_job_v1(
        p_job_id              => v_job2,
        p_worker_id           => 'worker-verify-B',
        p_error_message       => 'transient error on attempt 1',
        p_retry_delay_seconds => 0
    );

    ASSERT r.job_status    = 'pending', 'T6: fail with retries remaining must return pending';
    ASSERT r.available_at >= now(),     'T6: available_at must be >= now()';
    ASSERT r.completed_at  IS NULL,     'T6: completed_at must be null for a retry';

    SELECT lease_owner, lease_expires_at
    INTO   r
    FROM   public.background_jobs WHERE id = v_job2;

    ASSERT r.lease_owner      IS NULL, 'T6: lease_owner must be cleared after fail';
    ASSERT r.lease_expires_at IS NULL, 'T6: lease_expires_at must be cleared after fail';

    -- =========================================================================
    -- T7: fail_background_job_v1 at max_attempts → dead_letter
    -- =========================================================================
    RAISE NOTICE 'T7: fail at max_attempts dead-letters the job';

    -- Claim attempt 2.
    PERFORM public.claim_background_job_v1('worker-verify-B', 300, NULL);
    -- Fail attempt 2 → pending again (1 < 3, 2 < 3 — retries remain).
    PERFORM public.fail_background_job_v1(v_job2, 'worker-verify-B', 'transient 2', 0);

    -- Claim attempt 3 (max_attempts = 3; attempt_count will be 3 after claim).
    PERFORM public.claim_background_job_v1('worker-verify-B', 300, NULL);

    SELECT * INTO r
    FROM   public.fail_background_job_v1(
        p_job_id        => v_job2,
        p_worker_id     => 'worker-verify-B',
        p_error_message => 'final failure — exhausted',
        p_retry_delay_seconds => 0
    );

    ASSERT r.job_status   = 'dead_letter', 'T7: fail at max_attempts must return dead_letter';
    ASSERT r.completed_at IS NOT NULL,     'T7: completed_at must be set for dead_letter';

    SELECT job_status INTO v_status FROM public.background_jobs WHERE id = v_job2;
    ASSERT v_status = 'dead_letter', 'T7: table must reflect dead_letter';

    -- =========================================================================
    -- T8: recover expired lease with retries remaining → pending
    -- =========================================================================
    RAISE NOTICE 'T8: recover expired lease with retries remaining → pending';

    SELECT job_id INTO v_job3
    FROM   public.enqueue_background_job_v1(
        p_job_type    => 'other',
        p_created_by  => 'v44_verification_script',
        p_max_attempts => 2
    );

    -- Claim: attempt_count → 1; max_attempts = 2 → one retry remains.
    PERFORM public.claim_background_job_v1('worker-verify-C', 300, NULL);

    -- Simulate an expired lease (direct UPDATE is acceptable in a DBA verification
    -- script; workers must never update background_jobs directly).
    UPDATE public.background_jobs
    SET    lease_expires_at = now() - interval '1 second'
    WHERE  id = v_job3;

    SELECT recovered_count, dead_letter_count
    INTO   v_recovered, v_dead_letter
    FROM   public.recover_expired_background_jobs_v1(
        p_limit               => 100,
        p_retry_delay_seconds => 0
    );

    ASSERT v_recovered   >= 1, 'T8: recovered_count must be >= 1';

    SELECT job_status INTO v_status FROM public.background_jobs WHERE id = v_job3;
    ASSERT v_status = 'pending', 'T8: recovered job must be pending';

    -- =========================================================================
    -- T9: recover expired lease at max_attempts → dead_letter
    -- =========================================================================
    RAISE NOTICE 'T9: recover expired lease at max_attempts → dead_letter';

    -- Claim again: attempt_count → 2 = max_attempts; no retries remain.
    PERFORM public.claim_background_job_v1('worker-verify-C', 300, NULL);

    -- Simulate expired lease again.
    UPDATE public.background_jobs
    SET    lease_expires_at = now() - interval '1 second'
    WHERE  id = v_job3;

    SELECT recovered_count, dead_letter_count
    INTO   v_recovered, v_dead_letter
    FROM   public.recover_expired_background_jobs_v1(
        p_limit               => 100,
        p_retry_delay_seconds => 0
    );

    ASSERT v_dead_letter >= 1, 'T9: dead_letter_count must be >= 1';

    SELECT job_status INTO v_status FROM public.background_jobs WHERE id = v_job3;
    ASSERT v_status = 'dead_letter', 'T9: exhausted recovered job must be dead_letter';

    -- =========================================================================
    -- T10: claim on an empty queue returns zero rows
    -- =========================================================================
    RAISE NOTICE 'T10: claim on empty queue returns 0 rows';

    -- All test jobs are completed or dead_letter — nothing is claimable.
    SELECT COUNT(*) INTO v_recovered
    FROM   public.claim_background_job_v1('worker-verify-empty', 60, NULL);

    ASSERT v_recovered = 0, 'T10: claim on empty queue must return 0 rows';

    RAISE NOTICE '== All 10 lifecycle assertions passed ==';
END;
$$;

ROLLBACK;

-- After ROLLBACK, no rows from this script persist in background_jobs.
