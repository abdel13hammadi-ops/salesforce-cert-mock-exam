"""Verified production manifest for BA multi-select batch repair (questions 1046–1126)."""

from __future__ import annotations

from typing import Any, Dict, List

MIGRATION_NAME = "20260624220000_v45_repair_ba_multiselect_batch"
ACTOR = "system:v45_repair_ba_multiselect_batch"
QUESTION_IDS = [1046, 1055, 1081, 1091, 1094, 1102, 1107, 1116, 1125, 1126]


def _entry(
    question_id: int,
    *,
    stem: str,
    explanation: str,
    repaired_explanation: str | None,
    before_select_count: int,
    after_select_count: int,
    option_ids: List[int],
    option_labels: List[str],
    option_orders: List[int],
    option_texts: List[str],
    before_correct: List[bool],
    after_correct_labels: List[str],
) -> Dict[str, Any]:
    label_to_id = dict(zip(option_labels, option_ids, strict=True))
    after_correct_ids = [label_to_id[label] for label in after_correct_labels]
    return {
        "question_id": question_id,
        "stem": stem,
        "explanation": explanation,
        "repaired_explanation": repaired_explanation,
        "before_select_count": before_select_count,
        "after_select_count": after_select_count,
        "option_ids": option_ids,
        "option_labels": option_labels,
        "option_orders": option_orders,
        "option_texts": option_texts,
        "before_correct": before_correct,
        "after_correct_labels": after_correct_labels,
        "after_correct_ids": after_correct_ids,
    }


