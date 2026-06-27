-- =============================================================================
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
    c_migration_name constant text := '20260624220000_v45_repair_ba_multiselect_batch';
    c_actor          constant text := 'system:v45_repair_ba_multiselect_batch';
    c_question_ids   constant integer[] := ARRAY[1046, 1055, 1081, 1091, 1094, 1102, 1107, 1116, 1125, 1126]::integer[];

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
        (
            1046,
            $txt$A Salesforce BA is kicking off discovery for a manufacturing company looking to adopt Sales Cloud. The project sponsor wants to optimize the lead-to-order pipeline. The BA notes that while senior executives believe the pipeline is operating smoothly, mid-level managers complain of major data siloes, and end-user reps frequently input data into offline files. Which two discovery steps should the BA take to uncover the true root causes of this operational mismatch? (Select TWO)$txt$,
            $txt$Uncovering mismatched operational perspectives requires direct elicitation from the users experiencing the friction. Shadowing front-line reps (C) uncovers actual daily workarounds, while interviewing managers (D) defines the specific data silo pain points. Options A and E are out of scope for a BA because they represent technical development and data architecture tasks. Option B provides macro data but fails to diagnose operational root causes.$txt$,
            NULL,
            3,
            2,
            ARRAY[4267, 4268, 4269, 4270, 4271]::integer[],
            ARRAY['A', 'B', 'C', 'D', 'E']::text[],
            ARRAY[1, 2, 3, 4, 5]::integer[],
            ARRAY['Instruct the technical lead to start designing custom Apex triggers to automate data flows between disparate systems.', 'Review high-level corporate revenue performance reports to see if the pipeline issues are impacting macro metrics.', 'Coordinate job-shadowing and observation sessions with front-line sales representatives to watch how lead data is manually captured.', 'Host focused discovery interviews with mid-level managers to define what specific data constraints are causing siloes.', 'Ask the Salesforce Architect to draft an Entity-Relationship Diagram (ERD) of the legacy CRM database structure.']::text[],
            ARRAY[true, false, true, true, false]::boolean[],
            ARRAY[4269, 4270]::integer[],
            ARRAY['C', 'D']::text[]
        ),
        (
            1055,
            $txt$A Salesforce BA is setting up requirements workshops for a multi-cloud transformation project. The BA wants to ensure that the workshops remain highly productive and avoid descending into unstructured debates. Which three management approaches should the BA execute to ensure successful stakeholder collaboration? (Select THREE)$txt$,
            $txt$Productive workshops require tight governance and active facilitation. A DACI matrix (A) sets clear roles, an agenda (B) focuses the conversation, and visual models (D) keep stakeholders grounded in business process realities. Option C creates massive decision paralysis due to group size. Option E allows scope creep to overtake the session. Option F conflates business analysis with active configuration, which disrupts elicitation.$txt$,
            NULL,
            2,
            3,
            ARRAY[4304, 4305, 4306, 4307, 4308, 4309]::integer[],
            ARRAY['A', 'B', 'C', 'D', 'E', 'F']::text[],
            ARRAY[1, 2, 3, 4, 5, 6]::integer[],
            ARRAY['Establish and share a DACI matrix for the project to clarify decision rights before the workshops begin.', 'Define a clear, itemized agenda for each workshop session and distribute it to all participants in advance.', 'Invite all 250 end-users to every session to guarantee that everyone''s voice is heard at the exact same time.', 'Utilize visual facilitation tools, like digital boards or UPN process flows, to focus conversations on business activities.', 'Allow stakeholders to completely steer the conversation off-topic to maintain high organic morale.', 'Have the Salesforce Developer build out live configurations during the session to prove technical speed.']::text[],
            ARRAY[true, true, false, false, false, false]::boolean[],
            ARRAY[4304, 4305, 4307]::integer[],
            ARRAY['A', 'B', 'D']::text[]
        ),
        (
            1081,
            $txt$During a discovery workshop for an Omni-Channel Service Cloud project, a business lead requests: "We need a way to automatically route incoming urgent tier-2 customer cases to our specialized engineering support team." To transform this into an actionable requirement, which two distinct business parameters must the BA elicit? (Select TWO)$txt$,
            $txt$To define a functional routing requirement, the BA must capture the underlying business logic: the exact criteria that trigger the escalation (B), and the skills/capacity definitions of the target agent team (C). Options A and D represent technical code naming and UI styling specifications. Option E is a hardware infrastructure concern that is completely out of scope.$txt$,
            NULL,
            4,
            2,
            ARRAY[4411, 4412, 4413, 4414, 4415]::integer[],
            ARRAY['A', 'B', 'C', 'D', 'E']::text[],
            ARRAY[1, 2, 3, 4, 5]::integer[],
            ARRAY['The specific programmatic naming conventions for the underlying Apex routing queues.', 'The explicit business rules and criteria that define what constitutes an "urgent tier-2 customer case."', 'The complete list of active service agents, including their specific skill matrices and capacity thresholds.', 'The exact CSS styling configurations for the Omni-Channel utility bar interface components.', 'The physical hardware server configurations of the company’s internal network routing switchboards.']::text[],
            ARRAY[true, true, true, true, false]::boolean[],
            ARRAY[4412, 4413]::integer[],
            ARRAY['B', 'C']::text[]
        ),
        (
            1091,
            $txt$A business analyst is managing a Salesforce product backlog. They need to ensure that all user stories meet the formal "Definition of Ready" (DoR) before allowing them to be planned into an active development sprint cycle. Which two criteria must be satisfied to meet this definition? (Select TWO)$txt$,
            $txt$A user story meets the Definition of Ready (DoR) when it is functionally mature enough for a developer to build without encountering basic requirement blockers. This requires clear, testable acceptance criteria (A) and an estimation of effort by the development team (C). Options B and D occur during and after the development sprint. Option E is a macro-level project funding concern.$txt$,
            NULL,
            3,
            2,
            ARRAY[4452, 4453, 4454, 4455, 4456]::integer[],
            ARRAY['A', 'B', 'C', 'D', 'E']::text[],
            ARRAY[1, 2, 3, 4, 5]::integer[],
            ARRAY['The user story must feature explicit, testable Acceptance Criteria agreed upon by the business stakeholders.', 'The user story must have its underlying custom Apex code classes fully drafted and saved inside a scratch sandbox org.', 'The development and QA teams must have reviewed the story and provided an estimated effort sizing (e.g., story points).', 'The user story must have already been successfully executed and validated by business end-users inside the production org.', 'The user story must be signed off by the enterprise''s chief financial officer to verify project budget alignment.']::text[],
            ARRAY[true, false, true, true, false]::boolean[],
            ARRAY[4452, 4454]::integer[],
            ARRAY['A', 'C']::text[]
        ),
        (
            1094,
            $txt$A Salesforce Business Analyst is compiling user stories for a Service Cloud implementation. The BA wants to ensure that each story package provides the technical delivery team with adequate context. Which three distinct components should be included within a fully refined user story package? (Select THREE)$txt$,
            $txt$A complete, refined user story package contains the core business narrative (A), explicit testing boundaries via acceptance criteria (B), and supporting visual context models (C) to guide implementation. Options D, E, and F represent confidential HR data, out-of-scope technical engineering syntax, and macro corporate finance reports, none of which belong in a functional user story package.$txt$,
            NULL,
            2,
            3,
            ARRAY[4465, 4466, 4467, 4468, 4469, 4470]::integer[],
            ARRAY['A', 'B', 'C', 'D', 'E', 'F']::text[],
            ARRAY[1, 2, 3, 4, 5, 6]::integer[],
            ARRAY['A structured user narrative detailing the target persona, desired capability, and business value.', 'A set of clear, testable Acceptance Criteria written in a behavior-driven format like Given-When-Then.', 'Supporting visual context artifacts, such as a UPN business process flow snippet or a low-fidelity UI wireframe layout sketch.', 'The individual salaries and hourly resource billing rates of the developers assigned to the sprint.', 'The exact programmatic SQL or Apex code text needed to execute database triggers on the Case object.', 'A copy of the company''s annual corporate financial report.']::text[],
            ARRAY[true, true, false, false, false, false]::boolean[],
            ARRAY[4465, 4466, 4467]::integer[],
            ARRAY['A', 'B', 'C']::text[]
        ),
        (
            1102,
            $txt$A Salesforce Business Analyst is preparing a business unit for an upcoming User Acceptance Testing (UAT) phase. To ensure that the testing outcomes are legally and operationally sound, which two distinct metrics or components should be defined inside every UAT test script? (Select TWO)$txt$,
            $txt$A functional UAT test script requires clear step-by-step business instructions (B) so the user knows what to test, and a defined Expected Result (C) so the user can objectively confirm whether the system passed or failed. Options A, D, and E represent technical engineering and performance metrics that are out of scope for business user acceptance validation.$txt$,
            NULL,
            4,
            2,
            ARRAY[4499, 4500, 4501, 4502, 4503]::integer[],
            ARRAY['A', 'B', 'C', 'D', 'E']::text[],
            ARRAY[1, 2, 3, 4, 5]::integer[],
            ARRAY['The exact name of the Apex developer who wrote the underlying custom code.', 'A clear, step-by-step description of the business action the user must execute.', 'The specific, measurable "Expected Result" or business state change that confirms the system is working correctly.', 'The precise database server response time measured in milliseconds.', 'The total lines of custom JavaScript code executing behind the page layout canvas.']::text[],
            ARRAY[true, true, true, true, false]::boolean[],
            ARRAY[4500, 4501]::integer[],
            ARRAY['B', 'C']::text[]
        ),
        (
            1107,
            $txt$A Salesforce BA is kicking off discovery for a corporate wellness enterprise deploying Health Cloud. The VP of Operations wants to streamline the patient intake lifecycle. The BA observes that senior executives believe the intake process takes less than 10 minutes, but front-line clinic coordinators complain of system crashes, and patient check-in sheets are regularly processed on paper first. Which two discovery actions should the BA take to identify the real friction points? (Select TWO)$txt$,
            $txt$When executive perception differs from reality, direct observation (A) and targeted workshops with operational users (C) are the most effective ways for a BA to uncover the actual current-state challenges. Option B is an engineering task that jumps straight to solutioning. Option D is a technical architecture task. Option E provides high-level financial data but does not diagnose real operational bottlenecks.$txt$,
            NULL,
            3,
            2,
            ARRAY[4520, 4521, 4522, 4523, 4524]::integer[],
            ARRAY['A', 'B', 'C', 'D', 'E']::text[],
            ARRAY[1, 2, 3, 4, 5]::integer[],
            ARRAY['Coordinate a series of active job-shadowing sessions with front-line clinic coordinators to document the step-by-step physical check-in process.', 'Instruct the technical lead to build an Apex trigger that prevents records from being saved with missing patient information.', 'Conduct targeted discovery workshops with clinic coordinators to document specific software and operational pain points during intake.', 'Ask the Lead Architect to draft a detailed Salesforce schema diagram to evaluate the current custom metadata boundaries.', 'Extract macro financial billing reports to review the university''s quarterly operational overhead trends.']::text[],
            ARRAY[true, false, true, true, false]::boolean[],
            ARRAY[4520, 4522]::integer[],
            ARRAY['A', 'C']::text[]
        ),
        (
            1116,
            $txt$A Salesforce BA is designing a requirements gathering strategy for a large-scale Field Service Cloud rollout. The BA wants to ensure that the upcoming collaboration workshops remain highly focused and productive. Which three actions should the BA take? (Select THREE)$txt$,
            $txt$Productive workshops require tight governance and active facilitation. A DACI matrix (A) sets clear roles, an agenda (B) focuses the conversation, and visual models (D) keep stakeholders grounded in business process realities. Option C creates massive decision paralysis due to group size. Option E allows scope creep to overtake the session. Option F conflates business analysis with active configuration, which disrupts elicitation.$txt$,
            NULL,
            2,
            3,
            ARRAY[4557, 4558, 4559, 4560, 4561, 4562]::integer[],
            ARRAY['A', 'B', 'C', 'D', 'E', 'F']::text[],
            ARRAY[1, 2, 3, 4, 5, 6]::integer[],
            ARRAY['Establish and distribute a project DACI matrix to clarify decision rights before the workshops begin.', 'Create an itemized workshop agenda and share it with all invitees well in advance.', 'Invite all 400 field service technicians to every session to guarantee broad representation.', 'Use visual models, such as UPN process flows, to keep conversations focused on business activities.', 'Allow stakeholders to completely steer the conversation off-topic to maintain high organic morale.', 'Direct the Salesforce Developer to build live configurations in production during the workshop.']::text[],
            ARRAY[true, true, false, false, false, false]::boolean[],
            ARRAY[4557, 4558, 4560]::integer[],
            ARRAY['A', 'B', 'D']::text[]
        ),
        (
            1125,
            $txt$During a requirements elicitation workshop for a new Service Cloud rollout, the customer service team expresses deep frustration with their current legacy system's case escalation process. The service agents can explain what triggers an escalation but struggle to articulate how cases flow between different tier levels or where bottlenecks occur. The BA wants to document this visually to ensure a shared understanding before mapping the future state. Which two design choices align with Universal Process Notation (UPN) standards for mapping this process? (Select TWO)$txt$,
            $txt$According to Salesforce UPN standards, a valid process diagram must maintain a strict structure where each activity box clearly communicates: What (verb-noun phrasing inside the box), Who (the role or resource attached to the bottom of the box), and How (links to documentation, data details, or lower-level diagrams attached to the box). E correctly identifies this operational structure of UPN, and B summarizes the core requirements of a complete UPN activity element. Distractor analysis: A: UPN best practices dictate that a single diagram level should be kept readable by limiting it to 4 to 5 activity boxes, not 8 to 10, which creates visual noise and cognitive overload. C: Documenting technical platform architecture or specific backend configurations (like Omni-Channel routing rules) is an Admin or Technical Architect task. A BA should focus strictly on the business process layer. D: Cross-functional swimlanes are standard in traditional BPMN (Business Process Model and Notation) mapping, but they are explicitly not used in UPN. In UPN, roles are assigned directly to the individual activity boxes to allow for flexible, drill-down hierarchical layouts.$txt$,
            NULL,
            4,
            2,
            ARRAY[4595, 4596, 4597, 4598, 4599]::integer[],
            ARRAY['A', 'B', 'C', 'D', 'E']::text[],
            ARRAY[1, 2, 3, 4, 5]::integer[],
            ARRAY['Limit each diagram level to 8–10 activity boxes to maintain clarity.', 'Ensure every activity box answers "What happens?", "Who does it?", and "How is it done?".', 'Detail the underlying Salesforce technical architecture, such as Omni-Channel routing configurations, directly inside the activity text.', 'Use cross-functional horizontal swimlanes to indicate changing roles across process boundaries.', 'Keep activity boxes simple by answering "What happens?", using resources to show "Who", and attachments/links to show "How".']::text[],
            ARRAY[true, true, false, true, true]::boolean[],
            ARRAY[4596, 4599]::integer[],
            ARRAY['B', 'E']::text[]
        ),
        (
            1126,
            $txt$A Salesforce BA is drafting user stories for a Sales Cloud enhancement. The sales team wants a way to prevent representatives from discounting opportunities by more than 15% without regional manager approval. The BA writes the following user story: "As a Sales Representative, I want to request approval for deep discounts so that we can close competitive deals without violating margin policies." Which three elements should the BA verify to ensure this user story is fully refined and ready for the development team? (Select THREE)$txt$,
            $txt$nd D For a user story to be considered "ready" from a Salesforce BA perspective, it must have a properly structured core (As a... I want to... So that...) which is validated by A. It must also possess explicit, testable boundaries via Acceptance Criteria (C), and outline the expected standard business flow alongside logical exceptions (D), allowing developers to build without ambiguity. Distractor analysis: B: Writing Apex code blueprints or determining the programmatic approach to lock records falls strictly under the technical purview of a Salesforce Developer or Architect, making it out of scope for a BA. E: Defining exact validation formulas and identifying field API names is the technical implementation work of a Salesforce Administrator. The BA focuses on the business requirement (e.g., "Max 15% discount requires approval"), not the system syntax.$txt$,
            $txt$A, C, and D. For a user story to be considered "ready" from a Salesforce BA perspective, it must have a properly structured core (As a... I want to... So that...) which is validated by A. It must also possess explicit, testable boundaries via Acceptance Criteria (C), and outline the expected standard business flow alongside logical exceptions (D), allowing developers to build without ambiguity. Distractor analysis: B: Writing Apex code blueprints or determining the programmatic approach to lock records falls strictly under the technical purview of a Salesforce Developer or Architect, making it out of scope for a BA. E: Defining exact validation formulas and identifying field API names is the technical implementation work of a Salesforce Administrator. The BA focuses on the business requirement (e.g., "Max 15% discount requires approval"), not the system syntax.$txt$,
            2,
            3,
            ARRAY[4600, 4601, 4602, 4603, 4604]::integer[],
            ARRAY['A', 'B', 'C', 'D', 'E']::text[],
            ARRAY[1, 2, 3, 4, 5]::integer[],
            ARRAY['The story follows the standard structural framework: Persona, Action, and Business Value.', 'The story includes a drafted Apex trigger blueprint detailing how the platform will lock the record.', 'The story contains measurable, testable Acceptance Criteria written in a clear framework (such as Given-When-Then).', 'The story explicitly outlines the business definition of a "Happy Path" and exceptions for the approval flow.', 'The story includes the specific validation rule formulas and field API names needed to execute the logic.']::text[],
            ARRAY[true, false, true, false, false]::boolean[],
            ARRAY[4600, 4602, 4603]::integer[],
            ARRAY['A', 'C', 'D']::text[]
        )
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
            COALESCE(v_q.question_text,   '') || E'\x01' ||
            COALESCE(v_q.explanation,     '') || E'\x01' ||
            COALESCE(v_q.category,        '') || E'\x01' ||
            COALESCE(v_q.difficulty,      '') || E'\x01' ||
            COALESCE(v_q.question_type,   '') || E'\x01' ||
            v_q.select_count::text            || E'\x01' ||
            COALESCE(v_q.language_code,   '') || E'\x01' ||
            COALESCE(v_q.cognitive_level, '') || E'\x01' ||
            COALESCE(v_q.concept_key,     '') || E'\x01' ||
            COALESCE(
                (
                    SELECT string_agg(
                        ao.option_label || E'\x02' ||
                        ao.option_text  || E'\x02' ||
                        ao.is_correct::text,
                        E'\x03'
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
        WHERE  q.id = ANY (ARRAY[1046, 1055, 1081, 1091, 1094, 1102, 1107, 1116, 1125, 1126])
    ) THEN
        RAISE NOTICE 'repair_ba_multiselect_batch_v1 skipped: batch questions not present in this database';
    ELSE
        PERFORM public.repair_ba_multiselect_batch_v1();
    END IF;
END;
$$;

DROP FUNCTION IF EXISTS public.repair_ba_multiselect_batch_v1();