REPAIR_MANIFEST: List[Dict[str, Any]] = [
    _entry(
        1046,
        stem=(
            "A Salesforce BA is kicking off discovery for a manufacturing company looking to adopt Sales Cloud. "
            "The project sponsor wants to optimize the lead-to-order pipeline. The BA notes that while senior "
            "executives believe the pipeline is operating smoothly, mid-level managers complain of major data siloes, "
            "and end-user reps frequently input data into offline files. Which two discovery steps should the BA take "
            "to uncover the true root causes of this operational mismatch? (Select TWO)"
        ),
        explanation=(
            "Uncovering mismatched operational perspectives requires direct elicitation from the users experiencing "
            "the friction. Shadowing front-line reps (C) uncovers actual daily workarounds, while interviewing managers "
            "(D) defines the specific data silo pain points. Options A and E are out of scope for a BA because they "
            "represent technical development and data architecture tasks. Option B provides macro data but fails to "
            "diagnose operational root causes."
        ),
        repaired_explanation=None,
        before_select_count=3,
        after_select_count=2,
        option_ids=[4267, 4268, 4269, 4270, 4271],
        option_labels=["A", "B", "C", "D", "E"],
        option_orders=[1, 2, 3, 4, 5],
        option_texts=[
            "Instruct the technical lead to start designing custom Apex triggers to automate data flows between disparate systems.",
            "Review high-level corporate revenue performance reports to see if the pipeline issues are impacting macro metrics.",
            "Coordinate job-shadowing and observation sessions with front-line sales representatives to watch how lead data is manually captured.",
            "Host focused discovery interviews with mid-level managers to define what specific data constraints are causing siloes.",
            "Ask the Salesforce Architect to draft an Entity-Relationship Diagram (ERD) of the legacy CRM database structure.",
        ],
        before_correct=[True, False, True, True, False],
        after_correct_labels=["C", "D"],
    ),
    _entry(
        1055,
        stem=(
            "A Salesforce BA is setting up requirements workshops for a multi-cloud transformation project. "
            "The BA wants to ensure that the workshops remain highly productive and avoid descending into unstructured "
            "debates. Which three management approaches should the BA execute to ensure successful stakeholder "
            "collaboration? (Select THREE)"
        ),
        explanation=(
            "Productive workshops require tight governance and active facilitation. A DACI matrix (A) sets clear roles, "
            "an agenda (B) focuses the conversation, and visual models (D) keep stakeholders grounded in business process "
            "realities. Option C creates massive decision paralysis due to group size. Option E allows scope creep to "
            "overtake the session. Option F conflates business analysis with active configuration, which disrupts elicitation."
        ),
        repaired_explanation=None,
        before_select_count=2,
        after_select_count=3,
        option_ids=[4304, 4305, 4306, 4307, 4308, 4309],
        option_labels=["A", "B", "C", "D", "E", "F"],
        option_orders=[1, 2, 3, 4, 5, 6],
        option_texts=[
            "Establish and share a DACI matrix for the project to clarify decision rights before the workshops begin.",
            "Define a clear, itemized agenda for each workshop session and distribute it to all participants in advance.",
            "Invite all 250 end-users to every session to guarantee that everyone's voice is heard at the exact same time.",
            "Utilize visual facilitation tools, like digital boards or UPN process flows, to focus conversations on business activities.",
            "Allow stakeholders to completely steer the conversation off-topic to maintain high organic morale.",
            "Have the Salesforce Developer build out live configurations during the session to prove technical speed.",
        ],
        before_correct=[True, True, False, False, False, False],
        after_correct_labels=["A", "B", "D"],
    ),
    _entry(
        1081,
        stem=(
            "During a discovery workshop for an Omni-Channel Service Cloud project, a business lead requests: "
            "\"We need a way to automatically route incoming urgent tier-2 customer cases to our specialized engineering "
            "support team.\" To transform this into an actionable requirement, which two distinct business parameters "
            "must the BA elicit? (Select TWO)"
        ),
        explanation=(
            "To define a functional routing requirement, the BA must capture the underlying business logic: the exact "
            "criteria that trigger the escalation (B), and the skills/capacity definitions of the target agent team (C). "
            "Options A and D represent technical code naming and UI styling specifications. Option E is a hardware "
            "infrastructure concern that is completely out of scope."
        ),
        repaired_explanation=None,
        before_select_count=4,
        after_select_count=2,
        option_ids=[4411, 4412, 4413, 4414, 4415],
        option_labels=["A", "B", "C", "D", "E"],
        option_orders=[1, 2, 3, 4, 5],
        option_texts=[
            "The specific programmatic naming conventions for the underlying Apex routing queues.",
            "The explicit business rules and criteria that define what constitutes an \"urgent tier-2 customer case.\"",
            "The complete list of active service agents, including their specific skill matrices and capacity thresholds.",
            "The exact CSS styling configurations for the Omni-Channel utility bar interface components.",
            "The physical hardware server configurations of the company\u2019s internal network routing switchboards.",
        ],
        before_correct=[True, True, True, True, False],
        after_correct_labels=["B", "C"],
    ),
    _entry(
        1091,
        stem=(
            "A business analyst is managing a Salesforce product backlog. They need to ensure that all user stories "
            "meet the formal \"Definition of Ready\" (DoR) before allowing them to be planned into an active development "
            "sprint cycle. Which two criteria must be satisfied to meet this definition? (Select TWO)"
        ),
        explanation=(
            "A user story meets the Definition of Ready (DoR) when it is functionally mature enough for a developer to "
            "build without encountering basic requirement blockers. This requires clear, testable acceptance criteria (A) "
            "and an estimation of effort by the development team (C). Options B and D occur during and after the development "
            "sprint. Option E is a macro-level project funding concern."
        ),
        repaired_explanation=None,
        before_select_count=3,
        after_select_count=2,
        option_ids=[4452, 4453, 4454, 4455, 4456],
        option_labels=["A", "B", "C", "D", "E"],
        option_orders=[1, 2, 3, 4, 5],
        option_texts=[
            "The user story must feature explicit, testable Acceptance Criteria agreed upon by the business stakeholders.",
            "The user story must have its underlying custom Apex code classes fully drafted and saved inside a scratch sandbox org.",
            "The development and QA teams must have reviewed the story and provided an estimated effort sizing (e.g., story points).",
            "The user story must have already been successfully executed and validated by business end-users inside the production org.",
            "The user story must be signed off by the enterprise's chief financial officer to verify project budget alignment.",
        ],
        before_correct=[True, False, True, True, False],
        after_correct_labels=["A", "C"],
    ),
    _entry(
        1094,
        stem=(
            "A Salesforce Business Analyst is compiling user stories for a Service Cloud implementation. The BA wants "
            "to ensure that each story package provides the technical delivery team with adequate context. Which three "
            "distinct components should be included within a fully refined user story package? (Select THREE)"
        ),
        explanation=(
            "A complete, refined user story package contains the core business narrative (A), explicit testing boundaries "
            "via acceptance criteria (B), and supporting visual context models (C) to guide implementation. Options D, E, "
            "and F represent confidential HR data, out-of-scope technical engineering syntax, and macro corporate finance "
            "reports, none of which belong in a functional user story package."
        ),
        repaired_explanation=None,
        before_select_count=2,
        after_select_count=3,
        option_ids=[4465, 4466, 4467, 4468, 4469, 4470],
        option_labels=["A", "B", "C", "D", "E", "F"],
        option_orders=[1, 2, 3, 4, 5, 6],
        option_texts=[
            "A structured user narrative detailing the target persona, desired capability, and business value.",
            "A set of clear, testable Acceptance Criteria written in a behavior-driven format like Given-When-Then.",
            "Supporting visual context artifacts, such as a UPN business process flow snippet or a low-fidelity UI wireframe layout sketch.",
            "The individual salaries and hourly resource billing rates of the developers assigned to the sprint.",
            "The exact programmatic SQL or Apex code text needed to execute database triggers on the Case object.",
            "A copy of the company's annual corporate financial report.",
        ],
        before_correct=[True, True, False, False, False, False],
        after_correct_labels=["A", "B", "C"],
    ),
    _entry(
        1102,
        stem=(
            "A Salesforce Business Analyst is preparing a business unit for an upcoming User Acceptance Testing (UAT) "
            "phase. To ensure that the testing outcomes are legally and operationally sound, which two distinct metrics "
            "or components should be defined inside every UAT test script? (Select TWO)"
        ),
        explanation=(
            "A functional UAT test script requires clear step-by-step business instructions (B) so the user knows what to "
            "test, and a defined Expected Result (C) so the user can objectively confirm whether the system passed or failed. "
            "Options A, D, and E represent technical engineering and performance metrics that are out of scope for business "
            "user acceptance validation."
        ),
        repaired_explanation=None,
        before_select_count=4,
        after_select_count=2,
        option_ids=[4499, 4500, 4501, 4502, 4503],
        option_labels=["A", "B", "C", "D", "E"],
        option_orders=[1, 2, 3, 4, 5],
        option_texts=[
            "The exact name of the Apex developer who wrote the underlying custom code.",
            "A clear, step-by-step description of the business action the user must execute.",
            "The specific, measurable \"Expected Result\" or business state change that confirms the system is working correctly.",
            "The precise database server response time measured in milliseconds.",
            "The total lines of custom JavaScript code executing behind the page layout canvas.",
        ],
        before_correct=[True, True, True, True, False],
        after_correct_labels=["B", "C"],
    ),
    _entry(
        1107,
        stem=(
            "A Salesforce BA is kicking off discovery for a corporate wellness enterprise deploying Health Cloud. "
            "The VP of Operations wants to streamline the patient intake lifecycle. The BA observes that senior executives "
            "believe the intake process takes less than 10 minutes, but front-line clinic coordinators complain of system "
            "crashes, and patient check-in sheets are regularly processed on paper first. Which two discovery actions should "
            "the BA take to identify the real friction points? (Select TWO)"
        ),
        explanation=(
            "When executive perception differs from reality, direct observation (A) and targeted workshops with operational "
            "users (C) are the most effective ways for a BA to uncover the actual current-state challenges. Option B is an "
            "engineering task that jumps straight to solutioning. Option D is a technical architecture task. Option E provides "
            "high-level financial data but does not diagnose real operational bottlenecks."
        ),
        repaired_explanation=None,
        before_select_count=3,
        after_select_count=2,
        option_ids=[4520, 4521, 4522, 4523, 4524],
        option_labels=["A", "B", "C", "D", "E"],
        option_orders=[1, 2, 3, 4, 5],
        option_texts=[
            "Coordinate a series of active job-shadowing sessions with front-line clinic coordinators to document the step-by-step physical check-in process.",
            "Instruct the technical lead to build an Apex trigger that prevents records from being saved with missing patient information.",
            "Conduct targeted discovery workshops with clinic coordinators to document specific software and operational pain points during intake.",
            "Ask the Lead Architect to draft a detailed Salesforce schema diagram to evaluate the current custom metadata boundaries.",
            "Extract macro financial billing reports to review the university's quarterly operational overhead trends.",
        ],
        before_correct=[True, False, True, True, False],
        after_correct_labels=["A", "C"],
    ),
    _entry(
        1116,
        stem=(
            "A Salesforce BA is designing a requirements gathering strategy for a large-scale Field Service Cloud rollout. "
            "The BA wants to ensure that the upcoming collaboration workshops remain highly focused and productive. "
            "Which three actions should the BA take? (Select THREE)"
        ),
        explanation=(
            "Productive workshops require tight governance and active facilitation. A DACI matrix (A) sets clear roles, "
            "an agenda (B) focuses the conversation, and visual models (D) keep stakeholders grounded in business process "
            "realities. Option C creates massive decision paralysis due to group size. Option E allows scope creep to "
            "overtake the session. Option F conflates business analysis with active configuration, which disrupts elicitation."
        ),
        repaired_explanation=None,
        before_select_count=2,
        after_select_count=3,
        option_ids=[4557, 4558, 4559, 4560, 4561, 4562],
        option_labels=["A", "B", "C", "D", "E", "F"],
        option_orders=[1, 2, 3, 4, 5, 6],
        option_texts=[
            "Establish and distribute a project DACI matrix to clarify decision rights before the workshops begin.",
            "Create an itemized workshop agenda and share it with all invitees well in advance.",
            "Invite all 400 field service technicians to every session to guarantee broad representation.",
            "Use visual models, such as UPN process flows, to keep conversations focused on business activities.",
            "Allow stakeholders to completely steer the conversation off-topic to maintain high organic morale.",
            "Direct the Salesforce Developer to build live configurations in production during the workshop.",
        ],
        before_correct=[True, True, False, False, False, False],
        after_correct_labels=["A", "B", "D"],
    ),
    _entry(
        1125,
        stem=(
            "During a requirements elicitation workshop for a new Service Cloud rollout, the customer service team expresses "
            "deep frustration with their current legacy system's case escalation process. The service agents can explain what "
            "triggers an escalation but struggle to articulate how cases flow between different tier levels or where bottlenecks "
            "occur. The BA wants to document this visually to ensure a shared understanding before mapping the future state. "
            "Which two design choices align with Universal Process Notation (UPN) standards for mapping this process? (Select TWO)"
        ),
        explanation=(
            "According to Salesforce UPN standards, a valid process diagram must maintain a strict structure where each activity "
            "box clearly communicates: What (verb-noun phrasing inside the box), Who (the role or resource attached to the bottom "
            "of the box), and How (links to documentation, data details, or lower-level diagrams attached to the box). E correctly "
            "identifies this operational structure of UPN, and B summarizes the core requirements of a complete UPN activity element. "
            "Distractor analysis: A: UPN best practices dictate that a single diagram level should be kept readable by limiting it "
            "to 4 to 5 activity boxes, not 8 to 10, which creates visual noise and cognitive overload. C: Documenting technical "
            "platform architecture or specific backend configurations (like Omni-Channel routing rules) is an Admin or Technical "
            "Architect task. A BA should focus strictly on the business process layer. D: Cross-functional swimlanes are standard in "
            "traditional BPMN (Business Process Model and Notation) mapping, but they are explicitly not used in UPN. In UPN, roles "
            "are assigned directly to the individual activity boxes to allow for flexible, drill-down hierarchical layouts."
        ),
        repaired_explanation=None,
        before_select_count=4,
        after_select_count=2,
        option_ids=[4595, 4596, 4597, 4598, 4599],
        option_labels=["A", "B", "C", "D", "E"],
        option_orders=[1, 2, 3, 4, 5],
        option_texts=[
            "Limit each diagram level to 8\u201310 activity boxes to maintain clarity.",
            "Ensure every activity box answers \"What happens?\", \"Who does it?\", and \"How is it done?\".",
            "Detail the underlying Salesforce technical architecture, such as Omni-Channel routing configurations, directly inside the activity text.",
            "Use cross-functional horizontal swimlanes to indicate changing roles across process boundaries.",
            "Keep activity boxes simple by answering \"What happens?\", using resources to show \"Who\", and attachments/links to show \"How\".",
        ],
        before_correct=[True, True, False, True, True],
        after_correct_labels=["B", "E"],
    ),
    _entry(
        1126,
        stem=(
            "A Salesforce BA is drafting user stories for a Sales Cloud enhancement. The sales team wants a way to prevent "
            "representatives from discounting opportunities by more than 15% without regional manager approval. The BA writes "
            "the following user story: \"As a Sales Representative, I want to request approval for deep discounts so that we "
            "can close competitive deals without violating margin policies.\" Which three elements should the BA verify to ensure "
            "this user story is fully refined and ready for the development team? (Select THREE)"
        ),
        explanation=(
            "nd D For a user story to be considered \"ready\" from a Salesforce BA perspective, it must have a properly "
            "structured core (As a... I want to... So that...) which is validated by A. It must also possess explicit, "
            "testable boundaries via Acceptance Criteria (C), and outline the expected standard business flow alongside logical "
            "exceptions (D), allowing developers to build without ambiguity. Distractor analysis: B: Writing Apex code blueprints "
            "or determining the programmatic approach to lock records falls strictly under the technical purview of a Salesforce "
            "Developer or Architect, making it out of scope for a BA. E: Defining exact validation formulas and identifying field "
            "API names is the technical implementation work of a Salesforce Administrator. The BA focuses on the business "
            "requirement (e.g., \"Max 15% discount requires approval\"), not the system syntax."
        ),
        repaired_explanation=(
            "A, C, and D. For a user story to be considered \"ready\" from a Salesforce BA perspective, it must have a properly "
            "structured core (As a... I want to... So that...) which is validated by A. It must also possess explicit, testable "
            "boundaries via Acceptance Criteria (C), and outline the expected standard business flow alongside logical exceptions "
            "(D), allowing developers to build without ambiguity. Distractor analysis: B: Writing Apex code blueprints or "
            "determining the programmatic approach to lock records falls strictly under the technical purview of a Salesforce "
            "Developer or Architect, making it out of scope for a BA. E: Defining exact validation formulas and identifying field "
            "API names is the technical implementation work of a Salesforce Administrator. The BA focuses on the business "
            "requirement (e.g., \"Max 15% discount requires approval\"), not the system syntax."
        ),
        before_select_count=2,
        after_select_count=3,
        option_ids=[4600, 4601, 4602, 4603, 4604],
        option_labels=["A", "B", "C", "D", "E"],
        option_orders=[1, 2, 3, 4, 5],
        option_texts=[
            "The story follows the standard structural framework: Persona, Action, and Business Value.",
            "The story includes a drafted Apex trigger blueprint detailing how the platform will lock the record.",
            "The story contains measurable, testable Acceptance Criteria written in a clear framework (such as Given-When-Then).",
            "The story explicitly outlines the business definition of a \"Happy Path\" and exceptions for the approval flow.",
            "The story includes the specific validation rule formulas and field API names needed to execute the logic.",
        ],
        before_correct=[True, False, True, False, False],
        after_correct_labels=["A", "C", "D"],
    ),
]

QUESTION_1126_CORRUPTED_EXPLANATION_PREFIX = REPAIR_MANIFEST[-1]["explanation"][:20]
QUESTION_1126_REPAIRED_EXPLANATION = REPAIR_MANIFEST[-1]["repaired_explanation"]
